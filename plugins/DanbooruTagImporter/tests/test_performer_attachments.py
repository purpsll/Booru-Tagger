import importlib.util
import pathlib
import sys
import unittest

from stash_client import Stash

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

spec = importlib.util.spec_from_file_location(
    "plugin_main_performer_attachment", PLUGIN_DIR / "DanbooruTagImporter.py"
)
plugin = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(plugin)


class PerformerAttachmentTests(unittest.TestCase):
    def test_stash_performer_cache_indexes_alias_to_canonical(self):
        stash = Stash({"Host": "localhost", "Port": 9999})

        def fake_gql(query, variables=None):
            return {
                "findPerformers": {
                    "count": 1,
                    "performers": [
                        {
                            "id": "10",
                            "name": "Cindy the Marten",
                            "alias_list": ["cindy_the_marten"],
                        }
                    ],
                }
            }

        stash.gql = fake_gql
        performers = stash.all_performers()

        self.assertEqual(performers["cindy_the_marten"]["id"], "10")
        self.assertEqual(performers["cindy_the_marten"]["name"], "Cindy the Marten")

    def test_normalized_merge_returns_canonical_performer_and_alias(self):
        canonical = {"id": "10", "name": "Cindy the Marten", "alias_list": []}
        cache = {"cindy the marten": canonical}

        class FakeStash:
            def create_performer(self, name):
                raise AssertionError("performer should be reused, not created")

            def update_performer_aliases(self, performer_id, aliases):
                self.assert_id = performer_id
                return {
                    "id": performer_id,
                    "name": "Cindy the Marten",
                    "alias_list": list(aliases),
                }

            def all_performers(self):
                return cache

        stash = FakeStash()
        result = plugin.ensure_performer(
            stash,
            cache,
            "cindy_the_marten",
            dry_run=False,
            merge_normalized=True,
            merge_similar=True,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["id"], "10")
        self.assertIn("cindy_the_marten", result["alias_list"])
        self.assertEqual(cache["cindy_the_marten"]["id"], "10")

    def test_exact_alias_match_attaches_canonical_performer(self):
        canonical = {
            "id": "10",
            "name": "Cindy the Marten",
            "alias_list": ["cindy_the_marten"],
        }
        cache = {
            "cindy the marten": canonical,
            "cindy_the_marten": canonical,
        }

        class FakeStash:
            def create_performer(self, name):
                raise AssertionError("alias match must not create a new performer")

            def update_performer_aliases(self, performer_id, aliases):
                raise AssertionError("existing exact alias should not need an alias update")

            def all_performers(self):
                return cache

        result = plugin.ensure_performer(
            FakeStash(),
            cache,
            "cindy_the_marten",
            dry_run=False,
            merge_normalized=True,
            merge_similar=True,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["id"], "10")
        self.assertEqual(result["name"], "Cindy the Marten")

    def test_duplicate_attachment_is_replaced_by_canonical_id(self):
        canonical = {
            "id": "10",
            "name": "Cindy the Marten",
            "alias_list": ["cindy_the_marten"],
        }
        stale_duplicate = {
            "id": "22",
            "name": "cindy_the_marten",
            "alias_list": [],
        }
        unrelated = {
            "id": "30",
            "name": "Another Character",
            "alias_list": [],
        }
        cache = {
            "cindy the marten": canonical,
            "cindy_the_marten": canonical,
            "stale": stale_duplicate,
            "another character": unrelated,
        }
        image = {
            "performers": [
                {"id": "22", "name": "cindy_the_marten"},
                {"id": "30", "name": "Another Character"},
            ]
        }

        duplicate_ids = plugin._equivalent_attached_performer_ids(
            image,
            cache,
            "cindy_the_marten",
            canonical,
        )

        self.assertEqual(duplicate_ids, {"22"})

        final_ids = {"22", "30"}
        final_ids.difference_update(duplicate_ids)
        final_ids.add(str(canonical["id"]))
        self.assertEqual(final_ids, {"10", "30"})

    def test_fuzzy_only_similarity_never_removes_existing_attachment(self):
        canonical = {
            "id": "10",
            "name": "Alexandra Stone",
            "alias_list": ["Alexandra Ston"],
        }
        different = {
            "id": "22",
            "name": "Alexandra Stove",
            "alias_list": [],
        }
        cache = {
            "alexandra stone": canonical,
            "alexandra ston": canonical,
            "alexandra stove": different,
        }
        image = {"performers": [{"id": "22", "name": "Alexandra Stove"}]}

        duplicate_ids = plugin._equivalent_attached_performer_ids(
            image,
            cache,
            "Alexandra Ston",
            canonical,
        )

        self.assertEqual(duplicate_ids, set())


if __name__ == "__main__":
    unittest.main()
