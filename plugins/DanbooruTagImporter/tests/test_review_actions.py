import copy
import importlib.util
import pathlib
import sys
import unittest
from unittest import mock

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

spec = importlib.util.spec_from_file_location(
    "plugin_review_actions", PLUGIN_DIR / "DanbooruTagImporter.py"
)
plugin = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(plugin)


CANDIDATE_URL = "https://danbooru.donmai.us/posts/12345"
REVIEW_URL = CANDIDATE_URL + "#booru-importer-review-confidence=89.4"
EXTERNAL_CANDIDATE_URL = "https://twitter.com/example/status/123456789"
EXTERNAL_REVIEW_URL = (
    EXTERNAL_CANDIDATE_URL + "#booru-importer-review-confidence=87.6"
)

class ReviewFakeStash:
    def __init__(self, image):
        self.image = copy.deepcopy(image)
        self.updated = []

    def settings(self):
        return {}

    def find_image(self, image_id):
        if str(image_id) != str(self.image["id"]):
            return None
        return self.image

    def all_tags(self):
        return {
            "keep-me": {"id": "user-tag", "name": "keep-me", "aliases": []},
            plugin.REVIEW_MARKER_TAG.casefold(): {
                "id": "review-tag",
                "name": plugin.REVIEW_MARKER_TAG,
                "aliases": [],
            },
            plugin.IMPORT_MARKER_TAG.casefold(): {
                "id": "import-tag",
                "name": plugin.IMPORT_MARKER_TAG,
                "aliases": [],
            },
            plugin.NO_MATCH_MARKER_TAG.casefold(): {
                "id": "no-match-tag",
                "name": plugin.NO_MATCH_MARKER_TAG,
                "aliases": [],
            },
        }

    def image_bytes(self, image_id):
        return b"review-image"

    def all_studios(self):
        return {}

    def all_performers(self):
        return {}

    def update_image_tags(
        self,
        image_id,
        tag_ids,
        studio_id=None,
        performer_ids=None,
        date=None,
        urls=None,
        title=None,
        photographer=None,
    ):
        self.updated.append(
            {
                "image_id": str(image_id),
                "tag_ids": list(tag_ids),
                "studio_id": studio_id,
                "performer_ids": list(performer_ids) if performer_ids is not None else None,
                "date": date,
                "urls": list(urls) if urls is not None else None,
                "title": title,
                "photographer": photographer,
            }
        )


def review_image():
    return {
        "id": "42",
        "tags": [
            {"id": "user-tag", "name": "keep-me"},
            {"id": "review-tag", "name": plugin.REVIEW_MARKER_TAG},
        ],
        "performers": [],
        "studio": None,
        "date": "",
        "urls": [
            "https://example.com/user-url",
            CANDIDATE_URL,
        ],
        "files": [
            {
                "fingerprints": [
                    {"type": "md5", "value": "a" * 32},
                    {"type": "phash", "value": "0123456789abcdef"},
                ]
            }
        ],
    }


