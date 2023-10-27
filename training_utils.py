import gc
from threading import Thread
import jax
import jax.numpy as jnp
import numpy as np
from dataclasses import dataclass
from diffusers import (
    FlaxAutoencoderKL,
    FlaxStableDiffusionPipeline,
    FlaxUNet2DConditionModel,
)
from schedulers import FlaxDDPMScheduler
from transformers import CLIPTokenizer, FlaxCLIPTextModel
from flax.training import train_state
import optax
from flax import struct
from typing import Callable, Tuple, Any
from jax.experimental.compilation_cache import compilation_cache as cc

from streamer.utils import TimingContextManager

# sharding
from jax.sharding import Mesh
from jax.sharding import PartitionSpec
from jax.sharding import NamedSharding
from jax.experimental import mesh_utils

# adjust this sharding mesh to create appropriate sharding rule
# assume we have 8 device
# (1,8) = model parallel
# (8,1) = data parallel
# (4,2)/(2,4) = model data parallel
devices = mesh_utils.create_device_mesh((jax.device_count(), 1))
# create axis name on how many parallelism slice you want on your model
mesh = Mesh(devices, axis_names=("data_parallel", "model_parallel"))


class FrozenModel(struct.PyTreeNode):
    """
    mimic the behaviour of train_state but this time for frozen params
    to make it passable to the jitted function
    """

    # use pytree_node=False to indicate an attribute should not be touched
    # by Jax transformations.
    call: Callable = struct.field(pytree_node=False)
    params: dict = struct.field(pytree_node=True)

    @classmethod
    def create(cls, apply_fn, params):
        return cls(
            call=call,
            params=params,
        )


@dataclass
class TrainingConfig:
    """
    reading model properties from json. i should modify the json to when the model is done training
    format:
    {
        "model_path":"model_checkpoints/path"
        "batch_size": 64,
        "learning_rate": 1e-6,
        "unet_learning_rate": 1e-6,
        "text_encoder_learning_rate": 1e-6,
        "lr_scheduler": "constant",
        "adam_to_lion_scale_factor": 7.0,
        "compilation_cache_path": "jax_cache",
        "keep_compiled_fn_in_cache": true,
        "text_encoder_context_window": 77,
        "context_window_concatenation_count": 3,
        "aot_compile": true,
        "image_area_root": [576, 704, 832, 960, 1088], 
        "minimum_axis_length": [384, 512, 576, 704, 832]
    }
    """

    model_path: str
    batch_size: int
    learning_rate: float
    unet_learning_rate: float
    text_encoder_learning_rate: float
    lr_scheduler: str
    adam_to_lion_scale_factor: float
    compilation_cache_path: str
    keep_compiled_fn_in_cache: bool
    text_encoder_context_window: int
    context_window_concatenation_count: int
    aot_compile: bool
    image_area_root: list
    minimum_axis_length: list


