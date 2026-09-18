import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

import stash_client
from constants import VISUAL_SEARCH_MAX_UPLOAD_BYTES
from stash_client import Stash


class VisualPayloadTests(unittest.TestCase):
    def test_small_thumbnail_is_not_reencoded(self):
        stash = Stash({"Host": "localhost", "Port": 9999})
        payload = b"small-image"
        with mock.patch.object(stash, "_ffmpeg_path") as ffmpeg_path:
            result = stash._bounded_visual_search_bytes(payload)
        self.assertEqual(result, payload)
        ffmpeg_path.assert_not_called()

    def test_oversized_thumbnail_is_reencoded_before_upload(self):
        stash = Stash({"Host": "localhost", "Port": 9999})
        payload = b"x" * (VISUAL_SEARCH_MAX_UPLOAD_BYTES + 1)
        resized = b"jpeg" * 100
        with mock.patch.object(stash, "_ffmpeg_path", return_value="/stash/ffmpeg"), \
             mock.patch.object(
                 stash_client.subprocess,
                 "run",
                 return_value=SimpleNamespace(
                     returncode=0,
                     stdout=resized,
                     stderr=b"",
                 ),
             ) as run:
            result = stash._bounded_visual_search_bytes(payload)
        self.assertEqual(result, resized)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][0], "/stash/ffmpeg")
        self.assertEqual(run.call_args.kwargs["input"], payload)

    def test_resize_failure_keeps_original_for_non_saucenao_providers(self):
        stash = Stash({"Host": "localhost", "Port": 9999})
        payload = b"x" * (VISUAL_SEARCH_MAX_UPLOAD_BYTES + 1)
        with mock.patch.object(stash, "_ffmpeg_path", return_value="/stash/ffmpeg"), \
             mock.patch.object(
                 stash_client.subprocess,
                 "run",
                 side_effect=OSError("ffmpeg unavailable"),
             ):
            result = stash._bounded_visual_search_bytes(payload)
        self.assertEqual(result, payload)


if __name__ == "__main__":
    unittest.main()
