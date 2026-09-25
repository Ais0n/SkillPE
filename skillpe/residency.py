"""Process-local residency adapter for the frozen, non-LoRA LTX pipeline."""
from contextlib import contextmanager


def builder_key(builder, device, dtype, kwargs):
    # The frozen SingleGPUModelBuilder.build ignores **kwargs (including
    # per-call video_tools); they do not change model weights or cache identity.
    if builder.loras:
        raise ValueError('Resident cache supports the frozen no-LoRA build only')
    return (repr(builder.model_path), builder._model_class_configurator,
            getattr(builder.model_sd_ops, 'name', None),
            tuple(op.name for op in builder.module_ops), str(device), str(dtype))


@contextmanager
def retain_model(model, **kwargs):
    # Strong references in the build cache own weights until worker exit.
    yield model


def install():
    import torch
    from ltx_core.loader.single_gpu_model_builder import SingleGPUModelBuilder
    from ltx_pipelines.utils import blocks
    if getattr(SingleGPUModelBuilder, '_ltx_resident_installed', False):
        return
    original = SingleGPUModelBuilder.build
    cache = {}

    def build(builder, device=None, dtype=None, **kwargs):
        device = torch.device('cuda') if device is None else torch.device(device)
        if device.type != 'cuda':
            raise ValueError('GPU-resident mode forbids CPU model construction')
        key = builder_key(builder, device, dtype, kwargs)
        if key not in cache:
            model = original(builder, device=device, dtype=dtype, **kwargs)
            if any(p.device.type != 'cuda' for p in model.parameters()):
                raise RuntimeError('Model contains non-CUDA parameters')
            cache[key] = model
            print(f'GPU_RESIDENT loaded {builder._model_class_configurator.__name__}; cached_models={len(cache)}', flush=True)
        return cache[key]

    SingleGPUModelBuilder.build = build
    SingleGPUModelBuilder._ltx_resident_installed = True
    blocks.gpu_model = retain_model
