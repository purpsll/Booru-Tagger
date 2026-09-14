import unittest

from matching import PHashIndex, phash_hamming_distance


def image(image_id):
    return {"id": str(image_id), "tags": []}


class PHashIndexTests(unittest.TestCase):
    def test_full_hash_search_finds_distance_above_four(self):
        index = PHashIndex()
        index.add("000000000000001f", image(1))  # five low bits differ from zero
        result = index.unique_nearest("0000000000000000", 5)
        self.assertIsNotNone(result)
        self.assertEqual(result[0], 5)
        self.assertEqual(result[1]["id"], "1")

    def test_tied_nearest_is_rejected(self):
        index = PHashIndex()
        index.add("0000000000000001", image(1))
        index.add("0000000000000002", image(2))
        self.assertIsNone(index.unique_nearest("0000000000000000", 3))

    def test_winner_margin_is_enforced(self):
        index = PHashIndex()
        index.add("0000000000000001", image(1))  # distance 1
        index.add("0000000000000007", image(2))  # distance 3
        self.assertIsNone(index.unique_nearest("0000000000000000", 4, min_margin=3))
        result = index.unique_nearest("0000000000000000", 4, min_margin=2)
        self.assertIsNotNone(result)
        self.assertEqual(result[1]["id"], "1")

    def test_excluded_image_is_ignored(self):
        index = PHashIndex()
        index.add("0000000000000000", image(1))
        index.add("0000000000000001", image(2))
        result = index.unique_nearest("0000000000000000", 2, exclude_image_id="1")
        self.assertIsNotNone(result)
        self.assertEqual(result[1]["id"], "2")

    def test_hamming_distance(self):
        self.assertEqual(phash_hamming_distance("0", "f"), 4)
        self.assertIsNone(phash_hamming_distance("not-a-hash", "f"))


if __name__ == "__main__":
    unittest.main()
