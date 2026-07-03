from __future__ import annotations

import base64
import json
import mimetypes
import re
from pathlib import Path
from typing import Any

import requests

from .config import AppSettings, DEFAULT_CATEGORIES, LLAMA_BASE_URL, LLAMA_MODEL_FILE
from .database import search_tokens_from_text
from .image_tools import infer_folder_from_filename, meaningful_filename_tokens, slugify
from .llama_runtime import LlamaRuntime


CATEGORY_ALIASES = {
    "Animals": {"animal", "animals", "bird", "cat", "dog", "pet", "wildlife"},
    "People": {"person", "people", "portrait", "man", "woman", "child", "crowd"},
    "Food": {"food", "meal", "drink", "restaurant"},
    "Documents": {"document", "documents", "paper", "receipt", "letter"},
    "Screenshots": {"screenshot", "screen", "interface", "ui"},
    "Vehicles": {"vehicle", "vehicles", "boat", "boats", "car", "cars", "yacht", "train"},
    "Nature": {
        "beach",
        "bay",
        "coast",
        "coastal",
        "forest",
        "landscape",
        "nature",
        "tree",
        "trees",
        "wildflower",
        "wildflowers",
        "woodland",
    },
    "Buildings": {
        "architecture",
        "building",
        "buildings",
        "church",
        "city",
        "cityscape",
        "landmark",
        "skyscraper",
        "street",
        "tower",
        "towers",
        "town",
    },
    "Products": {"product", "products", "item", "object"},
    "Art": {"art", "artwork", "painting", "drawing", "illustration"},
    "Events": {"event", "events", "party", "wedding", "concert", "festival"},
    "Needs Review": {"review", "needsreview", "uncertain", "failed"},
}


class AIClient:
    def __init__(self, settings: AppSettings | None = None) -> None:
        self.settings = settings or AppSettings()

    def with_settings(self, settings: AppSettings) -> "AIClient":
        return AIClient(settings)

    def test_connection(self) -> dict[str, Any]:
        runtime = LlamaRuntime()
        status = runtime.status()
        if not status["runtime_ready"] or not status["model_ready"] or not status["mmproj_ready"]:
            return {
                "ok": False,
                "message": "Local AI is not prepared yet.",
                "runtime": status,
                "models": None,
            }
        try:
            runtime.ensure_server()
            response = requests.get(f"{LLAMA_BASE_URL}/models", timeout=3)
            response.raise_for_status()
            models = response.json()
            names = {
                str(model.get("id") or model.get("name") or model.get("model") or "")
                for model in models.get("data", [])
                if isinstance(model, dict)
            }
            has_required_model = not names or any(LLAMA_MODEL_FILE in name for name in names)
            if has_required_model:
                return {
                    "ok": True,
                    "message": f"Local AI is ready with {LLAMA_MODEL_FILE}.",
                    "models": models,
                }
            return {
                "ok": False,
                "message": f"Local AI is running, but {LLAMA_MODEL_FILE} was not reported.",
                "models": models,
            }
        except Exception as exc:
            return {
                "ok": False,
                "message": f"Could not reach local AI: {exc}",
                "models": None,
            }

    def analyze_image(
        self,
        image_path: Path,
        *,
        categories: list[str] | None = None,
        filename_structure_enabled: bool = True,
    ) -> dict[str, Any]:
        allowed_categories = normalize_categories(categories)
        prompt = image_prompt(allowed_categories)
        content = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": image_data_url(image_path)}},
        ]
        raw = self._chat(content, temperature=0.1, max_tokens=700, json_mode=False)
        try:
            result = parse_json_object(raw)
        except Exception:
            retry_content = [
                {
                    "type": "text",
                    "text": image_prompt(allowed_categories, strict_retry=True),
                },
                {"type": "image_url", "image_url": {"url": image_data_url(image_path)}},
            ]
            raw = self._chat(retry_content, temperature=0.0, max_tokens=500, json_mode=False)
            result = parse_json_object(raw)
        return self._validate_image_result(
            result,
            image_path=image_path,
            categories=allowed_categories,
            filename_structure_enabled=filename_structure_enabled,
        )

    def translate_search_query(
        self, query: str, categories: list[str] | None = None
    ) -> dict[str, Any]:
        query = query.strip()
        if not query:
            return {"query": "", "keywords": [], "category": "", "needs_review": False}
        allowed_categories = normalize_categories(categories)
        prompt = search_prompt(query, allowed_categories)
        try:
            raw = self._chat(prompt, temperature=0.0, max_tokens=300)
            parsed = parse_json_object(raw)
            keywords = parsed.get("keywords") or parsed.get("tags") or []
            if isinstance(keywords, str):
                keywords = [keywords]
            category = parsed.get("category") if parsed.get("category") in allowed_categories else ""
            needs_review = bool(parsed.get("needs_review"))
        except Exception:
            keywords = []
            category = ""
            needs_review = "review" in query.lower()

        return normalize_search_intent(
            original_query=query,
            keywords=keywords,
            category=category,
            needs_review=needs_review,
            allowed_categories=allowed_categories,
        )

    def _chat(
        self,
        content: Any,
        *,
        temperature: float,
        max_tokens: int,
        json_mode: bool = True,
    ) -> str:
        if isinstance(content, str):
            messages = [{"role": "user", "content": content}]
        else:
            messages = [{"role": "user", "content": content}]

        payload = {
            "model": self.settings.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        response = requests.post(
            f"{self.settings.base_url.rstrip('/')}/chat/completions",
            headers=self._headers(),
            json=payload,
            timeout=(5, 120),
        )
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.settings.api_key}"}

    def _validate_image_result(
        self,
        result: dict[str, Any],
        *,
        image_path: Path,
        categories: list[str],
        filename_structure_enabled: bool,
    ) -> dict[str, Any]:
        fallback = fallback_image_result(image_path, categories)
        caption = str(result.get("caption") or fallback["caption"]).strip()
        category = str(result.get("category") or "").strip()
        if category not in categories:
            category = "Needs Review" if "Needs Review" in categories else fallback["category"]
        subcategory = str(result.get("subcategory") or "").strip()

        tags = clean_string_list(result.get("tags") or [])
        objects = clean_string_list(result.get("objects") or [])
        filename_folder = infer_folder_from_filename(image_path.name) if filename_structure_enabled else None
        if filename_folder and filename_folder not in tags:
            tags.append(filename_folder)

        suggested_filename = slugify(
            str(result.get("suggested_filename") or fallback["suggested_filename"]),
            fallback="image",
            max_length=55,
        )
        confidence = clamp_float(result.get("confidence"), default=0.5)
        needs_review = bool(result.get("needs_review")) or confidence < self.settings.confidence_threshold
        return {
            "caption": caption,
            "category": category,
            "subcategory": subcategory,
            "tags": tags[:20],
            "objects": objects[:20],
            "visible_text": str(result.get("visible_text") or "").strip(),
            "suggested_filename": suggested_filename,
            "suggested_folder": filename_folder,
            "confidence": confidence,
            "needs_review": needs_review,
            "model_name": self.settings.model,
            "last_error": "",
        }


