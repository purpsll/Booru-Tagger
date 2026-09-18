import importlib.util
import pathlib
import sys
import unittest
import urllib.error
from unittest import mock

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

spec = importlib.util.spec_from_file_location("plugin_main", PLUGIN_DIR / "DanbooruTagImporter.py")
plugin = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(plugin)


class FakeStash:
    def __init__(self):
        self.create_calls = 0
    def create_tag(self, name):
        self.create_calls += 1
        raise RuntimeError("UNIQUE constraint failed: tags.name")
    def all_tags(self):
        return {
            plugin.NO_MATCH_MARKER_TAG.casefold(): {
                "id": "99", "name": plugin.NO_MATCH_MARKER_TAG, "aliases": []
            }
        }




class ProcessFakeStash:
    def __init__(self):
        self.updated = []
        self._next_tag_id = 100
    def create_tag(self, name):
        self._next_tag_id += 1
        return {"id": str(self._next_tag_id), "name": name}
    def all_tags(self):
        return {}
    def update_image_tags(self, image_id, tag_ids, **kwargs):
        self.updated.append((str(image_id), list(tag_ids), kwargs))
    def image_bytes(self, image_id):
        return b"fake-image-bytes"


def md5_image():
    return {
        "id": "7",
        "tags": [],
        "performers": [],
        "studio": None,
        "date": "",
        "urls": [],
        "files": [{"fingerprints": [{"type": "md5", "value": "a" * 32}]}],
    }


