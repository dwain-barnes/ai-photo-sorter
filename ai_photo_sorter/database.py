from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import database_path
from .image_tools import slugify


IMAGE_COLUMNS = [
    "original_path",
    "current_path",
    "original_filename",
    "new_filename",
    "suggested_folder",
    "file_hash",
    "perceptual_hash",
    "file_size",
    "width",
    "height",
    "exif_date",
    "scan_date",
    "category",
    "subcategory",
    "caption",
    "tags_json",
    "objects_json",
    "visible_text",
    "model_name",
    "confidence",
    "needs_review",
    "thumbnail_path",
    "last_error",
]

SEARCH_STOP_WORDS = {
    "a",
    "about",
    "all",
    "an",
    "and",
    "any",
    "are",
    "can",
    "find",
    "for",
    "from",
    "has",
    "have",
    "i",
    "image",
    "images",
    "in",
    "is",
    "me",
    "of",
    "on",
    "photo",
    "photos",
    "picture",
    "pictures",
    "pic",
    "pics",
    "please",
    "search",
    "show",
    "that",
    "the",
    "to",
    "with",
    "you",
}


class ImageDatabase:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or database_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self) -> Iterable[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS images (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    original_path TEXT NOT NULL,
                    current_path TEXT,
                    original_filename TEXT,
                    new_filename TEXT,
                    suggested_folder TEXT,
                    file_hash TEXT,
                    perceptual_hash TEXT,
                    file_size INTEGER,
                    width INTEGER,
                    height INTEGER,
                    exif_date TEXT,
                    scan_date TEXT,
                    category TEXT,
                    subcategory TEXT,
                    caption TEXT,
                    tags_json TEXT,
                    objects_json TEXT,
                    visible_text TEXT,
                    model_name TEXT,
                    confidence REAL,
                    needs_review INTEGER,
                    thumbnail_path TEXT,
                    last_error TEXT
                )
                """
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_images_original_path ON images(original_path)"
            )
            connection.execute("CREATE INDEX IF NOT EXISTS idx_images_hash ON images(file_hash)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_images_category ON images(category)")
            connection.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS images_fts USING fts5(
                    caption,
                    tags,
                    category,
                    subcategory,
                    visible_text,
                    original_filename,
                    new_filename,
                    suggested_folder
                )
                """
            )

    def upsert_image(self, data: dict[str, Any]) -> int:
        payload = self._normalize_payload(data)
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT id FROM images WHERE original_path = ?", (payload["original_path"],)
            ).fetchone()
            if existing:
                image_id = int(existing["id"])
                # A rescan must not forget where the photo was already sorted to.
                assignments = ", ".join(
                    "current_path = COALESCE(?, current_path)" if column == "current_path" else f"{column} = ?"
                    for column in IMAGE_COLUMNS
                )
                values = [payload.get(column) for column in IMAGE_COLUMNS]
                connection.execute(
                    f"UPDATE images SET {assignments} WHERE id = ?",
                    [*values, image_id],
                )
            else:
                placeholders = ", ".join("?" for _ in IMAGE_COLUMNS)
                columns = ", ".join(IMAGE_COLUMNS)
                values = [payload.get(column) for column in IMAGE_COLUMNS]
                cursor = connection.execute(
                    f"INSERT INTO images ({columns}) VALUES ({placeholders})",
                    values,
                )
                image_id = int(cursor.lastrowid)
            self._replace_fts_row(connection, image_id, payload)
            return image_id

    def mark_applied(self, image_id: int, current_path: str, new_filename: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE images SET current_path = ?, new_filename = ? WHERE id = ?",
                (current_path, new_filename, image_id),
            )
            row = connection.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
            if row:
                self._replace_fts_row(connection, image_id, dict(row))

    def clear_current_path(self, image_id: int) -> None:
        with self.connect() as connection:
            connection.execute("UPDATE images SET current_path = NULL WHERE id = ?", (image_id,))

    def get_image(self, image_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
            return self._row_to_dict(row) if row else None

    def get_by_original_path(self, original_path: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM images WHERE original_path = ?", (original_path,)
            ).fetchone()
            return self._row_to_dict(row) if row else None

    def get_by_hash(self, file_hash: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM images
                WHERE file_hash = ? AND caption IS NOT NULL AND caption != ''
                ORDER BY scan_date DESC
                LIMIT 1
                """,
                (file_hash,),
            ).fetchone()
            return self._row_to_dict(row) if row else None

    def get_images(self, image_ids: list[int]) -> list[dict[str, Any]]:
        if not image_ids:
            return []
        placeholders = ", ".join("?" for _ in image_ids)
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM images WHERE id IN ({placeholders})", image_ids
            ).fetchall()
            by_id = {int(row["id"]): self._row_to_dict(row) for row in rows}
            return [by_id[image_id] for image_id in image_ids if image_id in by_id]

    def update_review_fields(self, image_id: int, data: dict[str, Any]) -> None:
        allowed = {
            "new_filename",
            "suggested_folder",
            "category",
            "subcategory",
            "caption",
            "visible_text",
            "confidence",
            "needs_review",
        }
        payload = {key: data[key] for key in allowed if key in data}
        if "tags" in data:
            payload["tags_json"] = _json_text(data.get("tags") or [])
        if "objects" in data:
            payload["objects_json"] = _json_text(data.get("objects") or [])
        if not payload:
            return
        if "needs_review" in payload:
            payload["needs_review"] = 1 if payload["needs_review"] else 0
        assignments = ", ".join(f"{key} = ?" for key in payload)
        values = [payload[key] for key in payload]
        with self.connect() as connection:
            connection.execute(
                f"UPDATE images SET {assignments} WHERE id = ?",
                [*values, image_id],
            )
            row = connection.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
            if row:
                self._replace_fts_row(connection, image_id, dict(row))

    def categories(self) -> list[str]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT category
                FROM images
                WHERE category IS NOT NULL AND category != ''
                ORDER BY category COLLATE NOCASE
                """
            ).fetchall()
            return [str(row["category"]) for row in rows]

    def search(
        self,
        *,
        query: str = "",
        category: str = "",
        needs_review: bool = False,
        limit: int = 80,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        join = ""

        fts_query = fts_query_from_text(query)
        if fts_query:
            join = "JOIN images_fts ON images_fts.rowid = images.id"
            clauses.append("images_fts MATCH ?")
            params.append(fts_query)

        if category:
            clauses.append("images.category = ?")
            params.append(category)

        if needs_review:
            clauses.append("images.needs_review = 1")

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
            SELECT images.*
            FROM images
            {join}
            {where}
            ORDER BY COALESCE(images.current_path, images.original_path) COLLATE NOCASE
            LIMIT ?
        """
        params.append(limit)

        with self.connect() as connection:
            try:
                rows = connection.execute(sql, params).fetchall()
            except sqlite3.OperationalError:
                rows = self._fallback_like_search(connection, query, category, needs_review, limit)
            return [self._row_to_dict(row) for row in rows]

    def _fallback_like_search(
        self,
        connection: sqlite3.Connection,
        query: str,
        category: str,
        needs_review: bool,
        limit: int,
    ) -> list[sqlite3.Row]:
        tokens = search_tokens_from_text(query)
        clauses: list[str] = []
        params: list[Any] = []
        for token in tokens:
            clauses.append(
                """
                (
                    caption LIKE ?
                    OR tags_json LIKE ?
                    OR visible_text LIKE ?
                    OR original_filename LIKE ?
                    OR new_filename LIKE ?
                    OR suggested_folder LIKE ?
                )
                """
            )
            like_value = f"%{token}%"
            params.extend([like_value] * 6)
        if category:
            clauses.append("category = ?")
            params.append(category)
        if needs_review:
            clauses.append("needs_review = 1")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        return connection.execute(f"SELECT * FROM images {where} LIMIT ?", params).fetchall()

    def _replace_fts_row(
        self, connection: sqlite3.Connection, image_id: int, payload: dict[str, Any]
    ) -> None:
        tags = payload.get("tags_json") or ""
        try:
            parsed_tags = json.loads(tags)
            if isinstance(parsed_tags, list):
                tags = " ".join(str(tag) for tag in parsed_tags)
        except Exception:
            pass

        connection.execute("DELETE FROM images_fts WHERE rowid = ?", (image_id,))
        connection.execute(
            """
            INSERT INTO images_fts (
                rowid,
                caption,
                tags,
                category,
                subcategory,
                visible_text,
                original_filename,
                new_filename,
                suggested_folder
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                image_id,
                payload.get("caption") or "",
                tags,
                payload.get("category") or "",
                payload.get("subcategory") or "",
                payload.get("visible_text") or "",
                payload.get("original_filename") or "",
                payload.get("new_filename") or "",
                payload.get("suggested_folder") or "",
            ),
        )

    def _normalize_payload(self, data: dict[str, Any]) -> dict[str, Any]:
        payload = {column: data.get(column) for column in IMAGE_COLUMNS}
        payload["original_path"] = str(data["original_path"])
        payload["original_filename"] = data.get("original_filename") or Path(
            payload["original_path"]
        ).name
        payload["scan_date"] = data.get("scan_date") or datetime.now(timezone.utc).isoformat()
        payload["tags_json"] = _json_text(data.get("tags") or data.get("tags_json") or [])
        payload["objects_json"] = _json_text(data.get("objects") or data.get("objects_json") or [])
        payload["needs_review"] = 1 if data.get("needs_review") else 0
        if data.get("thumbnail_path") is not None:
            payload["thumbnail_path"] = str(data.get("thumbnail_path"))
        return payload

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        for key in ("tags_json", "objects_json"):
            value = data.get(key)
            try:
                data[key.replace("_json", "")] = json.loads(value) if value else []
            except Exception:
                data[key.replace("_json", "")] = []
        data["needs_review"] = bool(data.get("needs_review"))
        return data


def _json_text(value: Any) -> str:
    if isinstance(value, str):
        try:
            json.loads(value)
            return value
        except Exception:
            return json.dumps([value])
    return json.dumps(value or [], ensure_ascii=True)


def fts_query_from_text(text: str) -> str:
    return " ".join(f"{token}*" for token in search_tokens_from_text(text))


def search_tokens_from_text(text: str) -> list[str]:
    tokens: list[str] = []
    for raw_token in re.findall(r"[a-z0-9]+", text.lower()):
        token = normalize_search_token(raw_token)
        if len(token) < 2 or token in SEARCH_STOP_WORDS or token in tokens:
            continue
        tokens.append(token)
    return tokens


def normalize_search_token(token: str) -> str:
    token = slugify(token, fallback="").replace("_", "")
    if len(token) > 4 and token.endswith("ies"):
        token = f"{token[:-3]}y"
    elif len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        token = token[:-1]
    return token
