from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


APP_NAME = "AI Photo Sorter"
APP_DIR_NAME = ".ai_photo_sorter"
LLAMA_CPP_RELEASE_TAG = "b9860"
LLAMA_SERVER_PORT = 8321
LLAMA_BASE_URL = f"http://127.0.0.1:{LLAMA_SERVER_PORT}/v1"
LLAMA_API_KEY = "none"
LLAMA_MODEL_REPO = "unsloth/gemma-4-E4B-it-GGUF"
LLAMA_MODEL_FILE = "gemma-4-E4B-it-Q4_K_S.gguf"
LLAMA_MMPROJ_FILE = "mmproj-F16.gguf"
LLAMA_MODEL_URL = (
    f"https://huggingface.co/{LLAMA_MODEL_REPO}/resolve/main/{LLAMA_MODEL_FILE}"
)
LLAMA_MMPROJ_URL = (
    f"https://huggingface.co/{LLAMA_MODEL_REPO}/resolve/main/{LLAMA_MMPROJ_FILE}"
)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}

DEFAULT_CATEGORIES = [
    "Animals",
    "People",
    "Food",
    "Documents",
    "Screenshots",
    "Vehicles",
    "Nature",
    "Buildings",
    "Products",
    "Art",
    "Events",
    "Other",
    "Needs Review",
]


def app_data_dir() -> Path:
    root = Path(os.environ.get("AI_PHOTO_SORTER_HOME", Path.home() / APP_DIR_NAME))
    root.mkdir(parents=True, exist_ok=True)
    return root


@dataclass
class AppSettings:
    base_url: str = LLAMA_BASE_URL
    model: str = LLAMA_MODEL_FILE
    api_key: str = LLAMA_API_KEY
    confidence_threshold: float = float(os.environ.get("AI_PHOTO_SORTER_CONFIDENCE", "0.75"))
    copy_mode: str = os.environ.get("AI_PHOTO_SORTER_FILE_MODE", "copy")

    def __post_init__(self) -> None:
        self.base_url = LLAMA_BASE_URL
        self.model = LLAMA_MODEL_FILE
        self.api_key = LLAMA_API_KEY


def database_path() -> Path:
    return app_data_dir() / "image_library.db"


def thumbnail_dir() -> Path:
    path = app_data_dir() / "thumbnails"
    path.mkdir(parents=True, exist_ok=True)
    return path
