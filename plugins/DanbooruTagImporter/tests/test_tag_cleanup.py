import pathlib
import sys
import unittest

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from tag_cleanup import build_tag_cleanup_plan


def make_tag(tag_id, name, **overrides):
    tag = {
        "id": str(tag_id),
        "name": name,
        "aliases": [],
        "scene_count": 0,
        "scene_marker_count": 0,
        "image_count": 0,
        "gallery_count": 0,
        "performer_count": 0,
        "studio_count": 0,
        "group_count": 0,
        "description": "",
        "sort_name": "",
        "favorite": False,
        "ignore_auto_tag": False,
        "image_path": "",
        "custom_fields": {},
        "parents": [],
        "children": [],
    }
    tag.update(overrides)
    return tag


class TagCleanupTests(unittest.TestCase):
    def test_separator_case_variants_are_safe(self):
        plan = build_tag_cleanup_plan([
            make_tag(1, "big_breasts", image_count=10),
            make_tag(2, "Big Breasts", image_count=3),
        ])
        self.assertEqual(len(plan.safe_merges), 1)
        candidate = plan.safe_merges[0]
        self.assertEqual(candidate.destination_id, "1")
        self.assertEqual(candidate.source_ids, ("2",))
        self.assertTrue(candidate.auto_merge)

    def test_joined_word_variant_is_safe_without_character_changes(self):
        plan = build_tag_cleanup_plan([
            make_tag(1, "deepthroat", image_count=1),
            make_tag(2, "deep_throat", image_count=2),
        ])
        self.assertEqual(len(plan.safe_merges), 1)
        self.assertEqual(plan.safe_merges[0].destination_name, "deep_throat")

    def test_semantically_related_but_different_tag_is_not_merged(self):
        plan = build_tag_cleanup_plan([
            make_tag(1, "anal", image_count=10),
            make_tag(2, "anal_sex", image_count=5),
            make_tag(3, "large_breasts", image_count=5),
            make_tag(4, "breasts", image_count=10),
        ])
        self.assertEqual(plan.safe_merges, ())
        self.assertEqual(plan.review, ())

    def test_fuzzy_match_is_review_only(self):
        plan = build_tag_cleanup_plan([
            make_tag(1, "abcdefghijklmnopqrstuvwxy"),
            make_tag(2, "abcdefghijklmnopqrstuvwxz"),
        ])
        self.assertEqual(plan.safe_merges, ())
        self.assertEqual(len(plan.review), 1)
        self.assertEqual(plan.review[0].kind, "fuzzy")
        self.assertFalse(plan.review[0].auto_merge)

    def test_conflicting_rich_metadata_blocks_auto_merge(self):
        plan = build_tag_cleanup_plan([
            make_tag(1, "big_breasts", description="first"),
            make_tag(2, "Big Breasts", description="second"),
        ])
        self.assertEqual(plan.safe_merges, ())
        self.assertEqual(len(plan.review), 1)
        self.assertIn("metadata-conflict", plan.review[0].kind)

    def test_single_rich_metadata_tag_is_preserved_as_destination(self):
        plan = build_tag_cleanup_plan([
            make_tag(1, "big_breasts", image_count=50),
            make_tag(2, "Big Breasts", image_count=1, description="keep me"),
        ])
        self.assertEqual(len(plan.safe_merges), 1)
        self.assertEqual(plan.safe_merges[0].destination_id, "2")

    def test_usage_count_breaks_plain_duplicate_ties(self):
        plan = build_tag_cleanup_plan([
            make_tag(1, "Big Breasts", image_count=2),
            make_tag(2, "big_breasts", image_count=20),
        ])
        self.assertEqual(plan.safe_merges[0].destination_id, "2")

    def test_booru_style_breaks_equal_usage_tie(self):
        plan = build_tag_cleanup_plan([
            make_tag(1, "Big Breasts", image_count=5),
            make_tag(2, "big_breasts", image_count=5),
        ])
        self.assertEqual(plan.safe_merges[0].destination_id, "2")

    def test_workflow_tags_are_never_cleanup_targets(self):
        plan = build_tag_cleanup_plan(
            [
                make_tag(1, "Multi-Booru Imported"),
                make_tag(2, "multi_booru_imported"),
            ],
            protected_names=["Multi-Booru Imported"],
        )
        self.assertEqual(plan.safe_merges, ())
        self.assertEqual(plan.review, ())

    def test_primary_name_matching_existing_alias_is_safe(self):
        plan = build_tag_cleanup_plan([
            make_tag(1, "doggy_style", aliases=["doggystyle"], image_count=10),
            make_tag(2, "Doggystyle", image_count=2),
        ])
        self.assertEqual(len(plan.safe_merges), 1)
        self.assertEqual(plan.safe_merges[0].destination_id, "1")


if __name__ == "__main__":
    unittest.main()
