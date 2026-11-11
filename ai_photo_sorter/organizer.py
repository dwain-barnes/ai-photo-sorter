from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .ai_client import AIClient, fallback_image_result, normalize_categories
from .config import AppSettings, DEFAULT_CATEGORIES
from .database import ImageDatabase
from .image_tools import (
    build_filename,
    image_metadata,
    infer_folder_from_filename,
    make_thumbnail,
    sanitize_folder_name,
    scan_image_files,
    sha256_file,
    slugify,
)


ProgressCallback = Callable[[int, int, str], None]


@dataclass
class ScanOptions:
    input_folder: Path
    output_folder: Path
    rename_images: bool = True
    organize_folders: bool = True
    create_database: bool = True
    filename_structure: bool = True
    category_mode: str = "automatic"
    custom_categories: str = ""
    batch_limit: int = 0
    allow_filename_only_fallback: bool = False
    settings: AppSettings | None = None

    @property
    def categories(self) -> list[str]:
        if self.category_mode == "custom":
            categories = [
                line.strip()
                for line in re.split(r"[\n,]+", self.custom_categories)
                if line.strip()
            ]
            return normalize_categories(categories or DEFAULT_CATEGORIES)
        return DEFAULT_CATEGORIES


class ImageOrganizer:
    def __init__(
        self,
        database: ImageDatabase | None = None,
        ai_client: AIClient | None = None,
    ) -> None:
        self.database = database or ImageDatabase()
        self.ai_client = ai_client or AIClient()

    def scan(self, options: ScanOptions, progress: ProgressCallback | None = None) -> list[dict[str, Any]]:
        settings = options.settings or AppSettings()
        client = self.ai_client.with_settings(settings)
        files = scan_image_files(options.input_folder)
        files = exclude_output_folder(files, options.input_folder, options.output_folder)
        if options.batch_limit and options.batch_limit > 0:
            files = files[: options.batch_limit]

        total = len(files)
        results: list[dict[str, Any]] = []
        reserved_destinations: set[str] = set()
        connection = client.test_connection()
        ai_available = bool(connection.get("ok"))
        if not ai_available and not options.allow_filename_only_fallback:
            message = str(connection.get("message") or "Local AI is not ready.")
            raise RuntimeError(
                f"{message} The scan was stopped because filename-only guesses would not inspect the images."
            )

        for index, image_path in enumerate(files, start=1):
            if progress:
                progress(index - 1, total, f"Scanning {image_path.name}")
            try:
                result = self._scan_one(
                    image_path,
                    options=options,
                    client=client,
                    ai_available=ai_available,
                    reserved_destinations=reserved_destinations,
                )
                results.append(result)
            except Exception as exc:
                fallback = self._error_result(image_path, str(exc), options, reserved_destinations)
                results.append(fallback)
            if progress:
                progress(index, total, f"Scanned {image_path.name}")

        return results

    def apply_changes(
        self,
        rows: list[dict[str, Any]],
        *,
        output_folder: Path,
        mode: str = "copy",
    ) -> dict[str, Any]:
        output_folder.mkdir(parents=True, exist_ok=True)
        undo_path = output_folder / f"undo_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
        applied = []
        skipped = []
        reserved: set[str] = set()

        with undo_path.open("a", encoding="utf-8") as undo_file:
            for row in rows:
                if not row.get("approved", True):
                    skipped.append({"id": row.get("id"), "reason": "Not approved"})
                    continue

                image_id = int(row["id"])
                image = self.database.get_image(image_id)
                if not image:
                    skipped.append({"id": image_id, "reason": "Image is no longer in the database"})
                    continue

                sorted_path = sorted_destination(image)
                if sorted_path:
                    skipped.append({"id": image_id, "reason": f"Already sorted to {sorted_path}"})
                    continue

                source = Path(image["original_path"])
                if not source.exists():
                    skipped.append({"id": image_id, "reason": "Original file is missing"})
                    continue

                review_update = review_update_from_row(row)
                self.database.update_review_fields(image_id, review_update)

                folder = sanitize_relative_folder(
                    review_update.get("suggested_folder")
                    or image.get("suggested_folder")
                    or image.get("category")
                    or "Other"
                )
                destination_dir = output_folder / folder
                filename = sanitize_filename(
                    review_update.get("new_filename") or image.get("new_filename") or source.name,
                    original_extension=source.suffix,
                )
                destination = None
                try:
                    destination_dir.mkdir(parents=True, exist_ok=True)
                    destination = unique_destination(destination_dir, filename, reserved)
                    if mode == "move":
                        shutil.move(str(source), str(destination), copy_function=copy_file)
                        action = "move"
                    else:
                        copy_file(source, destination)
                        action = "copy"
                except OSError as exc:
                    # Never leave a half-written copy behind; the original stays where it was.
                    if destination is not None and source.exists():
                        try:
                            destination.unlink(missing_ok=True)
                        except OSError:
                            pass
                    skipped.append({"id": image_id, "reason": f"Could not write file: {exc.strerror or exc}"})
                    continue

                undo_record = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "action": action,
                    "image_id": image_id,
                    "source": str(source),
                    "destination": str(destination),
                    "undo_hint": "delete_destination" if action == "copy" else "move_destination_back",
                }
                undo_file.write(json.dumps(undo_record, ensure_ascii=True) + "\n")
                self.database.mark_applied(image_id, str(destination), destination.name)
                applied.append({"id": image_id, "destination": str(destination)})

        return {
            "applied": applied,
            "skipped": skipped,
            "undo_log": str(undo_path),
        }

    def _scan_one(
        self,
        image_path: Path,
        *,
        options: ScanOptions,
        client: AIClient,
        ai_available: bool,
        reserved_destinations: set[str],
    ) -> dict[str, Any]:
        file_hash = sha256_file(image_path)
        metadata = image_metadata(image_path)
        thumbnail_path = make_thumbnail(image_path, file_hash)
        cached = self.database.get_by_hash(file_hash)
        if cached and has_real_ai_analysis(cached):
            ai_result = result_from_cached_row(cached)
            filename_folder = infer_folder_from_filename(image_path.name) if options.filename_structure else None
            if filename_folder:
                ai_result["suggested_folder"] = filename_folder
                if filename_folder not in ai_result.get("tags", []):
                    ai_result.setdefault("tags", []).append(filename_folder)
        elif ai_available:
            ai_result = client.analyze_image(
                image_path,
                categories=options.categories,
                filename_structure_enabled=options.filename_structure,
            )
        else:
            ai_result = fallback_image_result(image_path, options.categories)
            if options.filename_structure:
                filename_folder = infer_folder_from_filename(image_path.name)
                if filename_folder:
                    ai_result["suggested_folder"] = filename_folder

        suggested_folder = choose_suggested_folder(ai_result, options.organize_folders)
        new_filename = choose_suggested_filename(
            image_path=image_path,
            metadata=metadata,
            ai_result=ai_result,
            output_folder=options.output_folder,
            suggested_folder=suggested_folder,
            rename_images=options.rename_images,
            reserved_destinations=reserved_destinations,
        )

        db_payload = {
            "original_path": str(image_path),
            "current_path": None,
            "original_filename": image_path.name,
            "new_filename": new_filename,
            "file_hash": file_hash,
            "perceptual_hash": None,
            "thumbnail_path": str(thumbnail_path) if thumbnail_path else None,
            **metadata,
            **ai_result,
            "suggested_folder": suggested_folder,
        }
        return self._save_scan_row(image_path, db_payload)

    def _error_result(
        self,
        image_path: Path,
        error: str,
        options: ScanOptions,
        reserved_destinations: set[str],
    ) -> dict[str, Any]:
        file_hash = sha256_file(image_path)
        metadata = image_metadata(image_path)
        thumbnail_path = make_thumbnail(image_path, file_hash)
        folder = infer_folder_from_filename(image_path.name) or "Needs Review"
        ai_result = {
            "caption": f"Needs review: {error}",
            "category": "Needs Review",
            "subcategory": "",
            "tags": [],
            "objects": [],
            "visible_text": "",
            "suggested_filename": slugify(image_path.stem, fallback="image"),
            "suggested_folder": folder,
            "confidence": 0.0,
            "needs_review": True,
            "model_name": "",
            "last_error": error,
        }
        new_filename = choose_suggested_filename(
            image_path=image_path,
            metadata=metadata,
            ai_result=ai_result,
            output_folder=options.output_folder,
            suggested_folder=folder,
            rename_images=options.rename_images,
            reserved_destinations=reserved_destinations,
        )
        db_payload = {
            "original_path": str(image_path),
            "current_path": None,
            "original_filename": image_path.name,
            "new_filename": new_filename,
            "file_hash": file_hash,
            "perceptual_hash": None,
            "thumbnail_path": str(thumbnail_path) if thumbnail_path else None,
            **metadata,
            **ai_result,
            "suggested_folder": folder,
        }
        return self._save_scan_row(image_path, db_payload)


    def _save_scan_row(self, image_path: Path, db_payload: dict[str, Any]) -> dict[str, Any]:
        previous = self.database.get_by_original_path(str(image_path))
        sorted_path = sorted_destination(previous)
        if sorted_path:
            # Already copied/moved earlier: show where it went instead of a new suggestion.
            db_payload["new_filename"] = sorted_path.name
        image_id = self.database.upsert_image(db_payload)
        if previous and previous.get("current_path") and not sorted_path:
            # The earlier copy is gone, so the photo can be sorted again.
            self.database.clear_current_path(image_id)
        row = self.database.get_image(image_id) or db_payload
        row["id"] = image_id
        row["already_sorted"] = bool(sorted_path)
        return row


