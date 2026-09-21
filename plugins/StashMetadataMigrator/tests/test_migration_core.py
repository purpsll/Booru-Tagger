import json
import pathlib
import sys
import tempfile
import unittest
import zipfile

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from migration_core import (
    build_current_media_indexes,
    find_entity_match,
    match_old_media,
    normalize_name,
    open_export_source,
)


class MigrationCoreTests(unittest.TestCase):
    def test_changed_path_matches_by_md5(self):
        current = [{
            "id": "10",
            "files": [{
                "path": "/new/library/a.jpg",
                "fingerprints": [{"type": "md5", "value": "ABC123"}],
            }],
        }]
        fp_index, path_index = build_current_media_indexes(current)
        old_files = {
            r"D:\old\a.jpg": {
                "path": r"D:\old\a.jpg",
                "fingerprints": {"md5": "abc123"},
            }
        }
        match = match_old_media(
            [r"D:\old\a.jpg"], old_files, fp_index, path_index
        )
        self.assertEqual(match.object_id, "10")
        self.assertEqual(match.kind, "fingerprint")

    def test_oshash_matches_video_when_path_changed(self):
        current = [{
            "id": "20",
            "files": [{
                "path": "/media/movie.mp4",
                "fingerprints": [{"type": "oshash", "value": "001122"}],
            }],
        }]
        fp_index, path_index = build_current_media_indexes(current)
        old_files = {
            "/old/movie.mp4": {
                "path": "/old/movie.mp4",
                "fingerprints": {"oshash": "001122"},
            }
        }
        match = match_old_media(
            ["/old/movie.mp4"], old_files, fp_index, path_index
        )
        self.assertEqual(match.object_id, "20")

    def test_strict_phash_plus_exact_size_matches_as_last_resort(self):
        current = [{
            "id": "30",
            "files": [{
                "path": "/new/a.jpg",
                "size": 12345,
                "fingerprints": [{"type": "phash", "value": "ffff"}],
            }],
        }]
        fp_index, path_index = build_current_media_indexes(current)
        old_files = {
            "/old/a.jpg": {
                "path": "/old/a.jpg",
                "size": 12345,
                "fingerprints": {"phash": "ffff"},
            }
        }
        match = match_old_media(
            ["/old/a.jpg"], old_files, fp_index, path_index
        )
        self.assertEqual(match.object_id, "30")
        self.assertEqual(match.kind, "strict-phash")

    def test_strict_phash_requires_exact_file_size(self):
        current = [{
            "id": "31",
            "files": [{
                "path": "/new/a.jpg",
                "size": 12346,
                "fingerprints": [{"type": "phash", "value": "ffff"}],
            }],
        }]
        fp_index, path_index = build_current_media_indexes(current)
        old_files = {
            "/old/a.jpg": {
                "path": "/old/a.jpg",
                "size": 12345,
                "fingerprints": {"phash": "ffff"},
            }
        }
        match = match_old_media(
            ["/old/a.jpg"], old_files, fp_index, path_index
        )
        self.assertIsNone(match.object_id)
        self.assertEqual(match.kind, "unmatched")

    def test_strict_phash_does_not_override_old_strong_hash(self):
        current = [{
            "id": "32",
            "files": [{
                "path": "/new/a.jpg",
                "size": 12345,
                "fingerprints": [
                    {"type": "phash", "value": "ffff"},
                    {"type": "md5", "value": "current-md5"},
                ],
            }],
        }]
        fp_index, path_index = build_current_media_indexes(current)
        old_files = {
            "/old/a.jpg": {
                "path": "/old/a.jpg",
                "size": 12345,
                "fingerprints": {
                    "phash": "ffff",
                    "md5": "different-old-md5",
                },
            }
        }
        match = match_old_media(
            ["/old/a.jpg"], old_files, fp_index, path_index
        )
        self.assertIsNone(match.object_id)
        self.assertEqual(match.kind, "unmatched")

    def test_strict_phash_duplicate_candidates_are_ambiguous(self):
        current = [
            {
                "id": "33",
                "files": [{
                    "path": "/new/a.jpg",
                    "size": 12345,
                    "fingerprints": [{"type": "phash", "value": "ffff"}],
                }],
            },
            {
                "id": "34",
                "files": [{
                    "path": "/new/b.jpg",
                    "size": 12345,
                    "fingerprints": [{"type": "phash", "value": "ffff"}],
                }],
            },
        ]
        fp_index, path_index = build_current_media_indexes(current)
        old_files = {
            "/old/a.jpg": {
                "path": "/old/a.jpg",
                "size": 12345,
                "fingerprints": {"phash": "ffff"},
            }
        }
        match = match_old_media(
            ["/old/a.jpg"], old_files, fp_index, path_index
        )
        self.assertIsNone(match.object_id)
        self.assertEqual(match.kind, "ambiguous")

    def test_phash_is_never_used_as_identity(self):
        current = [{
            "id": "30",
            "files": [{
                "path": "/new/a.jpg",
                "fingerprints": [{"type": "phash", "value": "ffff"}],
            }],
        }]
        fp_index, path_index = build_current_media_indexes(current)
        old_files = {
            "/old/a.jpg": {
                "path": "/old/a.jpg",
                "fingerprints": {"phash": "ffff"},
            }
        }
        match = match_old_media(
            ["/old/a.jpg"], old_files, fp_index, path_index
        )
        self.assertIsNone(match.object_id)
        self.assertEqual(match.kind, "unmatched")

    def test_ambiguous_fingerprint_is_skipped(self):
        current = [
            {"id": "1", "files": [{"path": "/a", "fingerprints": [{"type": "md5", "value": "same"}]}]},
            {"id": "2", "files": [{"path": "/b", "fingerprints": [{"type": "md5", "value": "same"}]}]},
        ]
        fp_index, path_index = build_current_media_indexes(current)
        old_files = {"/old": {"path": "/old", "fingerprints": {"md5": "same"}}}
        match = match_old_media(["/old"], old_files, fp_index, path_index)
        self.assertIsNone(match.object_id)
        self.assertEqual(match.kind, "ambiguous")

    def test_exact_path_is_safe_fallback(self):
        current = [{"id": "7", "files": [{"path": "/same/path.jpg", "fingerprints": []}]}]
        fp_index, path_index = build_current_media_indexes(current)
        match = match_old_media(
            ["/same/path.jpg"],
            {"/same/path.jpg": {"path": "/same/path.jpg", "fingerprints": {}}},
            fp_index,
            path_index,
        )
        self.assertEqual(match.object_id, "7")
        self.assertEqual(match.kind, "exact-path")

    def test_tag_formatting_variant_resolves_without_fuzzy(self):
        entities = [{"id": "1", "name": "big_breasts", "aliases": []}]
        match = find_entity_match(
            "Big Breasts",
            entities,
            alias_field="aliases",
            allow_fuzzy=False,
            threshold=1.0,
            margin=1.0,
        )
        self.assertEqual(match.entity["id"], "1")
        self.assertIn(match.kind, {"normalized", "formatting"})

    def test_performer_high_similarity_can_reuse(self):
        entities = [{"id": "1", "name": "Alexandra Stone", "alias_list": []}]
        match = find_entity_match(
            "Alexandra Ston",
            entities,
            alias_field="alias_list",
            allow_fuzzy=True,
            threshold=0.95,
            margin=0.03,
        )
        self.assertIsNotNone(match)
        self.assertEqual(match.kind, "fuzzy")

    def test_fuzzy_winner_margin_prevents_guess(self):
        entities = [
            {"id": "1", "name": "Alexandra Stone", "alias_list": []},
            {"id": "2", "name": "Alexandra Stowe", "alias_list": []},
        ]
        match = find_entity_match(
            "Alexandra Stoe",
            entities,
            alias_field="alias_list",
            allow_fuzzy=True,
            threshold=0.90,
            margin=0.10,
        )
        self.assertIsNone(match)

    def test_normalization_does_not_change_semantics(self):
        self.assertNotEqual(normalize_name("anal"), normalize_name("anal sex"))

    def test_zip_path_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = pathlib.Path(tmp) / "bad.zip"
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr("../escape.txt", "bad")
                archive.writestr("files/a.json", "{}")
                archive.writestr("images/a.json", "{}")
            with self.assertRaises(ValueError):
                with open_export_source(str(zip_path)):
                    pass

    def test_zip_with_nested_export_root_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = pathlib.Path(tmp) / "ok.zip"
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr("export/files/a.json", json.dumps({
                    "type": "image", "path": "/old/a.jpg", "size": 1,
                    "fingerprints": [{"type": "md5", "fingerprint": "a"}],
                }))
                archive.writestr("export/images/a.json", json.dumps({
                    "title": "A", "files": ["/old/a.jpg"],
                }))
            with open_export_source(str(zip_path)) as root:
                self.assertTrue((root / "files").is_dir())
                self.assertTrue((root / "images").is_dir())


if __name__ == "__main__":
    unittest.main()
