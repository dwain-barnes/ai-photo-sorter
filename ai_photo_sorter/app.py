from __future__ import annotations

import os
import platform
import socket
import subprocess
import threading
import uuid
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template, request, send_file

from .ai_client import AIClient
from .config import AppSettings, DEFAULT_CATEGORIES, database_path
from .database import ImageDatabase
from .hardware import detect_hardware
from .llama_runtime import LlamaRuntime
from .organizer import ImageOrganizer, ScanOptions


class ScanCancelled(Exception):
    pass


@dataclass
class ScanJob:
    id: str
    status: str = "queued"
    done: int = 0
    total: int = 0
    message: str = ""
    results: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    cancel_requested: bool = False


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, ScanJob] = {}
        self._lock = threading.Lock()

    def create(self) -> ScanJob:
        job = ScanJob(id=uuid.uuid4().hex)
        with self._lock:
            self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> ScanJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def update(self, job_id: str, **changes: Any) -> None:
        with self._lock:
            job = self._jobs[job_id]
            for key, value in changes.items():
                setattr(job, key, value)

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False
            if job.status in {"done", "error", "cancelled"}:
                return True
            job.cancel_requested = True
            job.status = "cancelling"
            job.message = "Stopping scan..."
            return True

    def is_cancelled(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            return bool(job and job.cancel_requested)


database = ImageDatabase()
organizer = ImageOrganizer(database=database)
jobs = JobStore()


def create_app() -> Flask:
    app = Flask(__name__)

    @app.get("/")
    def index() -> str:
        return render_template(
            "index.html",
            categories=DEFAULT_CATEGORIES,
            database_path=str(database_path()),
            default_settings=AppSettings(),
        )

    @app.get("/thumbnail/<int:image_id>")
    def thumbnail(image_id: int):
        row = database.get_image(image_id)
        path = Path(row["thumbnail_path"]) if row and row.get("thumbnail_path") else None
        if path and path.exists():
            return send_file(path, mimetype="image/jpeg", max_age=3600)
        return ("", 404)

    @app.post("/api/pick-folder")
    def pick_folder():
        path = open_folder_picker()
        return jsonify({"path": path})

    @app.post("/api/test-ai")
    def test_ai():
        settings = settings_from_payload(request.get_json(silent=True) or {})
        return jsonify(AIClient(settings).test_connection())

    @app.post("/api/prepare-local-ai")
    def prepare_local_ai():
        job = jobs.create()
        thread = threading.Thread(target=run_local_ai_setup_job, args=(job.id,), daemon=True)
        thread.start()
        return jsonify({"ok": True, "job_id": job.id})

    @app.post("/api/scan")
    def scan():
        payload = request.get_json(force=True)
        try:
            options = scan_options_from_payload(payload)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        job = jobs.create()
        thread = threading.Thread(target=run_scan_job, args=(job.id, options), daemon=True)
        thread.start()
        return jsonify({"ok": True, "job_id": job.id})

    @app.post("/api/jobs/<job_id>/cancel")
    def cancel_job(job_id: str):
        if not jobs.cancel(job_id):
            return jsonify({"ok": False, "error": "Job not found"}), 404
        return jsonify({"ok": True})

    @app.get("/api/jobs/<job_id>")
    def job_status(job_id: str):
        job = jobs.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "Job not found"}), 404
        return jsonify(
            {
                "ok": True,
                "id": job.id,
                "status": job.status,
                "done": job.done,
                "total": job.total,
                "message": job.message,
                "error": job.error,
                "results": [row_for_api(row) for row in job.results] if job.status == "done" else [],
            }
        )

    @app.post("/api/apply")
    def apply_changes():
        payload = request.get_json(force=True)
        output_folder = Path(str(payload.get("output_folder") or "")).expanduser()
        if not output_folder:
            return jsonify({"ok": False, "error": "Choose an output folder first."}), 400
        mode = "move" if payload.get("mode") == "move" else "copy"
        rows = payload.get("rows") or []
        result = organizer.apply_changes(rows, output_folder=output_folder, mode=mode)
        return jsonify({"ok": True, **result})

    @app.post("/api/search")
    def search():
        payload = request.get_json(force=True)
        settings = settings_from_payload(payload)
        query = str(payload.get("query") or "").strip()
        selected_category = str(payload.get("category") or "").strip()
        needs_review = bool(payload.get("needs_review"))
        known_categories = database.categories() or DEFAULT_CATEGORIES
        translated = AIClient(settings).translate_search_query(query, known_categories)
        category = selected_category or translated.get("category") or ""
        search_query = translated.get("query") or query
        results = database.search(
            query=search_query,
            category=category,
            needs_review=needs_review or bool(translated.get("needs_review")),
            limit=100,
        )
        return jsonify(
            {
                "ok": True,
                "interpreted_query": search_query,
                "category": category,
                "results": [row_for_api(row) for row in results],
            }
        )

    @app.get("/api/categories")
    def categories():
        return jsonify({"ok": True, "categories": database.categories() or DEFAULT_CATEGORIES})

    @app.get("/api/hardware")
    def hardware():
        profile = detect_hardware()
        return jsonify({"ok": True, "hardware": profile.__dict__})

    @app.post("/api/open-folder")
    def open_folder():
        payload = request.get_json(force=True)
        raw_path = str(payload.get("path") or "")
        if not raw_path:
            return jsonify({"ok": False, "error": "Missing path."}), 400
        path = Path(raw_path)
        folder = path if path.is_dir() else path.parent
        try:
            reveal_folder(folder)
            return jsonify({"ok": True})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500

    return app


