from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from ai_photo_sorter.ai_client import AIClient, normalize_search_intent
from ai_photo_sorter.app import JobStore, create_app, scan_options_from_payload
from ai_photo_sorter.config import AppSettings, LLAMA_BASE_URL, LLAMA_MODEL_FILE
from ai_photo_sorter.database import ImageDatabase
from ai_photo_sorter.image_tools import infer_folder_from_filename
from ai_photo_sorter.organizer import (
    ImageOrganizer,
    ScanOptions,
    cached_suggested_filename,
    choose_suggested_filename,
    sanitize_filename,
    unique_destination,
)
from ai_photo_sorter.hardware import HardwareProfile
from ai_photo_sorter.llama_runtime import runtime_asset, runtime_dependency_assets


class OfflineAIClient:
    def with_settings(self, settings: AppSettings) -> "OfflineAIClient":
        return self

    def test_connection(self) -> dict[str, object]:
        return {"ok": False}


class FailingVisionAIClient:
    def with_settings(self, settings: AppSettings) -> "FailingVisionAIClient":
        return self

    def test_connection(self) -> dict[str, object]:
        return {"ok": True}

    def analyze_image(self, *args: object, **kwargs: object) -> dict[str, object]:
        raise RuntimeError("vision request failed")


class SuccessfulVisionAIClient:
    def with_settings(self, settings: AppSettings) -> "SuccessfulVisionAIClient":
        return self

    def test_connection(self) -> dict[str, object]:
        return {"ok": True}

    def analyze_image(self, *args: object, **kwargs: object) -> dict[str, object]:
        return {
            "caption": "The Eiffel Tower in Paris, France.",
            "category": "Buildings",
            "subcategory": "Landmarks",
            "tags": ["eiffel tower", "paris", "france", "landmark"],
            "objects": ["tower"],
            "visible_text": "",
            "suggested_filename": "eiffel_tower_paris",
            "suggested_folder": "",
            "confidence": 0.93,
            "needs_review": False,
            "model_name": LLAMA_MODEL_FILE,
            "last_error": "",
        }


