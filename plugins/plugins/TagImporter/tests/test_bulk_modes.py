import importlib.util
import pathlib
import sys
import unittest
from unittest import mock

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

spec = importlib.util.spec_from_file_location("plugin_main_bulk", PLUGIN_DIR / "DanbooruTagImporter.py")
plugin = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(plugin)


MARKER_IDS = {
    plugin.IMPORT_MARKER_TAG: "tag-imported",
    plugin.UNRESOLVED_MARKER_TAG: "tag-unresolved",
    plugin.REVIEW_MARKER_TAG: "tag-review",
    plugin.NO_MATCH_MARKER_TAG: "tag-no-match",
}


class BulkFakeStash:
    def __init__(self, images):
        self.images = images

    def all_tags(self):
        out = {}
        for marker, marker_id in MARKER_IDS.items():
            if any(
                any(t.get("name") == marker for t in image.get("tags", []))
                for image in self.images
            ):
                out[marker.casefold()] = {"id": marker_id, "name": marker}
        return out

    def all_studios(self):
        return {}

    def all_performers(self):
        return {}

    def find_images(self, page, per_page=100, tag_id=None):
        pool = self.images
        if tag_id:
            pool = [
                image for image in pool
                if any(str(tag.get("id")) == str(tag_id) for tag in image.get("tags", []))
            ]
        start = (page - 1) * per_page
        end = start + per_page
        return len(pool), pool[start:end]

    def all_images_with_tag(self, tag_id, limit=0):
        pool = [
            image for image in self.images
            if any(str(tag.get("id")) == str(tag_id) for tag in image.get("tags", []))
        ]
        return pool[:limit] if limit else pool

    def imported_images_for_phash_reuse(self, marker_tag_id):
        return self.all_images_with_tag(marker_tag_id)


def image(image_id, marker=None):
    tags = []
    if marker:
        tags.append({"id": MARKER_IDS[marker], "name": marker})
    return {
        "id": str(image_id),
        "tags": tags,
        "performers": [],
        "studio": None,
        "date": "",
        "urls": [],
        "files": [{"fingerprints": [{"type": "md5", "value": f"{image_id:032x}"[-32:]}]}],
    }


class BulkModeTests(unittest.TestCase):
    def test_fast_limit_counts_only_unclassified_images(self):
        images = [
            image(1, plugin.IMPORT_MARKER_TAG),
            image(2, plugin.NO_MATCH_MARKER_TAG),
            image(3, plugin.UNRESOLVED_MARKER_TAG),
            image(4, plugin.REVIEW_MARKER_TAG),
            image(5),
            image(6),
            image(7),
        ]
        stash = BulkFakeStash(images)
        processed = []

        def fake_process(stash_obj, img, settings, tag_cache, dry_run, *args, **kwargs):
            processed.append(img["id"])
            return "unresolved"

        with mock.patch.object(plugin, "process_image", side_effect=fake_process):
            stats = plugin.import_all(
                stash,
                {},
                {"dry_run": True, "lookup_mode": "fast", "limit": 2},
            )

        self.assertEqual(processed, ["5", "6"])
        self.assertEqual(stats["seen"], 2)
        self.assertEqual(stats["scanned"], 6)
        self.assertEqual(stats["skipped_imported"], 1)
        self.assertEqual(stats["skipped_no_match"], 1)
        self.assertEqual(stats["skipped_unresolved"], 1)
        self.assertEqual(stats["skipped_review"], 1)
        self.assertEqual(stats["unresolved"], 2)
        self.assertEqual(stats["lookup_mode"], "fast")

    def test_deep_mode_targets_only_unresolved_marker(self):
        images = [
            image(1),
            image(2, plugin.UNRESOLVED_MARKER_TAG),
            image(3, plugin.REVIEW_MARKER_TAG),
            image(4, plugin.UNRESOLVED_MARKER_TAG),
            image(5, plugin.NO_MATCH_MARKER_TAG),
        ]
        stash = BulkFakeStash(images)
        processed = []

        def fake_process(stash_obj, img, settings, tag_cache, dry_run, *args, **kwargs):
            processed.append((img["id"], kwargs.get("lookup_mode"), kwargs.get("force_recheck")))
            return "no_match"

        with mock.patch.object(plugin, "process_image", side_effect=fake_process):
            stats = plugin.import_all(
                stash,
                {},
                {"dry_run": True, "lookup_mode": "deep", "limit": 10},
            )

        self.assertEqual(processed, [("2", "deep", False), ("4", "deep", False)])
        self.assertEqual(stats["seen"], 2)
        self.assertEqual(stats["scanned"], 2)
        self.assertEqual(stats["no_match"], 2)

    def test_review_recheck_targets_only_review_marker_and_forces_processing(self):
        images = [
            image(1, plugin.UNRESOLVED_MARKER_TAG),
            image(2, plugin.REVIEW_MARKER_TAG),
            image(3, plugin.NO_MATCH_MARKER_TAG),
        ]
        stash = BulkFakeStash(images)
        processed = []

        def fake_process(stash_obj, img, settings, tag_cache, dry_run, *args, **kwargs):
            processed.append((img["id"], kwargs.get("force_recheck")))
            return "review_candidate"

        with mock.patch.object(plugin, "process_image", side_effect=fake_process):
            stats = plugin.import_all(
                stash, {},
                {"lookup_mode": "deep", "only_review": True},
            )

        self.assertEqual(processed, [("2", True)])
        self.assertEqual(stats["seen"], 1)
        self.assertEqual(stats["review_candidate"], 1)

    def test_default_bulk_mode_is_fast(self):
        stash = BulkFakeStash([image(1)])
        seen_modes = []

        def fake_process(stash_obj, img, settings, tag_cache, dry_run, *args, **kwargs):
            seen_modes.append(kwargs.get("lookup_mode"))
            return "unresolved"

        with mock.patch.object(plugin, "process_image", side_effect=fake_process):
            stats = plugin.import_all(
                stash,
                {},
                {"dry_run": True, "limit": 1},
            )

        self.assertEqual(seen_modes, ["fast"])
        self.assertEqual(stats["lookup_mode"], "fast")


if __name__ == "__main__":
    unittest.main()
