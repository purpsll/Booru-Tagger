#!/usr/bin/env python3
"""Restore a Stash JSON export onto media already scanned into another Stash.

Design goal: never create Scene/Image/File records. Old JSON is treated only as
metadata. Media matching is fingerprint-first and must be unique.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from migration_core import (
    build_current_media_indexes,
    build_old_file_index,
    decide_entity_match,
    load_json_files,
    match_old_media,
    merge_stash_ids,
    merge_strings,
    normalize_name,
    open_export_source,
    scalar_fill,
    source_name_map,
)
from stash_client import Stash
from dedup import (
    merged_entity_values,
    safe_formatting_merge,
    tag_alias_preflight,
)


TAG_THRESHOLD = 0.96
TAG_MARGIN = 0.02
PERFORMER_THRESHOLD = 0.98
PERFORMER_MARGIN = 0.03
STUDIO_THRESHOLD = 0.96
STUDIO_MARGIN = 0.03

_STASH_LOG_LEVEL_CHARS = {
    "TRACE": "t",
    "DEBUG": "d",
    "INFO": "i",
    "WARNING": "w",
    "WARN": "w",
    "ERROR": "e",
}


def _stash_log_prefix(level_char: str) -> str:
    return "\x01" + level_char + "\x02"


def log(level: str, message: str) -> None:
    level_char = _STASH_LOG_LEVEL_CHARS.get(str(level).upper(), "i")
    print(f"{_stash_log_prefix(level_char)}{message}", file=sys.stderr, flush=True)


def progress(value: float) -> None:
    value = max(0.0, min(1.0, float(value)))
    print(f"{_stash_log_prefix('p')}{value:.6f}", file=sys.stderr, flush=True)


def read_input() -> Dict[str, Any]:
    raw = sys.stdin.read()
    return json.loads(raw) if raw.strip() else {}


def _clean_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _clean_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _custom_field_delta(current: Any, old: Any) -> Dict[str, Any]:
    current_map = current if isinstance(current, dict) else {}
    old_map = old if isinstance(old, dict) else {}
    return {key: value for key, value in old_map.items() if key not in current_map}


def _same_endpoint_identity_conflict(
    current: Iterable[Mapping[str, Any]],
    old: Iterable[Mapping[str, Any]],
) -> bool:
    current_map = {
        str(item.get("endpoint") or "").casefold().strip(): str(item.get("stash_id") or "").strip()
        for item in current or []
        if str(item.get("endpoint") or "").strip() and str(item.get("stash_id") or "").strip()
    }
    for item in old or []:
        endpoint = str(item.get("endpoint") or "").casefold().strip()
        stash_id = str(item.get("stash_id") or "").strip()
        if endpoint and stash_id and endpoint in current_map and current_map[endpoint] != stash_id:
            return True
    return False


def _safe_stash_ids(
    current: Iterable[Mapping[str, Any]],
    old: Iterable[Mapping[str, Any]],
) -> Tuple[List[Dict[str, str]], int]:
    current_list = merge_stash_ids(current)
    by_endpoint = {
        str(item["endpoint"]).casefold(): str(item["stash_id"])
        for item in current_list
    }
    out = list(current_list)
    conflicts = 0
    for item in merge_stash_ids(old):
        endpoint_key = str(item["endpoint"]).casefold()
        existing = by_endpoint.get(endpoint_key)
        if existing is not None and existing != str(item["stash_id"]):
            conflicts += 1
            continue
        if existing is None:
            out.append(item)
            by_endpoint[endpoint_key] = str(item["stash_id"])
    return out, conflicts


def _unique_ids(values: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for value in values:
        text = str(value or "")
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


class MigrationEngine:
    def __init__(
        self,
        stash: Stash,
        root: Path,
        *,
        dry_run: bool,
    ):
        self.stash = stash
        self.root = root
        self.dry_run = dry_run

        self.old_tags = load_json_files(root / "tags")
        self.old_performers = load_json_files(root / "performers")
        self.old_studios = load_json_files(root / "studios")
        self.old_galleries = load_json_files(root / "galleries")
        self.old_groups = load_json_files(root / "groups")
        if not self.old_groups:
            self.old_groups = load_json_files(root / "movies")
        self.old_scenes = load_json_files(root / "scenes")
        self.old_images = load_json_files(root / "images")
        self.old_files = build_old_file_index(root)

        self.old_tags_by_name = source_name_map(self.old_tags)
        self.old_performers_by_name = source_name_map(self.old_performers)
        self.old_studios_by_name = source_name_map(self.old_studios)
        self.old_groups_by_name = source_name_map(self.old_groups)

        performer_name_counts: Dict[str, int] = {}
        for performer in self.old_performers:
            name_key = str(performer.get("name") or "").casefold().strip()
            if name_key:
                performer_name_counts[name_key] = performer_name_counts.get(name_key, 0) + 1
        self.ambiguous_old_performer_names = {
            key for key, count in performer_name_counts.items() if count > 1
        }
        self._warned_ambiguous_old_performers = set()

        self.tags = stash.tags()
        self.performers = stash.performers()
        self.studios = stash.studios()
        self.galleries = stash.galleries()
        self.groups = stash.groups()
        self.scenes = stash.scenes()
        self.images = stash.images()

        self.scenes_by_id = {str(item["id"]): item for item in self.scenes}
        self.images_by_id = {str(item["id"]): item for item in self.images}
        self.galleries_by_id = {str(item["id"]): item for item in self.galleries}
        self.groups_by_id = {str(item["id"]): item for item in self.groups}

        self.scene_fp_index, self.scene_path_index = build_current_media_indexes(self.scenes)
        self.image_fp_index, self.image_path_index = build_current_media_indexes(self.images)
        self.gallery_fp_index, self.gallery_path_index = build_current_media_indexes(self.galleries)
        self.gallery_folder_index: Dict[str, List[str]] = {}
        for gallery in self.galleries:
            folder = gallery.get("folder") or {}
            folder_path = str(folder.get("path") or "").strip()
            if folder_path:
                from migration_core import normalized_path
                self.gallery_folder_index.setdefault(normalized_path(folder_path), []).append(str(gallery["id"]))

        self.resolved_tags: Dict[str, Optional[str]] = {}
        self.resolved_performers: Dict[str, Optional[str]] = {}
        self.resolved_studios: Dict[str, Optional[str]] = {}
        self.resolved_galleries: Dict[str, Optional[str]] = {}
        self.resolved_groups: Dict[str, Optional[str]] = {}
        self._studio_resolution_stack = set()
        self._group_resolution_stack = set()

        self.stats: Dict[str, int] = {
            "source_files": len(self.old_files),
            "source_scenes": len(self.old_scenes),
            "source_images": len(self.old_images),
            "source_tags": len(self.old_tags),
            "source_performers": len(self.old_performers),
            "source_studios": len(self.old_studios),
            "source_galleries": len(self.old_galleries),
            "source_groups": len(self.old_groups),
            "matched_scenes": 0,
            "matched_images": 0,
            "updated_scenes": 0,
            "updated_images": 0,
            "unmatched_scenes": 0,
            "unmatched_images": 0,
            "ambiguous_scenes": 0,
            "ambiguous_images": 0,
            "source_target_collisions": 0,
            "created_tags": 0,
            "created_performers": 0,
            "created_studios": 0,
            "created_galleries": 0,
            "created_groups": 0,
            "reused_tags": 0,
            "reused_performers": 0,
            "reused_studios": 0,
            "reused_galleries": 0,
            "reused_groups": 0,
            "fuzzy_tag_reuse": 0,
            "fuzzy_performer_reuse": 0,
            "fuzzy_studio_reuse": 0,
            "merged_duplicate_tags": 0,
            "merged_duplicate_performers": 0,
            "merged_duplicate_studios": 0,
            "gallery_chapters_created": 0,
            "scene_markers_created": 0,
            "image_o_increments": 0,
            "unresolved_galleries": 0,
            "ambiguous_galleries": 0,
            "entity_identity_conflicts": 0,
            "ambiguous_entities_skipped": 0,
            "errors": 0,
        }

    def _source_entity(self, mapping: Dict[str, Dict[str, Any]], name: str) -> Dict[str, Any]:
        return mapping.get(str(name or "").casefold(), {"name": name})

    def resolve_tag(self, name: str) -> Optional[str]:
        key = normalize_name(name)
        if not key:
            return None
        if key in self.resolved_tags:
            return self.resolved_tags[key]

        decision = decide_entity_match(
            name,
            self.tags,
            alias_field="aliases",
            allow_fuzzy=False,
            threshold=1.0,
            margin=1.0,
        )
        source = self._source_entity(self.old_tags_by_name, name)
        if decision.ambiguous:
            self.stats["ambiguous_entities_skipped"] += 1
            log("WARNING", f"Tag '{name}' has ambiguous current matches; relationship skipped.")
            self.resolved_tags[key] = None
            return None
        match = decision.match
        if match and _same_endpoint_identity_conflict(
            match.entity.get("stash_ids") or [], source.get("stash_ids") or []
        ):
            self.stats["entity_identity_conflicts"] += 1
            log("WARNING", f"Tag '{name}' has a conflicting same-endpoint Stash ID; relationship skipped.")
            self.resolved_tags[key] = None
            return None
        if match:
            self.stats["reused_tags"] += 1
            entity = match.entity
            if not self.dry_run:
                entity = self._merge_tag_metadata(entity, source)
                self._replace_entity(self.tags, entity)
            tag_id = str(entity["id"])
            self.resolved_tags[key] = tag_id
            return tag_id

        if self.dry_run:
            self.stats["created_tags"] += 1
            synthetic_id = f"DRYRUN:TAG:{key}"
            self.resolved_tags[key] = synthetic_id
            return synthetic_id

        created = self.stash.create_tag(self._tag_create_input(source, name))
        self.tags.append(created)
        self.stats["created_tags"] += 1
        tag_id = str(created["id"])
        self.resolved_tags[key] = tag_id
        return tag_id

    def _tag_create_input(self, source: Dict[str, Any], fallback_name: str) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "name": str(source.get("name") or fallback_name).strip(),
            "aliases": merge_strings(source.get("aliases") or []),
            "favorite": bool(source.get("favorite")),
            "ignore_auto_tag": bool(source.get("ignore_auto_tag")),
            "stash_ids": merge_stash_ids(source.get("stash_ids") or []),
        }
        for src, dst in (("sort_name", "sort_name"), ("description", "description")):
            value = str(source.get(src) or "").strip()
            if value:
                data[dst] = value
        if isinstance(source.get("custom_fields"), dict) and source["custom_fields"]:
            data["custom_fields"] = source["custom_fields"]
        return data

    def _merge_tag_metadata(self, current: Dict[str, Any], source: Dict[str, Any]) -> Dict[str, Any]:
        aliases = merge_strings(
            current.get("aliases") or [],
            source.get("aliases") or [],
            [str(source.get("name") or "")],
        )
        aliases = [a for a in aliases if a.casefold() != str(current.get("name") or "").casefold()]
        stash_ids, conflicts = _safe_stash_ids(
            current.get("stash_ids") or [], source.get("stash_ids") or []
        )
        self.stats["entity_identity_conflicts"] += conflicts
        delta = _custom_field_delta(current.get("custom_fields"), source.get("custom_fields"))
        input_data: Dict[str, Any] = {
            "id": str(current["id"]),
            "aliases": aliases,
            "favorite": bool(current.get("favorite") or source.get("favorite")),
            "ignore_auto_tag": bool(current.get("ignore_auto_tag") or source.get("ignore_auto_tag")),
            "stash_ids": stash_ids,
        }
        sort_name = scalar_fill(current.get("sort_name"), source.get("sort_name"))
        description = scalar_fill(current.get("description"), source.get("description"))
        if sort_name:
            input_data["sort_name"] = sort_name
        if description:
            input_data["description"] = description
        if delta:
            input_data["custom_fields"] = {"partial": delta}
        return self.stash.update_tag(input_data)

    def resolve_performer(self, name: str) -> Optional[str]:
        key = normalize_name(name)
        if not key:
            return None
        if key in self.resolved_performers:
            return self.resolved_performers[key]

        if str(name or "").casefold().strip() in self.ambiguous_old_performer_names:
            if key not in self._warned_ambiguous_old_performers:
                log(
                    "WARNING",
                    f"Old export contains multiple Performers named '{name}' with different identities; "
                    "name-only media relationship cannot be resolved safely and will be skipped.",
                )
                self._warned_ambiguous_old_performers.add(key)
            self.stats["ambiguous_entities_skipped"] += 1
            self.resolved_performers[key] = None
            return None

        decision = decide_entity_match(
            name,
            self.performers,
            alias_field="alias_list",
            allow_fuzzy=True,
            threshold=PERFORMER_THRESHOLD,
            margin=PERFORMER_MARGIN,
        )
        source = self._source_entity(self.old_performers_by_name, name)
        if decision.ambiguous:
            self.stats["ambiguous_entities_skipped"] += 1
            log("WARNING", f"Performer '{name}' has ambiguous current matches; relationship skipped.")
            self.resolved_performers[key] = None
            return None
        match = decision.match
        if match:
            current_disambiguation = str(match.entity.get("disambiguation") or "").casefold().strip()
            source_disambiguation = str(source.get("disambiguation") or "").casefold().strip()
            identity_conflict = _same_endpoint_identity_conflict(
                match.entity.get("stash_ids") or [], source.get("stash_ids") or []
            )
            if (
                identity_conflict
                or (
                    current_disambiguation
                    and source_disambiguation
                    and current_disambiguation != source_disambiguation
                )
            ):
                self.stats["entity_identity_conflicts"] += 1
                log(
                    "WARNING",
                    f"Performer '{name}' matched by name but identity metadata conflicts; "
                    "relationship skipped instead of merging or creating a duplicate.",
                )
                self.resolved_performers[key] = None
                return None
            self.stats["reused_performers"] += 1
            if match.kind == "fuzzy":
                self.stats["fuzzy_performer_reuse"] += 1
                log(
                    "INFO",
                    f"Performer fuzzy reuse: '{name}' -> '{match.entity.get('name')}' "
                    f"({match.score * 100:.1f}%, next {match.second_score * 100:.1f}%).",
                )
            entity = match.entity
            if not self.dry_run:
                entity = self._merge_performer_metadata(entity, source)
                self._replace_entity(self.performers, entity)
            performer_id = str(entity["id"])
            self.resolved_performers[key] = performer_id
            return performer_id

        if self.dry_run:
            self.stats["created_performers"] += 1
            synthetic_id = f"DRYRUN:PERFORMER:{key}"
            self.resolved_performers[key] = synthetic_id
            return synthetic_id

        created = self.stash.create_performer(self._performer_create_input(source, name))
        self.performers.append(created)
        self.stats["created_performers"] += 1
        performer_id = str(created["id"])
        self.resolved_performers[key] = performer_id
        return performer_id

    def _performer_common_input(
        self,
        source: Dict[str, Any],
        *,
        current: Optional[Dict[str, Any]] = None,
        fallback_name: str = "",
    ) -> Dict[str, Any]:
        current = current or {}
        aliases = merge_strings(
            current.get("alias_list") or [],
            source.get("aliases") or [],
            [str(source.get("name") or fallback_name)],
        )
        current_name = str(current.get("name") or source.get("name") or fallback_name)
        aliases = [a for a in aliases if a.casefold() != current_name.casefold()]

        urls = merge_strings(
            current.get("urls") or [],
            source.get("urls") or [],
            [source.get("url") or "", source.get("twitter") or "", source.get("instagram") or ""],
        )
        stash_ids, conflicts = _safe_stash_ids(
            current.get("stash_ids") or [], source.get("stash_ids") or []
        )
        self.stats["entity_identity_conflicts"] += conflicts

        data: Dict[str, Any] = {
            "alias_list": aliases,
            "urls": urls,
            "favorite": bool(current.get("favorite") or source.get("favorite")),
            "ignore_auto_tag": bool(current.get("ignore_auto_tag") or source.get("ignore_auto_tag")),
            "stash_ids": stash_ids,
        }

        mapping = (
            ("disambiguation", "disambiguation"),
            ("gender", "gender"),
            ("birthdate", "birthdate"),
            ("ethnicity", "ethnicity"),
            ("country", "country"),
            ("eye_color", "eye_color"),
            ("measurements", "measurements"),
            ("fake_tits", "fake_tits"),
            ("circumcised", "circumcised"),
            ("career_start", "career_start"),
            ("career_end", "career_end"),
            ("tattoos", "tattoos"),
            ("piercings", "piercings"),
            ("details", "details"),
            ("death_date", "death_date"),
            ("hair_color", "hair_color"),
        )
        for src, dst in mapping:
            value = scalar_fill(current.get(dst), source.get(src))
            if value not in (None, ""):
                data[dst] = value

        height = current.get("height_cm")
        if height in (None, 0):
            height = _clean_int(source.get("height"))
        if height not in (None, 0):
            data["height_cm"] = height

        for src, dst in (("penis_length", "penis_length"), ("rating", "rating100"), ("weight", "weight")):
            current_value = current.get(dst)
            old_value = source.get(src)
            if dst == "penis_length":
                old_value = _clean_float(old_value)
            else:
                old_value = _clean_int(old_value)
            value = scalar_fill(current_value, old_value)
            if value not in (None, "", 0):
                data[dst] = value

        tag_ids = [str(t.get("id")) for t in (current.get("tags") or []) if t.get("id")]
        for tag_name in source.get("tags") or []:
            resolved = self.resolve_tag(str(tag_name))
            if resolved:
                tag_ids.append(resolved)
        if tag_ids:
            data["tag_ids"] = _unique_ids(tag_ids)

        custom_delta = _custom_field_delta(current.get("custom_fields"), source.get("custom_fields"))
        if current:
            if custom_delta:
                data["custom_fields"] = {"partial": custom_delta}
        elif isinstance(source.get("custom_fields"), dict) and source["custom_fields"]:
            data["custom_fields"] = source["custom_fields"]
        return data

    def _performer_create_input(self, source: Dict[str, Any], fallback_name: str) -> Dict[str, Any]:
        data = self._performer_common_input(source, fallback_name=fallback_name)
        data["name"] = str(source.get("name") or fallback_name).strip()
        return data

    def _merge_performer_metadata(self, current: Dict[str, Any], source: Dict[str, Any]) -> Dict[str, Any]:
        data = self._performer_common_input(source, current=current)
        data["id"] = str(current["id"])
        return self.stash.update_performer(data)

    def resolve_studio(self, name: str) -> Optional[str]:
        key = normalize_name(name)
        if not key:
            return None
        if key in self.resolved_studios:
            return self.resolved_studios[key]
        if key in self._studio_resolution_stack:
            return None

        self._studio_resolution_stack.add(key)
        try:
            decision = decide_entity_match(
                name,
                self.studios,
                alias_field="aliases",
                allow_fuzzy=True,
                threshold=STUDIO_THRESHOLD,
                margin=STUDIO_MARGIN,
            )
            source = self._source_entity(self.old_studios_by_name, name)
            if decision.ambiguous:
                self.stats["ambiguous_entities_skipped"] += 1
                log("WARNING", f"Studio '{name}' has ambiguous current matches; relationship skipped.")
                self.resolved_studios[key] = None
                return None
            match = decision.match
            if match and _same_endpoint_identity_conflict(
                match.entity.get("stash_ids") or [], source.get("stash_ids") or []
            ):
                self.stats["entity_identity_conflicts"] += 1
                log(
                    "WARNING",
                    f"Studio '{name}' matched by name but has a conflicting same-endpoint "
                    "Stash ID; relationship skipped instead of merging or duplicating it.",
                )
                self.resolved_studios[key] = None
                return None
            if match:
                self.stats["reused_studios"] += 1
                if match.kind == "fuzzy":
                    self.stats["fuzzy_studio_reuse"] += 1
                    log(
                        "INFO",
                        f"Studio fuzzy reuse: '{name}' -> '{match.entity.get('name')}' "
                        f"({match.score * 100:.1f}%, next {match.second_score * 100:.1f}%).",
                    )
                entity = match.entity
                if not self.dry_run:
                    entity = self._merge_studio_metadata(entity, source)
                    self._replace_entity(self.studios, entity)
                studio_id = str(entity["id"])
                self.resolved_studios[key] = studio_id
                return studio_id

            if self.dry_run:
                self.stats["created_studios"] += 1
                synthetic_id = f"DRYRUN:STUDIO:{key}"
                self.resolved_studios[key] = synthetic_id
                return synthetic_id

            created = self.stash.create_studio(self._studio_create_input(source, name))
            self.studios.append(created)
            self.stats["created_studios"] += 1
            studio_id = str(created["id"])
            self.resolved_studios[key] = studio_id
            return studio_id
        finally:
            self._studio_resolution_stack.discard(key)

    def _studio_common_input(
        self,
        source: Dict[str, Any],
        *,
        current: Optional[Dict[str, Any]] = None,
        fallback_name: str = "",
    ) -> Dict[str, Any]:
        current = current or {}
        current_name = str(current.get("name") or source.get("name") or fallback_name)
        aliases = merge_strings(
            current.get("aliases") or [],
            source.get("aliases") or [],
            [str(source.get("name") or fallback_name)],
        )
        aliases = [a for a in aliases if a.casefold() != current_name.casefold()]
        urls = merge_strings(
            current.get("urls") or [], source.get("urls") or [], [source.get("url") or ""]
        )
        stash_ids, conflicts = _safe_stash_ids(
            current.get("stash_ids") or [], source.get("stash_ids") or []
        )
        self.stats["entity_identity_conflicts"] += conflicts

        data: Dict[str, Any] = {
            "aliases": aliases,
            "urls": urls,
            "favorite": bool(current.get("favorite") or source.get("favorite")),
            "ignore_auto_tag": bool(current.get("ignore_auto_tag") or source.get("ignore_auto_tag")),
            "organized": bool(current.get("organized") or source.get("organized")),
            "stash_ids": stash_ids,
        }
        details = scalar_fill(current.get("details"), source.get("details"))
        if details:
            data["details"] = details
        rating = scalar_fill(current.get("rating100"), _clean_int(source.get("rating")))
        if rating not in (None, "", 0):
            data["rating100"] = rating

        tag_ids = [str(t.get("id")) for t in (current.get("tags") or []) if t.get("id")]
        for tag_name in source.get("tags") or []:
            resolved = self.resolve_tag(str(tag_name))
            if resolved:
                tag_ids.append(resolved)
        if tag_ids:
            data["tag_ids"] = _unique_ids(tag_ids)

        parent_name = str(source.get("parent_studio") or "").strip()
        if parent_name and not current.get("parent_studio"):
            parent_id = self.resolve_studio(parent_name)
            if parent_id:
                data["parent_id"] = parent_id

        custom_delta = _custom_field_delta(current.get("custom_fields"), source.get("custom_fields"))
        if current:
            if custom_delta:
                data["custom_fields"] = {"partial": custom_delta}
        elif isinstance(source.get("custom_fields"), dict) and source["custom_fields"]:
            data["custom_fields"] = source["custom_fields"]
        return data

    def _studio_create_input(self, source: Dict[str, Any], fallback_name: str) -> Dict[str, Any]:
        data = self._studio_common_input(source, fallback_name=fallback_name)
        data["name"] = str(source.get("name") or fallback_name).strip()
        return data

    def _merge_studio_metadata(self, current: Dict[str, Any], source: Dict[str, Any]) -> Dict[str, Any]:
        data = self._studio_common_input(source, current=current)
        data["id"] = str(current["id"])
        return self.stash.update_studio(data)

    @staticmethod
    def _replace_entity(collection: List[Dict[str, Any]], replacement: Dict[str, Any]) -> None:
        replacement_id = str(replacement.get("id") or "")
        for index, item in enumerate(collection):
            if str(item.get("id") or "") == replacement_id:
                collection[index] = replacement
                return
        collection.append(replacement)

    def _relation_ids(
        self,
        current: Sequence[Dict[str, Any]],
        old_names: Sequence[str],
        resolver,
    ) -> List[str]:
        ids = [str(item.get("id")) for item in current if item.get("id")]
        for name in old_names or []:
            resolved = resolver(str(name))
            if resolved:
                ids.append(resolved)
        return _unique_ids(ids)

    def restore_scene(self, old: Dict[str, Any], current: Dict[str, Any]) -> bool:
        input_data: Dict[str, Any] = {"id": str(current["id"])}

        scalar_fields = (
            ("title", "title"),
            ("code", "code"),
            ("details", "details"),
            ("director", "director"),
            ("date", "date"),
        )
        for old_key, current_key in scalar_fields:
            value = scalar_fill(current.get(current_key), old.get(old_key))
            if value not in (None, "") and value != current.get(current_key):
                input_data[current_key] = value

        old_rating = _clean_int(old.get("rating"))
        rating = scalar_fill(current.get("rating100"), old_rating)
        if rating not in (None, "", 0) and rating != current.get("rating100"):
            input_data["rating100"] = rating

        if bool(old.get("organized")) and not bool(current.get("organized")):
            input_data["organized"] = True

        urls = merge_strings(current.get("urls") or [], old.get("urls") or [], [old.get("url") or ""])
        if urls != list(current.get("urls") or []):
            input_data["urls"] = urls

        tag_ids = self._relation_ids(current.get("tags") or [], old.get("tags") or [], self.resolve_tag)
        if tag_ids != [str(item.get("id")) for item in (current.get("tags") or []) if item.get("id")]:
            input_data["tag_ids"] = tag_ids

        performer_ids = self._relation_ids(
            current.get("performers") or [], old.get("performers") or [], self.resolve_performer
        )
        if performer_ids != [
            str(item.get("id")) for item in (current.get("performers") or []) if item.get("id")
        ]:
            input_data["performer_ids"] = performer_ids

        old_studio = str(old.get("studio") or "").strip()
        if old_studio and not current.get("studio"):
            studio_id = self.resolve_studio(old_studio)
            if studio_id:
                input_data["studio_id"] = studio_id

        stash_ids, conflicts = _safe_stash_ids(
            current.get("stash_ids") or [], old.get("stash_ids") or []
        )
        self.stats["entity_identity_conflicts"] += conflicts
        if stash_ids != merge_stash_ids(current.get("stash_ids") or []):
            input_data["stash_ids"] = stash_ids

        resume = scalar_fill(current.get("resume_time"), _clean_float(old.get("resume_time")))
        if resume not in (None, 0, "") and resume != current.get("resume_time"):
            input_data["resume_time"] = resume

        duration = scalar_fill(current.get("play_duration"), _clean_float(old.get("play_duration")))
        if duration not in (None, 0, "") and duration != current.get("play_duration"):
            input_data["play_duration"] = duration

        custom_delta = _custom_field_delta(current.get("custom_fields"), old.get("custom_fields"))
        if custom_delta:
            input_data["custom_fields"] = {"partial": custom_delta}

        changed = len(input_data) > 1
        if changed and not self.dry_run:
            self.stash.update_scene(input_data)

            current_plays = set(str(v) for v in (current.get("play_history") or []))
            old_plays = [
                str(v) for v in (old.get("play_history") or [])
                if str(v) and str(v) not in current_plays
            ]
            current_os = set(str(v) for v in (current.get("o_history") or []))
            old_os = [
                str(v) for v in (old.get("o_history") or [])
                if str(v) and str(v) not in current_os
            ]
            if old_plays:
                self.stash.add_scene_plays(str(current["id"]), old_plays)
            if old_os:
                self.stash.add_scene_os(str(current["id"]), old_os)

        elif not self.dry_run:
            current_plays = set(str(v) for v in (current.get("play_history") or []))
            old_plays = [str(v) for v in (old.get("play_history") or []) if str(v) not in current_plays]
            current_os = set(str(v) for v in (current.get("o_history") or []))
            old_os = [str(v) for v in (old.get("o_history") or []) if str(v) not in current_os]
            if old_plays:
                self.stash.add_scene_plays(str(current["id"]), old_plays)
                changed = True
            if old_os:
                self.stash.add_scene_os(str(current["id"]), old_os)
                changed = True

        return changed

    def restore_image(self, old: Dict[str, Any], current: Dict[str, Any]) -> bool:
        input_data: Dict[str, Any] = {"id": str(current["id"])}
        scalar_fields = (
            ("title", "title"),
            ("code", "code"),
            ("details", "details"),
            ("photographer", "photographer"),
            ("date", "date"),
        )
        for old_key, current_key in scalar_fields:
            value = scalar_fill(current.get(current_key), old.get(old_key))
            if value not in (None, "") and value != current.get(current_key):
                input_data[current_key] = value

        old_rating = _clean_int(old.get("rating"))
        rating = scalar_fill(current.get("rating100"), old_rating)
        if rating not in (None, "", 0) and rating != current.get("rating100"):
            input_data["rating100"] = rating

        if bool(old.get("organized")) and not bool(current.get("organized")):
            input_data["organized"] = True

        urls = merge_strings(current.get("urls") or [], old.get("urls") or [], [old.get("url") or ""])
        if urls != list(current.get("urls") or []):
            input_data["urls"] = urls

        tag_ids = self._relation_ids(current.get("tags") or [], old.get("tags") or [], self.resolve_tag)
        if tag_ids != [str(item.get("id")) for item in (current.get("tags") or []) if item.get("id")]:
            input_data["tag_ids"] = tag_ids

        performer_ids = self._relation_ids(
            current.get("performers") or [], old.get("performers") or [], self.resolve_performer
        )
        if performer_ids != [
            str(item.get("id")) for item in (current.get("performers") or []) if item.get("id")
        ]:
            input_data["performer_ids"] = performer_ids

        old_studio = str(old.get("studio") or "").strip()
        if old_studio and not current.get("studio"):
            studio_id = self.resolve_studio(old_studio)
            if studio_id:
                input_data["studio_id"] = studio_id

        custom_delta = _custom_field_delta(current.get("custom_fields"), old.get("custom_fields"))
        if custom_delta:
            input_data["custom_fields"] = {"partial": custom_delta}

        changed = len(input_data) > 1
        if changed and not self.dry_run:
            self.stash.update_image(input_data)
        return changed

    def run(self) -> Dict[str, int]:
        mode = "ANALYZE" if self.dry_run else "RESTORE"
        log(
            "INFO",
            f"{mode}: old export has {len(self.old_scenes)} scene JSON file(s), "
            f"{len(self.old_images)} image JSON file(s), and {len(self.old_files)} file record(s).",
        )

        total = len(self.old_scenes) + len(self.old_images)
        done = 0
        used_scene_targets = set()
        used_image_targets = set()

        for old in self.old_scenes:
            match = match_old_media(
                old.get("files") or [],
                self.old_files,
                self.scene_fp_index,
                self.scene_path_index,
            )
            if not match.object_id:
                key = "ambiguous_scenes" if match.kind == "ambiguous" else "unmatched_scenes"
                self.stats[key] += 1
                log("WARNING", f"Scene '{old.get('title') or '(untitled)'}': {match.detail}; skipped.")
            elif match.object_id in used_scene_targets:
                self.stats["source_target_collisions"] += 1
                log("WARNING", f"Multiple old scene records resolve to current Scene {match.object_id}; later record skipped.")
            else:
                used_scene_targets.add(match.object_id)
                self.stats["matched_scenes"] += 1
                current = self.scenes_by_id[match.object_id]
                try:
                    if self.restore_scene(old, current):
                        self.stats["updated_scenes"] += 1
                except Exception as exc:
                    self.stats["errors"] += 1
                    log("ERROR", f"Scene {match.object_id} restore failed: {exc}")
            done += 1
            if total:
                progress(done / total)

        for old in self.old_images:
            match = match_old_media(
                old.get("files") or [],
                self.old_files,
                self.image_fp_index,
                self.image_path_index,
            )
            if not match.object_id:
                key = "ambiguous_images" if match.kind == "ambiguous" else "unmatched_images"
                self.stats[key] += 1
                log("WARNING", f"Image '{old.get('title') or '(untitled)'}': {match.detail}; skipped.")
            elif match.object_id in used_image_targets:
                self.stats["source_target_collisions"] += 1
                log("WARNING", f"Multiple old image records resolve to current Image {match.object_id}; later record skipped.")
            else:
                used_image_targets.add(match.object_id)
                self.stats["matched_images"] += 1
                current = self.images_by_id[match.object_id]
                try:
                    if self.restore_image(old, current):
                        self.stats["updated_images"] += 1
                except Exception as exc:
                    self.stats["errors"] += 1
                    log("ERROR", f"Image {match.object_id} restore failed: {exc}")
            done += 1
            if total:
                progress(done / total)

        log(
            "INFO",
            f"{mode} complete: matched {self.stats['matched_scenes']}/{len(self.old_scenes)} scenes "
            f"and {self.stats['matched_images']}/{len(self.old_images)} images; "
            f"{self.stats['unmatched_scenes'] + self.stats['unmatched_images']} unmatched, "
            f"{self.stats['ambiguous_scenes'] + self.stats['ambiguous_images']} ambiguous, "
            f"{self.stats['errors']} error(s).",
        )
        if self.dry_run:
            log(
                "INFO",
                "No changes were made. Restore will update only already-existing matched "
                "Scenes/Images; it never creates media records.",
            )
        return self.stats


def main() -> None:
    payload = read_input()
    conn = payload.get("server_connection") or {}
    args = payload.get("args") or {}
    stash = Stash(conn)
    settings = stash.settings()

    source_path = str(
        args.get("source_path")
        or settings.get("migration_source_path")
        or ""
    ).strip()
    if not source_path:
        raise RuntimeError(
            "Set 'Old Stash export path' in the plugin settings to a Stash export ZIP "
            "or extracted export directory visible inside the Stash container."
        )

    action = str(args.get("action") or "analyze").strip().casefold()
    if action not in {"analyze", "restore"}:
        raise RuntimeError("Unknown action; expected analyze or restore")

    with open_export_source(source_path) as root:
        engine = MigrationEngine(stash, root, dry_run=(action == "analyze"))
        stats = engine.run()

    print(json.dumps({"output": "ok", "action": action, "stats": stats}))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("ERROR", str(exc))
        print(json.dumps({"output": "error", "error": str(exc)}))
        sys.exit(1)
