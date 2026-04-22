# v0.17.20 — FastEmbed survives half-installed CUDA

User reported doctor failing on **Embeddings** with:

```
EP Error CUDA_PATH is set but CUDA wasnt able to be loaded. Please install
the correct version of CUDA and cuDNN … when using ['CUDAExecutionProvider']
Falling back to ['CUDAExecutionProvider', 'CPUExecutionProvider'] and retrying.

✗ Embeddings: D:\a\_work\1\s\onnxruntime\python\onnxruntime_pybind_state.cc:857 …
```

Everything else green. Inference working. Classic half-installed CUDA: `CUDA_PATH` env var points at a CUDA Toolkit that's gone / wrong version, `onnxruntime-gpu` is in the venv and tries the CUDA provider first, `onnxruntime_providers_cuda.dll` can't find its dependent cuDNN DLLs → LoadLibrary error 126.

ONNX logs "Falling back" but the retry is unreliable on Windows when the CUDA DLL was already partially loaded. FastEmbed re-raised instead of ending up on CPU, and our doctor check simply propagated the exception.

## 🔧 Fix

### `memory._init_fastembed()` — explicit CUDA → CPU fallback

New helper wraps both dense and sparse model initialization:

```python
def _init_fastembed(cls, model_name):
    providers = _fastembed_providers()  # from setting or env
    init_kwargs = {"model_name": model_name}
    if providers is not None:
        init_kwargs["providers"] = providers
    try:
        return cls(**init_kwargs)
    except Exception as first_err:
        if providers is None:  # auto mode — retry CPU
            _log.warning(f"FastEmbed auto-init failed: {first_err}; retrying CPU")
            return cls(model_name=model_name, providers=["CPUExecutionProvider"])
        raise  # explicit mode — re-raise so user sees the real error
```

Under the hood during the first attempt, stderr is redirected to a buffer so ONNX's noisy C-level "LoadLibrary failed" messages don't spam the user's terminal — they get logged at DEBUG if the retry succeeds.

### New setting: `embed_device`

Three modes:

| Value | Behavior |
|---|---|
| `auto` (default) | Try whatever FastEmbed picks (typically CUDA if `onnxruntime-gpu` installed). Fall back to CPU on ANY exception. |
| `cpu` | Force `providers=["CPUExecutionProvider"]`. Skip CUDA entirely. Use this when CUDA is half-installed and the auto-fallback still prints noise. |
| `cuda` | Force `providers=["CUDAExecutionProvider"]`. Error loudly on failure — useful for debugging GPU setup. |

Can be set via:
- **Settings → Memory** (web UI)
- `kv_set setting:embed_device cpu` (CLI)
- `QWE_EMBED_DEVICE=cpu` environment variable (takes precedence over setting)

### Doctor check tightened

`cli.py` Embeddings check now:

1. Reports the active ONNX provider on success: `✓ FastEmbed (..., 384d) via CPU` or `via CUDA`.
2. On failure, detects CUDA-flavored error strings (`cuda`, `loadlibrary`, `onnxruntime_providers_cuda`) and appends an actionable hint: `— set QWE_EMBED_DEVICE=cpu or Settings → embed_device=cpu`.

## 📦 Upgrade

```bash
git pull && pip install -e . --upgrade
# Restart the server
```

If you're still getting CUDA errors in the doctor output after restart:

```bash
# Option 1 — quick env override
set QWE_EMBED_DEVICE=cpu
qwe-qwe --doctor

# Option 2 — persist in settings
qwe-qwe
# inside: /settings embed_device cpu
```

Or, to actually fix CUDA rather than avoid it:
- Install CUDA Toolkit 11.8 or 12.x (match the `onnxruntime-gpu` version — usually 12.x for latest)
- Install cuDNN 8.x matching the CUDA version
- Make sure `%CUDA_PATH%\bin` is on `PATH`
- Either keep `CUDA_PATH` set, or unset it entirely if you removed the Toolkit

**Permanent CPU-only install** (avoids the whole class of CUDA issues):

```bash
pip uninstall onnxruntime-gpu onnxruntime
pip install onnxruntime
```

## Why this was hidden before

FastEmbed's auto mode calls `TextEmbedding(model_name=...)` without explicit providers. On a clean Linux/Mac install or a Windows box with either *no* CUDA-adjacent env or a *correctly* installed CUDA, auto works. The half-installed case only surfaces for users who:

- Installed `onnxruntime-gpu` (either explicitly or via a `[gpu]` extra)
- Have `CUDA_PATH` still set from a previous install
- Don't have a matching cuDNN on PATH

Smoke-tested on this machine: both `QWE_EMBED_DEVICE=auto` and `=cpu` now return a valid 384d vector.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
