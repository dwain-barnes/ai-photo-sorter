from __future__ import annotations

import ctypes
import os
import platform
import shutil
import stat
import subprocess
import tarfile
import time
import zipfile
from pathlib import Path
from typing import Callable

import requests

from .config import (
    LLAMA_BASE_URL,
    LLAMA_CPP_RELEASE_TAG,
    LLAMA_MMPROJ_FILE,
    LLAMA_MMPROJ_URL,
    LLAMA_MODEL_FILE,
    LLAMA_MODEL_URL,
    LLAMA_SERVER_PORT,
    app_data_dir,
)
from .hardware import HardwareProfile, detect_hardware


ProgressCallback = Callable[[float, str], None]
SERVER_CONFIG_KEY = "gemma4-e4b-mmproj-reasoning-off-v1"

_server_process: subprocess.Popen | None = None
_server_log_handle = None


class LlamaRuntime:
    def __init__(self) -> None:
        self.root = app_data_dir() / "llama_cpp"
        self.downloads = self.root / "downloads"
        self.runtime_dir = self.root / "runtime"
        self.models_dir = self.root / "models"
        self.runtime_marker = self.runtime_dir / ".runtime_asset"
        self.server_config_marker = self.root / "server_config.txt"
        self.log_path = self.root / "llama_server.log"
        self.hardware = detect_hardware()

    @property
    def model_path(self) -> Path:
        return self.models_dir / LLAMA_MODEL_FILE

    @property
    def mmproj_path(self) -> Path:
        return self.models_dir / LLAMA_MMPROJ_FILE

    def status(self) -> dict[str, object]:
        server = self.server_path()
        return {
            "runtime_ready": server is not None,
            "server_path": str(server) if server else "",
            "model_ready": self.model_path.exists(),
            "model_path": str(self.model_path),
            "mmproj_ready": self.mmproj_path.exists(),
            "mmproj_path": str(self.mmproj_path),
            "server_running": self.is_server_running(),
            "base_url": LLAMA_BASE_URL,
            "hardware": self.hardware.__dict__,
        }

    def prepare(self, progress: ProgressCallback | None = None) -> None:
        self.downloads.mkdir(parents=True, exist_ok=True)
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)

        asset_name, asset_url = runtime_asset(self.hardware)
        dependency_assets = runtime_dependency_assets(asset_name)
        archive_path = self.downloads / asset_name

        if self.runtime_needs_refresh(asset_name):
            self._progress(progress, 0.05, "Downloading llama.cpp runtime")
            download_file(asset_url, archive_path, progress, start=0.05, end=0.25)
            for dependency_name, dependency_url in dependency_assets:
                dependency_path = self.downloads / dependency_name
                self._progress(progress, 0.25, f"Downloading {dependency_name}")
                download_file(dependency_url, dependency_path, progress, start=0.25, end=0.29)
            self._progress(progress, 0.27, "Extracting llama.cpp runtime")
            self.stop_server()
            self._extract_runtime(archive_path)
            for dependency_name, _ in dependency_assets:
                self._extract_runtime_addon(self.downloads / dependency_name)
            self.runtime_marker.write_text(asset_name, encoding="utf-8")
        else:
            self._progress(progress, 0.25, "llama.cpp runtime already installed")

        if not self.model_path.exists():
            self._progress(progress, 0.30, f"Downloading {LLAMA_MODEL_FILE}")
            download_file(LLAMA_MODEL_URL, self.model_path, progress, start=0.30, end=0.82)
        else:
            self._progress(progress, 0.82, "Model already downloaded")

        if not self.mmproj_path.exists():
            self._progress(progress, 0.84, f"Downloading {LLAMA_MMPROJ_FILE}")
            download_file(LLAMA_MMPROJ_URL, self.mmproj_path, progress, start=0.84, end=0.96)
        else:
            self._progress(progress, 0.96, "Projector already downloaded")

        self._progress(progress, 0.98, "Starting local AI server")
        self.ensure_server(progress=progress)
        self._progress(progress, 1.0, "Local AI is ready")

    def ensure_server(self, progress: ProgressCallback | None = None) -> None:
        if self.is_server_running():
            if self.server_config_current():
                self._progress(progress, 0.99, "Local AI server is already running")
                return
            self.stop_server()

        server = self.server_path()
        if not server:
            raise RuntimeError("llama.cpp runtime is not installed.")
        if not self.model_path.exists():
            raise RuntimeError(f"Model file is missing: {self.model_path}")
        if not self.mmproj_path.exists():
            raise RuntimeError(f"Multimodal projector is missing: {self.mmproj_path}")

        self.root.mkdir(parents=True, exist_ok=True)
        start_llama_server(server, self.model_path, self.mmproj_path, self.log_path)

        deadline = time.time() + 180
        while time.time() < deadline:
            if self.is_server_running():
                self.server_config_marker.write_text(SERVER_CONFIG_KEY, encoding="utf-8")
                return
            time.sleep(1)
        raise RuntimeError(f"llama-server did not become ready. See log: {self.log_path}")

    def is_server_running(self) -> bool:
        try:
            response = requests.get(f"http://127.0.0.1:{LLAMA_SERVER_PORT}/v1/models", timeout=2)
            return response.status_code < 500
        except Exception:
            return False

    def server_path(self) -> Path | None:
        names = ["llama-server.exe"] if platform.system() == "Windows" else ["llama-server"]
        for name in names:
            for path in self.runtime_dir.rglob(name):
                if path.is_file():
                    return path
        return None

    def runtime_needs_refresh(self, asset_name: str) -> bool:
        if not self.server_path():
            return True
        wants_cuda = "cuda" in asset_name.lower()
        if wants_cuda and not self.cuda_runtime_dependencies_ready(asset_name):
            return True
        installed = self.installed_runtime_asset()
        if installed == asset_name:
            return False
        if installed:
            return True

        has_cuda_files = any(
            "cuda" in path.name.lower() or "cublas" in path.name.lower()
            for path in self.runtime_dir.rglob("*")
            if path.is_file()
        )
        return wants_cuda != has_cuda_files

    def cuda_runtime_dependencies_ready(self, asset_name: str) -> bool:
        if "cuda-12.4" in asset_name.lower():
            return (
                (self.runtime_dir / "cudart64_12.dll").exists()
                and (self.runtime_dir / "cublas64_12.dll").exists()
            )
        return True

    def installed_runtime_asset(self) -> str:
        try:
            return self.runtime_marker.read_text(encoding="utf-8").strip()
        except Exception:
            return ""

    def server_config_current(self) -> bool:
        try:
            return self.server_config_marker.read_text(encoding="utf-8").strip() == SERVER_CONFIG_KEY
        except Exception:
            return False

    def stop_server(self) -> None:
        global _server_process, _server_log_handle

        if _server_process and _server_process.poll() is None:
            _server_process.terminate()
            try:
                _server_process.wait(timeout=10)
            except Exception:
                _server_process.kill()
        _server_process = None

        if _server_log_handle:
            try:
                _server_log_handle.close()
            except Exception:
                pass
            _server_log_handle = None

        try:
            self.server_config_marker.unlink()
        except FileNotFoundError:
            pass
        except Exception:
            pass

        if platform.system() == "Windows":
            script = (
                "Get-CimInstance Win32_Process -Filter \"Name = 'llama-server.exe'\" "
                "| Where-Object { $_.CommandLine -like '*AI_PHOTO_SORTER*' "
                f"-or $_.CommandLine -like '*{self.root}*' "
                f"-or $_.CommandLine -like '*{LLAMA_SERVER_PORT}*' }} "
                "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
            )
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", script],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )

    def _extract_runtime(self, archive_path: Path) -> None:
        if self.runtime_dir.exists():
            shutil.rmtree(self.runtime_dir)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self._extract_archive(archive_path)

        server = self.server_path()
        if not server:
            raise RuntimeError("The llama.cpp runtime archive did not contain llama-server.")
        if platform.system() != "Windows":
            server.chmod(server.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def _extract_runtime_addon(self, archive_path: Path) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self._extract_archive(archive_path)

    def _extract_archive(self, archive_path: Path) -> None:
        if archive_path.suffix.lower() == ".zip":
            with zipfile.ZipFile(archive_path) as archive:
                safe_extract_zip(archive, self.runtime_dir)
        else:
            with tarfile.open(archive_path) as archive:
                safe_extract_tar(archive, self.runtime_dir)

    def _progress(self, progress: ProgressCallback | None, value: float, message: str) -> None:
        if progress:
            progress(value, message)


def runtime_asset(profile: HardwareProfile | None = None) -> tuple[str, str]:
    profile = profile or detect_hardware()
    system = profile.system
    machine = profile.machine

    if system == "Windows":
        if profile.acceleration == "cuda":
            asset = f"llama-{LLAMA_CPP_RELEASE_TAG}-bin-win-cuda-12.4-x64.zip"
        else:
            asset = f"llama-{LLAMA_CPP_RELEASE_TAG}-bin-win-cpu-x64.zip"
    elif system == "Darwin":
        arch = "arm64" if machine in {"arm64", "aarch64"} else "x64"
        asset = f"llama-{LLAMA_CPP_RELEASE_TAG}-bin-macos-{arch}.tar.gz"
    else:
        arch = "arm64" if machine in {"arm64", "aarch64"} else "x64"
        # llama.cpp does not publish Linux CUDA builds; Vulkan is the GPU build that runs on NVIDIA.
        if profile.has_gpu_acceleration and arch == "x64" and linux_vulkan_available():
            asset = f"llama-{LLAMA_CPP_RELEASE_TAG}-bin-ubuntu-vulkan-x64.tar.gz"
        else:
            asset = f"llama-{LLAMA_CPP_RELEASE_TAG}-bin-ubuntu-{arch}.tar.gz"

    return asset, f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_CPP_RELEASE_TAG}/{asset}"