def run_scan_job(job_id: str, options: ScanOptions) -> None:
    def progress(done: int, total: int, message: str) -> None:
        if jobs.is_cancelled(job_id):
            raise ScanCancelled()
        jobs.update(job_id, status="running", done=done, total=total, message=message)

    try:
        if jobs.is_cancelled(job_id):
            raise ScanCancelled()
        jobs.update(job_id, status="running", message="Starting scan")
        results = organizer.scan(options, progress=progress)
        jobs.update(
            job_id,
            status="done",
            done=len(results),
            total=len(results),
            message="Scan complete",
            results=results,
        )
    except ScanCancelled:
        jobs.update(job_id, status="cancelled", message="Scan cancelled")
    except Exception as exc:
        jobs.update(job_id, status="error", error=str(exc), message="Scan failed")


def run_local_ai_setup_job(job_id: str) -> None:
    def progress(fraction: float, message: str) -> None:
        jobs.update(
            job_id,
            status="running",
            done=max(0, min(100, int(fraction * 100))),
            total=100,
            message=message,
        )

    try:
        jobs.update(
            job_id,
            status="running",
            done=0,
            total=100,
            message="Preparing local AI",
        )
        LlamaRuntime().prepare(progress=progress)
        jobs.update(
            job_id,
            status="done",
            done=100,
            total=100,
            message="Local AI is ready",
            results=[],
        )
    except Exception as exc:
        jobs.update(job_id, status="error", done=100, total=100, error=str(exc), message="Local AI setup failed")


def scan_options_from_payload(payload: dict[str, Any]) -> ScanOptions:
    input_folder = Path(str(payload.get("input_folder") or "")).expanduser()
    output_folder = Path(str(payload.get("output_folder") or "")).expanduser()
    if not input_folder.exists() or not input_folder.is_dir():
        raise ValueError("Choose a valid input folder.")
    if not output_folder:
        raise ValueError("Choose a valid output folder.")
    return ScanOptions(
        input_folder=input_folder,
        output_folder=output_folder,
        rename_images=bool(payload.get("rename_images", True)),
        organize_folders=bool(payload.get("organize_folders", True)),
        create_database=bool(payload.get("create_database", True)),
        filename_structure=bool(payload.get("filename_structure", True)),
        category_mode=str(payload.get("category_mode") or "automatic"),
        custom_categories=str(payload.get("custom_categories") or ""),
        batch_limit=int(payload.get("batch_limit") or 0),
        allow_filename_only_fallback=bool(payload.get("allow_filename_only_fallback", False)),
        settings=settings_from_payload(payload),
    )


def settings_from_payload(payload: dict[str, Any]) -> AppSettings:
    return AppSettings(
        confidence_threshold=float(payload.get("confidence_threshold") or 0.75),
        copy_mode=str(payload.get("mode") or "copy"),
    )


def row_for_api(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "original_path": row.get("original_path"),
        "current_path": row.get("current_path"),
        "original_filename": row.get("original_filename"),
        "new_filename": row.get("new_filename"),
        "suggested_folder": row.get("suggested_folder") or "",
        "category": row.get("category") or "",
        "subcategory": row.get("subcategory") or "",
        "caption": row.get("caption") or "",
        "tags": row.get("tags") or [],
        "visible_text": row.get("visible_text") or "",
        "confidence": row.get("confidence") or 0,
        "needs_review": bool(row.get("needs_review")),
        "thumbnail_url": f"/thumbnail/{row.get('id')}",
        "last_error": row.get("last_error") or "",
    }


def open_folder_picker() -> str:
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory()
        root.destroy()
        return selected or ""
    except Exception:
        return ""


def reveal_folder(path: Path) -> None:
    system = platform.system()
    if system == "Windows":
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif system == "Darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def find_port(start: int = 7865, end: int = 7899) -> int:
    for port in range(start, end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("No local port available.")


def main() -> None:
    port = int(os.environ.get("AI_PHOTO_SORTER_PORT") or find_port())
    url = f"http://127.0.0.1:{port}"
    if os.environ.get("AI_PHOTO_SORTER_NO_BROWSER") != "1":
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    create_app().run(host="127.0.0.1", port=port, debug=False, threaded=True, use_reloader=False)
