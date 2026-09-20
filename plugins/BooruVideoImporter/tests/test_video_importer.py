import importlib.util
import pathlib
import sys
import unittest
from unittest import mock

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

spec = importlib.util.spec_from_file_location(
    "booru_video_importer", PLUGIN_DIR / "BooruVideoImporter.py"
)
plugin = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(plugin)

import video_match


class VideoMatchTests(unittest.TestCase):
    def test_hamming_distance(self):
        self.assertEqual(video_match.hamming_distance(0b1010, 0b1010), 0)
        self.assertEqual(video_match.hamming_distance(0b1010, 0b0011), 2)

    def test_high_confidence_requires_multiple_agreeing_frames(self):
        local = [0, 1, 2, 3, 4, 5, 6]
        remote = [0, 1, 2, 3, 4, 5, 63]
        with mock.patch.object(video_match, "probe_duration", return_value=100.0), \
             mock.patch.object(video_match, "frame_hashes", side_effect=[local, remote]):
            result = video_match.verify_video_candidate(
                "local.mp4", "remote.mp4", local_duration=100.0
            )

        self.assertTrue(result["high"])
        self.assertFalse(result["review"])
        self.assertGreaterEqual(result["matched_frames"], 5)

    def test_unrelated_video_does_not_match(self):
        local = [0x0000000000000000] * 7
        remote = [0xFFFFFFFFFFFFFFFF] * 7
        with mock.patch.object(video_match, "probe_duration", return_value=100.0), \
             mock.patch.object(video_match, "frame_hashes", side_effect=[local, remote]):
            result = video_match.verify_video_candidate(
                "local.mp4", "remote.mp4", local_duration=100.0
            )

        self.assertFalse(result["high"])
        self.assertFalse(result["review"])
        self.assertEqual(result["matched_frames"], 0)

    def test_e621_video_media_filter(self):
        post = {"file": {"ext": "webm", "url": "https://static.example/video.webm"}}
        self.assertEqual(
            plugin.media_url("e621", post),
            "https://static.example/video.webm",
        )
        image = {"file": {"ext": "jpg", "url": "https://static.example/image.jpg"}}
        self.assertIsNone(plugin.media_url("e621", image))

    def test_e621_metadata_maps_artist_character_tags_and_urls(self):
        post = {
            "id": 123,
            "created_at": "2026-09-01T12:34:56Z",
            "tags": {
                "artist": ["artist_one", "artist_two"],
                "character": ["character_one"],
                "general": ["tag_one"],
                "species": ["species_one"],
                "copyright": ["series_one"],
                "lore": [],
            },
            "sources": ["https://source.example/post"],
            "file": {"ext": "webm", "url": "https://static.example/video.webm"},
        }
        metadata = plugin.post_metadata("e621", post, {})

        self.assertEqual(metadata["artists"], ["artist_one", "artist_two"])
        self.assertEqual(metadata["characters"], ["character_one"])
        self.assertIn("tag_one", metadata["tags"])
        self.assertIn("species_one", metadata["tags"])
        self.assertIn("series_one", metadata["tags"])
        self.assertIn("artist_two", metadata["tags"])
        self.assertEqual(metadata["date"], "2026-09-01")
        self.assertEqual(metadata["urls"][0], "https://e621.net/posts/123")
        self.assertIn("https://source.example/post", metadata["urls"])


if __name__ == "__main__":
    unittest.main()
