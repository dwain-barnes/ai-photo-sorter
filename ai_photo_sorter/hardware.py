from __future__ import annotations

import platform
import shutil
import subprocess
from dataclasses import dataclass


@dataclass
class HardwareProfile:
    system: str
    machine: str
    gpu_vendor: str = "cpu"
    gpu_name: str = ""
    acceleration: str = "cpu"

    @property
    def has_gpu_acceleration(self) -> bool:
        return self.acceleration != "cpu"


def detect_hardware() -> HardwareProfile:
    system = platform.system()
    machine = platform.machine().lower()
    profile = HardwareProfile(system=system, machine=machine)

    if system == "Darwin" and machine in {"arm64", "aarch64"}:
        profile.gpu_vendor = "apple"
        profile.gpu_name = "Apple Silicon"
        profile.acceleration = "metal"
        return profile

    gpu_names = detect_gpu_names(system)
    profile.gpu_name = ", ".join(gpu_names)

    if any("nvidia" in name.lower() for name in gpu_names):
        profile.gpu_vendor = "nvidia"
        profile.acceleration = "cuda"
    elif any("apple" in name.lower() for name in gpu_names):
        profile.gpu_vendor = "apple"
        profile.acceleration = "metal"
    elif any(token in name.lower() for name in gpu_names for token in ("amd", "radeon", "intel", "arc")):
        profile.gpu_vendor = "generic"
        profile.acceleration = "cpu"

    return profile


def detect_gpu_names(system: str | None = None) -> list[str]:
    system = system or platform.system()
    if system == "Windows":
        return detect_windows_gpus()
    if system == "Darwin":
        return detect_macos_gpus()
    return detect_linux_gpus()


def detect_windows_gpus() -> list[str]:
    command = [
        "powershell.exe",
        "-NoProfile",
        "-Command",
        "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name",
    ]
    return run_lines(command)


def detect_macos_gpus() -> list[str]:
    lines = run_lines(["system_profiler", "SPDisplaysDataType"])
    names = []
    for line in lines:
        line = line.strip()
        if line.startswith("Chipset Model:"):
            names.append(line.split(":", 1)[1].strip())
    return names


def detect_linux_gpus() -> list[str]:
    if shutil.which("lspci"):
        return [line for line in run_lines(["lspci"]) if "vga" in line.lower() or "3d" in line.lower()]
    return []


def run_lines(command: list[str]) -> list[str]:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:
        return []
    if completed.returncode != 0:
        return []
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]
