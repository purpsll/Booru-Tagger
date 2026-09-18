import importlib.util
import io
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


class SauceNaoExternalSourceTests(unittest.TestCase):
    def setUp(self):
        plugin._SAUCENAO_DAILY_EXHAUSTED = False
        plugin._SAUCENAO_DISABLED_REASON = ""
        plugin._SAUCENAO_PAUSE_UNTIL = 0.0
        plugin._SAUCENAO_OUTAGE_STREAK = 0

    def test_external_post_identifies_site_and_metadata(self):
        header = {
            "index_id": 41,
            "index_name": "Index #41: Twitter - image.jpg",
        }
        data = {
            "ext_urls": ["https://twitter.com/i/web/status/879295443850506242"],
            "created_at": "2017-06-26T11:09:04Z",
            "tweet_id": "879295443850506242",
            "twitter_user_handle": "petty_lily_xxx",
        }

        post = plugin._saucenao_external_post(header, data, 95.0)

        self.assertEqual(post["_source_site"], "Twitter")
        self.assertEqual(
            post["_source_url"],
            "https://twitter.com/i/web/status/879295443850506242",
        )
        self.assertEqual(post["_source_artists"], ["petty_lily_xxx"])
        self.assertEqual(post["_source_date"], "2017-06-26T11:09:04Z")
        self.assertEqual(post["_saucenao_score"], 95.0)

    def test_high_confidence_external_match_is_match_not_no_match(self):
        sauce_payload = {
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
                        "similarity": "95.0",
                        "index_id": 41,
                        "index_name": "Index #41: Twitter - image.jpg",
                    },
                    "data": {
                        "ext_urls": [
                            "https://twitter.com/i/web/status/879295443850506242"
                        ],
                        "created_at": "2017-06-26T11:09:04Z",
                        "tweet_id": "879295443850506242",
                        "twitter_user_handle": "petty_lily_xxx",
                    },
                }
            ],
        }

        with mock.patch.object(plugin, "_saucenao_wait_for_slot", return_value=0.0), \
             mock.patch.object(plugin.HTTP, "urlopen", return_value=FakeResponse(sauce_payload)):
            result = plugin.saucenao_resolve(
                b"image",
                "key",
                94.0,
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
            )

        self.assertIsNotNone(result)
        source, post = result
        self.assertEqual(source, "saucenao_external")
        self.assertEqual(post["_source_site"], "Twitter")
        outcome = plugin.LookupOutcome(
            "SauceNAO", "visual", plugin.LookupStatus.MATCH, result
        )
        self.assertFalse(plugin.can_mark_no_match([outcome]))

    def test_konachan_match_fetches_authoritative_tags(self):
        sauce_payload = {
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
                        "similarity": "96.0",
                        "index_id": 26,
                        "index_name": "Index #26: Konachan - image.jpg",
                    },
                    "data": {
                        "ext_urls": ["https://konachan.com/post/show/82192"],
                        "konachan_id": 82192,
                        "creator": "gainax, nanao",
                        "characters": "yoko littner",
                    },
                }
            ],
        }
        konachan_payload = [
            {
                "id": 82192,
                "tags": "gainax nanao yoko_littner long_hair",
                "created_at": 1234567890,
            }
        ]

        with mock.patch.object(plugin, "_saucenao_wait_for_slot", return_value=0.0), \
             mock.patch.object(
                 plugin.HTTP,
                 "urlopen",
                 side_effect=[
                     FakeResponse(sauce_payload),
                     FakeResponse(konachan_payload),
                 ],
             ):
            result = plugin.saucenao_resolve(
                b"image",
                "key",
                94.0,
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
            )

        self.assertIsNotNone(result)
        source, post = result
        self.assertEqual(source, "konachan")
        self.assertEqual(
            plugin.tag_names(post, source, include_meta=False),
            ["gainax", "nanao", "yoko_littner", "long_hair"],
        )
        self.assertEqual(post["_source_artists"], ["gainax", "nanao"])
        self.assertEqual(post["_source_characters"], ["yoko littner"])


if __name__ == "__main__":
    unittest.main()
