from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from .config import IMAGE_EXTENSIONS, thumbnail_dir

try:
    import pillow_heif  # type: ignore

    pillow_heif.register_heif_opener()
except Exception:
    pillow_heif = None


CAMERA_PREFIXES = {
    "img",
    "dsc",
    "dscn",
    "photo",
    "image",
    "pic",
    "screenshot",
    "screen",
    "capture",
    "whatsapp",
    "pxl",
}

STOCK_SOURCE_PREFIXES = {
    "adobestock",
    "dreamstime",
    "freepik",
    "getty",
    "gettyimages",
    "istock",
    "pexels",
    "pixabay",
    "shutterstock",
    "stock",
    "unsplash",
}

STOP_TOKENS = {
    "the",
    "and",
    "with",
    "from",
    "copy",
    "edited",
    "final",
    "new",
    "old",
    "scan",
    "untitled",
}


def is_supported_image(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS


def scan_image_files(folder: Path) -> list[Path]:
    return sorted(path for path in folder.rglob("*") if is_supported_image(path))


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def image_metadata(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "file_size": path.stat().st_size,
        "width": None,
        "height": None,
        "exif_date": None,
    }
    try:
        with Image.open(path) as image:
            result["width"], result["height"] = image.size
            exif = image.getexif()
            date_value = None
            for tag in (36867, 36868, 306):
                value = exif.get(tag)
                if value:
                    date_value = str(value)
                    break
            result["exif_date"] = normalize_exif_date(date_value)
    except Exception:
        pass
    return result


def normalize_exif_date(value: str | None) -> str | None:
    if not value:
        return None
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def make_thumbnail(path: Path, file_hash: str, max_size: tuple[int, int] = (320, 320)) -> Path | None:
    destination = thumbnail_dir() / f"{file_hash}.jpg"
    if destination.exists():
        return destination
    try:
        with Image.open(path) as image:
            image = ImageOps.exif_transpose(image)
            image.thumbnail(max_size)
            canvas = Image.new("RGB", max_size, (248, 250, 252))
            x = (max_size[0] - image.width) // 2
            y = (max_size[1] - image.height) // 2
            if image.mode in ("RGBA", "LA"):
                canvas.paste(image.convert("RGBA"), (x, y), image.convert("RGBA"))
            else:
                canvas.paste(image.convert("RGB"), (x, y))
            canvas.save(destination, "JPEG", quality=86, optimize=True)
        return destination
    except Exception:
        return None


def tokenize_filename(filename: str) -> list[str]:
    stem = Path(filename).stem.lower()
    return [token for token in re.split(r"[^a-z0-9]+", stem) if token]


def clean_token(token: str) -> str:
    token = re.sub(r"[^a-z0-9]+", "", token.lower())
    return token.strip("_")


def meaningful_filename_tokens(filename: str) -> list[str]:
    tokens = tokenize_filename(filename)
    if tokens and clean_token(tokens[0]) in STOCK_SOURCE_PREFIXES:
        return []

    cleaned: list[str] = []
    for token in tokens:
        token = clean_token(token)
        if not token:
            continue
        if token.isdigit():
            continue
        if token in CAMERA_PREFIXES or token in STOP_TOKENS:
            continue
        if token in STOCK_SOURCE_PREFIXES:
            continue
        if re.fullmatch(r"\d{4}", token) or re.fullmatch(r"\d{2}", token):
            continue
        cleaned.append(token)
    return cleaned


def infer_folder_from_filename(filename: str) -> str | None:
    tokens = meaningful_filename_tokens(filename)
    if len(tokens) < 2:
        return None

    stem = Path(filename).stem
    if not re.search(r"[_\-\s]", stem):
        return None

    candidates = tokens[1:] if len(tokens) > 1 else tokens
    for token in candidates:
        if len(token) >= 3 and not token.isdigit():
            return sanitize_folder_name(token)
    return None


def sanitize_folder_name(value: str, fallback: str = "Other") -> str:
    cleaned = re.sub(r"[<>:\"/\\|?*\x00-\x1f]+", "", value).strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:80] or fallback


def slugify(value: str, fallback: str = "image", max_length: int = 80) -> str:
    value = value.lower()
    value = value.replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    if not value:
        value = fallback
    return value[:max_length].strip("_") or fallback


def build_filename(
    *,
    date_value: str | None,
    category: str,
    description: str,
    extension: str,
    include_category: bool = True,
) -> str:
    date_part = date_value or datetime.now().date().isoformat()
    parts = [date_part]
    if include_category:
        parts.append(slugify(category, fallback="image", max_length=30))
    parts.append(slugify(description, fallback="image", max_length=45))
    base = "_".join(part for part in parts if part)
    return f"{base}{extension.lower()}"
