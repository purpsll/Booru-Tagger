import pathlib
import sys
import unittest

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from stash_client import Stash


class FakeEntityStash(Stash):
    def __init__(self):
        self.calls = []

    def gql(self, query, variables=None):
        variables = variables or {}
        self.calls.append((query, variables))

        if "PerformersForCleanup" in query:
            return {"findPerformers": {"count": 1, "performers": [{
                "id": "1", "name": "jane_doe", "alias_list": [], "urls": [],
                "tags": [], "stash_ids": [], "scene_count": 1, "image_count": 0,
                "gallery_count": 0, "group_count": 0,
            }]}}

        if "StudiosForCleanup" in query:
            return {"findStudios": {"count": 1, "studios": [{
                "id": "10", "name": "artist_name", "aliases": [], "urls": [],
                "tags": [], "stash_ids": [], "parent_studio": None,
                "child_studios": [], "scene_count": 1, "image_count": 0,
                "gallery_count": 0, "group_count": 0,
            }]}}

        if "MergePerformers" in query:
            return {"performerMerge": {
                "id": variables["input"]["destination"],
                "name": "jane_doe",
                "alias_list": variables["input"]["values"].get("alias_list", []),
                "urls": variables["input"]["values"].get("urls", []),
                "stash_ids": variables["input"]["values"].get("stash_ids", []),
            }}

        if "PrepareStudio" in query:
            return {"studioUpdate": {"id": variables["input"]["id"]}}

        if "ObjectsByStudio" in query:
            for root, collection in (
                ("findScenes", "scenes"),
                ("findImages", "images"),
                ("findGalleries", "galleries"),
                ("findGroups", "groups"),
            ):
                if root in query:
                    return {root: {"count": 1, collection: [{"id": root + "-1"}]}}
            raise AssertionError("unknown object-by-studio query")

        if "AssignStudio" in query:
            if "bulkSceneUpdate" in query:
                return {"bulkSceneUpdate": [{"id": "s"}]}
            if "bulkImageUpdate" in query:
                return {"bulkImageUpdate": [{"id": "i"}]}
            if "bulkGalleryUpdate" in query:
                return {"bulkGalleryUpdate": [{"id": "g"}]}
            if "bulkGroupUpdate" in query:
                return {"bulkGroupUpdate": [{"id": "m"}]}

        if "DestroyMergedStudio" in query:
            return {"studioDestroy": True}

        if "FinalizeStudioMerge" in query:
            values = variables["input"]
            return {"studioUpdate": {
                "id": values["id"],
                "name": "artist_name",
                "aliases": values.get("aliases", []),
                "urls": values.get("urls", []),
                "stash_ids": values.get("stash_ids", []),
            }}

        raise AssertionError("Unexpected GraphQL query: " + query)


class StashEntityCleanupTests(unittest.TestCase):
    def test_cleanup_queries_fetch_performers_and_studios(self):
        stash = FakeEntityStash()
        performers = stash.performers_for_cleanup()
        studios = stash.studios_for_cleanup()
        self.assertEqual(performers[0]["id"], "1")
        self.assertEqual(studios[0]["id"], "10")
        self.assertIn("disambiguation", stash.calls[0][0])
        self.assertIn("parent_studio", stash.calls[1][0])

    def test_native_performer_merge_carries_merged_values(self):
        stash = FakeEntityStash()
        result = stash.merge_performers(
            ["2"], "1",
            {
                "alias_list": ["Jane Doe"],
                "urls": ["https://example.test"],
                "tag_ids": ["t1"],
                "stash_ids": [{"endpoint": "box", "stash_id": "abc"}],
            },
        )
        self.assertEqual(result["id"], "1")
        query, variables = stash.calls[-1]
        self.assertIn("performerMerge", query)
        self.assertEqual(variables["input"]["source"], ["2"])
        self.assertEqual(variables["input"]["values"]["id"], "1")
        self.assertIn("Jane Doe", variables["input"]["values"]["alias_list"])

    def test_studio_merge_moves_all_supported_attachments_before_delete(self):
        stash = FakeEntityStash()
        result = stash.merge_studios(
            ["11"], "10",
            {
                "aliases": ["Artist Name"],
                "urls": ["https://example.test"],
                "tag_ids": ["t1"],
                "stash_ids": [{"endpoint": "box", "stash_id": "abc"}],
            },
        )
        self.assertEqual(result["id"], "10")
        self.assertEqual(
            result["_moved"],
            {"scenes": 1, "images": 1, "galleries": 1, "groups": 1},
        )

        combined = "\n".join(query for query, _ in stash.calls)
        for token in (
            "bulkSceneUpdate", "bulkImageUpdate", "bulkGalleryUpdate",
            "bulkGroupUpdate", "studioDestroy", "FinalizeStudioMerge",
        ):
            self.assertIn(token, combined)

        destroy_index = next(
            i for i, (query, _) in enumerate(stash.calls)
            if "DestroyMergedStudio" in query
        )
        final_index = next(
            i for i, (query, _) in enumerate(stash.calls)
            if "FinalizeStudioMerge" in query
        )
        self.assertLess(destroy_index, final_index)


if __name__ == "__main__":
    unittest.main()
