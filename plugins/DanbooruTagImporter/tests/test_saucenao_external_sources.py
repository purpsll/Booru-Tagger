import importlib.util
import json
import pathlib
import sys
import unittest
from unittest import mock

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

spec = importlib.util.spec_from_file_location(
    "plugin_saucenao_external", PLUGIN_DIR / "DanbooruTagImporter.py"
)
plugin = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(plugin)


class FakeResponse:
    def __init__(self, body):
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self.body


def sauce_payload(similarity, data, index_name="Index #41: External - image.jpg"):
    return {
        "header": {
            "status": 0,
            "short_limit": 10,
            "short_remaining": 9,
            "long_limit": 200,
            "long_remaining": 199,
        },
        "results": [
            {
                "header": {
                    "similarity": str(similarity),
                    "index_id": 41,
                    "index_name": index_name,
                },
                "data": data,
            }
        ],
    }


class SauceNaoSourceRestrictionTests(unittest.TestCase):
    def setUp(self):
        plugin._SAUCENAO_DAILY_EXHAUSTED = False
        plugin._SAUCENAO_DISABLED_REASON = ""
        plugin._SAUCENAO_PAUSE_UNTIL = 0.0
        plugin._SAUCENAO_OUTAGE_STREAK = 0

    def _resolve(self, payload, diagnostics=None):
        with mock.patch.object(plugin, "_saucenao_wait_for_slot", return_value=0.0), \
             mock.patch.object(plugin.HTTP, "urlopen", return_value=FakeResponse(payload)):
            return plugin.saucenao_resolve(
                b"image",
                "key",
                93.0,
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                diagnostics=diagnostics,
            )

    def test_supported_source_detector_accepts_only_four_metadata_sites(self):
        supported = [
            {"ext_urls": ["https://danbooru.donmai.us/posts/123"]},
            {"ext_urls": ["https://gelbooru.com/index.php?page=post&s=view&id=123"]},
            {"ext_urls": ["https://rule34.xxx/index.php?page=post&s=view&id=123"]},
            {"ext_urls": ["https://e621.net/posts/123"]},
        ]
        for data in supported:
            with self.subTest(data=data):
                self.assertTrue(plugin._saucenao_data_has_supported_booru(data))

        for url in (
            "https://www.patreon.com/posts/example-123",
            "https://twitter.com/example/status/123",
            "https://konachan.com/post/show/123",
            "https://yande.re/post/show/123",
            "https://www.pixiv.net/artworks/123",
        ):
            with self.subTest(url=url):
                self.assertFalse(
                    plugin._saucenao_data_has_supported_booru({"ext_urls": [url]})
                )

    def test_high_confidence_patreon_hit_is_not_a_metadata_source(self):
        url = "https://www.patreon.com/posts/example-123"
        diagnostics = {}
        result = self._resolve(
            sauce_payload(
                97.2,
                {
                    "ext_urls": [url],
                    "creator": "example_creator",
                    "title": "Example title",
                },
                "Index #999: Patreon - image.jpg",
            ),
            diagnostics=diagnostics,
        )

        self.assertIsNone(result)
        self.assertEqual(diagnostics["best_external_similarity"], 97.2)
        self.assertEqual(diagnostics["best_external_url"], url)
        self.assertEqual(diagnostics["best_supported_similarity"], 0.0)
        self.assertEqual(diagnostics["best_supported_url"], "")

    def test_high_confidence_twitter_hit_is_not_a_metadata_source(self):
        url = "https://twitter.com/example/status/123456789"
        result = self._resolve(
            sauce_payload(
                96.0,
                {
                    "ext_urls": [url],
                    "tweet_id": "123456789",
                    "twitter_user_handle": "example_creator",
                },
                "Index #41: Twitter - image.jpg",
            )
        )
        self.assertIsNone(result)

    def test_konachan_hit_is_not_fetched_for_metadata(self):
        payload = sauce_payload(
            96.0,
            {
                "ext_urls": ["https://konachan.com/post/show/82192"],
                "konachan_id": 82192,
                "creator": "gainax",
            },
            "Index #26: Konachan - image.jpg",
        )
        with mock.patch.object(plugin, "_saucenao_wait_for_slot", return_value=0.0), \
             mock.patch.object(plugin.HTTP, "urlopen", return_value=FakeResponse(payload)), \
             mock.patch.object(plugin, "moebooru_post_by_id") as fetch_moebooru:
            result = plugin.saucenao_resolve(
                b"image", "key", 93.0, "", "", "", "", "", "", "", ""
            )

        self.assertIsNone(result)
        fetch_moebooru.assert_not_called()

    def test_supported_danbooru_url_still_resolves_authoritative_metadata(self):
        payload = sauce_payload(
            95.0,
            {"ext_urls": ["https://danbooru.donmai.us/posts/6906968"]},
            "Index #9: Danbooru - image.jpg",
        )
        post = {
            "id": 6906968,
            "tag_string_general": "blue_hair",
            "tag_string_artist": "sample_artist",
            "tag_string_character": "sample_character",
        }
        with mock.patch.object(plugin, "_saucenao_wait_for_slot", return_value=0.0), \
             mock.patch.object(plugin.HTTP, "urlopen", return_value=FakeResponse(payload)), \
             mock.patch.object(plugin, "danbooru_post_by_id", return_value=post) as resolver:
            result = plugin.saucenao_resolve(
                b"image", "key", 93.0, "", "", "", "", "", "", "", ""
            )

        self.assertIsNotNone(result)
        source, resolved = result
        self.assertEqual(source, "danbooru")
        self.assertEqual(resolved["id"], 6906968)
        self.assertEqual(resolved["_saucenao_score"], 95.0)
        resolver.assert_called_once_with("6906968", "", "")

    def test_external_review_band_hit_remains_inconclusive_without_appending_source(self):
        self.assertTrue(
            plugin._saucenao_strong_unsupported_is_inconclusive(
                90.0,
                0.0,
                85.0,
            )
        )
        outcome = plugin.LookupOutcome(
            "SauceNAO",
            "visual",
            plugin.LookupStatus.UNAVAILABLE,
            None,
            "strong visual match from unsupported source (90.0%)",
        )
        self.assertFalse(plugin.can_mark_no_match([outcome]))


if __name__ == "__main__":
    unittest.main()