def linux_vulkan_available() -> bool:
    try:
        ctypes.CDLL("libvulkan.so.1")
        return True
    except OSError:
        return False


def runtime_dependency_assets(asset_name: str) -> list[tuple[str, str]]:
    if "win-cuda-12.4-x64" not in asset_name:
        return []
    dependency = f"cudart-llama-bin-win-cuda-12.4-x64.zip"
    return [
        (
            dependency,
            f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_CPP_RELEASE_TAG}/{dependency}",
        )
    ]


def download_file(
    url: str,
    destination: Path,
    progress: ProgressCallback | None = None,
    *,
    start: float,
    end: float,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    part_path = destination.with_suffix(destination.suffix + ".part")

    with requests.get(url, stream=True, timeout=(15, 60)) as response:
        response.raise_for_status()
        total = int(response.headers.get("content-length") or 0)
        downloaded = 0
        with part_path.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                handle.write(chunk)
                downloaded += len(chunk)
                if progress and total:
                    fraction = start + ((downloaded / total) * (end - start))
                    mb_done = downloaded / (1024 * 1024)
                    mb_total = total / (1024 * 1024)
                    progress(min(end, fraction), f"Downloading {destination.name}: {mb_done:.0f} / {mb_total:.0f} MB")

    part_path.replace(destination)
    if destination.stat().st_size < 10 * 1024 * 1024:
        raise RuntimeError(f"Downloaded file looks too small: {destination}")


def start_llama_server(server: Path, model: Path, mmproj: Path, log_path: Path) -> None:
    global _server_process, _server_log_handle

    command = [
        str(server),
        "-m",
        str(model),
        "--mmproj",
        str(mmproj),
        "--host",
        "127.0.0.1",
        "--port",
        str(LLAMA_SERVER_PORT),
        "-c",
        "4096",
        "--reasoning",
        "off",
        "--reasoning-budget",
        "0",
        "--chat-template-kwargs",
        '{"enable_thinking":false}',
    ]

    hardware = detect_hardware()
    if hardware.has_gpu_acceleration:
        command.extend(["-ngl", "99"])

    env = os.environ.copy()
    env["PATH"] = str(server.parent) + os.pathsep + env.get("PATH", "")

    log_path.parent.mkdir(parents=True, exist_ok=True)
    _server_log_handle = log_path.open("ab")
    creationflags = 0
    if platform.system() == "Windows":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    _server_process = subprocess.Popen(
        command,
        cwd=str(server.parent),
        stdout=_server_log_handle,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        env=env,
        creationflags=creationflags,
    )


def safe_extract_zip(archive: zipfile.ZipFile, destination: Path) -> None:
    destination = destination.resolve()
    for member in archive.infolist():
        target = (destination / member.filename).resolve()
        if not target.is_relative_to(destination):
            raise RuntimeError(f"Refusing to extract outside runtime directory: {member.filename}")
    archive.extractall(destination)


def safe_extract_tar(archive: tarfile.TarFile, destination: Path) -> None:
    destination = destination.resolve()
    for member in archive.getmembers():
        target = (destination / member.name).resolve()
        if not target.is_relative_to(destination):
            raise RuntimeError(f"Refusing to extract outside runtime directory: {member.name}")
    archive.extractall(destination)
