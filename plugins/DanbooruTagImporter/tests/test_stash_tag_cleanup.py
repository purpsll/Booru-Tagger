import pathlib
import sys
import unittest

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from stash_client import Stash


class FakeStash(Stash):
    def __init__(self):
        self.calls = []

    def gql(self, query, variables=None):
        self.calls.append((query, variables or {}))
        if "TagsForCleanup" in query:
            page = int((variables or {}).get("filter", {}).get("page", 1))
            if page == 1:
                return {
                    "findTags": {
                        "count": 2,
                        "tags": [
                            {
                                "id": "1",
                                "name": "tag_one",
                                "aliases": [],
                                "description": "",
                                "sort_name": None,
                                "ignore_auto_tag": False,
                                "favorite": False,
                                "image_path": None,
                                "custom_fields": {},
                                "scene_count": 0,
                                "scene_marker_count": 0,
                                "image_count": 1,
                                "gallery_count": 0,
                                "performer_count": 0,
                                "studio_count": 0,
                                "group_count": 0,
                                "parents": [],
                                "children": [],
                            },
                            {
                                "id": "2",
                                "name": "tag two",
                                "aliases": [],
                                "description": "",
                                "sort_name": None,
                                "ignore_auto_tag": False,
                                "favorite": False,
                                "image_path": None,
                                "custom_fields": {},
                                "scene_count": 0,
                                "scene_marker_count": 0,
                                "image_count": 1,
                                "gallery_count": 0,
                                "performer_count": 0,
                                "studio_count": 0,
                                "group_count": 0,
                                "parents": [],
                                "children": [],
                            },
                        ],
                    }
                }
            raise AssertionError("Unexpected extra cleanup page")
        if "MergeTags" in query:
            return {
                "tagsMerge": {
                    "id": variables["input"]["destination"],
                    "name": "tag_one",
                    "aliases": ["tag two"],
                }
            }
        raise AssertionError("Unexpected GraphQL query")


class StashTagCleanupClientTests(unittest.TestCase):
    def test_tags_for_cleanup_fetches_required_metadata(self):
        stash = FakeStash()
        tags = stash.tags_for_cleanup()
        self.assertEqual([tag["id"] for tag in tags], ["1", "2"])
        query, variables = stash.calls[0]
        self.assertIn("custom_fields", query)
        self.assertIn("parents { id }", query)
        self.assertIn("image_count", query)
        self.assertEqual(variables["filter"]["per_page"], 250)

    def test_merge_tags_uses_native_tags_merge_mutation(self):
        stash = FakeStash()
        merged = stash.merge_tags(["2", "3"], "1")
        self.assertEqual(merged["id"], "1")
        query, variables = stash.calls[-1]
        self.assertIn("tagsMerge", query)
        self.assertEqual(
            variables["input"],
            {"source": ["2", "3"], "destination": "1"},
        )

    def test_merge_tags_rejects_destination_as_source(self):
        stash = FakeStash()
        with self.assertRaises(ValueError):
            stash.merge_tags(["1"], "1")


if __name__ == "__main__":
    unittest.main()
