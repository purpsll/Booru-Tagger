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
    ):
        self.updated.append(
            {
                "image_id": str(image_id),
                "tag_ids": list(tag_ids),
                "studio_id": studio_id,
                "performer_ids": list(performer_ids) if performer_ids is not None else None,
                "date": date,
                "urls": list(urls) if urls is not None else None,
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

    def test_review_ui_is_loaded_and_exposes_yes_no_controls(self):
        manifest = (PLUGIN_DIR / "DanbooruTagImporter.yml").read_text(encoding="utf-8")
        ui = (PLUGIN_DIR / "BooruImporterReview.js").read_text(encoding="utf-8")

        self.assertIn("BooruImporterReview.js", manifest)
        self.assertIn('PluginApi.patch.after("ImageDetailPanel"', ui)
        self.assertIn('mode: "review_decision"', ui)
        self.assertIn('choose("yes")', ui)
        self.assertIn('choose("no")', ui)
        self.assertIn("Yes — import this source", ui)
        self.assertIn("No — mark No Match", ui)
        self.assertIn("runPluginOperation", ui)


if __name__ == "__main__":
    unittest.main()
