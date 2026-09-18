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

    def test_normalized_alias_variant_resolves_to_canonical_tag(self):
        canonical = {
            "id": "7000",
            "name": "canonical_tag",
            "aliases": ["old-merged-tag"],
        }
        cache = {
            "canonical_tag": canonical,
            "old-merged-tag": canonical,
        }
        normalized = plugin.build_normalized_tag_index(cache)
        buckets = plugin.build_similarity_buckets(normalized)

        ids = plugin.resolve_existing_tag_ids(
            cache,
            ["old_merged_tag"],
            merge_similar=True,
            similarity_threshold=0.50,
            similarity_margin=0.0,
            normalized_index=normalized,
            similarity_buckets=buckets,
            image_id="3419",
        )

        self.assertEqual(ids, ["7000"])

    def test_recovery_never_fuzzy_guesses_deleted_tag(self):
        existing = {
            "id": "8000",
            "name": "alexandra_stove",
            "aliases": [],
        }
        cache = {"alexandra_stove": existing}
        normalized = plugin.build_normalized_tag_index(cache)
        buckets = plugin.build_similarity_buckets(normalized)

        ids = plugin.resolve_existing_tag_ids(
            cache,
            ["alexandra_ston"],
            merge_similar=True,
            similarity_threshold=0.50,
            similarity_margin=0.0,
            normalized_index=normalized,
            similarity_buckets=buckets,
            image_id="3419",
        )

        self.assertEqual(ids, [])

    def test_status_marker_retries_with_fresh_canonical_tag_ids(self):
        stale_marker = {"id": "900", "name": "Multi-Booru Unresolved", "aliases": []}
        fresh_marker = {"id": "901", "name": "Multi-Booru Unresolved", "aliases": []}
        fresh_user_tag = {"id": "7000", "name": "canonical_tag", "aliases": ["old_tag"]}

        class FakeStash:
            def __init__(self):
                self.calls = []

            def update_image_tags(self, image_id, tag_ids, **kwargs):
                self.calls.append((str(image_id), list(tag_ids), kwargs))
                if len(self.calls) == 1:
                    raise RuntimeError(
                        "Stash GraphQL error: error executing INSERT INTO images_tags "
                        "(image_id, tag_id) VALUES (?, ?) [[3419 6558]]: "
                        "FOREIGN KEY constraint failed path imageUpdate"
                    )

            def find_image(self, image_id):
                return {
                    "id": str(image_id),
                    "tags": [{"id": "7000", "name": "canonical_tag"}],
                    "urls": [],
                }

            def all_tags(self):
                return {
                    "canonical_tag": fresh_user_tag,
                    "old_tag": fresh_user_tag,
                    "multi-booru unresolved": fresh_marker,
                }

            def create_tag(self, name):
                raise AssertionError("current marker already exists after refresh")

        image = {
            "id": "3419",
            "tags": [{"id": "6558", "name": "old_tag"}],
            "urls": [],
        }
        cache = {
            "old_tag": {"id": "6558", "name": "old_tag", "aliases": []},
            "multi-booru unresolved": stale_marker,
        }
        normalized = plugin.build_normalized_tag_index(cache)
        buckets = plugin.build_similarity_buckets(normalized)
        stash = FakeStash()

        changed = plugin._write_status_marker(
            stash,
            image,
            "Multi-Booru Unresolved",
            cache,
            normalized,
            buckets,
        )

        self.assertTrue(changed)
        self.assertEqual(len(stash.calls), 2)
        self.assertEqual(set(stash.calls[1][1]), {"7000", "901"})
        self.assertEqual(
            {tag["id"] for tag in image["tags"]},
            {"7000", "901"},
        )

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
