import pathlib
import sys
import unittest

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from entity_cleanup import build_entity_cleanup_plan, merged_entity_values


def performer(pid, name, **extra):
    obj = {
        "id": str(pid), "name": name, "alias_list": [], "urls": [], "tags": [],
        "stash_ids": [], "scene_count": 0, "image_count": 0, "gallery_count": 0,
        "group_count": 0, "disambiguation": "", "gender": None, "birthdate": None,
        "ethnicity": "", "country": "", "eye_color": "", "height_cm": None,
        "measurements": "", "fake_tits": "", "penis_length": None,
        "circumcised": None, "career_start": None, "career_end": None,
        "tattoos": "", "piercings": "", "favorite": False, "rating100": None,
        "details": "", "death_date": None, "hair_color": "", "weight": None,
        "ignore_auto_tag": False, "image_path": "/performer/x/image?default=true",
        "custom_fields": {},
    }
    obj.update(extra)
    return obj


def studio(sid, name, **extra):
    obj = {
        "id": str(sid), "name": name, "aliases": [], "urls": [], "tags": [],
        "stash_ids": [], "scene_count": 0, "image_count": 0, "gallery_count": 0,
        "group_count": 0, "parent_studio": None, "child_studios": [],
        "rating100": None, "details": "", "favorite": False,
        "ignore_auto_tag": False, "organized": False,
        "image_path": "/studio/x/image?default=true", "custom_fields": {},
    }
    obj.update(extra)
    return obj


class EntityCleanupTests(unittest.TestCase):
    def test_performer_format_duplicate_is_safe(self):
        plan = build_entity_cleanup_plan(
            [performer(1, "jane_doe", scene_count=10), performer(2, "Jane Doe")],
            entity_type="performer", alias_field="alias_list", fuzzy_threshold=0.98,
        )
        self.assertEqual(len(plan.safe_merges), 1)
        self.assertEqual(plan.safe_merges[0].destination_id, "1")

    def test_performer_fuzzy_match_is_review_only(self):
        plan = build_entity_cleanup_plan(
            [performer(1, "alexandra stone"), performer(2, "alexandra ston")],
            entity_type="performer", alias_field="alias_list", fuzzy_threshold=0.95,
        )
        self.assertEqual(plan.safe_merges, ())
        self.assertEqual(len(plan.review), 1)
        self.assertEqual(plan.review[0].kind, "fuzzy")

    def test_disambiguation_conflict_blocks_performer_merge(self):
        plan = build_entity_cleanup_plan(
            [
                performer(1, "jane_doe", disambiguation="US"),
                performer(2, "Jane Doe", disambiguation="UK"),
            ],
            entity_type="performer", alias_field="alias_list", fuzzy_threshold=0.98,
        )
        self.assertEqual(plan.safe_merges, ())
        self.assertIn("metadata-conflict", plan.review[0].kind)

    def test_same_endpoint_different_stash_ids_block_merge(self):
        plan = build_entity_cleanup_plan(
            [
                performer(1, "jane_doe", stash_ids=[{"endpoint": "box", "stash_id": "A"}]),
                performer(2, "Jane Doe", stash_ids=[{"endpoint": "box", "stash_id": "B"}]),
            ],
            entity_type="performer", alias_field="alias_list", fuzzy_threshold=0.98,
        )
        self.assertEqual(plan.safe_merges, ())
        self.assertIn("stash-id-conflict", plan.review[0].kind)

    def test_different_stash_endpoints_can_merge(self):
        plan = build_entity_cleanup_plan(
            [
                performer(1, "jane_doe", stash_ids=[{"endpoint": "box-a", "stash_id": "A"}]),
                performer(2, "Jane Doe", stash_ids=[{"endpoint": "box-b", "stash_id": "B"}]),
            ],
            entity_type="performer", alias_field="alias_list", fuzzy_threshold=0.98,
        )
        self.assertEqual(len(plan.safe_merges), 1)

    def test_studio_parent_conflict_requires_review(self):
        plan = build_entity_cleanup_plan(
            [
                studio(1, "artist_name", parent_studio={"id": "10"}),
                studio(2, "Artist Name", parent_studio={"id": "11"}),
            ],
            entity_type="studio", alias_field="aliases", fuzzy_threshold=0.96,
        )
        self.assertEqual(plan.safe_merges, ())
        self.assertIn("metadata-conflict", plan.review[0].kind)

    def test_single_rich_studio_is_kept(self):
        plan = build_entity_cleanup_plan(
            [
                studio(1, "Artist Name", scene_count=100),
                studio(2, "artist_name", details="keep this"),
            ],
            entity_type="studio", alias_field="aliases", fuzzy_threshold=0.96,
        )
        self.assertEqual(plan.safe_merges[0].destination_id, "2")

    def test_merged_values_preserve_names_aliases_urls_tags_and_ids(self):
        items = [
            performer(
                1, "jane_doe", alias_list=["Jane"], urls=["https://a"],
                tags=[{"id": "t1"}], stash_ids=[{"endpoint": "box-a", "stash_id": "1"}],
                scene_count=5,
            ),
            performer(
                2, "Jane Doe", alias_list=["J. Doe"], urls=["https://b"],
                tags=[{"id": "t2"}], stash_ids=[{"endpoint": "box-b", "stash_id": "2"}],
            ),
        ]
        plan = build_entity_cleanup_plan(
            items, entity_type="performer", alias_field="alias_list", fuzzy_threshold=0.98,
        )
        by_id = {obj["id"]: obj for obj in items}
        values = merged_entity_values(by_id, plan.safe_merges[0], alias_field="alias_list")
        self.assertIn("Jane Doe", values["alias_list"])
        self.assertIn("J. Doe", values["alias_list"])
        self.assertEqual(set(values["urls"]), {"https://a", "https://b"})
        self.assertEqual(set(values["tag_ids"]), {"t1", "t2"})
        self.assertEqual(len(values["stash_ids"]), 2)


if __name__ == "__main__":
    unittest.main()
