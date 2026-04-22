"""Setup Inference Wizard — detect GPU, install Ollama, configure provider."""

import platform
import subprocess
import shutil
from rich.console import Console

console = Console()


def detect_gpu() -> dict:
    """Detect available GPU/accelerator.

    Returns: {"type": "nvidia"|"apple_silicon"|"cpu", "name": str, "vram_gb": float|None}
    """
    # 1. Check NVIDIA via nvidia-smi
    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10
            )
            if out.returncode == 0 and out.stdout.strip():
                parts = out.stdout.strip().split(",")
                name = parts[0].strip()
                vram = float(parts[1].strip()) / 1024  # MB to GB
                return {"type": "nvidia", "name": name, "vram_gb": round(vram, 1)}
        except Exception:
            pass

    # 2. Check Apple Silicon
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        try:
            out = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True, text=True, timeout=5
            )
            name = out.stdout.strip() or "Apple Silicon"
        except Exception:
            name = "Apple Silicon"

        try:
            out = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True, text=True, timeout=5
            )
            mem_gb = int(out.stdout.strip()) / (1024 ** 3)
        except Exception:
            mem_gb = None

        return {"type": "apple_silicon", "name": name, "vram_gb": round(mem_gb, 1) if mem_gb else None}

    # 3. CPU only
    return {"type": "cpu", "name": platform.processor() or "Unknown CPU", "vram_gb": None}


def _check_ollama_installed() -> bool:
    """Check if Ollama is already installed."""
    return shutil.which("ollama") is not None


def _check_ollama_running() -> bool:
    """Check if Ollama server is running."""
    import requests
    try:
        r = requests.get("http://localhost:11434/api/tags", timeout=3)
        return r.ok
    except Exception:
        return False


def recommend_model(gpu: dict) -> str:
    """Recommend a model based on available memory."""
    mem = gpu.get("vram_gb") or 0
    if mem >= 48:
        return "qwen3.5:35b"
    elif mem >= 24:
        return "qwen3.5:27b"
    elif mem >= 8:
        return "qwen3.5:9b"
    elif mem >= 4:
        return "qwen3.5:4b"
    elif mem >= 2:
        return "qwen3.5:2b"
    else:
        return "qwen3.5:0.8b"


def install_ollama() -> bool:
    """Install Ollama. Returns True on success."""
    system = platform.system()

    if system == "Darwin":
        if shutil.which("brew"):
            console.print("  [dim]$ brew install ollama[/]")
            result = subprocess.run(["brew", "install", "ollama"], timeout=300)
            return result.returncode == 0
        else:
            console.print("  [yellow]Install Ollama from: https://ollama.com/download[/]")
            return False

    elif system == "Linux":
        console.print("  [dim]$ curl -fsSL https://ollama.com/install.sh | sh[/]")
        result = subprocess.run(
            ["sh", "-c", "curl -fsSL https://ollama.com/install.sh | sh"],
            timeout=300
        )
        return result.returncode == 0

    else:  # Windows
        console.print("  [yellow]Install Ollama from: https://ollama.com/download[/]")
        console.print("  [dim]Or: winget install Ollama.Ollama[/]")
        return False


def pull_model(model: str) -> bool:
    """Download a model via Ollama. Returns True on success."""
    console.print(f"  [dim]$ ollama pull {model}[/]\n")
    result = subprocess.run(["ollama", "pull", model], timeout=600)
    return result.returncode == 0