def calculate_resolution_array(
    max_res_area=512**2, bucket_lower_bound_res=256, rounding=64
):
    """
    helper function to calculate image bucket

    Parameters:
    - max_res_area (int): The maximum target resolution area of the image.
    - bucket_lower_bound_res (int): minimum minor axis (smaller axis).
    - rounding (int): rounding steps / rounding increment.

    Returns:
    - resolution (numpy.ndarray): A 2D NumPy array representing the resolution pairs (width, height).
    """
    root_max_res = max_res_area ** (1 / 2)
    centroid = int(root_max_res)

    # a sequence of number that divisible by 64 with constraint
    w = np.arange(
        bucket_lower_bound_res // rounding * rounding,
        centroid // rounding * rounding + rounding,
        rounding,
    )
    # y=1/x formula with rounding down to the nearest multiple of 64
    # will maximize the clamped resolution to maximum res area
    h = ((max_res_area / w) // rounding * rounding).astype(int)

    # is square array possible? if so chop the last bit before combining
    if w[-1] - h[-1] == 0:
        w_delta = np.flip(w[:-1])
        h_delta = np.flip(h[:-1])
    else:
        w_delta = np.flip(w)
        h_delta = np.flip(h)

    w = np.concatenate([w, h_delta])
    h = np.concatenate([h, w_delta])

    resolution = np.stack([w, h]).T

    return resolution


def load_models(model_dir: str) -> dict:
    """
    Load models from a directory using HuggingFace. the config hard coded for now!

    Args:
        model_dir (str): The path to the directory containing the models.

    Returns:
        dict: A dictionary containing the loaded models and their parameters.
            {
                "unet":{
                    "unet_params": unet_params,
                    "unet_model": unet,
                },
                "vae":{
                    "vae_params": vae_params,
                    "vae_model": vae,
                },
                "text_encoder":{
                    "text_encoder_params": text_encoder_params,
                    "text_encoder_model": text_encoder,
                },
                "schedulers":{
                    "noise_scheduler_state": noise_scheduler_state,
                    "noise_scheduler_object": noise_scheduler,
                },
            }
    """

    # load the model params and model object

    tokenizer = CLIPTokenizer.from_pretrained(model_dir, subfolder="tokenizer")
    unet, unet_params = FlaxUNet2DConditionModel.from_pretrained(
        model_dir,
        subfolder="unet",
        dtype=jnp.bfloat16,
        use_memory_efficient_attention=True,
    )
    text_encoder, text_encoder_params = FlaxCLIPTextModel.from_pretrained(
        model_dir, subfolder="text_encoder", dtype=jnp.bfloat16, _do_init=False
    )
    vae, vae_params = FlaxAutoencoderKL.from_pretrained(
        model_dir,
        dtype=jnp.bfloat16,
        subfolder="vae",
    )
    noise_scheduler = FlaxDDPMScheduler(
        beta_start=0.00085,
        beta_end=0.012,
        beta_schedule="zero_snr_scaled_linear",
        num_train_timesteps=1000,
        prediction_type="v_prediction",
    )
    noise_scheduler_state = noise_scheduler.create_state()

    # should've put this in dataclasses
    return {
        "unet": {
            "unet_params": unet_params,
            "unet_model": unet,
        },
        "vae": {
            "vae_params": vae_params,
            "vae_model": vae,
        },
        "text_encoder": {
            "text_encoder_params": text_encoder_params,
            "text_encoder_model": text_encoder,
        },
        "schedulers": {
            "noise_scheduler_state": noise_scheduler_state,
            "noise_scheduler_object": noise_scheduler,
        },
    }


def create_frozen_states(models: dict):
    """
    create frozen training states that bundled with teh function or method associated with it

    Args:
        models (dict): A dictionary containing models and parameters.

    Returns:
        dict: A dictionary containing the optimizer states for U-Net and text encoder models.
            {
                "vae_state": vae_state,
                "schedulers_state": schedulers_state
            }
    """

    vae_state = FrozenModel(
        call=models["vae"]["vae_model"],
        params=models["vae"]["vae_params"],
    )

    schedulers_state = FrozenModel(
        # welp not a function but eh it should works
        call=models["schedulers"]["noise_scheduler_object"],
        params=models["schedulers"]["noise_scheduler_state"],
    )
    return {"vae_state": vae_state, "schedulers_state": schedulers_state}


def create_lion_optimizer_states(
    models: dict,
    train_unet: bool = True,
    train_text_encoder: bool = True,
    adam_to_lion_scale_factor: int = 7,
    u_net_learning_rate: float = 1e-6,
    text_encoder_learning_rate: float = 1e-6,
):
    """
    Create optimizer states for Lion, a custom optimizer, for U-Net and CLIP text encoder models.

    Args:
        models (dict): A dictionary containing the U-Net and text encoder models and parameters.
            {
                "unet": {
                    "unet_model": your_unet_model,
                    "unet_params": your_unet_params,
                },
                "text_encoder": {
                    "text_encoder_model": your_text_encoder_model,
                    "text_encoder_params": your_text_encoder_params,
                }
            }
        train_unet (bool): Whether to train the U-Net model.
        train_text_encoder (bool): Whether to train the text encoder model.
        adam_to_lion_scale_factor (int): Scaling factor for adjusting learning rates.
        u_net_learning_rate (float): unet learning rate
        text_encoder_learning_rate (float): text encoder learning rate

    Returns:
        dict: A dictionary containing the optimizer states for U-Net and text encoder models.
            {
                "unet_state": unet_state or None,
                "text_encoder_state": text_encoder_state or None
            }


    """
    # no fancy optimizer atm just use linear constant lr
    # optimizer for U-Net
    # use this context manager to ensure all of this ops happening in CPU
    # so it does not waste precious HBM space in TPU

    unet_state = None
    text_encoder_state = None

    with jax.default_device(jax.devices("cpu")[0]):
        if train_unet:
            u_net_constant_scheduler = optax.constant_schedule(
                u_net_learning_rate / adam_to_lion_scale_factor
            )
            u_net_lion = optax.lion(
                learning_rate=u_net_constant_scheduler,
                b1=0.9,
                b2=0.99,
                weight_decay=1e-2 * adam_to_lion_scale_factor,
            )
            u_net_optimizer = optax.chain(
                optax.clip_by_global_norm(1),  # prevent explosion
                u_net_lion,
            )
            unet_state = train_state.TrainState.create(
                apply_fn=models["unet"]["unet_model"].apply,
                params=models["unet"]["unet_params"],
                tx=u_net_optimizer,
            )

        # optimizer for CLIP text encoder
        if train_text_encoder:
            text_encoder_constant_scheduler = optax.constant_schedule(
                text_encoder_learning_rate / adam_to_lion_scale_factor
            )
            text_encoder_lion = optax.lion(
                learning_rate=text_encoder_constant_scheduler,
                b1=0.9,
                b2=0.99,
                weight_decay=1e-2 * adam_to_lion_scale_factor,
            )
            text_encoder_optimizer = optax.chain(
                optax.clip_by_global_norm(1),  # prevent explosion
                text_encoder_lion,
            )
            text_encoder_state = train_state.TrainState.create(
                # transformer implementation does not have apply method apparently
                apply_fn=models["text_encoder"]["text_encoder_model"].__call__,
                params=models["text_encoder"]["text_encoder_params"],
                tx=text_encoder_optimizer,
            )

    return {"unet_state": unet_state, "text_encoder_state": text_encoder_state}


def on_device_model_training_state(training_config:TrainingConfig):
    models = load_models(model_dir=training_config.model_path)
    trained_model_states = create_lion_optimizer_states(
        models=models, train_text_encoder=True, train_unet=True, adam_to_lion_scale_factor=7
    )
    frozen_states = create_frozen_states(
        models=models,
    )
    unet_state = jax.tree_map(
        lambda leaf: jax.device_put(leaf, device=NamedSharding(mesh, PartitionSpec())),
        trained_model_states["unet_state"],
    )
    text_encoder_state = jax.tree_map(
        lambda leaf: jax.device_put(leaf, device=NamedSharding(mesh, PartitionSpec())),
        trained_model_states["text_encoder_state"],
    )
    frozen_vae = jax.tree_map(
        lambda leaf: jax.device_put(leaf, device=NamedSharding(mesh, PartitionSpec())),
        frozen_states["vae_state"],
    )
    frozen_schedulers = jax.tree_map(
        lambda leaf: jax.device_put(leaf, device=NamedSharding(mesh, PartitionSpec())),
        frozen_states["schedulers_state"],
    )
    return unet_state, text_encoder_state, frozen_vae, frozen_schedulers


def train_step(
    # donated args
    unet_state: Any,  # define sharding rule!
    text_encoder_state: Any,  # define sharding rule!
    # variable args
    batch: dict,  # define sharding rule!
    train_rng: jax.random.PRNGKey,  # define sharding rule!
    # unhashable static args
    frozen_vae_state: Any,
    frozen_noise_scheduler_state: Any,  # welp technically not a trainable by any means
    # static args
    use_offset_noise: bool = False,
    strip_bos_eos_token: bool = True,
):
    """
    this jittable trainstep function just lightly wraps
    the actual loss function and adding some states to it
    """

    # generate rng and return new_train_rng to be used for the next iteration step
    # rng is comunicated though device aparently
    dropout_rng, sample_rng, new_train_rng = jax.random.split(train_rng, num=3)

    def compute_loss(
        unet_params, text_encoder_params, vae_params, noise_scheduler_state, batch
    ):
        # Convert images to latent space
        vae_outputs = frozen_vae_state.call.apply(
            variables={"params": frozen_vae_state.params},
            sample=batch["pixel_values"],
            deterministic=True,
            method=frozen_vae_state.call.encode,
        )

        # get sample distribution from VAE latent
        latents = vae_outputs.latent_dist.sample(sample_rng)
        # (NHWC) -> (NCHW)
        latents = jnp.transpose(latents, (0, 3, 1, 2))
        # weird scaling don't touch it's a lazy normalization
        latents = latents * 0.18215

        # Sample noise that we'll add to the latents
        # I think I should combine this with the first noise seed generator
        noise_offset_rng, noise_rng, timestep_rng = jax.random.split(
            key=sample_rng, num=3
        )
        noise = jax.random.normal(key=noise_rng, shape=latents.shape)
        if use_offset_noise:
            # mean offset noise, why add offset?
            # here https://www.crosslabs.org//blog/diffusion-with-offset-noise
            noise_offset = (
                jax.random.normal(
                    key=noise_offset_rng,
                    shape=(latents.shape[0], latents.shape[1], 1, 1),
                )
                * 0.1
            )
            noise = noise + noise_offset

        # Sample a random timestep for each image
        batch_size = latents.shape[0]
        timesteps = jax.random.randint(
            key=timestep_rng,
            shape=(batch_size,),
            minval=0,
            maxval=frozen_noise_scheduler_state.call.config.num_train_timesteps,
        )

        # Add noise to the latents according to the noise magnitude at each timestep
        # (this is the forward diffusion process)
        noisy_latents = frozen_noise_scheduler_state.call.add_noise(
            state=frozen_noise_scheduler_state.params,
            original_samples=latents,
            noise=noise,
            timesteps=timesteps,
        )
        print(batch["input_ids"].shape)
        encoder_hidden_states = text_encoder_state.apply_fn(
            params=text_encoder_params,
            input_ids=batch["input_ids"],
            dropout_rng=dropout_rng,
            train=True,
        )[0]
        print(encoder_hidden_states.shape)
        # reshape encoder_hidden_states to shape (batch, token_append, token, hidden_states)
        encoder_hidden_states = jnp.reshape(
            encoder_hidden_states,
            (latents.shape[0], -1, 77, encoder_hidden_states.shape[-1]),
        )
        print(encoder_hidden_states.shape)

        if strip_bos_eos_token:
            encoder_hidden_states = jnp.concatenate(
                [
                    # first encoder hidden states without eos token
                    encoder_hidden_states[:, 0, :-1, :],
                    # the rest of encoder hidden states without both bos and eos token
                    jnp.reshape(
                        encoder_hidden_states[:, 1:-1, 1:-1, :],
                        (
                            encoder_hidden_states.shape[0],
                            -1,
                            encoder_hidden_states.shape[-1],
                        ),
                    ),
                    # last encoder hidden states without bos token
                    encoder_hidden_states[:, -1, 1:, :],
                ],
                axis=1,
            )
        else:
            # reshape encoder_hidden_states to shape (batch, token_append & token, hidden_states)
            encoder_hidden_states = jnp.reshape(
                encoder_hidden_states,
                (encoder_hidden_states.shape[0], -1, encoder_hidden_states.shape[-1]),
            )
        print(encoder_hidden_states.shape)

        # Predict the noise residual because predicting image is hard :P
        # essentially try to undo the noise process
        model_pred = unet_state.apply_fn(
            variables={"params": unet_params},
            sample=noisy_latents,
            timesteps=timesteps,
            encoder_hidden_states=encoder_hidden_states,
            train=True,
        ).sample

        # Get the target for loss depending on the prediction type
        # sd1.x use epsilon aka noise residual but sd2.1 use velocity prediction
        if frozen_noise_scheduler_state.call.config.prediction_type == "epsilon":
            target = noise
        elif frozen_noise_scheduler_state.call.config.prediction_type == "v_prediction":
            target = frozen_noise_scheduler_state.call.get_velocity(
                state=frozen_noise_scheduler_state.params,
                sample=latents,
                noise=noise,
                timesteps=timesteps,
            )
        else:
            # panic!!
            raise ValueError(
                f"Unknown prediction type {frozen_noise_scheduler_state.call.config.prediction_type}"
            )

        # MSE loss
        loss = (target - model_pred) ** 2
        loss = loss.mean()

        return loss

    # perform autograd
    # TODO: define the differentiable input !
    # i havent updated this to include all params!

    # autograd transform function to get gradient of the input params
    # TODO: use reduce_axes to sum all of the gradient inplace!
    # this will significantly reduce memory consumption
    grad_fn = jax.value_and_grad(
        fun=compute_loss, argnums=[0, 1]  # differentiate first and second input
    )
    # grad is a tuple here because multiple params is provided
    loss, grad = grad_fn(
        unet_state.params,  # unet_params
        text_encoder_state.params,  # text_encoder_params
        frozen_vae_state.params,  # frozen_vae_state.params
        frozen_noise_scheduler_state.params,  # frozen_noise_scheduler_state.params
        batch,  # batch
    )

    # update weight and bias value
    new_unet_state = unet_state.apply_gradients(grads=grad[0])
    new_text_encoder_state = text_encoder_state.apply_gradients(grads=grad[1])

    # calculate loss
    metrics = {"loss": loss}

    # idk how jax check this output for donation
    # but just in case i put the donated args with the same position as the input
    # donated args are new_unet_state and new_text_encoder_state since it has the same
    # data structure so inplace update is good
    return new_unet_state, new_text_encoder_state, metrics, new_train_rng


def dp_compile_all_unique_resolution(
    unet_state, text_encoder_state, frozen_vae, frozen_schedulers, training_config:TrainingConfig
):
    # keep the compiled function in cache
    if jax.devices()[0].platform == "tpu" and training_config.keep_compiled_fn_in_cache:
        cc.initialize_cache(training_config.compilation_cache_path)

    ### compute all possible resolution bucket to be precompiled ###
    all_possible_resolution = []
    resolution_constraints = list(
        zip(training_config.image_area_root, training_config.minimum_axis_length)
    )
    for resolution_constraint in resolution_constraints:
        bucket = calculate_resolution_array(
            max_res_area=resolution_constraint[0] ** 2,
            bucket_lower_bound_res=resolution_constraint[1],
            rounding=64,
        )
        all_possible_resolution.append(bucket)
    # merge it
    all_possible_resolution = np.concatenate(all_possible_resolution)

    ### inner function that tranverse model object and lower it into stableHLO
    def _create_lowered_hlo(
        bucket_resolution: np.array,
    ) -> Tuple[jax.stages.Lowered, np.shape]:
        # placeholder rngs just for compilation purposes
        dummy_rngs = jax.random.PRNGKey(2)
        # create dummy batch
        with jax.default_device(jax.devices("cpu")[0]):
            batch = {
                "pixel_values": jnp.zeros(
                    # (NCHW)
                    shape=(
                        training_config.batch_size,
                        3,
                        bucket_resolution[0],
                        bucket_resolution[1],
                    ),
                    dtype=jnp.float32,
                ),
                "input_ids": jnp.zeros(
                    # (batch, context_window)
                    shape=(
                        training_config.batch_size
                        * training_config.context_window_concatenation_count,
                        training_config.text_encoder_context_window,
                    ),
                    dtype=jnp.int32,
                ),
                "attention_mask": jnp.zeros(
                    # (batch, context_window)
                    shape=(
                        training_config.batch_size
                        * training_config.context_window_concatenation_count,
                        training_config.text_encoder_context_window,
                    ),
                    dtype=jnp.int32,
                ),
            }

        # store the pixel values for indicating that this compiled function is a specific for this image size
        image_shape = batch["pixel_values"].shape
        # define sharding rule (im doing data parallelism here)
        batch = jax.tree_map(
            lambda leaf: jax.device_put(
                leaf, device=NamedSharding(mesh, PartitionSpec("data_parallel", None))
            ),
            batch,
        )

        # just gonna be verbose here for less headache
        p_train_step = jax.jit(
            train_step,
            # donated arguments (inplace update)
            donate_argnums=(
                0,  # "unet_state"
                1,  # "text_encoder_state"
            ),
            in_shardings=(
                # unet_state
                jax.tree_map(
                    lambda leaf: NamedSharding(mesh, PartitionSpec()),
                    unet_state,
                ),
                # text_encoder_state
                jax.tree_map(
                    lambda leaf: NamedSharding(mesh, PartitionSpec()),
                    text_encoder_state,
                ),
                # batch
                jax.tree_map(
                    lambda leaf: NamedSharding(
                        mesh, PartitionSpec("data_parallel", None)
                    ),
                    batch,
                ),
                # rngs
                None,  # honestly, donno how to shard this one, COMPILER! TAKE THE WHEEL HERE
                # frozen_vae
                jax.tree_map(
                    lambda leaf: NamedSharding(mesh, PartitionSpec()),
                    frozen_vae,
                ),
                # frozen_schedulers
                jax.tree_map(
                    lambda leaf: NamedSharding(mesh, PartitionSpec()),
                    frozen_schedulers,
                ),
                # use_offset_noise
                # None, # moved to static
                # strip_bos_eos_token
                # None, # moved to static
            ),
            # compiled as static value
            # only hashable one!
            static_argnames=(
                "use_offset_noise",
                "strip_bos_eos_token",
            ),
            out_shardings=(
                jax.tree_map(
                    lambda leaf: NamedSharding(mesh, PartitionSpec()),

                    unet_state,
                ),
                jax.tree_map(
                    lambda leaf: NamedSharding(mesh, PartitionSpec()),
                    text_encoder_state,
                ),
                {"loss": NamedSharding(mesh, PartitionSpec())},
                None,
            ),
        )

        # lower jitted function to HLO representation
        with TimingContextManager(f"lowering {bucket_resolution}"):
            lowered_hlo = p_train_step.lower(
                # donated args
                unet_state,  # unet_state
                text_encoder_state,  # text_encoder_state
                # variable args
                batch,  # batch
                dummy_rngs,  # train_rng
                # unhashable static args
                frozen_vae,  # frozen_vae_state
                frozen_schedulers,  # frozen_noise_scheduler_state
                # static args
                False,  # use_offset_noise
                True,  # strip_bos_eos_token
            )
            # store in dict
            # lowered_hlos[f"{bucket_resolution[0]},{bucket_resolution[1]}"] = lowered_hlo

        del batch
        gc.collect()
        return lowered_hlo, image_shape


    compiled_train_step = {}
    # wrap jax compile so i can use threading to dispatch compilation
    def _compile_unique_res_train_step(HLO: jax.stages.Lowered, resolution: np.shape):
        # this wont have collision right :fingers_crossed:
        compiled_train_step[resolution] = HLO.compile()


    # lower all of the possible resolution sequentially
    with TimingContextManager(f"lowering all res"):
        threads = []
        for bucket_resolution in all_possible_resolution:
            lowered = _create_lowered_hlo(bucket_resolution)

            # dispatch compilation while lowering other HLOs
            thread = Thread(target=_compile_unique_res_train_step, args=lowered)
            threads.append(thread)
            thread.start()

        for thread in threads:
            thread.join()

    return compiled_train_step