def sorted_destination(image: dict[str, Any] | None) -> Path | None:
    """Return where a photo was already sorted to, if that copy still exists."""
    if not image:
        return None
    current = image.get("current_path")
    if not current or current == image.get("original_path"):
        return None
    path = Path(current)
    try:
        return path if path.is_file() else None
    except OSError:
        return None


def exclude_output_folder(files: list[Path], input_folder: Path, output_folder: Path) -> list[Path]:
    try:
        input_root = input_folder.resolve()
        output_root = output_folder.resolve()
    except OSError:
        return files
    if output_root == input_root or not output_root.is_relative_to(input_root):
        return files
    return [path for path in files if not path.resolve().is_relative_to(output_root)]


def choose_suggested_folder(ai_result: dict[str, Any], organize_folders: bool) -> str:
    if not organize_folders:
        return ""
    filename_folder = ai_result.get("suggested_folder")
    if filename_folder:
        return sanitize_relative_folder(filename_folder)
    if ai_result.get("needs_review"):
        return "Needs Review"
    category = sanitize_folder_name(str(ai_result.get("category") or "Other"), fallback="Other")
    subcategory = sanitize_folder_name(str(ai_result.get("subcategory") or ""), fallback="")
    if subcategory and subcategory.lower() not in {category.lower(), "other", "unknown"}:
        return sanitize_relative_folder(f"{category}/{subcategory}")
    return sanitize_relative_folder(category)