class ReviewDecisionTests(unittest.TestCase):
    def setUp(self):
        plugin._PROVIDER_WARNING_ONCE.clear()

    def test_no_moves_review_to_no_match_and_removes_only_candidate_url(self):
        stash = ReviewFakeStash(review_image())

        result = plugin.review_candidate_action(
            stash,
            {},
            {
                "action": "no",
                "image_id": "42",
                "candidate_url": CANDIDATE_URL,
            },
        )

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["marker"], plugin.NO_MATCH_MARKER_TAG)
        self.assertEqual(len(stash.updated), 1)
        update = stash.updated[0]
        self.assertIn("user-tag", update["tag_ids"])
        self.assertIn("no-match-tag", update["tag_ids"])
        self.assertNotIn("review-tag", update["tag_ids"])
        self.assertNotIn("import-tag", update["tag_ids"])
        self.assertEqual(update["urls"], ["https://example.com/user-url"])

    def test_yes_imports_approved_source_through_normal_metadata_path(self):
        stash = ReviewFakeStash(review_image())
        post = {
            "id": 12345,
            "tag_string_general": "blue_hair",
            "tag_string_artist": "sample_artist",
            "tag_string_character": "sample_character",
            "tag_string_copyright": "",
            "tag_string_meta": "",
            "created_at": "2025-01-02T03:04:05Z",
        }

        with mock.patch.object(
            plugin,
            "_resolve_supported_booru_url",
            return_value=("danbooru", post),
        ) as resolver, mock.patch.object(
            plugin,
            "ensure_tags",
            return_value=["source-tag"],
        ), mock.patch.object(
            plugin,
            "ensure_studio",
            return_value={"id": "studio-1", "name": "sample_artist"},
        ), mock.patch.object(
            plugin,
            "ensure_performer",
            return_value={"id": "performer-1", "name": "sample_character"},
        ):
            result = plugin.review_candidate_action(
                stash,
                {},
                {
                    "action": "yes",
                    "image_id": "42",
                    "candidate_url": CANDIDATE_URL,
                },
            )

        self.assertEqual(result["status"], "imported")
        self.assertEqual(result["marker"], plugin.IMPORT_MARKER_TAG)
        resolver.assert_called_once()
        self.assertEqual(len(stash.updated), 1)
        update = stash.updated[0]
        self.assertIn("user-tag", update["tag_ids"])
        self.assertIn("source-tag", update["tag_ids"])
        self.assertIn("import-tag", update["tag_ids"])
        self.assertNotIn("review-tag", update["tag_ids"])
        self.assertEqual(update["studio_id"], "studio-1")
        self.assertEqual(update["performer_ids"], ["performer-1"])
        self.assertEqual(update["date"], "2025-01-02")
        # The approved candidate was already attached during Review and remains as source URL.
        self.assertIsNone(update["urls"])

    def test_external_review_candidate_is_valid_and_active(self):
        image = review_image()
        image["urls"][-1] = EXTERNAL_REVIEW_URL
        stash = ReviewFakeStash(image)

        result = plugin.review_candidate_action(
            stash,
            {},
            {
                "action": "no",
                "image_id": "42",
                "candidate_url": EXTERNAL_REVIEW_URL,
            },
        )

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["candidate_url"], EXTERNAL_CANDIDATE_URL)
        self.assertEqual(result["review_confidence"], 87.6)
        self.assertEqual(
            stash.updated[-1]["urls"],
            ["https://example.com/user-url"],
        )

    def test_yes_external_candidate_rechecks_saucenao_and_imports_verified_metadata(self):
        image = review_image()
        image["urls"][-1] = EXTERNAL_REVIEW_URL
        stash = ReviewFakeStash(image)
        external_post = {
            "id": "123456789",
            "_saucenao_score": 87.6,
            "_saucenao_external": True,
            "_source_site": "Twitter",
            "_source_url": EXTERNAL_CANDIDATE_URL,
            "_source_title": "Example source title",
            "_source_artists": ["example_creator"],
            "_source_characters": [],
            "_source_date": "2025-01-02T03:04:05Z",
        }

        with mock.patch.object(
            plugin,
            "_resolve_supported_booru_url",
            return_value=None,
        ), mock.patch.object(
            plugin,
            "saucenao_resolve",
            return_value=("saucenao_external", external_post),
        ) as sauce, mock.patch.object(
            plugin,
            "ensure_studio",
            return_value={"id": "studio-1", "name": "example_creator"},
        ):
            result = plugin.review_candidate_action(
                stash,
                {"saucenao_api_key": "key"},
                {
                    "action": "yes",
                    "image_id": "42",
                    "candidate_url": EXTERNAL_REVIEW_URL,
                },
            )

        self.assertEqual(result["status"], "imported")
        self.assertEqual(result["candidate_url"], EXTERNAL_CANDIDATE_URL)
        self.assertEqual(result["review_confidence"], 87.6)
        sauce.assert_called_once()
        update = stash.updated[-1]
        self.assertIn("import-tag", update["tag_ids"])
        self.assertNotIn("review-tag", update["tag_ids"])
        self.assertEqual(update["studio_id"], "studio-1")
        self.assertEqual(update["date"], "2025-01-02")
        self.assertEqual(update["title"], "Example source title")
        self.assertEqual(update["photographer"], "example_creator")
        self.assertEqual(
            update["urls"],
            ["https://example.com/user-url", EXTERNAL_CANDIDATE_URL],
        )

    def test_decision_rejects_a_non_active_supported_url(self):
        image = review_image()
        image["urls"].insert(1, "https://e621.net/posts/999")
        stash = ReviewFakeStash(image)

        with self.assertRaisesRegex(RuntimeError, "active candidate"):
            plugin.review_candidate_action(
                stash,
                {},
                {
                    "action": "yes",
                    "image_id": "42",
                    "candidate_url": "https://e621.net/posts/999",
                },
            )

    def test_review_transition_persists_saucenao_confidence_in_hidden_fragment(self):
        image = review_image()
        stash = ReviewFakeStash(image)
        tag_cache = stash.all_tags()
        normalized = plugin.build_normalized_tag_index(tag_cache)
        buckets = plugin.build_similarity_buckets(normalized)

        plugin.transition_image_status(
            stash,
            image,
            plugin.REVIEW_MARKER_TAG,
            tag_cache,
            normalized,
            buckets,
            extra_url=CANDIDATE_URL,
            review_confidence=89.4,
        )

        stored = stash.updated[-1]["urls"][-1]
        self.assertEqual(stored, REVIEW_URL)
        canonical, score = plugin._review_candidate_parts(stored)
        self.assertEqual(canonical, CANDIDATE_URL)
        self.assertEqual(score, 89.4)

    def test_yes_cleans_internal_confidence_fragment_and_keeps_canonical_source(self):
        image = review_image()
        image["urls"][-1] = REVIEW_URL
        stash = ReviewFakeStash(image)
        post = {
            "id": 12345,
            "tag_string_general": "blue_hair",
            "tag_string_artist": "",
            "tag_string_character": "",
            "tag_string_copyright": "",
            "tag_string_meta": "",
            "created_at": "2025-01-02T03:04:05Z",
        }

        with mock.patch.object(
            plugin,
            "_resolve_supported_booru_url",
            return_value=("danbooru", post),
        ), mock.patch.object(
            plugin,
            "ensure_tags",
            return_value=["source-tag"],
        ):
            result = plugin.review_candidate_action(
                stash,
                {},
                {
                    "action": "yes",
                    "image_id": "42",
                    "candidate_url": REVIEW_URL,
                },
            )

        self.assertEqual(result["review_confidence"], 89.4)
        self.assertEqual(result["candidate_url"], CANDIDATE_URL)
        self.assertEqual(stash.updated[-1]["urls"], [
            "https://example.com/user-url",
            CANDIDATE_URL,
        ])

    def test_legacy_review_candidate_without_confidence_remains_supported(self):
        canonical, score = plugin._review_candidate_parts(CANDIDATE_URL)
        self.assertEqual(canonical, CANDIDATE_URL)
        self.assertIsNone(score)

    def test_review_transition_keeps_active_candidate_url_last(self):
        image = review_image()
        image["urls"] = [
            CANDIDATE_URL,
            "https://e621.net/posts/999",
            "https://example.com/user-url",
        ]
        stash = ReviewFakeStash(image)
        tag_cache = stash.all_tags()
        normalized = plugin.build_normalized_tag_index(tag_cache)
        buckets = plugin.build_similarity_buckets(normalized)

        plugin.transition_image_status(
            stash,
            image,
            plugin.REVIEW_MARKER_TAG,
            tag_cache,
            normalized,
            buckets,
            extra_url=CANDIDATE_URL,
        )

        self.assertEqual(
            stash.updated[-1]["urls"],
            [
                "https://e621.net/posts/999",
                "https://example.com/user-url",
                CANDIDATE_URL,
            ],
        )

    def test_review_ui_is_loaded_and_exposes_yes_no_controls(self):
        manifest = (PLUGIN_DIR / "DanbooruTagImporter.yml").read_text(encoding="utf-8")
        ui = (PLUGIN_DIR / "BooruImporterReview.js").read_text(encoding="utf-8")

        self.assertIn("BooruImporterReview.js", manifest)
        self.assertIn('PluginApi.patch.after("ImageDetailPanel"', ui)
        self.assertIn('function (props, _context, rendered)', ui)
        self.assertNotIn('function (props, rendered)', ui)
        self.assertIn('mode: "review_decision"', ui)
        self.assertIn('choose("yes")', ui)
        self.assertIn('choose("no")', ui)
        self.assertIn("Yes — import this source", ui)
        self.assertIn("No — mark No Match", ui)
        self.assertIn("Review confidence: ", ui)
        self.assertIn("isReviewCandidateUrl", ui)
        self.assertIn("fragment.has(REVIEW_CONFIDENCE_KEY)", ui)
        self.assertIn("85.0–92.9%", ui)
        self.assertIn("Not recorded — recheck this Review candidate to populate it.", ui)
        self.assertIn("runPluginOperation", ui)


if __name__ == "__main__":
    unittest.main()