class PluginSafetyTests(unittest.TestCase):
    def test_attempt_lookup_marks_wrapped_429_retryable(self):
        def failing():
            try:
                raise urllib.error.HTTPError("https://x", 429, "rate", {}, None)
            except urllib.error.HTTPError as exc:
                raise RuntimeError("provider failed") from exc
        outcome = plugin._attempt_lookup("test", "md5", failing)
        self.assertEqual(outcome.status, plugin.LookupStatus.RETRYABLE_ERROR)

    def test_empty_json_body_is_provider_failure(self):
        with self.assertRaises(RuntimeError):
            plugin._safe_json_response(b"", "Example")

    def test_rule34_empty_json_body_can_be_authoritative_miss(self):
        self.assertIsNone(
            plugin._safe_json_response(b"", "Rule34", empty_is_miss=True)
        )

    def test_rule34_md5_empty_http_200_is_miss(self):
        class EmptyResponse:
            def __enter__(self):
                return self
            def __exit__(self, exc_type, exc, tb):
                return False
            def read(self):
                return b""

        plugin._SOURCE_DISABLED.clear()
        with mock.patch.object(plugin.HTTP, "urlopen", return_value=EmptyResponse()):
            result = plugin.rule34_post("0" * 32, "key", "123")
        self.assertIsNone(result)

    def test_gelbooru_artist_is_mapped_to_studio_in_process_flow(self):
        stash = ProcessFakeStash()
        settings = {"gelbooru_api_key": "gkey", "gelbooru_user_id": "42"}
        post = {
            "id": 12345,
            "tags": "1girl sample_artist sample_character blue_hair",
        }
        typed = {
            "1girl": 0,
            "sample_artist": 1,
            "sample_character": 4,
            "blue_hair": 0,
        }
        logs = []
        with mock.patch.object(plugin, "ENABLE_DANBOORU", False), \
             mock.patch.object(plugin, "ENABLE_RULE34", False), \
             mock.patch.object(plugin, "ENABLE_E621", False), \
             mock.patch.object(plugin, "ENABLE_LOCAL_PHASH_REUSE", False), \
             mock.patch.object(plugin, "gelbooru_post", return_value=post), \
             mock.patch.object(plugin, "gelbooru_style_tag_metadata", return_value=typed), \
             mock.patch.object(plugin, "ensure_studio") as ensure_studio, \
             mock.patch.object(plugin, "ensure_performer"), \
             mock.patch.object(plugin, "log", side_effect=lambda level, message: logs.append((level, message))):
            result = plugin.process_image(
                stash, md5_image(), settings, {}, True, {}, {}, {}, {},
                plugin.PHashIndex(), lookup_mode="fast",
            )
        self.assertEqual(result, "matched_gelbooru")
        self.assertTrue(any(call.args[2] == "sample_artist" for call in ensure_studio.call_args_list))
        # The artist and character are mapped to Studio/Performer, not duplicated
        # as ordinary Stash tags under the fixed public policy.
        self.assertTrue(any("2 tags" in message for _level, message in logs))
        self.assertTrue(any("Studio 'sample_artist' would be assigned" in message for _level, message in logs))

    def test_secondary_artists_become_tags_while_primary_becomes_studio(self):
        stash = ProcessFakeStash()
        settings = {"gelbooru_api_key": "gkey", "gelbooru_user_id": "42"}
        post = {
            "id": 12345,
            "tags": "conditional_dnp primary_artist secondary_artist third_artist blue_hair",
        }
        typed = {
            "conditional_dnp": 1,
            "primary_artist": 1,
            "secondary_artist": 1,
            "third_artist": 1,
            "blue_hair": 0,
        }
        with mock.patch.object(plugin, "ENABLE_DANBOORU", False), \
             mock.patch.object(plugin, "ENABLE_RULE34", False), \
             mock.patch.object(plugin, "ENABLE_E621", False), \
             mock.patch.object(plugin, "ENABLE_LOCAL_PHASH_REUSE", False), \
             mock.patch.object(plugin, "gelbooru_post", return_value=post), \
             mock.patch.object(plugin, "gelbooru_style_tag_metadata", return_value=typed), \
             mock.patch.object(plugin, "ensure_studio", return_value={"id": "studio-1", "name": "primary_artist"}) as ensure_studio, \
             mock.patch.object(plugin, "ensure_tags", return_value=[]) as ensure_tags:
            result = plugin.process_image(
                stash, md5_image(), settings, {}, False, {}, {}, {}, {},
                plugin.PHashIndex(), lookup_mode="fast",
            )

        self.assertEqual(result, "updated_gelbooru")
        self.assertEqual([call.args[2] for call in ensure_studio.call_args_list], ["primary_artist"])
        imported_names = list(ensure_tags.call_args.args[2])
        self.assertIn("secondary_artist", imported_names)
        self.assertIn("third_artist", imported_names)
        self.assertIn("blue_hair", imported_names)
        self.assertNotIn("primary_artist", imported_names)
        self.assertNotIn("conditional_dnp", imported_names)

    def test_third_party_edit_artist_is_ignored_on_flat_source(self):
        stash = ProcessFakeStash()
        settings = {"gelbooru_api_key": "gkey", "gelbooru_user_id": "42"}
        post = {
            "id": 12346,
            "tags": "third-party_edit primary_artist secondary_artist blue_hair",
        }
        typed = {
            "third-party_edit": 1,
            "primary_artist": 1,
            "secondary_artist": 1,
            "blue_hair": 0,
        }
        with mock.patch.object(plugin, "ENABLE_DANBOORU", False), \
             mock.patch.object(plugin, "ENABLE_RULE34", False), \
             mock.patch.object(plugin, "ENABLE_E621", False), \
             mock.patch.object(plugin, "ENABLE_LOCAL_PHASH_REUSE", False), \
             mock.patch.object(plugin, "gelbooru_post", return_value=post), \
             mock.patch.object(plugin, "gelbooru_style_tag_metadata", return_value=typed), \
             mock.patch.object(plugin, "ensure_studio", return_value={"id": "studio-1", "name": "primary_artist"}) as ensure_studio, \
             mock.patch.object(plugin, "ensure_tags", return_value=[]) as ensure_tags:
            result = plugin.process_image(
                stash, md5_image(), settings, {}, False, {}, {}, {}, {},
                plugin.PHashIndex(), lookup_mode="fast",
            )

        self.assertEqual(result, "updated_gelbooru")
        self.assertEqual([call.args[2] for call in ensure_studio.call_args_list], ["primary_artist"])
        imported_names = list(ensure_tags.call_args.args[2])
        self.assertIn("secondary_artist", imported_names)
        self.assertIn("blue_hair", imported_names)
        self.assertNotIn("third-party_edit", imported_names)
        self.assertNotIn("primary_artist", imported_names)

    def test_third_party_edit_artist_is_ignored_on_structured_source(self):
        stash = ProcessFakeStash()
        post = {
            "id": 22346,
            "md5": "a" * 32,
            "tag_string_artist": "third-party_edit primary_artist secondary_artist",
            "tag_string_character": "",
            "tag_string_copyright": "",
            "tag_string_general": "blue_hair",
        }
        with mock.patch.object(plugin, "ENABLE_GELBOORU", False), \
             mock.patch.object(plugin, "ENABLE_RULE34", False), \
             mock.patch.object(plugin, "ENABLE_E621", False), \
             mock.patch.object(plugin, "ENABLE_LOCAL_PHASH_REUSE", False), \
             mock.patch.object(plugin, "danbooru_post", return_value=post), \
             mock.patch.object(plugin, "ensure_studio", return_value={"id": "studio-1", "name": "primary_artist"}) as ensure_studio, \
             mock.patch.object(plugin, "ensure_tags", return_value=[]) as ensure_tags:
            result = plugin.process_image(
                stash, md5_image(), {}, {}, False, {}, {}, {}, {},
                plugin.PHashIndex(), lookup_mode="fast",
            )

        self.assertEqual(result, "updated_danbooru")
        self.assertEqual([call.args[2] for call in ensure_studio.call_args_list], ["primary_artist"])
        imported_names = list(ensure_tags.call_args.args[2])
        self.assertIn("secondary_artist", imported_names)
        self.assertIn("blue_hair", imported_names)
        self.assertNotIn("third-party_edit", imported_names)
        self.assertNotIn("primary_artist", imported_names)

    def test_conditional_dnp_artist_is_skipped_for_studio_selection(self):
        stash = ProcessFakeStash()
        settings = {"gelbooru_api_key": "gkey", "gelbooru_user_id": "42"}
        post = {
            "id": 12345,
            "tags": "conditional_dnp sample_artist blue_hair",
        }
        typed = {
            "conditional_dnp": 1,
            "sample_artist": 1,
            "blue_hair": 0,
        }
        logs = []
        with mock.patch.object(plugin, "ENABLE_DANBOORU", False), \
             mock.patch.object(plugin, "ENABLE_RULE34", False), \
             mock.patch.object(plugin, "ENABLE_E621", False), \
             mock.patch.object(plugin, "ENABLE_LOCAL_PHASH_REUSE", False), \
             mock.patch.object(plugin, "gelbooru_post", return_value=post), \
             mock.patch.object(plugin, "gelbooru_style_tag_metadata", return_value=typed), \
             mock.patch.object(plugin, "ensure_studio") as ensure_studio, \
             mock.patch.object(plugin, "log", side_effect=lambda level, message: logs.append((level, message))):
            result = plugin.process_image(
                stash, md5_image(), settings, {}, True, {}, {}, {}, {},
                plugin.PHashIndex(), lookup_mode="fast",
            )

        self.assertEqual(result, "matched_gelbooru")
        studio_names = [call.args[2] for call in ensure_studio.call_args_list]
        self.assertIn("sample_artist", studio_names)
        self.assertNotIn("conditional_dnp", studio_names)
        self.assertTrue(any("Studio 'sample_artist' would be assigned" in message for _level, message in logs))

    def test_gelbooru_md5_empty_http_200_remains_provider_failure(self):
        class EmptyResponse:
            def __enter__(self):
                return self
            def __exit__(self, exc_type, exc, tb):
                return False
            def read(self):
                return b""

        plugin._SOURCE_DISABLED.clear()
        with mock.patch.object(plugin.HTTP, "urlopen", return_value=EmptyResponse()):
            with self.assertRaises(RuntimeError):
                plugin.gelbooru_post("0" * 32, "", "")

    def test_marker_creation_recovers_from_uniqueness_race(self):
        stash = FakeStash()
        tag_cache = {}
        normalized = {}
        buckets = {}
        marker_id = plugin.ensure_no_match_marker_tag(stash, tag_cache, normalized, buckets)
        self.assertEqual(marker_id, "99")
        self.assertIn(plugin.NO_MATCH_MARKER_TAG.casefold(), tag_cache)



    def test_gelbooru_artist_type_is_extracted_for_studio_mapping(self):
        post = {"tags": "1girl sample_artist sample_character blue_hair"}
        typed = {
            "1girl": 0,
            "sample_artist": 1,
            "sample_character": 4,
            "blue_hair": 0,
        }
        with mock.patch.object(plugin, "gelbooru_style_tag_metadata", return_value=typed) as metadata:
            artists = plugin.artist_names(
                post, "gelbooru",
                gelbooru_api_key="gkey", gelbooru_user_id="42",
            )
        self.assertEqual(artists, ["sample_artist"])
        metadata.assert_called_once_with(
            plugin.GELBOORU_BASE,
            ["1girl", "sample_artist", "sample_character", "blue_hair"],
            "Gelbooru",
            "gkey",
            "42",
            False,
        )

    def test_gelbooru_artist_metadata_reuses_typed_tag_cache_for_character_lookup(self):
        post = {"tags": "sample_artist sample_character"}
        plugin._TYPED_TAG_CACHE.clear()

        def populate(base_url, names, source_name, *args):
            for name, type_id in {"sample_artist": 1, "sample_character": 4}.items():
                plugin._TYPED_TAG_CACHE[(source_name.casefold(), name.casefold())] = type_id
            return {"sample_artist": 1, "sample_character": 4}

        with mock.patch.object(plugin, "gelbooru_style_tag_metadata", side_effect=populate) as metadata:
            self.assertEqual(
                plugin.artist_names(post, "gelbooru", "gkey", "42"),
                ["sample_artist"],
            )
            # performer_names uses the same resolver. In production its second call is
            # served from _TYPED_TAG_CACHE, so no per-tag network fan-out is introduced.
            self.assertEqual(
                plugin.performer_names(post, "gelbooru", "gkey", "42"),
                ["sample_character"],
            )
        self.assertEqual(metadata.call_count, 2)

    def test_process_writes_no_match_only_after_definitive_miss(self):
        stash = ProcessFakeStash()
        settings = {}
        with mock.patch.object(plugin, "ENABLE_GELBOORU", False), \
             mock.patch.object(plugin, "ENABLE_RULE34", False), \
             mock.patch.object(plugin, "ENABLE_E621", False), \
             mock.patch.object(plugin, "ENABLE_DANBOORU_IQDB", False), \
             mock.patch.object(plugin, "ENABLE_E621_IQDB", False), \
             mock.patch.object(plugin, "ENABLE_SAUCENAO", False), \
             mock.patch.object(plugin, "ENABLE_LOCAL_PHASH_REUSE", False), \
             mock.patch.object(plugin, "danbooru_post", return_value=None):
            result = plugin.process_image(
                stash, md5_image(), settings, {}, False, {}, {}, {}, {}, plugin.PHashIndex()
            )
        self.assertEqual(result, "no_match")
        self.assertEqual(len(stash.updated), 1)

    def test_process_does_not_write_no_match_after_transient_error(self):
        stash = ProcessFakeStash()
        settings = {}
        def rate_limited(*args, **kwargs):
            raise urllib.error.HTTPError("https://x", 429, "rate", {}, None)
        with mock.patch.object(plugin, "ENABLE_GELBOORU", False), \
             mock.patch.object(plugin, "ENABLE_RULE34", False), \
             mock.patch.object(plugin, "ENABLE_E621", False), \
             mock.patch.object(plugin, "ENABLE_DANBOORU_IQDB", False), \
             mock.patch.object(plugin, "ENABLE_E621_IQDB", False), \
             mock.patch.object(plugin, "ENABLE_SAUCENAO", False), \
             mock.patch.object(plugin, "ENABLE_LOCAL_PHASH_REUSE", False), \
             mock.patch.object(plugin, "danbooru_post", side_effect=rate_limited):
            result = plugin.process_image(
                stash, md5_image(), settings, {}, False, {}, {}, {}, {}, plugin.PHashIndex()
            )
        self.assertEqual(result, "retry_later")
        self.assertEqual(stash.updated, [])


    def test_fast_pass_leaves_definitive_miss_unresolved_and_skips_visual_search(self):
        stash = ProcessFakeStash()
        settings = {
            "danbooru_login": "user",
            "danbooru_api_key": "key",
            "saucenao_api_key": "key",
        }
        metrics = {}
        with mock.patch.object(plugin, "ENABLE_GELBOORU", False), \
             mock.patch.object(plugin, "ENABLE_RULE34", False), \
             mock.patch.object(plugin, "ENABLE_E621", False), \
             mock.patch.object(plugin, "ENABLE_LOCAL_PHASH_REUSE", False), \
             mock.patch.object(plugin, "danbooru_post", return_value=None), \
             mock.patch.object(plugin, "danbooru_iqdb") as iqdb, \
             mock.patch.object(plugin, "saucenao_resolve") as sauce:
            image_obj = md5_image()
            result = plugin.process_image(
                stash, image_obj, settings, {}, False, {}, {}, {}, {},
                plugin.PHashIndex(), lookup_mode="fast", metrics=metrics
            )
        self.assertEqual(result, "unresolved")
        self.assertEqual(len(stash.updated), 1)
        self.assertEqual(stash.updated[0][0], "7")
        self.assertEqual(len(stash.updated[0][1]), 1)
        self.assertEqual(
            {t["name"] for t in image_obj["tags"]},
            {plugin.UNRESOLVED_MARKER_TAG},
        )
        self.assertEqual(metrics.get("md5_queries"), 1)
        self.assertEqual(metrics.get("iqdb_queries", 0), 0)
        self.assertEqual(metrics.get("saucenao_queries", 0), 0)
        iqdb.assert_not_called()
        sauce.assert_not_called()

    def test_saucenao_review_band_can_be_left_for_manual_review(self):
        stash = ProcessFakeStash()
        settings = {"saucenao_api_key": "key"}
        metrics = {}

        def sauce_miss(*args, **kwargs):
            kwargs["diagnostics"].update({
                "best_similarity": 94.0,
                "best_supported_similarity": 94.0,
                "best_supported_url": "https://danbooru.donmai.us/posts/123",
            })
            return None

        with mock.patch.object(plugin, "ENABLE_GELBOORU", False), \
             mock.patch.object(plugin, "ENABLE_RULE34", False), \
             mock.patch.object(plugin, "ENABLE_E621", False), \
             mock.patch.object(plugin, "ENABLE_DANBOORU_IQDB", False), \
             mock.patch.object(plugin, "ENABLE_E621_IQDB", False), \
             mock.patch.object(plugin, "ENABLE_LOCAL_PHASH_REUSE", False), \
             mock.patch.object(plugin, "danbooru_post", return_value=None), \
             mock.patch.object(plugin, "saucenao_resolve", side_effect=sauce_miss):
            image_obj = md5_image()
            image_obj["tags"] = [{"id": "old-unresolved", "name": plugin.UNRESOLVED_MARKER_TAG}]
            result = plugin.process_image(
                stash, image_obj, settings, {}, False, {}, {}, {}, {},
                plugin.PHashIndex(), lookup_mode="deep", metrics=metrics
            )
        self.assertEqual(result, "review_candidate")
        self.assertEqual(len(stash.updated), 1)
        self.assertEqual({t["name"] for t in image_obj["tags"]}, {plugin.REVIEW_MARKER_TAG})
        self.assertIn("https://danbooru.donmai.us/posts/123", image_obj["urls"])
        self.assertEqual(metrics.get("saucenao_queries"), 1)
        self.assertEqual(metrics.get("saucenao_review_candidates"), 1)

    def test_unsupported_saucenao_result_does_not_become_review_candidate(self):
        stash = ProcessFakeStash()
        settings = {"saucenao_api_key": "key"}
        metrics = {}

        def unsupported_miss(*args, **kwargs):
            kwargs["diagnostics"].update({
                "best_similarity": 94.0,
                "best_supported_similarity": 0.0,
            })
            return None

        with mock.patch.object(plugin, "ENABLE_GELBOORU", False), \
             mock.patch.object(plugin, "ENABLE_RULE34", False), \
             mock.patch.object(plugin, "ENABLE_E621", False), \
             mock.patch.object(plugin, "ENABLE_DANBOORU_IQDB", False), \
             mock.patch.object(plugin, "ENABLE_E621_IQDB", False), \
             mock.patch.object(plugin, "ENABLE_LOCAL_PHASH_REUSE", False), \
             mock.patch.object(plugin, "danbooru_post", return_value=None), \
             mock.patch.object(plugin, "saucenao_resolve", side_effect=unsupported_miss):
            result = plugin.process_image(
                stash, md5_image(), settings, {}, True, {}, {}, {}, {},
                plugin.PHashIndex(), lookup_mode="deep", metrics=metrics
            )
        self.assertEqual(result, "no_match")
        self.assertEqual(metrics.get("saucenao_review_candidates", 0), 0)

    def test_legacy_tuning_settings_cannot_override_fixed_policy(self):
        high, review, accept = plugin._saucenao_thresholds({
            "saucenao_auto_accept_similarity": 1,
            "saucenao_review_min_similarity": 1,
            "saucenao_accept_review_band": True,
        })
        self.assertEqual(high, 95.0)
        self.assertEqual(review, 85.0)
        self.assertFalse(accept)

    def test_saucenao_confidence_bands(self):
        self.assertEqual(plugin._visual_confidence(96.0, 95.0, 85.0), "HIGH")
        self.assertEqual(plugin._visual_confidence(94.0, 95.0, 85.0), "REVIEW")
        self.assertEqual(plugin._visual_confidence(85.0, 95.0, 85.0), "REVIEW")
        self.assertEqual(plugin._visual_confidence(84.9, 95.0, 85.0), "LOW")

    def test_ambiguous_normalized_tag_key_is_not_auto_reused(self):
        cache = {
            "artist_name": {"id": "1", "name": "Artist_Name"},
            "artist-name": {"id": "2", "name": "Artist-Name"},
        }
        index = plugin.build_normalized_tag_index(cache)
        self.assertNotIn("artist name", index)

    def test_saucenao_diagnostics_capture_review_candidate_url(self):
        import json

        payload = {
            "header": {"status": 0},
            "results": [
                {
                    "header": {"similarity": "94.3"},
                    "data": {"danbooru_id": 12345},
                }
            ],
        }

        class JsonResponse:
            def __enter__(self):
                return self
            def __exit__(self, exc_type, exc, tb):
                return False
            def read(self):
                return json.dumps(payload).encode("utf-8")

        diagnostics = {}
        with mock.patch.object(plugin.HTTP, "urlopen", return_value=JsonResponse()):
            result = plugin.saucenao_resolve(
                b"image", "key", 95.0, "", "", "", "", "", "",
                diagnostics=diagnostics, requests_per_30_seconds=0.0,
            )
        self.assertIsNone(result)
        self.assertEqual(diagnostics["best_supported_similarity"], 94.3)
        self.assertEqual(
            diagnostics["best_supported_url"],
            "https://danbooru.donmai.us/posts/12345",
        )

    def test_status_transition_is_mutually_exclusive(self):
        stash = ProcessFakeStash()
        image_obj = md5_image()
        image_obj["tags"] = [
            {"id": "user-tag", "name": "keep-me"},
            {"id": "old-unresolved", "name": plugin.UNRESOLVED_MARKER_TAG},
        ]
        cache = {}
        plugin.transition_image_status(
            stash, image_obj, plugin.REVIEW_MARKER_TAG, cache, {}, {},
            extra_url="https://danbooru.donmai.us/posts/456",
        )
        self.assertEqual(
            {t["name"] for t in image_obj["tags"]},
            {"keep-me", plugin.REVIEW_MARKER_TAG},
        )
        self.assertIn("https://danbooru.donmai.us/posts/456", image_obj["urls"])

    def test_deep_retry_keeps_unresolved_status(self):
        stash = ProcessFakeStash()
        image_obj = md5_image()
        image_obj["tags"] = [{"id": "u", "name": plugin.UNRESOLVED_MARKER_TAG}]
        def rate_limited(*args, **kwargs):
            raise urllib.error.HTTPError("https://x", 429, "rate", {}, None)
        with mock.patch.object(plugin, "ENABLE_GELBOORU", False), \
             mock.patch.object(plugin, "ENABLE_RULE34", False), \
             mock.patch.object(plugin, "ENABLE_E621", True), \
             mock.patch.object(plugin, "ENABLE_DANBOORU_IQDB", False), \
             mock.patch.object(plugin, "ENABLE_E621_IQDB", True), \
             mock.patch.object(plugin, "ENABLE_SAUCENAO", False), \
             mock.patch.object(plugin, "ENABLE_LOCAL_PHASH_REUSE", False), \
             mock.patch.object(plugin, "e621_iqdb", side_effect=rate_limited):
            result = plugin.process_image(
                stash, image_obj, {}, {}, False, {}, {}, {}, {}, plugin.PHashIndex(),
                lookup_mode="deep",
            )
        self.assertEqual(result, "retry_later")
        self.assertEqual(stash.updated, [])
        self.assertEqual({t["name"] for t in image_obj["tags"]}, {plugin.UNRESOLVED_MARKER_TAG})

    def test_forced_no_match_does_not_downgrade_imported(self):
        stash = ProcessFakeStash()
        image_obj = md5_image()
        image_obj["tags"] = [{"id": "i", "name": plugin.IMPORT_MARKER_TAG}]
        with mock.patch.object(plugin, "ENABLE_GELBOORU", False), \
             mock.patch.object(plugin, "ENABLE_RULE34", False), \
             mock.patch.object(plugin, "ENABLE_E621", False), \
             mock.patch.object(plugin, "ENABLE_DANBOORU_IQDB", False), \
             mock.patch.object(plugin, "ENABLE_E621_IQDB", False), \
             mock.patch.object(plugin, "ENABLE_SAUCENAO", False), \
             mock.patch.object(plugin, "ENABLE_LOCAL_PHASH_REUSE", False), \
             mock.patch.object(plugin, "danbooru_post", return_value=None):
            result = plugin.process_image(
                stash, image_obj, {}, {}, False, {}, {}, {}, {}, plugin.PHashIndex(),
                force_recheck=True, lookup_mode="deep",
            )
        self.assertEqual(result, "preserved_imported")
        self.assertEqual(stash.updated, [])
        self.assertEqual({t["name"] for t in image_obj["tags"]}, {plugin.IMPORT_MARKER_TAG})