class FilenameRulesTest(unittest.TestCase):
    def test_organize_page_keeps_scan_controls_simple(self) -> None:
        response = create_app().test_client().get("/")
        html = response.get_data(as_text=True)

        self.assertIn('id="scanImages"', html)
        self.assertIn('id="cancelScan"', html)
        self.assertIn('id="fileMode"', html)
        self.assertNotIn("Prepare Local AI", html)
        self.assertNotIn("Batch limit", html)

    def test_job_store_can_request_scan_cancel(self) -> None:
        store = JobStore()
        job = store.create()

        self.assertTrue(store.cancel(job.id))
        self.assertTrue(store.is_cancelled(job.id))
        self.assertEqual(store.get(job.id).status, "cancelling")

    def test_scan_rejects_missing_output_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "output folder"):
                scan_options_from_payload({"input_folder": tmp, "output_folder": ""})

    def test_job_store_drops_old_finished_jobs(self) -> None:
        store = JobStore()
        first = store.create()
        store.update(first.id, status="done")
        for _ in range(JobStore.MAX_JOBS):
            job = store.create()
            store.update(job.id, status="done")

        self.assertIsNone(store.get(first.id))
        self.assertIsNotNone(store.get(job.id))

    def test_app_settings_lock_to_llama_cpp_gemma4_e4b_gguf(self) -> None:
        settings = AppSettings(base_url="http://example.invalid/v1", model="something-else")
        self.assertEqual(settings.base_url, LLAMA_BASE_URL)
        self.assertEqual(settings.model, LLAMA_MODEL_FILE)

    def test_llama_runtime_selects_cuda_for_nvidia_windows(self) -> None:
        asset, _ = runtime_asset(
            HardwareProfile(
                system="Windows",
                machine="amd64",
                gpu_vendor="nvidia",
                gpu_name="NVIDIA GeForce RTX",
                acceleration="cuda",
            )
        )
        self.assertIn("win-cuda-12.4-x64", asset)
        dependencies = [name for name, _ in runtime_dependency_assets(asset)]
        self.assertIn("cudart-llama-bin-win-cuda-12.4-x64.zip", dependencies)

    def test_llama_runtime_selects_cpu_for_windows_without_cuda(self) -> None:
        asset, _ = runtime_asset(HardwareProfile(system="Windows", machine="amd64"))
        self.assertIn("win-cpu-x64", asset)

    def test_llama_runtime_selects_macos_arm64_for_apple_silicon(self) -> None:
        asset, _ = runtime_asset(
            HardwareProfile(
                system="Darwin",
                machine="arm64",
                gpu_vendor="apple",
                gpu_name="Apple Silicon",
                acceleration="metal",
            )
        )
        self.assertIn("macos-arm64", asset)

    def test_structured_filename_prefers_second_topic_token(self) -> None:
        self.assertEqual(infer_folder_from_filename("Tamworth_townhall_01.jpg"), "townhall")
        self.assertEqual(infer_folder_from_filename("Tamworth-townhall-west.png"), "townhall")

    def test_camera_style_filename_does_not_force_folder(self) -> None:
        self.assertIsNone(infer_folder_from_filename("IMG_3928.jpg"))

    def test_stock_download_filename_does_not_force_folder(self) -> None:
        self.assertIsNone(infer_folder_from_filename("pexels-alejandro-aznar-155337093-17976859.jpg"))

    def test_vision_tags_are_not_polluted_by_stock_filename(self) -> None:
        result = AIClient()._validate_image_result(
            {
                "caption": "The Eiffel Tower in Paris, France.",
                "category": "Buildings",
                "subcategory": "Landmarks",
                "tags": ["eiffel tower", "paris", "france"],
                "objects": ["tower"],
                "visible_text": "",
                "suggested_filename": "eiffel_tower_paris",
                "confidence": 0.95,
                "needs_review": False,
            },
            image_path=Path("pexels-alejandro-aznar-155337093-17976859.jpg"),
            categories=["Buildings", "Needs Review"],
            filename_structure_enabled=True,
        )

        self.assertEqual(result["suggested_folder"], None)
        self.assertIn("eiffel tower", result["tags"])
        self.assertNotIn("pexels", result["tags"])
        self.assertNotIn("alejandro", result["tags"])

    def test_scan_preview_does_not_create_output_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "Organized"
            filename = choose_suggested_filename(
                image_path=Path("Tamworth_townhall_01.jpg"),
                metadata={"exif_date": None},
                ai_result={
                    "category": "Buildings",
                    "suggested_filename": "tamworth_townhall",
                },
                output_folder=output,
                suggested_folder="townhall",
                rename_images=True,
                reserved_destinations=set(),
            )
            self.assertTrue(filename.endswith("_001.jpg"))
            self.assertFalse((output / "townhall").exists())

    def test_filename_uses_scan_date_when_photo_date_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            filename = choose_suggested_filename(
                image_path=Path("city.jpg"),
                metadata={"exif_date": None},
                ai_result={
                    "category": "Buildings",
                    "suggested_filename": "city_skyline",
                },
                output_folder=output,
                suggested_folder="Buildings",
                rename_images=True,
                reserved_destinations=set(),
            )

            self.assertNotIn("unknown_date", filename)

    def test_cached_filename_does_not_keep_old_date_prefix(self) -> None:
        suggested = cached_suggested_filename(
            {
                "new_filename": "unknown_date_buildings_eiffel_tower_paris_001.jpg",
                "category": "Buildings",
            }
        )

        self.assertEqual(suggested, "eiffel_tower_paris")

    def test_unique_destination_never_overwrites(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "image_001.jpg").write_text("existing", encoding="utf-8")
            destination = unique_destination(folder, "image.jpg", set())
            self.assertEqual(destination.name, "image_002.jpg")

    def test_sanitize_filename_keeps_extension(self) -> None:
        self.assertEqual(sanitize_filename("Bad Name!!.JPG", original_extension=".jpg"), "bad_name.jpg")


class InstallerPackagingTest(unittest.TestCase):
    def test_macos_packaging_builds_product_installer(self) -> None:
        script = Path("installer/macos/build_macos_installer.sh").read_text(encoding="utf-8")
        spec = Path("installer/pyinstaller/AIPhotoSorter.spec").read_text(encoding="utf-8")
        readme = Path("installer/macos/resources/ReadMe.html").read_text(encoding="utf-8")

        self.assertIn('VERSION="0.2.2"', script)
        self.assertIn('chmod +x "$ROOT/installer/macos/scripts/postinstall"', script)
        self.assertIn("productbuild", script)
        self.assertIn("AIPhotoSorterInstaller.dmg", script)
        self.assertNotIn("AIPhotoSorterDragToApplications.dmg", script)
        self.assertIn('"universal2"', spec)
        self.assertIn("5.8 GB", readme)

    def test_ci_workflow_builds_platform_installers(self) -> None:
        workflow = Path(".github/workflows/build-installers.yml").read_text(encoding="utf-8")

        self.assertIn("windows-latest", workflow)
        self.assertIn("macos-latest", workflow)
        self.assertIn("release/AIPhotoSorterSetup-0.2.2.exe", workflow)
        self.assertIn("release/AIPhotoSorter.pkg", workflow)
        self.assertIn("release/AIPhotoSorterInstaller.dmg", workflow)

    def test_windows_setup_replaces_wrong_llama_runtime_flavor(self) -> None:
        script = Path("installer/windows/setup_local_ai_windows.ps1").read_text(encoding="utf-8")

        self.assertIn(".runtime_asset", script)
        self.assertIn("Runtime-Matches $RuntimeAsset", script)
        self.assertIn("Stop-LlamaServer", script)
        self.assertIn("cudart-llama-bin-win-cuda-12.4-x64.zip", script)
        self.assertIn("Cuda-Dlls-Ready", script)

    def test_llama_server_disables_gemma_thinking_mode(self) -> None:
        runtime = Path("ai_photo_sorter/llama_runtime.py").read_text(encoding="utf-8")

        self.assertIn("--reasoning", runtime)
        self.assertIn("--reasoning-budget", runtime)
        self.assertIn('"enable_thinking":false', runtime)


