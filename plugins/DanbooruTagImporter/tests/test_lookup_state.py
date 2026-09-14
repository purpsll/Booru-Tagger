import unittest

from lookup_state import LookupOutcome, LookupStatus, can_mark_no_match


class LookupStateTests(unittest.TestCase):
    def test_all_definitive_misses_can_mark_no_match(self):
        outcomes = [
            LookupOutcome("Danbooru", "md5", LookupStatus.MISS),
            LookupOutcome("Gelbooru", "md5", LookupStatus.MISS),
            LookupOutcome("e621", "iqdb", LookupStatus.MISS),
        ]
        self.assertTrue(can_mark_no_match(outcomes))

    def test_transient_error_prevents_no_match(self):
        outcomes = [
            LookupOutcome("Danbooru", "md5", LookupStatus.MISS),
            LookupOutcome("Gelbooru", "md5", LookupStatus.RETRYABLE_ERROR, detail="429"),
        ]
        self.assertFalse(can_mark_no_match(outcomes))

    def test_unavailable_provider_prevents_no_match(self):
        outcomes = [
            LookupOutcome("Danbooru", "md5", LookupStatus.MISS),
            LookupOutcome("Rule34", "md5", LookupStatus.UNAVAILABLE, detail="credentials"),
        ]
        self.assertFalse(can_mark_no_match(outcomes))

    def test_empty_outcome_set_is_not_authoritative(self):
        self.assertFalse(can_mark_no_match([]))


if __name__ == "__main__":
    unittest.main()
