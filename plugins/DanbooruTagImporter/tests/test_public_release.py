import base64
import importlib.util
import json
import pathlib
import sys
import unittest
from unittest import mock

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

import stash_client
from matching import PHashIndex

spec = importlib.util.spec_from_file_location("plugin_public", PLUGIN_DIR / "DanbooruTagImporter.py")
plugin = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(plugin)


class DummyResponse:
    def __init__(self, body):
        self.body = body
    def read(self):
        return self.body
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc, tb):
        return False


class PublicReleaseTests(unittest.TestCase):
    def test_stash_session_cookie_object_is_serialized_correctly(self):
        headers = stash_client.stash_headers({
            "SessionCookie": {"Name": "session", "Value": "secret-cookie"}
        })
        self.assertEqual(headers.get("Cookie"), "session=secret-cookie")
        self.assertNotIn("{'Name'", headers.get("Cookie", ""))

    def test_custom_stash_session_cookie_name_is_preserved(self):
        headers = stash_client.stash_headers({
            "SessionCookie": {"Name": "stash_session", "Value": "abc123"}
        })
        self.assertEqual(headers.get("Cookie"), "stash_session=abc123")

    def test_danbooru_api_key_is_not_put_in_request_url(self):
        body = json.dumps([{"id": 1, "md5": "a" * 32}]).encode()
        captured = {}
        def fake_open(req, **kwargs):
            captured["url"] = req.full_url
            captured["auth"] = req.headers.get("Authorization")
            return DummyResponse(body)
        with mock.patch.object(plugin.HTTP, "urlopen", side_effect=fake_open):
            result = plugin.danbooru_post("a" * 32, "myuser", "supersecret")
        self.assertEqual(result["id"], 1)
        self.assertNotIn("supersecret", captured["url"])
        self.assertNotIn("myuser", captured["url"])
        expected = base64.b64encode(b"myuser:supersecret").decode("ascii")
        self.assertEqual(captured["auth"], f"Basic {expected}")

    def test_phash_refreshes_provider_metadata_instead_of_copying_local_metadata(self):
        source = {
            "id": "99",
            "tags": [{"id": "personal", "name": "My Private Tag"}],
            "performers": [{"id": "custom-performer", "name": "Custom"}],
            "studio": {"id": "custom-studio", "name": "Custom Studio"},
            "date": "1999-01-01",
            "urls": ["https://danbooru.donmai.us/posts/123"],
            "files": [{"fingerprints": [{"type": "phash", "value": "0000000000000001"}]}],
        }
        target = {
            "id": "100",
            "tags": [], "performers": [], "studio": None, "date": "", "urls": [],
            "files": [{"fingerprints": [{"type": "phash", "value": "0000000000000000"}]}],
        }
        index = PHashIndex()
        index.add("0000000000000001", source)
        fresh_post = {"id": 123, "tag_string_general": "fresh_tag", "tag_string_artist": ""}
        with mock.patch.object(plugin, "danbooru_post_by_id", return_value=fresh_post) as resolver:
            outcome = plugin._try_local_phash_reuse(
                target, index,
                danbooru_login="", danbooru_api_key="",
                gelbooru_api_key="", gelbooru_user_id="",
                rule34_api_key="", rule34_user_id="",
                e621_username="", e621_api_key="",
            )
        self.assertTrue(outcome.matched)
        source_name, post = outcome.value
        self.assertEqual(source_name, "danbooru")
        self.assertEqual(post["id"], 123)
        self.assertNotIn("tags", post)
        self.assertNotIn("performers", post)
        self.assertNotIn("studio", post)
        self.assertNotIn("date", post)
        resolver.assert_called_once_with("123", "", "")

    def test_phash_refuses_ambiguous_multiple_booru_source_urls(self):
        source = {
            "id": "99",
            "urls": [
                "https://danbooru.donmai.us/posts/123",
                "https://e621.net/posts/456",
            ],
            "files": [{"fingerprints": [{"type": "phash", "value": "0000000000000001"}]}],
        }
        target = {
            "id": "100",
            "urls": [],
            "files": [{"fingerprints": [{"type": "phash", "value": "0000000000000000"}]}],
        }
        index = PHashIndex()
        index.add("0000000000000001", source)
        with mock.patch.object(plugin, "_resolve_supported_booru_url") as resolver:
            outcome = plugin._try_local_phash_reuse(
                target, index,
                danbooru_login="", danbooru_api_key="",
                gelbooru_api_key="", gelbooru_user_id="",
                rule34_api_key="", rule34_user_id="",
                e621_username="", e621_api_key="",
            )
        self.assertEqual(outcome.status, plugin.LookupStatus.MISS)
        resolver.assert_not_called()

    def test_missing_rule34_credentials_do_not_block_fast_classification(self):
        class FakeStash:
            def __init__(self): self.updated = []
            def create_tag(self, name): return {"id": "marker", "name": name}
            def update_image_tags(self, image_id, tag_ids, **kwargs): self.updated.append((image_id, tag_ids, kwargs))
        image = {
            "id": "7", "tags": [], "performers": [], "studio": None, "date": "", "urls": [],
            "files": [{"fingerprints": [{"type": "md5", "value": "a" * 32}]}],
        }
        stash = FakeStash()
        with mock.patch.object(plugin, "danbooru_post", return_value=None), \
             mock.patch.object(plugin, "gelbooru_post", return_value=None), \
             mock.patch.object(plugin, "e621_post", return_value=None), \
             mock.patch.object(plugin, "ENABLE_LOCAL_PHASH_REUSE", False):
            result = plugin.process_image(
                stash, image, {}, {}, False, {}, {}, {}, {}, PHashIndex(), lookup_mode="fast"
            )
        self.assertEqual(result, "unresolved")
        self.assertEqual(len(stash.updated), 1)

    def test_missing_optional_deep_credentials_do_not_force_retry_later(self):
        class FakeStash:
            def __init__(self): self.updated = []
            def create_tag(self, name): return {"id": "marker", "name": name}
            def update_image_tags(self, image_id, tag_ids, **kwargs): self.updated.append((image_id, tag_ids, kwargs))
        image = {
            "id": "7", "tags": [], "performers": [], "studio": None, "date": "", "urls": [],
            "files": [{"fingerprints": [{"type": "md5", "value": "a" * 32}]}],
        }
        stash = FakeStash()
        with mock.patch.object(plugin, "danbooru_post", return_value=None), \
             mock.patch.object(plugin, "gelbooru_post", return_value=None), \
             mock.patch.object(plugin, "e621_post", return_value=None), \
             mock.patch.object(plugin, "ENABLE_LOCAL_PHASH_REUSE", False), \
             mock.patch.object(plugin, "ENABLE_E621_IQDB", False):
            result = plugin.process_image(
                stash, image, {}, {}, False, {}, {}, {}, {}, PHashIndex(), lookup_mode="deep"
            )
        self.assertEqual(result, "no_match")
        self.assertEqual(len(stash.updated), 1)


if __name__ == "__main__":
    unittest.main()