if __name__ == "__main__":
    unittest.main()



class SauceNAOQuotaTests(unittest.TestCase):
    def setUp(self):
        plugin._SAUCENAO_RATE_LIMIT_HITS = 0
        plugin._SAUCENAO_DAILY_EXHAUSTED = False
        plugin._SAUCENAO_DISABLED_REASON = ""
        plugin._SAUCENAO_ACCOUNT_TYPE = ""
        plugin._SAUCENAO_SHORT_LIMIT = 0
        plugin._SAUCENAO_SHORT_REMAINING = -1
        plugin._SAUCENAO_LONG_LIMIT = 0
        plugin._SAUCENAO_LONG_REMAINING = -1
        plugin._SAUCENAO_LAST_REQUEST_AT = 0.0
        plugin._SAUCENAO_PAUSE_UNTIL = 0.0
        plugin._SAUCENAO_QUOTA_PAUSES = 0
        plugin._SAUCENAO_QUOTA_ANNOUNCED = False

    def test_auto_rate_uses_paid_account_short_limit(self):
        payload = {"header": {
            "account_type": "2", "short_limit": "40", "short_remaining": 39,
            "long_limit": "10000", "long_remaining": 9999, "status": 0,
        }}
        diagnostics = {}
        plugin._saucenao_update_quota(payload, 0.0, diagnostics)
        self.assertEqual(plugin._SAUCENAO_SHORT_LIMIT, 40)
        self.assertEqual(plugin._saucenao_effective_rate(0.0), 40.0)
        self.assertEqual(diagnostics["effective_requests_per_30_seconds"], 40.0)
        self.assertEqual(diagnostics["account_type"], "2")

    def test_user_rate_caps_but_never_exceeds_account_limit(self):
        plugin._SAUCENAO_SHORT_LIMIT = 40
        self.assertEqual(plugin._saucenao_effective_rate(20), 20.0)
        self.assertEqual(plugin._saucenao_effective_rate(100), 40.0)

    def test_zero_remaining_short_quota_schedules_pause(self):
        payload = {"header": {
            "short_limit": "40", "short_remaining": 0,
            "long_limit": "10000", "long_remaining": 9000, "status": 0,
        }}
        with mock.patch.object(plugin.time, "monotonic", return_value=100.0):
            plugin._saucenao_update_quota(payload, 0.0, {})
        self.assertEqual(plugin._SAUCENAO_PAUSE_UNTIL, 130.0)
        self.assertEqual(plugin._SAUCENAO_QUOTA_PAUSES, 1)
        self.assertFalse(plugin._SAUCENAO_DAILY_EXHAUSTED)

    def test_daily_quota_exhaustion_disables_only_saucenao_authority(self):
        payload = {"header": {
            "short_limit": "40", "short_remaining": 10,
            "long_limit": "10000", "long_remaining": 0, "status": -2,
        }}
        plugin._saucenao_update_quota(payload, 0.0, {})
        self.assertTrue(plugin._saucenao_is_disabled())
        self.assertIn("daily", plugin._SAUCENAO_DISABLED_REASON)

    def test_paid_rate_wait_interval_is_based_on_reported_limit(self):
        plugin._SAUCENAO_SHORT_LIMIT = 40
        plugin._SAUCENAO_LAST_REQUEST_AT = 100.0
        # 40 searches / 30 seconds -> 0.75s smooth interval.
        times = iter([100.25, 101.0, 101.0])
        with mock.patch.object(plugin.time, "monotonic", side_effect=lambda: next(times)), \
             mock.patch.object(plugin.time, "sleep") as sleeper:
            effective = plugin._saucenao_wait_for_slot(0.0)
        self.assertEqual(effective, 40.0)
        sleeper.assert_called_once()
        self.assertAlmostEqual(sleeper.call_args.args[0], 0.5, places=3)

    def test_configurable_saucenao_rate_setting_defaults_to_auto(self):
        self.assertEqual(plugin._saucenao_requested_rate({}), 0.0)
        self.assertEqual(plugin._saucenao_requested_rate({"saucenao_requests_per_30_seconds": 60}), 60.0)
        self.assertEqual(plugin._saucenao_requested_rate({"saucenao_requests_per_30_seconds": -1}), 0.0)

    def test_saucenao_429_pauses_but_does_not_disable_for_run(self):
        plugin._saucenao_note_rate_limit("HTTP 429", 5.0)
        self.assertEqual(plugin._SAUCENAO_RATE_LIMIT_HITS, 1)
        self.assertGreater(plugin._SAUCENAO_PAUSE_UNTIL, 0.0)
        self.assertFalse(plugin._saucenao_is_disabled())

    def test_process_passes_user_saucenao_rate_ceiling(self):
        stash = ProcessFakeStash()
        settings = {
            "saucenao_api_key": "key",
            "saucenao_requests_per_30_seconds": 60,
        }
        seen = {}
        def sauce(*args, **kwargs):
            # Positional arg 11 is requests_per_30_seconds after image/key/threshold
            # and the provider credential arguments.
            seen["rate"] = args[11]
            return None
        with mock.patch.object(plugin, "ENABLE_GELBOORU", False), \
             mock.patch.object(plugin, "ENABLE_RULE34", False), \
             mock.patch.object(plugin, "ENABLE_E621", False), \
             mock.patch.object(plugin, "ENABLE_DANBOORU_IQDB", False), \
             mock.patch.object(plugin, "ENABLE_E621_IQDB", False), \
             mock.patch.object(plugin, "ENABLE_LOCAL_PHASH_REUSE", False), \
             mock.patch.object(plugin, "danbooru_post", return_value=None), \
             mock.patch.object(plugin, "saucenao_resolve", side_effect=sauce):
            plugin.process_image(
                stash, md5_image(), settings, {}, True, {}, {}, {}, {},
                plugin.PHashIndex(), lookup_mode="deep", metrics={}
            )
        self.assertEqual(seen["rate"], 60.0)


