# v0.17.21 — Embeddings are CPU-only by design

Follow-up to v0.17.20. The user pointed out that adding GPU complexity to qwe-qwe's install path defeats its whole point — "single `pip install -e .` and go" should stay that way.

The v0.17.20 fix made CUDA failures recoverable. This release removes the question entirely: **by default, FastEmbed forces CPU** and doctor flags `onnxruntime-gpu` as a misconfiguration.

## 🔧 Changes

### Default `embed_device` flipped: `auto` → `cpu`

```python
# config.py EDITABLE_SETTINGS — before
"embed_device": ("setting:embed_device", str, "auto", ...)
# after
"embed_device": ("setting:embed_device", str, "cpu", ...)
```

`_fastembed_providers()` now returns `["CPUExecutionProvider"]` on fresh installs. No CUDA probe, no `LoadLibrary failed with error 126` noise, no half-installed-CUDA pain. The CPU embedder for `paraphrase-multilingual-MiniLM-L12-v2` runs comfortably — init ~2 s on a laptop, ~30 ms per embedding. Good enough for the 3 memory results per turn qwe-qwe retrieves.

### Doctor now detects `onnxruntime-gpu` and flags it

```
── Core ──
✓ Python: 3.11.9
✓ Dependencies
...
── ONNX Runtime ──
⚠ onnxruntime-gpu detected — qwe-qwe is CPU-only by design. Run:
    pip uninstall onnxruntime-gpu && pip install onnxruntime
```

`onnxruntime-gpu` ships with ~3 GB of CUDA DLLs and is the #1 source of the `error 126` import explosions. qwe-qwe embeddings don't need it. The new check uses `importlib.metadata` to detect the package without importing, so it's fast and doesn't trigger any loading.

On a clean `onnxruntime` install the check shows:

```
✓ onnxruntime (CPU) — correct for qwe-qwe
```

### Opting into GPU (if you really want it)

Nothing is removed — you can still:

1. Install CUDA Toolkit 12.x + cuDNN 8.x matching your `onnxruntime-gpu` version
2. `pip install onnxruntime-gpu`
3. Set `embed_device=cuda` (or `auto`) via Settings → Memory or `QWE_EMBED_DEVICE=cuda`

But that's now an explicit opt-in, not the default path. Most users on laptops don't need it — the LLM runs on GPU via LM Studio / Ollama; embeddings happen in a background thread and aren't the bottleneck.

## 🎯 Why this matters for install

qwe-qwe's pitch is "clone, `pip install -e .`, run — works on any laptop". Every release where a user opens an issue because of CUDA chews at that promise. Default-CPU + doctor-warning means:

- Fresh installs just work — no probe, no error 126.
- Users who inherited `onnxruntime-gpu` from a prior project (common — lots of ML libs list it in deps) see an amber warning with a one-line fix.
- GPU is still available if you explicitly want it, but it's no longer the default failure mode.

## 📦 Upgrade

```bash
git pull && pip install -e . --upgrade
# Restart the server
```

Doctor should now show `✓ FastEmbed (..., 384d) via CPU` and `✓ onnxruntime (CPU) — correct for qwe-qwe`. If it still shows the warning, run the uninstall command it suggests.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
