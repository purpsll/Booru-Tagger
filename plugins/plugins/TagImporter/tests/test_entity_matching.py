import unittest

from entity_matching import find_entity_match, normalized_entity_name


class EntityMatchingTests(unittest.TestCase):
    def test_normalized_variant_matches_without_fuzzy(self):
        cache = {"artist name": {"id": "1", "name": "Artist_Name", "aliases": []}}
        match = find_entity_match(
            "artist-name", cache, "aliases",
            allow_normalized=True, allow_fuzzy=False, threshold=0.99, margin=0.05,
        )
        self.assertIsNotNone(match)
        self.assertTrue(match.normalized_exact)
        self.assertEqual(match.entity["id"], "1")

    def test_ambiguous_normalized_match_is_rejected(self):
        cache = {
            "a": {"id": "1", "name": "Artist_Name", "aliases": []},
            "b": {"id": "2", "name": "Artist-Name", "aliases": []},
        }
        match = find_entity_match(
            "artist name", cache, "aliases",
            allow_normalized=True, allow_fuzzy=False, threshold=0.95, margin=0.02,
        )
        self.assertIsNone(match)

    def test_fuzzy_ambiguous_candidates_are_rejected(self):
        cache = {
            "one": {"id": "1", "name": "Alexander Stone", "aliases": []},
            "two": {"id": "2", "name": "Alexander Stowe", "aliases": []},
        }
        match = find_entity_match(
            "Alexander Stove", cache, "aliases",
            allow_normalized=False, allow_fuzzy=True, threshold=0.85, margin=0.08,
        )
        self.assertIsNone(match)

    def test_clear_fuzzy_winner_is_accepted(self):
        cache = {
            "one": {"id": "1", "name": "Alexandra Stone", "aliases": []},
            "two": {"id": "2", "name": "Bernard Smith", "aliases": []},
        }
        match = find_entity_match(
            "Alexandra Ston", cache, "aliases",
            allow_normalized=False, allow_fuzzy=True, threshold=0.90, margin=0.05,
        )
        self.assertIsNotNone(match)
        self.assertEqual(match.entity["id"], "1")
        self.assertFalse(match.normalized_exact)

    def test_normalizer(self):
        self.assertEqual(normalized_entity_name("  A_B-C  "), "a b c")


if __name__ == "__main__":
    unittest.main()