class Rule34CooldownPluginTests(unittest.TestCase):
    def setUp(self):
        plugin._SOURCE_DISABLED.clear()
        plugin._PROVIDER_WARNING_ONCE.clear()
        plugin._RULE34_COOLDOWN_ANNOUNCED = False

    def test_fast_rule34_429_continues_to_e621_and_leaves_image_pending(self):
        stash = ProcessFakeStash()
        settings = {"rule34_api_key": "key", "rule34_user_id": "123"}
        metrics = {}
        error = urllib.error.HTTPError("https://api.rule34.xxx", 429, "rate", {}, None)
        with mock.patch.object(plugin, "ENABLE_LOCAL_PHASH_REUSE", False), \
             mock.patch.object(plugin, "danbooru_post", return_value=None), \
             mock.patch.object(plugin, "gelbooru_post", return_value=None), \
             mock.patch.object(plugin, "rule34_post", side_effect=error), \
             mock.patch.object(plugin, "e621_post", return_value=None) as e621:
            result = plugin.process_image(
                stash, md5_image(), settings, {}, False, {}, {}, {}, {},
                plugin.PHashIndex(), lookup_mode="fast", metrics=metrics
            )
        self.assertEqual(result, "retry_later")
        e621.assert_called_once()
        self.assertEqual(stash.updated, [])
        self.assertEqual(metrics.get("pending_rule34_confirmation"), 1)
        self.assertEqual(metrics.get("rule34_rate_limit_hits"), 1)

    def test_fast_active_rule34_cooldown_skips_rule34_call_and_continues(self):
        stash = ProcessFakeStash()
        settings = {"rule34_api_key": "key", "rule34_user_id": "123"}
        metrics = {}
        with mock.patch.object(plugin, "ENABLE_LOCAL_PHASH_REUSE", False), \
             mock.patch.object(plugin, "danbooru_post", return_value=None), \
             mock.patch.object(plugin, "gelbooru_post", return_value=None), \
             mock.patch.object(plugin, "rule34_post") as rule34, \
             mock.patch.object(plugin, "e621_post", return_value=None) as e621, \
             mock.patch.object(plugin.HTTP, "cooldown_remaining", return_value=300.0):
            result = plugin.process_image(
                stash, md5_image(), settings, {}, False, {}, {}, {}, {},
                plugin.PHashIndex(), lookup_mode="fast", metrics=metrics
            )
        self.assertEqual(result, "retry_later")
        rule34.assert_not_called()
        e621.assert_called_once()
        self.assertEqual(stash.updated, [])
        self.assertEqual(metrics.get("rule34_cooldown_skips"), 1)
        self.assertEqual(metrics.get("pending_rule34_confirmation"), 1)
        # Three real external MD5 requests: Danbooru, Gelbooru and e621.
        self.assertEqual(metrics.get("md5_queries"), 3)