class DatabaseSearchTest(unittest.TestCase):
    def test_fts_search_finds_tags_and_caption(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = ImageDatabase(Path(tmp) / "library.db")
            image_id = database.upsert_image(
                {
                    "original_path": str(Path(tmp) / "dog.jpg"),
                    "original_filename": "dog.jpg",
                    "new_filename": "black_dog_001.jpg",
                    "suggested_folder": "Animals/Dogs",
                    "file_hash": "abc",
                    "caption": "A black dog on a sofa.",
                    "tags": ["dog", "sofa", "pet"],
                    "objects": ["dog"],
                    "category": "Animals",
                    "subcategory": "Dogs",
                    "confidence": 0.9,
                    "needs_review": False,
                }
            )
            rows = database.search(query="dog sofa")
            self.assertEqual([row["id"] for row in rows], [image_id])

    def test_natural_language_building_search_ignores_filler_words(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = ImageDatabase(Path(tmp) / "library.db")
            image_id = database.upsert_image(
                {
                    "original_path": str(Path(tmp) / "city.jpg"),
                    "original_filename": "city.jpg",
                    "new_filename": "2026_07_03_buildings_city_skyline_001.jpg",
                    "suggested_folder": "Buildings/Cityscape",
                    "file_hash": "city",
                    "caption": "A dense urban skyline under an overcast sky.",
                    "tags": ["cityscape", "urban", "skyscrapers", "architecture", "city"],
                    "objects": ["skyscrapers"],
                    "category": "Buildings",
                    "subcategory": "Cityscape",
                    "confidence": 0.95,
                    "needs_review": False,
                }
            )

            rows = database.search(query="show me images of a building")
            self.assertEqual([row["id"] for row in rows], [image_id])

    def test_plural_towers_search_matches_singular_tower_tags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = ImageDatabase(Path(tmp) / "library.db")
            eiffel_id = database.upsert_image(
                {
                    "original_path": str(Path(tmp) / "eiffel.jpg"),
                    "original_filename": "eiffel.jpg",
                    "new_filename": "2026_07_03_buildings_eiffel_tower_paris_001.jpg",
                    "suggested_folder": "Buildings/Landmarks",
                    "file_hash": "eiffel",
                    "caption": "View of the Eiffel Tower in Paris.",
                    "tags": ["paris", "eiffel tower", "architecture", "landmark"],
                    "objects": ["tower"],
                    "category": "Buildings",
                    "subcategory": "Landmarks",
                    "confidence": 0.95,
                    "needs_review": False,
                }
            )

            singular_rows = database.search(query="tower")
            plural_rows = database.search(query="show me towers")

            self.assertEqual([row["id"] for row in singular_rows], [eiffel_id])
            self.assertEqual([row["id"] for row in plural_rows], [eiffel_id])

    def test_search_intent_returns_clean_keywords_instead_of_full_sentence(self) -> None:
        intent = normalize_search_intent(
            original_query="show me images of a building",
            keywords=["building"],
            category="",
            needs_review=False,
            allowed_categories=["Buildings", "Needs Review"],
        )

        self.assertEqual(intent["query"], "building")
        self.assertEqual(intent["category"], "Buildings")


class OfflineScanTest(unittest.TestCase):
    def test_scan_stops_when_ai_is_offline_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            output_dir = root / "output"
            input_dir.mkdir()
            output_dir.mkdir()
            Image.new("RGB", (80, 60), (120, 90, 50)).save(input_dir / "Paris_eiffel.jpg")

            organizer = ImageOrganizer(
                database=ImageDatabase(root / "library.db"),
                ai_client=OfflineAIClient(),
            )

            with self.assertRaisesRegex(RuntimeError, "filename-only guesses"):
                organizer.scan(
                    ScanOptions(
                        input_folder=input_dir,
                        output_folder=output_dir,
                        settings=AppSettings(),
                    )
                )

    def test_scan_marks_image_for_review_when_vision_call_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            output_dir = root / "output"
            input_dir.mkdir()
            output_dir.mkdir()
            Image.new("RGB", (80, 60), (120, 90, 50)).save(input_dir / "Paris_eiffel.jpg")

            organizer = ImageOrganizer(
                database=ImageDatabase(root / "library.db"),
                ai_client=FailingVisionAIClient(),
            )
            rows = organizer.scan(
                ScanOptions(
                    input_folder=input_dir,
                    output_folder=output_dir,
                    settings=AppSettings(),
                )
            )

            self.assertEqual(rows[0]["category"], "Needs Review")
            self.assertEqual(rows[0]["confidence"], 0.0)
            self.assertIn("vision request failed", rows[0]["last_error"])

    def test_scan_ignores_cached_filename_only_guess_when_ai_is_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            output_dir = root / "output"
            input_dir.mkdir()
            output_dir.mkdir()
            image_path = input_dir / "155337093-17976859.jpg"
            Image.new("RGB", (80, 60), (120, 90, 50)).save(image_path)

            database = ImageDatabase(root / "library.db")
            organizer = ImageOrganizer(database=database, ai_client=OfflineAIClient())
            organizer.scan(
                ScanOptions(
                    input_folder=input_dir,
                    output_folder=output_dir,
                    allow_filename_only_fallback=True,
                    settings=AppSettings(),
                )
            )

            organizer = ImageOrganizer(database=database, ai_client=SuccessfulVisionAIClient())
            rows = organizer.scan(
                ScanOptions(
                    input_folder=input_dir,
                    output_folder=output_dir,
                    settings=AppSettings(),
                )
            )

            self.assertEqual(rows[0]["caption"], "The Eiffel Tower in Paris, France.")
            self.assertIn("eiffel tower", rows[0]["tags"])
            self.assertEqual(rows[0]["suggested_folder"], "Buildings/Landmarks")
            self.assertEqual(rows[0]["confidence"], 0.93)

    def test_scan_uses_filename_structure_when_ai_is_offline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            output_dir = root / "output"
            input_dir.mkdir()
            output_dir.mkdir()
            Image.new("RGB", (80, 60), (120, 90, 50)).save(
                input_dir / "Tamworth_townhall_01.jpg"
            )

            organizer = ImageOrganizer(
                database=ImageDatabase(root / "library.db"),
                ai_client=OfflineAIClient(),
            )
            rows = organizer.scan(
                ScanOptions(
                    input_folder=input_dir,
                    output_folder=output_dir,
                    allow_filename_only_fallback=True,
                    settings=AppSettings(),
                )
            )

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["suggested_folder"], "townhall")
            self.assertFalse(rows[0].get("last_error"))

    def test_scan_skips_output_folder_nested_inside_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            input_dir = Path(tmp)
            output_dir = input_dir / "Sorted"
            (output_dir / "townhall").mkdir(parents=True)
            Image.new("RGB", (80, 60), (120, 90, 50)).save(input_dir / "Tamworth_townhall_01.jpg")
            Image.new("RGB", (80, 60), (120, 90, 50)).save(
                output_dir / "townhall" / "already_sorted_copy.jpg"
            )

            organizer = ImageOrganizer(
                database=ImageDatabase(input_dir / "library.db"),
                ai_client=OfflineAIClient(),
            )
            rows = organizer.scan(
                ScanOptions(
                    input_folder=input_dir,
                    output_folder=output_dir,
                    allow_filename_only_fallback=True,
                    settings=AppSettings(),
                )
            )

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["original_filename"], "Tamworth_townhall_01.jpg")

    def test_apply_copies_to_suggested_folder_and_keeps_original(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            output_dir = root / "output"
            input_dir.mkdir()
            output_dir.mkdir()
            source = input_dir / "Tamworth_townhall_01.jpg"
            Image.new("RGB", (80, 60), (120, 90, 50)).save(source)

            organizer = ImageOrganizer(
                database=ImageDatabase(root / "library.db"),
                ai_client=OfflineAIClient(),
            )
            rows = organizer.scan(
                ScanOptions(
                    input_folder=input_dir,
                    output_folder=output_dir,
                    allow_filename_only_fallback=True,
                    settings=AppSettings(),
                )
            )
            rows[0]["approved"] = True
            result = organizer.apply_changes(rows, output_folder=output_dir, mode="copy")

            destination = Path(result["applied"][0]["destination"])
            self.assertEqual(destination.parent.name, "townhall")
            self.assertTrue(destination.exists())
            self.assertTrue(source.exists())
            self.assertTrue(Path(result["undo_log"]).exists())


if __name__ == "__main__":
    unittest.main()