def start_ollama() -> bool:
    """Start Ollama server if not running."""
    if _check_ollama_running():
        return True

    console.print("  [dim]Starting Ollama server...[/]")
    # Start in background
    subprocess.Popen(
        ["ollama", "serve"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True
    )

    # Wait for it to be ready
    import time
    for _ in range(15):
        time.sleep(1)
        if _check_ollama_running():
            return True

    return False


def configure_provider(model: str):
    """Auto-configure qwe-qwe to use Ollama (LLM only, embeddings handled by FastEmbed)."""
    import providers
    providers.add("ollama", url="http://localhost:11434/v1", key="ollama", models=[model])
    providers.switch("ollama")
    providers.set_model(model)


def run_wizard():
    """Interactive setup wizard."""
    console.print("\n  [bold yellow]⚡ Setup Inference Wizard[/]\n")

    # Step 1: Detect hardware
    console.print("  [cyan]Detecting hardware...[/]")
    gpu = detect_gpu()

    if gpu["type"] == "nvidia":
        vram = f" ({gpu['vram_gb']}GB VRAM)" if gpu["vram_gb"] else ""
        console.print(f"  [green]✓ NVIDIA GPU: {gpu['name']}{vram}[/]")
    elif gpu["type"] == "apple_silicon":
        mem = f" ({gpu['vram_gb']}GB unified)" if gpu["vram_gb"] else ""
        console.print(f"  [green]✓ Apple Silicon: {gpu['name']}{mem}[/]")
    else:
        console.print(f"  [yellow]⚠ No GPU detected — CPU-only mode (will be slow)[/]")

    # Step 2: Choose model
    mem = gpu.get("vram_gb") or 0
    recommended = recommend_model(gpu)
    models = [
        ("qwen3.5:0.8b", "0.8B", "~1GB", "Minimal, very fast"),
        ("qwen3.5:2b", "2B", "~2.7GB", "Light, basic tasks"),
        ("qwen3.5:4b", "4B", "~3.4GB", "Good balance for low memory"),
        ("qwen3.5:9b", "9B", "~6.6GB", "Best quality/performance ratio"),
        ("qwen3.5:27b", "27B", "~17GB", "High quality, needs 24GB+"),
        ("qwen3.5:35b", "35B", "~24GB", "Maximum local quality, needs 48GB+"),
    ]

    console.print(f"\n  [cyan]Choose a model:[/]\n")
    for i, (tag, size, vram, desc) in enumerate(models, 1):
        rec = " [green]← recommended[/]" if tag == recommended else ""
        fit = "✓" if mem >= float(vram.strip("~GB")) else "✗"
        color = "green" if fit == "✓" else "red"
        console.print(f"    [{color}]{fit}[/] {i}. [bold]{tag}[/] ({size}, {vram} RAM) — {desc}{rec}")

    console.print(f"\n  [yellow]Enter number (1-{len(models)}) or model name:[/]")
    try:
        choice = input("  > ").strip()
    except (EOFError, KeyboardInterrupt):
        console.print("\n  [dim]Cancelled.[/]")
        return

    if choice.isdigit() and 1 <= int(choice) <= len(models):
        model = models[int(choice) - 1][0]
    elif ":" in choice:
        model = choice  # custom model name like "llama3:8b"
    elif choice:
        model = choice
    else:
        model = recommended

    console.print(f"\n  [cyan]Selected:[/] [bold]{model}[/]")

    if gpu["type"] == "nvidia" and mem >= 24:
        console.print(f"    [dim]For production: consider vLLM (pip install vllm)[/]")

    # Step 3: Check if Ollama is already installed
    if _check_ollama_installed():
        console.print(f"\n  [green]✓ Ollama already installed[/]")
    else:
        console.print(f"\n  [yellow]Install Ollama? (y/n)[/]")
        try:
            answer = input("  > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print("\n  [dim]Cancelled.[/]")
            return

        if answer not in ("y", "yes", "да"):
            console.print("  [dim]Skipped. Install manually: https://ollama.com/download[/]")
            return

        console.print(f"\n  [yellow]Installing Ollama...[/]")
        if not install_ollama():
            console.print("  [red]✗ Installation failed. Install manually: https://ollama.com/download[/]")
            return
        console.print(f"  [green]✓ Ollama installed[/]")

    # Step 4: Start Ollama server
    console.print(f"\n  [cyan]Checking Ollama server...[/]")
    if not start_ollama():
        console.print("  [red]✗ Could not start Ollama. Run manually: ollama serve[/]")
        return
    console.print(f"  [green]✓ Ollama server running[/]")

    # Step 5: Pull model
    console.print(f"\n  [yellow]Download model {model}? This may take a few minutes. (y/n)[/]")
    try:
        answer = input("  > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        console.print("\n  [dim]Cancelled.[/]")
        return

    if answer in ("y", "yes", "да"):
        console.print(f"\n  [yellow]Downloading {model}...[/]")
        if not pull_model(model):
            console.print(f"  [red]✗ Download failed. Run manually: ollama pull {model}[/]")
            return
        console.print(f"  [green]✓ Model {model} downloaded[/]")
    else:
        console.print(f"  [dim]Skipped download. Run manually: ollama pull {model}[/]")

    # Step 6: Configure qwe-qwe
    console.print(f"\n  [cyan]Configuring qwe-qwe...[/]")
    configure_provider(model)
    console.print(f"  [green]✓ Provider: ollama (http://localhost:11434/v1)[/]")
    console.print(f"  [green]✓ Model: {model}[/]")
    console.print(f"  [green]✓ Embeddings: FastEmbed (local ONNX, no server needed)[/]")
    console.print(f"  [green]✓ Context window: 16384 tokens[/]")

    # Done
    console.print(f"\n  [bold green]⚡ Setup complete![/]")
    console.print(f"  [dim]Run: qwe-qwe[/]\n")