def normalize_categories(categories: list[str] | None) -> list[str]:
    cleaned = []
    for category in categories or DEFAULT_CATEGORIES:
        category = str(category).strip()
        if category and category not in cleaned:
            cleaned.append(category)
    if "Needs Review" not in cleaned:
        cleaned.append("Needs Review")
    return cleaned


def normalize_search_intent(
    *,
    original_query: str,
    keywords: list[Any],
    category: str,
    needs_review: bool,
    allowed_categories: list[str],
) -> dict[str, Any]:
    keyword_text = " ".join(str(item) for item in keywords if str(item).strip())
    tokens = search_tokens_from_text(" ".join(part for part in (keyword_text, original_query) if part))
    inferred_category = category or infer_search_category(tokens, allowed_categories)
    return {
        "query": " ".join(tokens),
        "keywords": tokens,
        "category": inferred_category,
        "needs_review": needs_review or inferred_category == "Needs Review",
    }


def infer_search_category(tokens: list[str], allowed_categories: list[str]) -> str:
    token_set = set(tokens)
    for category, aliases in CATEGORY_ALIASES.items():
        if category not in allowed_categories:
            continue
        normalized_aliases = set(search_tokens_from_text(" ".join(aliases)))
        if token_set & normalized_aliases:
            return category
    return ""


def image_prompt(categories: list[str], *, strict_retry: bool = False) -> str:
    category_text = ", ".join(categories)
    retry_text = ""
    if strict_retry:
        retry_text = """
Your previous answer could not be read by the app.
Return one JSON object only. Start with { and end with }.
Do not include markdown, notes, prose, or code fences.
""".strip()
    return f"""
You are an image classification assistant for a private local photo organizer.

Look at the image and return ONLY valid JSON.
{retry_text}

Choose one category from this allowed list:
{category_text}

Do not identify real people by name.
Do not guess private or sensitive information.
If uncertain, set category to "Needs Review".
Make the filename short, descriptive, lowercase, and safe for a filesystem.

Return exactly this JSON shape:
{{
  "caption": "short useful description",
  "category": "one allowed category",
  "subcategory": "short subfolder label or empty string",
  "tags": ["searchable", "short", "tags"],
  "objects": ["visible", "objects"],
  "visible_text": "text visible in the image or empty string",
  "suggested_filename": "short_safe_filename_without_extension",
  "confidence": 0.0,
  "needs_review": false
}}
""".strip()


def search_prompt(query: str, categories: list[str]) -> str:
    category_text = ", ".join(categories)
    return f"""
You turn a user's natural-language image-library search into strict JSON.

User search:
{query}

Allowed categories:
{category_text}

Return ONLY JSON:
{{
  "keywords": ["simple", "search", "terms"],
  "category": "one allowed category if obvious, otherwise empty string",
  "needs_review": false
}}
""".strip()


def image_data_url(path: Path) -> str:
    mime_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def parse_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text, flags=re.IGNORECASE).strip()
        text = re.sub(r"```$", "", text).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("Model did not return a JSON object.")
    return json.loads(text[start : end + 1])


def fallback_image_result(image_path: Path, categories: list[str]) -> dict[str, Any]:
    tokens = meaningful_filename_tokens(image_path.name)
    filename_folder = infer_folder_from_filename(image_path.name)
    description = " ".join(tokens[:6]) if tokens else image_path.stem
    category = "Other" if "Other" in categories else categories[0]
    if filename_folder and filename_folder not in tokens:
        tokens.append(filename_folder)
    return {
        "caption": f"Image inferred from filename: {description}.",
        "category": category,
        "subcategory": filename_folder or "",
        "tags": tokens[:12],
        "objects": [],
        "visible_text": "",
        "suggested_filename": slugify(description, fallback="image", max_length=55),
        "suggested_folder": filename_folder,
        "confidence": 0.35,
        "needs_review": True,
        "model_name": "",
        "last_error": "",
    }


def clean_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    cleaned = []
    for item in value:
        item = str(item).strip().lower()
        item = re.sub(r"\s+", " ", item)
        if item and item not in cleaned:
            cleaned.append(item)
    return cleaned


def clamp_float(value: Any, *, default: float) -> float:
    try:
        number = float(value)
    except Exception:
        return default
    return min(1.0, max(0.0, number))
