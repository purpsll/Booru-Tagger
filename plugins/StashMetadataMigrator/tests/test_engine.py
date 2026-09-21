import json
import pathlib
import sys
import tempfile
import unittest

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from StashMetadataMigrator import MigrationEngine


def write_json(root, folder, filename, data):
    path = pathlib.Path(root) / folder
    path.mkdir(parents=True, exist_ok=True)
    (path / filename).write_text(json.dumps(data), encoding="utf-8")


class FakeStash:
    def __init__(self):
        self.calls = []
        self._tags = [{
            "id": "t1", "name": "big_breasts", "aliases": [],
            "sort_name": None, "description": None, "favorite": False,
            "ignore_auto_tag": False, "stash_ids": [], "custom_fields": {},
        }]
        self._performers = [{
            "id": "p1", "name": "jane_doe", "disambiguation": "",
            "alias_list": [], "urls": [], "gender": None, "birthdate": None,
            "ethnicity": None, "country": None, "eye_color": None,
            "height_cm": None, "measurements": None, "fake_tits": None,
            "penis_length": None, "circumcised": None, "career_start": None,
            "career_end": None, "tattoos": None, "piercings": None,
            "favorite": False, "rating100": None, "details": None,
            "death_date": None, "hair_color": None, "weight": None,
            "ignore_auto_tag": False, "stash_ids": [], "tags": [],
            "custom_fields": {},
        }]
        self._studios = [{
            "id": "s1", "name": "artist_studio", "aliases": [], "urls": [],
            "rating100": None, "favorite": False, "details": None,
            "ignore_auto_tag": False, "organized": False,
            "parent_studio": None, "stash_ids": [], "tags": [],
            "custom_fields": {},
        }]
        self._scenes = [{
            "id": "scene1", "title": None, "code": None, "details": None,
            "director": None, "urls": [], "date": None, "rating100": None,
            "organized": False, "resume_time": 0.0, "play_duration": 0.0,
            "play_history": [], "o_history": [], "studio": None,
            "tags": [], "performers": [], "stash_ids": [],
            "custom_fields": {},
            "files": [{
                "id": "f1", "path": "/new/movie.mp4", "size": 123,
                "fingerprints": [{"type": "oshash", "value": "abc123"}],
            }],
        }]

    def tags(self):
        return [dict(x) for x in self._tags]

    def performers(self):
        return [dict(x) for x in self._performers]

    def studios(self):
        return [dict(x) for x in self._studios]

    def scenes(self):
        return [dict(x) for x in self._scenes]

    def images(self):
        return []

    def update_tag(self, data):
        self.calls.append(("update_tag", data))
        existing = next(x for x in self._tags if x["id"] == data["id"])
        existing.update({k: v for k, v in data.items() if k not in {"id", "custom_fields"}})
        if "custom_fields" in data:
            existing["custom_fields"].update(data["custom_fields"].get("partial", {}))
        return dict(existing)

    def create_tag(self, data):
        self.calls.append(("create_tag", data))
        item = {"id": "new-tag", "sort_name": None, "description": None,
                "favorite": False, "ignore_auto_tag": False, "stash_ids": [],
                "custom_fields": {}, **data}
        self._tags.append(item)
        return dict(item)

    def update_performer(self, data):
        self.calls.append(("update_performer", data))
        existing = next(x for x in self._performers if x["id"] == data["id"])
        existing.update({k: v for k, v in data.items() if k not in {"id", "custom_fields"}})
        if "custom_fields" in data:
            existing["custom_fields"].update(data["custom_fields"].get("partial", {}))
        return dict(existing)

    def create_performer(self, data):
        self.calls.append(("create_performer", data))
        item = {"id": "new-performer", "alias_list": [], "urls": [],
                "stash_ids": [], "tags": [], "custom_fields": {}, **data}
        self._performers.append(item)
        return dict(item)

    def update_studio(self, data):
        self.calls.append(("update_studio", data))
        existing = next(x for x in self._studios if x["id"] == data["id"])
        existing.update({k: v for k, v in data.items() if k not in {"id", "custom_fields"}})
        if "custom_fields" in data:
            existing["custom_fields"].update(data["custom_fields"].get("partial", {}))
        return dict(existing)

    def create_studio(self, data):
        self.calls.append(("create_studio", data))
        item = {"id": "new-studio", "aliases": [], "urls": [],
                "stash_ids": [], "tags": [], "custom_fields": {}, **data}
        self._studios.append(item)
        return dict(item)

    def update_scene(self, data):
        self.calls.append(("update_scene", data))
        return {"id": data["id"], "title": data.get("title")}

    def update_image(self, data):
        self.calls.append(("update_image", data))
        return {"id": data["id"], "title": data.get("title")}

    def add_scene_plays(self, scene_id, times):
        self.calls.append(("add_scene_plays", {"id": scene_id, "times": times}))

    def add_scene_os(self, scene_id, times):
        self.calls.append(("add_scene_os", {"id": scene_id, "times": times}))


