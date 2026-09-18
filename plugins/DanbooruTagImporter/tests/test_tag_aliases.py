import importlib.util
import pathlib
import sys
import unittest

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from stash_client import Stash

spec = importlib.util.spec_from_file_location(
    "plugin_main_tag_aliases", PLUGIN_DIR / "DanbooruTagImporter.py"
)
plugin = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(plugin)


class TagAliasTests(unittest.TestCase):
    def test_all_tags_indexes_stash_alias_to_canonical_tag(self):
        stash = Stash({"Host": "localhost", "Port": 9999})

        def fake_gql(query, variables=None):
            return {
                "findTags": {
                    "count": 1,
                    "tags": [
                        {
                            "id": "7",
                            "name": "large_penis",
                            "aliases": ["big_penis"],
                        }
                    ],
                }
            }

        stash.gql = fake_gql
        tags = stash.all_tags()

        self.assertEqual(tags["large_penis"]["id"], "7")
        self.assertEqual(tags["big_penis"]["id"], "7")
        self.assertEqual(tags["big_penis"]["name"], "large_penis")

    def test_recovery_resolves_merged_tag_alias_to_canonical_id(self):
        canonical = {
            "id": "7000",
            "name": "canonical_tag",
            "aliases": ["old_merged_tag"],
        }
        cache = {
            "canonical_tag": canonical,
            "old_merged_tag": canonical,
        }
        normalized = plugin.build_normalized_tag_index(cache)
        buckets = plugin.build_similarity_buckets(normalized)

        ids = plugin.resolve_existing_tag_ids(
            cache,
            ["old_merged_tag"],
            merge_similar=True,
            similarity_threshold=0.96,
            similarity_margin=0.02,
            normalized_index=normalized,
            similarity_buckets=buckets,
            image_id="3419",
        )

        self.assertEqual(ids, ["7000"])

    def test_recovery_skips_deleted_tag_instead_of_recreating(self):
        ids = plugin.resolve_existing_tag_ids(
            {},
            ["deleted_tag"],
            merge_similar=True,
            similarity_threshold=0.96,
            similarity_margin=0.02,
            normalized_index={},
            similarity_buckets={},
            image_id="3419",
        )

        self.assertEqual(ids, [])

    def test_relation_fk_classifier_handles_tags_and_performers(self):
        tag_error = RuntimeError(
            "Stash GraphQL error: error executing INSERT INTO images_tags "
            "(image_id, tag_id) VALUES (?, ?) [[3419 6558]]: "
            "FOREIGN KEY constraint failed path imageUpdate"
        )
        performer_error = RuntimeError(
            "Stash GraphQL error: error executing INSERT INTO performers_images "
            "(image_id, performer_id) VALUES (?, ?) [[5597 297]]: "
            "FOREIGN KEY constraint failed path imageUpdate"
        )
        other_error = RuntimeError("FOREIGN KEY constraint failed")

        self.assertEqual(plugin._relation_fk_failure_kind(tag_error), "tag")
        self.assertEqual(plugin._relation_fk_failure_kind(performer_error), "performer")
        self.assertIsNone(plugin._relation_fk_failure_kind(other_error))

    def test_ensure_tags_recovers_if_alias_is_added_after_cache_load(self):
        canonical = {
            "id": "7",
            "name": "large_penis",
            "aliases": ["big_penis"],
        }

        class AliasConflictStash:
            def __init__(self):
                self.create_calls = 0

            def create_tag(self, name):
                self.create_calls += 1
                raise RuntimeError(
                    "Stash GraphQL error: "
                    "[{'message': \"name 'big_penis' is used as alias for 'large_penis'\", "
                    "'path': ['tagCreate']}]"
                )

            def all_tags(self):
                return {
                    "large_penis": canonical,
                    "big_penis": canonical,
                }

        stash = AliasConflictStash()
        cache = {}
        ids = plugin.ensure_tags(
            stash,
            cache,
            ["big_penis"],
            dry_run=False,
            merge_similar=False,
        )

        self.assertEqual(ids, ["7"])
        self.assertEqual(stash.create_calls, 1)
        self.assertEqual(cache["big_penis"]["name"], "large_penis")


if __name__ == "__main__":
    unittest.main()
