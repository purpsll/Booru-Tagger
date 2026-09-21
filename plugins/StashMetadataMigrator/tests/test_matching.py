import pathlib
import sys
import unittest

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from migration_core import decide_entity_match, match_old_media
from StashMetadataMigrator import MigrationEngine


class CompactEntityMatchingTests(unittest.TestCase):
    def test_formatting_variants_are_identical_before_fuzzy_scoring(self):
        entities = [{
            "id": "t1",
            "name": "Big-Breasts",
            "aliases": [],
        }]
        decision = decide_entity_match(
            "big_breasts",
            entities,
            alias_field="aliases",
            allow_fuzzy=True,
            threshold=0.96,
            margin=0.02,
        )
        self.assertFalse(decision.ambiguous)
        self.assertIsNotNone(decision.match)
        self.assertEqual(decision.match.entity["id"], "t1")
        self.assertEqual(decision.match.score, 1.0)

    def test_fuzzy_scoring_ignores_punctuation_and_separators(self):
        entities = [{
            "id": "t1",
            "name": "looking.at-viewer",
            "aliases": [],
        }]
        decision = decide_entity_match(
            "looking_at_viewr",
            entities,
            alias_field="aliases",
            allow_fuzzy=True,
            threshold=0.90,
            margin=0.02,
        )
        self.assertFalse(decision.ambiguous)
        self.assertIsNotNone(decision.match)
        self.assertEqual(decision.match.entity["id"], "t1")
        self.assertGreater(decision.match.score, 0.90)

    def test_review_guard_uses_compact_names(self):
        engine = object.__new__(MigrationEngine)
        engine.tags = [{
            "id": "t1",
            "name": "big.breasts",
            "aliases": [],
        }]
        candidates = engine._plausible_existing_tag_candidates("big_brests")
        self.assertTrue(candidates)
        self.assertEqual(candidates[0][1]["id"], "t1")
        self.assertGreaterEqual(candidates[0][0], 0.90)


class MediaDiagnosticTests(unittest.TestCase):
    def test_unmatched_media_reports_old_path_and_missing_strong_hashes(self):
        path = r"D:\\Old\\missing.mp4"
        match = match_old_media(
            [path],
            {path: {"path": path, "fingerprints": {}}},
            {},
            {},
        )
        self.assertEqual(match.kind, "unmatched")
        self.assertIn(path, match.detail)
        self.assertIn("no strong exact content hash", match.detail)

    def test_sha256_matches_when_paths_changed(self):
        path = r"D:\\Old\\movie.mp4"
        current_path = "/new/movie.mp4"
        from migration_core import build_current_media_indexes
        fp_index, path_index = build_current_media_indexes([{
            "id": "scene-sha",
            "files": [{
                "path": current_path,
                "fingerprints": [{"type": "SHA-256", "value": "ABCDEF123456"}],
            }],
        }])
        match = match_old_media(
            [path],
            {path: {
                "path": path,
                "fingerprints": {"sha256": "abcdef123456"},
            }},
            fp_index,
            path_index,
        )
        self.assertEqual(match.object_id, "scene-sha")
        self.assertEqual(match.kind, "fingerprint")

    def test_phash_only_is_reported_but_not_used_as_identity(self):
        path = r"D:\\Old\\image.jpg"
        match = match_old_media(
            [path],
            {path: {
                "path": path,
                "fingerprints": {"phash": "ffff"},
            }},
            {},
            {},
        )
        self.assertIsNone(match.object_id)
        self.assertEqual(match.kind, "unmatched")
        self.assertIn("excluded identity types: phash", match.detail)

    def test_unmatched_media_reports_exported_strong_hashes(self):
        path = r"D:\\Old\\missing.mp4"
        match = match_old_media(
            [path],
            {path: {
                "path": path,
                "fingerprints": {"oshash": "abcdef1234567890"},
            }},
            {},
            {},
        )
        self.assertEqual(match.kind, "unmatched")
        self.assertIn("oshash:abcdef123456", match.detail)


if __name__ == "__main__":
    unittest.main()