def choose_suggested_filename(
    *,
    image_path: Path,
    metadata: dict[str, Any],
    ai_result: dict[str, Any],
    output_folder: Path,
    suggested_folder: str,
    rename_images: bool,
    reserved_destinations: set[str],
) -> str:
    if rename_images:
        category = ai_result.get("category") or suggested_folder or "image"
        description = ai_result.get("suggested_filename") or image_path.stem
        proposed = build_filename(
            date_value=metadata.get("exif_date"),
            category=str(category),
            description=str(description),
            extension=image_path.suffix,
        )
    else:
        proposed = image_path.name

    destination_dir = output_folder / sanitize_relative_folder(suggested_folder or "")
    return unique_destination(destination_dir, proposed, reserved_destinations).name


def result_from_cached_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "caption": row.get("caption") or "",
        "category": row.get("category") or "Other",
        "subcategory": row.get("subcategory") or "",
        "tags": row.get("tags") or [],
        "objects": row.get("objects") or [],
        "visible_text": row.get("visible_text") or "",
        "suggested_filename": cached_suggested_filename(row),
        "suggested_folder": row.get("suggested_folder") or "",
        "confidence": row.get("confidence") or 0.5,
        "needs_review": row.get("needs_review", False),
        "model_name": row.get("model_name") or "",
        "last_error": "",
    }


