import importlib.util
import pathlib
import sys
import threading
import unittest
import urllib.error
from unittest import mock

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

spec = importlib.util.spec_from_file_location(
    "plugin_perf", PLUGIN_DIR / "DanbooruTagImporter.py"
)
plugin = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(plugin)

from stash_client import Stash


class FakeResponse:
    def __init__(self, body=b"thumb"):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self.body


class FakeStash:
    def image_bytes(self, image_id):
        return b"small-thumbnail"

    def update_image_tags(self, *args, **kwargs):
        pass

    def create_tag(self, name):
        return {"id": "marker", "name": name}


def unresolved_image():
    return {
        "id": "11",
        "tags": [{"id": "u", "name": plugin.UNRESOLVED_MARKER_TAG}],
        "performers": [],
        "studio": None,
        "date": "",
        "urls": [],
        "files": [
            {
                "fingerprints": [
                    {"type": "md5", "value": "a" * 32},
                    {"type": "phash", "value": "0123456789abcdef"},
                ]
            }
        ],
    }


class DeepPerformanceTests(unittest.TestCase):
    def test_unresolved_deep_skips_completed_fast_stages(self):
        stash = FakeStash()
        with mock.patch.object(plugin, "ENABLE_DANBOORU_IQDB", False),              mock.patch.object(plugin, "ENABLE_E621_IQDB", False),              mock.patch.object(plugin, "ENABLE_SAUCENAO", False),              mock.patch.object(plugin, "danbooru_post") as danbooru,              mock.patch.object(plugin, "gelbooru_post") as gelbooru,              mock.patch.object(plugin, "rule34_post") as rule34,              mock.patch.object(plugin, "e621_post") as e621,              mock.patch.object(plugin, "_try_local_phash_reuse") as phash:
            result = plugin.process_image(
                stash,
                unresolved_image(),
                {},
                {},
                True,
                {},
                {},
                {},
                {},
                plugin.PHashIndex(),
                lookup_mode="deep",
            )
        self.assertIn(result, {"unresolved", "no_md5"})
        danbooru.assert_not_called()
        gelbooru.assert_not_called()
        rule34.assert_not_called()
        e621.assert_not_called()
        phash.assert_not_called()

    def test_e621_and_saucenao_run_concurrently_after_danbooru_miss(self):
        stash = FakeStash()
        barrier = threading.Barrier(2, timeout=2.0)

        def miss_together(*args, **kwargs):
            barrier.wait()
            return None

        settings = {"saucenao_api_key": "sauce"}
        with mock.patch.object(plugin, "ENABLE_DANBOORU_IQDB", False),              mock.patch.object(plugin, "e621_iqdb", side_effect=miss_together),              mock.patch.object(plugin, "saucenao_resolve", side_effect=miss_together):
            result = plugin.process_image(
                stash,
                unresolved_image(),
                settings,
                {},
                True,
                {},
                {},
                {},
                {},
                plugin.PHashIndex(),
                lookup_mode="deep",
            )
        self.assertEqual(result, "no_match")

    def test_visual_timeout_is_retryable_and_not_no_match(self):
        stash = FakeStash()
        settings = {"saucenao_api_key": "sauce"}

        def timeout(*args, **kwargs):
            raise urllib.error.URLError(TimeoutError("The write operation timed out"))

        with mock.patch.object(plugin, "ENABLE_DANBOORU_IQDB", False),              mock.patch.object(plugin, "e621_iqdb", side_effect=timeout),              mock.patch.object(plugin, "saucenao_resolve", side_effect=timeout):
            result = plugin.process_image(
                stash,
                unresolved_image(),
                settings,
                {},
                True,
                {},
                {},
                {},
                {},
                plugin.PHashIndex(),
                lookup_mode="deep",
            )
        self.assertEqual(result, "retry_later")

    def test_stash_image_bytes_uses_thumbnail_endpoint(self):
        stash = Stash({"Scheme": "http", "Host": "127.0.0.1", "Port": 9999})
        with mock.patch(
            "stash_client.urllib.request.urlopen",
            return_value=FakeResponse(b"tiny"),
        ) as opener:
            self.assertEqual(stash.image_bytes("42"), b"tiny")
        request = opener.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/image/42/thumbnail"))

    def test_cloudflare_521_is_retryable(self):
        exc = urllib.error.HTTPError(
            "https://saucenao.com", 521, "down", {}, None
        )
        self.assertTrue(plugin.is_retryable_exception(exc))


if __name__ == "__main__":
    unittest.main()
