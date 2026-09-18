from contextlib import contextmanager
import json
from pathlib import Path
import re
import socket
import subprocess
import time
from urllib.request import urlopen

from common import atomic, now, run
from process_guard import attach

PROFILES = {"Q3_K_XL": ("vulkan-q3", 99), "Q4_K_S": ("vulkan-q4s", 55), "Q4_K_M": ("vulkan-q4m", 50)}


def blocks_inference(process):
    name = str(process.get("ProcessName", ""))
    return (bool(re.search(r"llama-server|lm[ -]?studio|llmster", name, re.I))
            or ("overwatch" in name.lower() and process.get("WorkingSet64", 0) > 2 * 1024**3))


def ensure_available(folder):
    """Check before reserving a worker attempt; a busy GPU is not a failed test."""
    check = run(["powershell.exe", "-NoProfile", "-Command",
                 "Get-Process | Select-Object ProcessName,WorkingSet64 | ConvertTo-Json -Compress"], folder)
    try:
        processes = json.loads(check.stdout)
        if isinstance(processes, dict): processes = [processes]
        busy = any(blocks_inference(p) for p in processes)
    except (ValueError, TypeError, AttributeError):
        busy = True
    if check.returncode or busy:
        raise RuntimeError("GPU_busy_or_process_check_failed; existing apps were not stopped")
    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1", 8081)) == 0: raise RuntimeError("QA_port_8081_busy")


@contextmanager
def server(model, folder):
    """Own only this child PID; never stop a user's inference server."""
    folder = Path(folder); folder.mkdir(parents=True, exist_ok=True)
    profile, layers = PROFILES[model]
    ensure_available(folder)
    model_path = Path("D:/AI/Models/unsloth/Qwen3.8-27B-GGUF") / f"Qwen3.8-27B-UD-{model}.gguf"
    argv = ["D:/AI/llama.cpp/runtimes/b10997-vulkan/llama-server.exe", "-m", str(model_path),
            "--alias", "qa-local", "--host", "127.0.0.1", "--port", "8081", "--ctx-size", "16384",
            # Normal agent calls remain capped at 4096 in agent.py. Compaction
            # uses its own window-clamped budget and must not be cut at 4096.
            "--parallel", "1", "--n-predict", "8192", "--gpu-layers", str(layers), "--device", "Vulkan0",
            "--fit", "off", "--flash-attn", "on", "--cache-type-k", "f16", "--cache-type-v", "f16",
            "--batch-size", "256", "--ubatch-size", "256", "--threads", "8", "--threads-batch", "8",
            "--jinja", "--reasoning", "on", "--reasoning-budget", "1024", "--reasoning-format", "deepseek",
            "--temp", "1.0", "--top-p", "0.95", "--top-k", "20", "--min-p", "0", "--repeat-penalty", "1",
            "--no-context-shift", "--cache-ram", "512", "--offline", "--no-mmproj", "--metrics"]
    started = time.monotonic()
    with (folder / "server.stdout").open("w") as out, (folder / "server.stderr").open("w") as err:
        proc = subprocess.Popen(argv, stdout=out, stderr=err, creationflags=subprocess.CREATE_NO_WINDOW)
        attach(proc)
        atomic(folder / "server.json", {"pid": proc.pid, "started_at": now(), "model": model, "profile": profile, "arguments": argv, "model_bytes": model_path.stat().st_size})
        try:
            while time.monotonic() - started < 240:
                if proc.poll() is not None: raise RuntimeError("llama_server_failed_to_start")
                try:
                    with urlopen("http://127.0.0.1:8081/health", timeout=3) as response:
                        if response.status == 200: break
                except OSError: time.sleep(1)
            else: raise RuntimeError("llama_server_start_timeout")
            yield proc
        finally:
            if proc.poll() is None:
                proc.terminate()
                try: proc.wait(timeout=15)
                except subprocess.TimeoutExpired: proc.kill(); proc.wait()