def has_real_ai_analysis(row: dict[str, Any]) -> bool:
    caption = str(row.get("caption") or "")
    model_name = str(row.get("model_name") or "")
    if not model_name:
        return False
    if row.get("last_error"):
        return False
    if caption.startswith("Image inferred from filename:"):
        return False
    return True


def cached_suggested_filename(row: dict[str, Any]) -> str:
    stem = Path(str(row.get("new_filename") or row.get("original_filename") or "image")).stem
    stem = re.sub(r"_\d{3}$", "", stem)
    stem = re.sub(r"^(?:unknown_date|\d{4}[_-]\d{2}[_-]\d{2})_", "", stem, flags=re.IGNORECASE)
    category_slug = slugify(str(row.get("category") or ""), fallback="", max_length=30)
    if category_slug and stem.startswith(f"{category_slug}_"):
        stem = stem[len(category_slug) + 1 :]
    return slugify(stem, fallback="image", max_length=55)


def review_update_from_row(row: dict[str, Any]) -> dict[str, Any]:
    tags = row.get("tags", [])
    if isinstance(tags, str):
        tags = [tag.strip() for tag in tags.split(",") if tag.strip()]
    return {
        "new_filename": row.get("new_filename"),
        "suggested_folder": row.get("suggested_folder"),
        "category": row.get("category"),
        "subcategory": row.get("subcategory"),
        "caption": row.get("caption"),
        "visible_text": row.get("visible_text"),
        "tags": tags,
        "confidence": row.get("confidence"),
        "needs_review": row.get("needs_review"),
    }


def sanitize_relative_folder(value: str) -> str:
    value = str(value or "").replace("\\", "/")
    parts = [
        sanitize_folder_name(part, fallback="")
        for part in value.split("/")
        if sanitize_folder_name(part, fallback="")
    ]
    return "/".join(parts)


def sanitize_filename(filename: str, *, original_extension: str) -> str:
    name = Path(str(filename)).name
    extension = Path(name).suffix or original_extension
    stem = Path(name).stem if Path(name).suffix else name
    stem = slugify(stem, fallback="image", max_length=80)
    extension = extension.lower()
    if not extension.startswith("."):
        extension = f".{extension}"
    return f"{stem}{extension}"


def copy_file(source: str | Path, destination: str | Path) -> str:
    """Copy contents, then metadata on a best-effort basis.

    Network mounts such as GVFS SMB shares reject chmod/utime, which makes
    shutil.copy2 raise after the data has already been written.
    """
    shutil.copyfile(source, destination)
    try:
        shutil.copystat(source, destination)
    except OSError:
        pass
    return str(destination)


def unique_destination(directory: Path, filename: str, reserved: set[str] | None = None) -> Path:
    reserved = reserved if reserved is not None else set()
    safe_name = sanitize_filename(filename, original_extension=Path(filename).suffix or ".jpg")
    path = directory / safe_name
    stem = path.stem
    extension = path.suffix
    stem = re.sub(r"_\d{3}$", "", stem)
    counter = 1
    while True:
        candidate = directory / f"{stem}_{counter:03d}{extension}"
        key = str(candidate).lower()
        if not candidate.exists() and key not in reserved:
            reserved.add(key)
            return candidate
        counter += 1
