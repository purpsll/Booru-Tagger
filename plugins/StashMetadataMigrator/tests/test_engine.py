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
        self._galleries = []
        self._groups = []
        self._images = []
        self._scenes = [{
            "id": "scene1", "title": None, "code": None, "details": None,
            "director": None, "urls": [], "date": None, "rating100": None,
            "organized": False, "resume_time": 0.0, "play_duration": 0.0,
            "play_history": [], "o_history": [], "studio": None,
            "tags": [], "performers": [], "stash_ids": [],
            "galleries": [], "groups": [], "scene_markers": [],
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

    def galleries(self):
        return [dict(x) for x in self._galleries]

    def groups(self):
        return [dict(x) for x in self._groups]

    def scenes(self):
        return [dict(x) for x in self._scenes]

    def images(self):
        return [dict(x) for x in self._images]

    def backup_database(self):
        self.calls.append(("backup_database", {}))
        if getattr(self, "fail_backup", False):
            raise RuntimeError("backup failed")
        return None

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

    def update_tag_aliases(self, tag_id, aliases):
        self.calls.append(("update_tag_aliases", {"id": tag_id, "aliases": aliases}))
        existing = next(x for x in self._tags if x["id"] == tag_id)
        existing["aliases"] = list(aliases)
        return dict(existing)

    def merge_tags(self, source_ids, destination_id):
        self.calls.append(("merge_tags", {"source": source_ids, "destination": destination_id}))
        destination = next(x for x in self._tags if x["id"] == destination_id)
        sources = [x for x in self._tags if x["id"] in source_ids]
        destination["aliases"] = list(dict.fromkeys(
            destination.get("aliases", []) + [x["name"] for x in sources]
        ))
        self._tags[:] = [x for x in self._tags if x["id"] not in source_ids]
        return {"id": destination_id, "name": destination["name"], "aliases": destination["aliases"]}

    def merge_performers(self, source_ids, destination_id, values):
        self.calls.append(("merge_performers", {"source": source_ids, "destination": destination_id, "values": values}))
        destination = next(x for x in self._performers if x["id"] == destination_id)
        destination.update(values)
        destination["id"] = destination_id
        self._performers[:] = [x for x in self._performers if x["id"] not in source_ids]
        return dict(destination)

    def merge_studios(self, source_ids, destination_id, values):
        self.calls.append(("merge_studios", {"source": source_ids, "destination": destination_id, "values": values}))
        destination = next(x for x in self._studios if x["id"] == destination_id)
        destination.update(values)
        destination["id"] = destination_id
        self._studios[:] = [x for x in self._studios if x["id"] not in source_ids]
        return dict(destination)

    def create_gallery(self, data):
        self.calls.append(("create_gallery", data))
        item = {
            "id": "g1", "title": data.get("title"), "code": data.get("code"),
            "urls": data.get("urls", []), "date": data.get("date"),
            "details": data.get("details"), "photographer": data.get("photographer"),
            "rating100": data.get("rating100"), "organized": data.get("organized", False),
            "files": [], "folder": None, "chapters": [], "studio": None,
            "tags": [], "performers": [], "scenes": [], "custom_fields": {},
        }
        self._galleries.append(item)
        return dict(item)

    def update_gallery(self, data):
        self.calls.append(("update_gallery", data))
        existing = next(x for x in self._galleries if x["id"] == data["id"])
        existing.update({k: v for k, v in data.items() if k not in {"id", "custom_fields", "tag_ids", "performer_ids", "studio_id"}})
        return dict(existing)

    def create_gallery_chapter(self, gallery_id, title, image_index):
        self.calls.append(("create_gallery_chapter", {"gallery_id": gallery_id, "title": title, "image_index": image_index}))
        gallery = next(x for x in self._galleries if x["id"] == gallery_id)
        chapter = {"id": "chapter1", "title": title, "image_index": image_index}
        gallery["chapters"].append(chapter)
        return dict(chapter)

    def create_group(self, data):
        self.calls.append(("create_group", data))
        item = {
            "id": "grp1", "name": data.get("name"), "aliases": data.get("aliases"),
            "duration": data.get("duration"), "date": data.get("date"),
            "rating100": data.get("rating100"), "director": data.get("director"),
            "synopsis": data.get("synopsis"), "urls": data.get("urls", []),
            "studio": None, "tags": [], "sub_groups": [], "containing_groups": [],
            "front_image_path": None, "back_image_path": None, "scene_count": 0,
            "custom_fields": {},
        }
        self._groups.append(item)
        return dict(item)

    def update_group(self, data):
        self.calls.append(("update_group", data))
        existing = next(x for x in self._groups if x["id"] == data["id"])
        existing.update({k: v for k, v in data.items() if k not in {"id", "custom_fields", "tag_ids", "studio_id"}})
        if "sub_groups" in data:
            existing["sub_groups"] = [
                {"group": {"id": item["group_id"], "name": item["group_id"]}, "description": item.get("description")}
                for item in data["sub_groups"]
            ]
        return dict(existing)

    def create_scene_marker(self, data):
        self.calls.append(("create_scene_marker", data))
        return {
            "id": "marker1", "title": data["title"], "seconds": data["seconds"],
            "end_seconds": data.get("end_seconds"),
            "primary_tag": {"id": data["primary_tag_id"], "name": "tag"},
            "tags": [{"id": value, "name": value} for value in data.get("tag_ids", [])],
        }

    def update_scene_marker(self, data):
        self.calls.append(("update_scene_marker", data))
        return {"id": data["id"], **data}

    def increment_image_o(self, image_id, count):
        self.calls.append(("increment_image_o", {"id": image_id, "count": count}))

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

    def test_restore_creates_metadata_only_gallery_group_chapter_and_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._build_export(tmp)
            write_json(tmp, "galleries", "gallery.json", {
                "title": "Old Gallery",
                "tags": ["Big Breasts"],
                "performers": ["Jane Doe"],
                "chapters": [{"title": "Chapter One", "image_index": 0}],
            })
            write_json(tmp, "groups", "group.json", {
                "name": "Series One",
                "tags": ["Big Breasts"],
                "studio": "Artist Studio",
            })
            scene_path = pathlib.Path(tmp) / "scenes" / "scene.json"
            scene = json.loads(scene_path.read_text(encoding="utf-8"))
            scene["galleries"] = [{"title": "Old Gallery"}]
            scene["movies"] = [{"movieName": "Series One", "scene_index": 2}]
            scene["markers"] = [{
                "title": "Marker One", "seconds": "12.5",
                "primary_tag": "Big Breasts", "tags": ["Big Breasts"],
            }]
            scene_path.write_text(json.dumps(scene), encoding="utf-8")

            stash = FakeStash()
            engine = MigrationEngine(stash, pathlib.Path(tmp), dry_run=False)
            stats = engine.run()

            self.assertEqual(stats["created_galleries"], 1)
            self.assertEqual(stats["created_groups"], 1)
            self.assertEqual(stats["gallery_chapters_created"], 1)
            self.assertEqual(stats["scene_markers_created"], 1)
            update = next(data for name, data in stash.calls if name == "update_scene")
            self.assertEqual(update["gallery_ids"], ["g1"])
            self.assertEqual(update["groups"], [{"group_id": "grp1", "scene_index": 2}])
            self.assertTrue(any(name == "create_scene_marker" for name, _ in stash.calls))

    def test_restore_backs_up_before_first_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._build_export(tmp)
            stash = FakeStash()
            MigrationEngine(stash, pathlib.Path(tmp), dry_run=False).run()
            first_write = next(
                index for index, (name, _) in enumerate(stash.calls)
                if name in {
                    "update_tag", "create_tag", "update_performer", "create_performer",
                    "update_studio", "create_studio", "update_scene", "create_gallery",
                    "create_group", "merge_tags", "merge_performers", "merge_studios",
                }
            )
            backup_index = next(index for index, (name, _) in enumerate(stash.calls) if name == "backup_database")
            self.assertLess(backup_index, first_write)

    def test_low_media_match_rate_aborts_before_backup_or_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            for index in range(100):
                write_json(tmp, "scenes", f"scene{index}.json", {
                    "title": f"Missing {index}",
                    "files": [f"/missing/{index}.mp4"],
                })
            pathlib.Path(tmp, "files").mkdir(parents=True, exist_ok=True)
            stash = FakeStash()
            engine = MigrationEngine(stash, pathlib.Path(tmp), dry_run=False)
            with self.assertRaises(RuntimeError):
                engine.run()
            self.assertEqual(stash.calls, [])

    def test_existing_primary_tag_is_attached_to_image_even_when_alias_is_ambiguous_and_stash_id_conflicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._build_export(tmp)
            write_json(tmp, "files", "image.json", {
                "type": "image", "path": r"D:\Old\image.jpg", "size": 456,
                "fingerprints": [{"type": "md5", "fingerprint": "feedface"}],
            })
            write_json(tmp, "tags", "pov.json", {
                "name": "POV",
                "stash_ids": [{"endpoint": "https://box.example", "stash_id": "old-pov"}],
            })
            write_json(tmp, "images", "image.json", {
                "title": "POV test",
                "files": [r"D:\Old\image.jpg"],
                "tags": ["POV"],
            })

            stash = FakeStash()
            stash._tags.extend([
                {
                    "id": "t-pov", "name": "POV", "aliases": [],
                    "sort_name": None, "description": None, "favorite": False,
                    "ignore_auto_tag": False,
                    "stash_ids": [{"endpoint": "https://box.example", "stash_id": "current-pov"}],
                    "custom_fields": {}, "parents": [], "children": [], "image_path": None,
                    "scene_count": 0, "scene_marker_count": 0, "image_count": 10,
                    "gallery_count": 0, "performer_count": 0, "studio_count": 0,
                    "group_count": 0,
                },
                {
                    "id": "t-other", "name": "point_of_view", "aliases": ["POV"],
                    "sort_name": None, "description": None, "favorite": False,
                    "ignore_auto_tag": False, "stash_ids": [], "custom_fields": {},
                    "parents": [], "children": [], "image_path": None,
                    "scene_count": 0, "scene_marker_count": 0, "image_count": 1,
                    "gallery_count": 0, "performer_count": 0, "studio_count": 0,
                    "group_count": 0,
                },
            ])
            stash._images.append({
                "id": "image1", "title": None, "code": None, "details": None,
                "photographer": None, "urls": [], "date": None, "rating100": None,
                "organized": False, "o_counter": 0, "studio": None,
                "tags": [], "performers": [], "galleries": [], "custom_fields": {},
                "files": [{
                    "id": "if1", "path": "/new/image.jpg", "size": 456,
                    "fingerprints": [{"type": "md5", "value": "feedface"}],
                }],
            })

            engine = MigrationEngine(stash, pathlib.Path(tmp), dry_run=False)
            stats = engine.run()

            image_update = next(
                data for name, data in stash.calls
                if name == "update_image" and data["id"] == "image1"
            )
            self.assertEqual(image_update["tag_ids"], ["t-pov"])
            self.assertEqual(stats["tag_identity_conflict_relationship_reuse"], 1)
            self.assertFalse(any(
                name == "update_tag" and data.get("id") == "t-pov"
                for name, data in stash.calls
            ))

    def test_exact_stash_id_reuses_existing_tag_before_creating_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._build_export(tmp)
            write_json(tmp, "tags", "legacy-tag.json", {
                "name": "Legacy Tag Name",
                "stash_ids": [{"endpoint": "https://tags.example", "stash_id": "42"}],
            })

            stash = FakeStash()
            stash._tags.append({
                "id": "t-canonical", "name": "Canonical Tag", "aliases": [],
                "sort_name": None, "description": None, "favorite": False,
                "ignore_auto_tag": False,
                "stash_ids": [{"endpoint": "https://tags.example", "stash_id": "42"}],
                "custom_fields": {}, "parents": [], "children": [],
                "image_path": None,
            })

            engine = MigrationEngine(stash, pathlib.Path(tmp), dry_run=False)
            resolved = engine.resolve_tag("Legacy Tag Name")

            self.assertEqual(resolved, "t-canonical")
            self.assertEqual(engine.stats["stash_id_tag_reuse"], 1)
            self.assertEqual(engine.stats["created_tags"], 0)
            self.assertFalse(any(name == "create_tag" for name, _ in stash.calls))

    def test_plausible_existing_tag_blocks_new_tag_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._build_export(tmp)
            write_json(tmp, "tags", "near-tag.json", {
                "name": "Big Brests",
            })

            stash = FakeStash()
            engine = MigrationEngine(stash, pathlib.Path(tmp), dry_run=False)
            resolved = engine.resolve_tag("Big Brests")

            self.assertIsNone(resolved)
            self.assertEqual(engine.stats["tag_creation_guard_skips"], 1)
            self.assertEqual(engine.stats["created_tags"], 0)
            self.assertFalse(any(name == "create_tag" for name, _ in stash.calls))

    def test_primary_tag_name_beats_different_external_id_match_without_metadata_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._build_export(tmp)
            write_json(tmp, "tags", "3d.json", {
                "name": "3d",
                "aliases": ["three dee"],
                "description": "old description",
                "stash_ids": [{"endpoint": "https://tags.example", "stash_id": "old-3d"}],
            })

            stash = FakeStash()
            stash._tags.extend([
                {
                    "id": "t-3d", "name": "3d", "aliases": [],
                    "sort_name": None, "description": None, "favorite": False,
                    "ignore_auto_tag": False, "stash_ids": [], "custom_fields": {},
                    "parents": [], "children": [], "image_path": None,
                },
                {
                    "id": "t-external", "name": "3D Render", "aliases": [],
                    "sort_name": None, "description": None, "favorite": False,
                    "ignore_auto_tag": False,
                    "stash_ids": [{"endpoint": "https://tags.example", "stash_id": "old-3d"}],
                    "custom_fields": {}, "parents": [], "children": [], "image_path": None,
                },
            ])

            engine = MigrationEngine(stash, pathlib.Path(tmp), dry_run=False)
            resolved = engine.resolve_tag("3d")

            self.assertEqual(resolved, "t-3d")
            self.assertEqual(engine.stats["tag_identity_conflict_relationship_reuse"], 1)
            self.assertFalse(any(
                name == "update_tag" and data.get("id") == "t-3d"
                for name, data in stash.calls
            ))

    def test_conflicting_old_alias_is_filtered_instead_of_failing_tag_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._build_export(tmp)
            write_json(tmp, "tags", "legacy.json", {
                "name": "Legacy Name",
                "aliases": ["3d", "safe alias"],
                "stash_ids": [{"endpoint": "https://tags.example", "stash_id": "legacy-42"}],
            })

            stash = FakeStash()
            stash._tags.extend([
                {
                    "id": "t-3d", "name": "3d", "aliases": [],
                    "sort_name": None, "description": None, "favorite": False,
                    "ignore_auto_tag": False, "stash_ids": [], "custom_fields": {},
                    "parents": [], "children": [], "image_path": None,
                },
                {
                    "id": "t-canonical", "name": "Canonical Name", "aliases": [],
                    "sort_name": None, "description": None, "favorite": False,
                    "ignore_auto_tag": False,
                    "stash_ids": [{"endpoint": "https://tags.example", "stash_id": "legacy-42"}],
                    "custom_fields": {}, "parents": [], "children": [], "image_path": None,
                },
            ])

            engine = MigrationEngine(stash, pathlib.Path(tmp), dry_run=False)
            resolved = engine.resolve_tag("Legacy Name")

            self.assertEqual(resolved, "t-canonical")
            update = next(
                data for name, data in stash.calls
                if name == "update_tag" and data.get("id") == "t-canonical"
            )
            self.assertNotIn("3d", [value.casefold() for value in update["aliases"]])
            self.assertIn("safe alias", [value.casefold() for value in update["aliases"]])
            self.assertGreaterEqual(engine.stats["tag_alias_collision_skips"], 1)

    def test_raw_jpeg_tag_artwork_is_wrapped_as_data_uri(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._build_export(tmp)
            tag_path = pathlib.Path(tmp) / "tags" / "tag.json"
            tag = json.loads(tag_path.read_text(encoding="utf-8"))
            tag["image"] = "/9j/2Q=="
            tag_path.write_text(json.dumps(tag), encoding="utf-8")

            stash = FakeStash()
            stash._tags[0].update({
                "parents": [], "children": [],
                "image_path": "/tag/t1/image?default=true",
                "scene_count": 0, "scene_marker_count": 0, "image_count": 1,
                "gallery_count": 0, "performer_count": 0, "studio_count": 0,
                "group_count": 0,
            })

            engine = MigrationEngine(stash, pathlib.Path(tmp), dry_run=False)
            engine.resolve_tag("Big Breasts")

            update = next(
                data for name, data in stash.calls
                if name == "update_tag" and data.get("id") == "t1"
            )
            self.assertEqual(update["image"], "data:image/jpeg;base64,/9j/2Q==")
            self.assertEqual(engine.stats["entity_images_restored"], 1)

    def test_invalid_tag_artwork_is_skipped_without_aborting_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._build_export(tmp)
            tag_path = pathlib.Path(tmp) / "tags" / "tag.json"
            tag = json.loads(tag_path.read_text(encoding="utf-8"))
            tag["image"] = "this-is-not-an-image"
            tag["description"] = "restore me"
            tag_path.write_text(json.dumps(tag), encoding="utf-8")

            stash = FakeStash()
            stash._tags[0].update({
                "parents": [], "children": [],
                "image_path": "/tag/t1/image?default=true",
                "scene_count": 0, "scene_marker_count": 0, "image_count": 1,
                "gallery_count": 0, "performer_count": 0, "studio_count": 0,
                "group_count": 0,
            })

            engine = MigrationEngine(stash, pathlib.Path(tmp), dry_run=False)
            engine.resolve_tag("Big Breasts")

            update = next(
                data for name, data in stash.calls
                if name == "update_tag" and data.get("id") == "t1"
            )
            self.assertNotIn("image", update)
            self.assertEqual(update["description"], "restore me")
            self.assertEqual(engine.stats["entity_images_skipped_invalid"], 1)

    def test_safe_duplicate_tags_are_collapsed_during_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._build_export(tmp)
            stash = FakeStash()
            duplicate = dict(stash._tags[0])
            duplicate.update({"id": "t2", "name": "Big Breasts", "aliases": [], "image_count": 0})
            stash._tags[0].update({
                "image_count": 5, "scene_count": 0, "scene_marker_count": 0,
                "gallery_count": 0, "performer_count": 0, "studio_count": 0,
                "group_count": 0, "parents": [], "children": [], "image_path": None,
            })
            duplicate.update({
                "scene_count": 0, "scene_marker_count": 0, "gallery_count": 0,
                "performer_count": 0, "studio_count": 0, "group_count": 0,
                "parents": [], "children": [], "image_path": None,
            })
            stash._tags.append(duplicate)
            engine = MigrationEngine(stash, pathlib.Path(tmp), dry_run=False)
            resolved = engine.resolve_tag("Big Breasts")
            self.assertEqual(resolved, "t1")
            self.assertEqual(engine.stats["merged_duplicate_tags"], 1)
            self.assertTrue(any(name == "merge_tags" for name, _ in stash.calls))

    def test_backup_failure_aborts_before_metadata_mutations(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._build_export(tmp)
            stash = FakeStash()
            stash.fail_backup = True
            engine = MigrationEngine(stash, pathlib.Path(tmp), dry_run=False)
            with self.assertRaises(RuntimeError):
                engine.run()
            self.assertEqual(stash.calls, [("backup_database", {})])

    def test_gallery_mapping_can_be_inferred_from_uniquely_matched_scene_relationship(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._build_export(tmp)
            write_json(tmp, "galleries", "gallery.json", {
                "folder_path": r"D:\Old\Gallery",
                "title": "Moved Gallery",
            })
            scene_path = pathlib.Path(tmp) / "scenes" / "scene.json"
            scene = json.loads(scene_path.read_text(encoding="utf-8"))
            scene["galleries"] = [{"folder_path": r"D:\Old\Gallery"}]
            scene_path.write_text(json.dumps(scene), encoding="utf-8")

            stash = FakeStash()
            stash._galleries = [{
                "id": "g-existing", "title": "Moved Gallery", "code": None,
                "urls": [], "date": None, "details": None, "photographer": None,
                "rating100": None, "organized": False, "files": [],
                "folder": {"id": "folder-new", "path": "/new/Gallery"},
                "chapters": [], "studio": None, "tags": [], "performers": [],
                "scenes": [{"id": "scene1"}], "custom_fields": {},
            }]
            stash._scenes[0]["galleries"] = [{"id": "g-existing", "title": "Moved Gallery"}]

            engine = MigrationEngine(stash, pathlib.Path(tmp), dry_run=True)
            engine.run()
            key = engine._gallery_relation_key({"folder_path": r"D:\Old\Gallery"})
            self.assertEqual(engine.inferred_gallery_map[key], "g-existing")
            self.assertEqual(engine.stats["unresolved_galleries"], 0)

    def test_tag_hierarchy_and_missing_entity_artwork_are_restored(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._build_export(tmp)
            parent_path = pathlib.Path(tmp) / "tags" / "parent.json"
            parent_path.write_text(json.dumps({"name": "Body"}), encoding="utf-8")
            tag_path = pathlib.Path(tmp) / "tags" / "tag.json"
            tag = json.loads(tag_path.read_text(encoding="utf-8"))
            tag["parents"] = ["Body"]
            tag["image"] = "data:image/png;base64,AAAA"
            tag_path.write_text(json.dumps(tag), encoding="utf-8")

            stash = FakeStash()
            stash._tags[0].update({
                "parents": [], "children": [], "image_path": "/tag/t1/image?default=true",
                "scene_count": 0, "scene_marker_count": 0, "image_count": 1,
                "gallery_count": 0, "performer_count": 0, "studio_count": 0,
                "group_count": 0,
            })
            engine = MigrationEngine(stash, pathlib.Path(tmp), dry_run=False)
            engine.run()

            updates = [data for name, data in stash.calls if name == "update_tag"]
            self.assertTrue(any(data.get("image") == "data:image/png;base64,AAAA" for data in updates))
            self.assertTrue(any("parent_ids" in data for data in updates))
            self.assertEqual(engine.stats["tag_parent_links_added"], 1)

    def test_safe_duplicate_performers_are_collapsed_during_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._build_export(tmp)
            stash = FakeStash()
            base = stash._performers[0]
            base.update({
                "scene_count": 4, "image_count": 0, "gallery_count": 0, "group_count": 0,
                "image_path": "/performer/p1/image?default=true",
            })
            duplicate = dict(base)
            duplicate.update({
                "id": "p2", "name": "Jane Doe", "alias_list": [],
                "scene_count": 0,
            })
            stash._performers.append(duplicate)
            engine = MigrationEngine(stash, pathlib.Path(tmp), dry_run=False)
            resolved = engine.resolve_performer("Jane Doe")
            self.assertEqual(resolved, "p1")
            self.assertEqual(engine.stats["merged_duplicate_performers"], 1)
            self.assertTrue(any(name == "merge_performers" for name, _ in stash.calls))

    def test_safe_duplicate_studios_are_collapsed_during_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._build_export(tmp)
            stash = FakeStash()
            base = stash._studios[0]
            base.update({
                "scene_count": 4, "image_count": 0, "gallery_count": 0, "group_count": 0,
                "image_path": "/studio/s1/image?default=true", "child_studios": [],
            })
            duplicate = dict(base)
            duplicate.update({
                "id": "s2", "name": "Artist Studio", "aliases": [],
                "scene_count": 0,
            })
            stash._studios.append(duplicate)
            engine = MigrationEngine(stash, pathlib.Path(tmp), dry_run=False)
            resolved = engine.resolve_studio("Artist Studio")
            self.assertEqual(resolved, "s1")
            self.assertEqual(engine.stats["merged_duplicate_studios"], 1)
            self.assertTrue(any(name == "merge_studios" for name, _ in stash.calls))

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