class MigrationEngineTests(unittest.TestCase):
    def _build_export(self, root):
        write_json(root, "files", "movie.json", {
            "type": "video", "path": r"D:\Old\movie.mp4", "size": 123,
            "fingerprints": [{"type": "oshash", "fingerprint": "abc123"}],
        })
        write_json(root, "tags", "tag.json", {
            "name": "Big Breasts", "aliases": ["large breasts"],
        })
        write_json(root, "performers", "performer.json", {
            "name": "Jane Doe", "aliases": ["J. Doe"], "tags": ["Big Breasts"],
        })
        write_json(root, "studios", "studio.json", {
            "name": "Artist Studio", "aliases": ["ArtistStudio"],
            "tags": ["Big Breasts"],
        })
        write_json(root, "scenes", "scene.json", {
            "title": "Old Scene Title",
            "files": [r"D:\Old\movie.mp4"],
            "tags": ["Big Breasts"],
            "performers": ["Jane Doe"],
            "studio": "Artist Studio",
            "urls": ["https://old.example/scene"],
            "play_history": ["2025-01-01T00:00:00Z"],
            "o_history": ["2025-01-02T00:00:00Z"],
        })

    def test_restore_updates_existing_scene_and_reuses_entities(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._build_export(tmp)
            stash = FakeStash()
            engine = MigrationEngine(stash, pathlib.Path(tmp), dry_run=False)
            stats = engine.run()

            self.assertEqual(stats["matched_scenes"], 1)
            self.assertEqual(stats["updated_scenes"], 1)
            self.assertEqual(stats["created_tags"], 0)
            self.assertEqual(stats["created_performers"], 0)
            self.assertEqual(stats["created_studios"], 0)

            calls = dict((name, data) for name, data in stash.calls if name == "update_scene")
            update = calls["update_scene"]
            self.assertEqual(update["id"], "scene1")
            self.assertEqual(update["title"], "Old Scene Title")
            self.assertEqual(update["tag_ids"], ["t1"])
            self.assertEqual(update["performer_ids"], ["p1"])
            self.assertEqual(update["studio_id"], "s1")

            performer_update = next(data for name, data in stash.calls if name == "update_performer")
            self.assertIn("Jane Doe", performer_update["alias_list"])
            self.assertIn("J. Doe", performer_update["alias_list"])

            studio_update = next(data for name, data in stash.calls if name == "update_studio")
            self.assertIn("Artist Studio", studio_update["aliases"])

            self.assertTrue(any(name == "add_scene_plays" for name, _ in stash.calls))
            self.assertTrue(any(name == "add_scene_os" for name, _ in stash.calls))

    def test_analyze_is_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._build_export(tmp)
            stash = FakeStash()
            engine = MigrationEngine(stash, pathlib.Path(tmp), dry_run=True)
            stats = engine.run()
            self.assertEqual(stats["matched_scenes"], 1)
            self.assertEqual(stash.calls, [])

    def test_duplicate_old_performer_display_name_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._build_export(tmp)
            write_json(tmp, "performers", "performer2.json", {
                "name": "Jane Doe", "disambiguation": "different person",
            })
            stash = FakeStash()
            engine = MigrationEngine(stash, pathlib.Path(tmp), dry_run=False)
            resolved = engine.resolve_performer("Jane Doe")
            self.assertIsNone(resolved)
            self.assertEqual(engine.stats["ambiguous_entities_skipped"], 1)
            self.assertFalse(any(name == "create_performer" for name, _ in stash.calls))

    def test_same_endpoint_identity_conflict_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._build_export(tmp)
            # Give both old and current performer a same-endpoint but different identity.
            performer_path = pathlib.Path(tmp) / "performers" / "performer.json"
            data = json.loads(performer_path.read_text(encoding="utf-8"))
            data["stash_ids"] = [{"endpoint": "https://box", "stash_id": "old"}]
            performer_path.write_text(json.dumps(data), encoding="utf-8")

            stash = FakeStash()
            stash._performers[0]["stash_ids"] = [
                {"endpoint": "https://box", "stash_id": "current"}
            ]
            engine = MigrationEngine(stash, pathlib.Path(tmp), dry_run=False)
            self.assertIsNone(engine.resolve_performer("Jane Doe"))
            self.assertEqual(engine.stats["entity_identity_conflicts"], 1)


if __name__ == "__main__":
    unittest.main()
