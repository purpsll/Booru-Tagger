#!/usr/bin/env python3
"""Restore a Stash JSON export onto media already scanned into another Stash.

Design goal: never create Scene/Image/File records. Old JSON is treated only as
metadata. Media matching is fingerprint-first and must be unique.
"""

from __future__ import annotations

import base64
import binascii
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from migration_core import (
    build_current_media_indexes,
    build_old_file_index,
    decide_entity_match,
    EntityMatch,
    load_json_files,
    match_old_media,
    merge_stash_ids,
    merge_strings,
    normalize_name,
    normalized_path,
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
TAG_CREATION_GUARD_THRESHOLD = 0.90
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


def _has_custom_image(image_path: Any) -> bool:
    path = str(image_path or "").strip()
    return bool(path and "default=true" not in path.casefold())


def _normalize_artwork_value(value: Any) -> Optional[str]:
    """Return artwork in a GraphQL-safe form.

    Stash JSON exports may store entity artwork as raw base64 without a data URI.
    Stash GraphQL interprets a bare string as a URL, so raw base64 must be wrapped
    as a data URI before being sent back.
    """
    text = str(value or "").strip()
    if not text:
        return None

    lowered = text.casefold()
    if lowered.startswith("data:image/") or lowered.startswith("http://") or lowered.startswith("https://"):
        return text

    payload = "".join(text.split())
    if not payload:
        return None

    # Decode only a short prefix for type detection; the full payload remains
    # untouched so large artwork does not get copied/decoded unnecessarily.
    prefix = payload[:96]
    prefix += "=" * ((4 - len(prefix) % 4) % 4)
    try:
        raw = base64.b64decode(prefix, validate=False)
    except (binascii.Error, ValueError):
        return None

    mime: Optional[str] = None
    if raw.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif raw.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif raw.startswith((b"GIF87a", b"GIF89a")):
        mime = "image/gif"
    elif raw.startswith(b"RIFF") and len(raw) >= 12 and raw[8:12] == b"WEBP":
        mime = "image/webp"
    elif raw.lstrip().startswith(b"<svg"):
        mime = "image/svg+xml"

    if not mime:
        return None
    return f"data:{mime};base64,{payload}"


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
                self.gallery_folder_index.setdefault(normalized_path(folder_path), []).append(str(gallery["id"]))

        self.resolved_tags: Dict[str, Optional[str]] = {}
        self.resolved_performers: Dict[str, Optional[str]] = {}
        self.resolved_studios: Dict[str, Optional[str]] = {}
        self.resolved_galleries: Dict[str, Optional[str]] = {}
        self.resolved_groups: Dict[str, Optional[str]] = {}
        self.inferred_gallery_map: Dict[str, str] = {}
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
            "primary_tag_reuse": 0,
            "stash_id_tag_reuse": 0,
            "tag_identity_conflict_relationship_reuse": 0,
            "tag_creation_guard_skips": 0,
            "tag_alias_collision_skips": 0,
            "performer_alias_collision_skips": 0,
            "studio_alias_collision_skips": 0,
            "fuzzy_performer_reuse": 0,
            "fuzzy_studio_reuse": 0,
            "merged_duplicate_tags": 0,
            "merged_duplicate_performers": 0,
            "merged_duplicate_studios": 0,
            "gallery_chapters_created": 0,
            "scene_markers_created": 0,
            "scene_markers_updated": 0,
            "image_o_increments": 0,
            "entity_images_restored": 0,
            "entity_images_skipped_invalid": 0,
            "tag_parent_links_added": 0,
            "unresolved_galleries": 0,
            "ambiguous_galleries": 0,
            "entity_identity_conflicts": 0,
            "ambiguous_entities_skipped": 0,
            "errors": 0,
        }

    def _source_entity(self, mapping: Dict[str, Dict[str, Any]], name: str) -> Dict[str, Any]:
        return mapping.get(str(name or "").casefold(), {"name": name})

    def _remove_merged_entities(
        self,
        collection: List[Dict[str, Any]],
        source_ids: Iterable[str],
    ) -> None:
        remove = {str(value) for value in source_ids}
        collection[:] = [
            entity for entity in collection
            if str(entity.get("id") or "") not in remove
        ]

    def _safe_merge_ambiguous_tag(self, name: str) -> Optional[Dict[str, Any]]:
        merge = safe_formatting_merge(
            name, self.tags, kind="tag", alias_field="aliases"
        )
        if not merge:
            return None
        by_id = {str(tag.get("id") or ""): tag for tag in self.tags}
        destination = by_id.get(merge.destination_id)
        if not destination:
            return None
        removals, conflicts = tag_alias_preflight(self.tags, merge)
        if conflicts:
            return None
        if self.dry_run:
            self.stats["merged_duplicate_tags"] += len(merge.source_ids)
            return destination

        for owner_id, remove_keys in removals.items():
            owner = by_id.get(owner_id) or {}
            aliases = [
                str(alias)
                for alias in (owner.get("aliases") or [])
                if str(alias or "").casefold().strip() not in set(remove_keys)
            ]
            updated = self.stash.update_tag_aliases(owner_id, aliases)
            owner["aliases"] = updated.get("aliases") or aliases

        merged = self.stash.merge_tags(list(merge.source_ids), merge.destination_id)
        destination.update(merged)
        self._remove_merged_entities(self.tags, merge.source_ids)
        self.stats["merged_duplicate_tags"] += len(merge.source_ids)
        log(
            "INFO",
            f"Merged {len(merge.source_ids)} safe duplicate Tag(s) into "
            f"'{merge.destination_name}' while resolving old metadata.",
        )
        return destination

    def _safe_merge_ambiguous_performer(self, name: str) -> Optional[Dict[str, Any]]:
        merge = safe_formatting_merge(
            name, self.performers, kind="performer", alias_field="alias_list"
        )
        if not merge:
            return None
        by_id = {str(entity.get("id") or ""): entity for entity in self.performers}
        destination = by_id.get(merge.destination_id)
        if not destination:
            return None
        if self.dry_run:
            self.stats["merged_duplicate_performers"] += len(merge.source_ids)
            return destination
        values = merged_entity_values(
            self.performers, merge, alias_field="alias_list"
        )
        merged = self.stash.merge_performers(
            list(merge.source_ids), merge.destination_id, values
        )
        self._remove_merged_entities(self.performers, merge.source_ids)
        self._replace_entity(self.performers, merged)
        self.stats["merged_duplicate_performers"] += len(merge.source_ids)
        log(
            "INFO",
            f"Merged {len(merge.source_ids)} safe duplicate Performer(s) into "
            f"'{merge.destination_name}' while resolving old metadata.",
        )
        return merged

    def _safe_merge_ambiguous_studio(self, name: str) -> Optional[Dict[str, Any]]:
        merge = safe_formatting_merge(
            name, self.studios, kind="studio", alias_field="aliases"
        )
        if not merge:
            return None
        by_id = {str(entity.get("id") or ""): entity for entity in self.studios}
        destination = by_id.get(merge.destination_id)
        if not destination:
            return None
        if self.dry_run:
            self.stats["merged_duplicate_studios"] += len(merge.source_ids)
            return destination
        values = merged_entity_values(
            self.studios, merge, alias_field="aliases"
        )
        merged = self.stash.merge_studios(
            list(merge.source_ids), merge.destination_id, values
        )
        self._remove_merged_entities(self.studios, merge.source_ids)
        self._replace_entity(self.studios, merged)
        self.stats["merged_duplicate_studios"] += len(merge.source_ids)
        log(
            "INFO",
            f"Merged {len(merge.source_ids)} safe duplicate Studio(s) into "
            f"'{merge.destination_name}' while resolving old metadata.",
        )
        return merged

    def _tag_stash_id_match(
        self,
        source: Dict[str, Any],
    ) -> Tuple[Optional[EntityMatch], bool]:
        """Match an old Tag to a current Tag by exact endpoint + Stash ID."""
        source_ids = {
            (
                str(item.get("endpoint") or "").casefold().strip(),
                str(item.get("stash_id") or "").strip(),
            )
            for item in (source.get("stash_ids") or [])
            if str(item.get("endpoint") or "").strip()
            and str(item.get("stash_id") or "").strip()
        }
        if not source_ids:
            return None, False

        hits = []
        for tag in self.tags:
            current_ids = {
                (
                    str(item.get("endpoint") or "").casefold().strip(),
                    str(item.get("stash_id") or "").strip(),
                )
                for item in (tag.get("stash_ids") or [])
                if str(item.get("endpoint") or "").strip()
                and str(item.get("stash_id") or "").strip()
            }
            if source_ids & current_ids:
                hits.append(tag)

        if len(hits) == 1:
            return EntityMatch(hits[0], "stash-id", 1.0, 0.0), False
        if len(hits) > 1:
            return None, True
        return None, False

    def _plausible_existing_tag_candidates(self, name: str) -> List[Tuple[float, Dict[str, Any]]]:
        """Return near matches that make creating another Tag unsafe."""
        import difflib

        source_norm = compact_name(name)
        if len(source_norm) < 4:
            return []

        scored: List[Tuple[float, Dict[str, Any]]] = []
        for tag in self.tags:
            values = [str(tag.get("name") or "")]
            values.extend(str(value or "") for value in (tag.get("aliases") or []))
            best = 0.0
            for value in values:
                candidate = compact_name(value)
                if not candidate:
                    continue
                if candidate == source_norm:
                    best = 1.0
                    break
                if candidate[0] != source_norm[0]:
                    continue
                if abs(len(candidate) - len(source_norm)) > 6:
                    continue
                best = max(
                    best,
                    difflib.SequenceMatcher(None, source_norm, candidate).ratio(),
                )
            if best >= TAG_CREATION_GUARD_THRESHOLD:
                scored.append((best, tag))
        scored.sort(key=lambda item: item[0], reverse=True)
        return scored

    def _primary_tag_match(self, name: str) -> Tuple[Optional[EntityMatch], bool]:
        """Prefer current Tag primary names over aliases for relationships.

        Returns (match, ambiguous). Exact case-insensitive primary name wins first,
        followed by a unique normalized/formatting-equivalent primary name.
        """
        raw = str(name or "").strip()
        if not raw:
            return None, False

        exact_hits = [
            tag for tag in self.tags
            if str(tag.get("name") or "").strip().casefold() == raw.casefold()
        ]

        normalized = normalize_name(raw)
        normalized_hits = [
            tag for tag in self.tags
            if normalize_name(str(tag.get("name") or "")) == normalized
        ]

        # Formatting-equivalent primary duplicates (for example Big Breasts /
        # big_breasts) should still be collapsed safely instead of allowing an
        # exact-spelling variant to bypass deduplication.
        if len(normalized_hits) > 1:
            return None, True

        if len(exact_hits) == 1:
            return EntityMatch(exact_hits[0], "primary-exact", 1.0, 0.0), False
        if len(exact_hits) > 1:
            return None, True

        if len(normalized_hits) == 1:
            return EntityMatch(normalized_hits[0], "primary-normalized", 1.0, 0.0), False
        return None, False

    def resolve_tag(self, name: str) -> Optional[str]:
        key = normalize_name(name)
        if not key:
            return None
        if key in self.resolved_tags:
            return self.resolved_tags[key]

        source = self._source_entity(self.old_tags_by_name, name)
        stash_id_match, stash_id_ambiguous = self._tag_stash_id_match(source)
        primary_match, primary_ambiguous = self._primary_tag_match(name)
        relationship_only_due_identity_disagreement = False

        # Relationship identity follows the current primary Tag name first.
        # External IDs can disambiguate duplicate primaries or provide fallback
        # identity, but must not redirect an exact current primary-name match to
        # a differently named Tag.
        if primary_match is not None:
            match = primary_match
            self.stats["primary_tag_reuse"] += 1
            if (
                stash_id_match is not None
                and str(stash_id_match.entity.get("id") or "") != str(primary_match.entity.get("id") or "")
            ):
                self.stats["entity_identity_conflicts"] += 1
                relationship_only_due_identity_disagreement = True
                log(
                    "WARNING",
                    f"Tag '{name}' has a unique current primary-name match "
                    f"'{primary_match.entity.get('name')}', but its old external Stash ID "
                    f"matches different current Tag '{stash_id_match.entity.get('name')}'. "
                    "The relationship will use the primary-name Tag and conflicting identity "
                    "metadata will not be merged.",
                )
        elif primary_ambiguous:
            if (
                stash_id_match is not None
                and normalize_name(str(stash_id_match.entity.get("name") or "")) == key
            ):
                match = stash_id_match
                self.stats["stash_id_tag_reuse"] += 1
                log(
                    "INFO",
                    f"Tag identity disambiguation: '{name}' -> "
                    f"'{match.entity.get('name')}' by exact endpoint + Stash ID.",
                )
            else:
                merged_entity = self._safe_merge_ambiguous_tag(name)
                if merged_entity is None:
                    self.stats["ambiguous_entities_skipped"] += 1
                    log(
                        "WARNING",
                        f"Tag '{name}' has multiple current primary-name matches that cannot "
                        "be safely disambiguated or collapsed; relationship skipped.",
                    )
                    self.resolved_tags[key] = None
                    return None
                match = EntityMatch(
                    merged_entity,
                    "safe-duplicate-merge",
                    1.0,
                    0.0,
                )
        elif stash_id_match is not None:
            match = stash_id_match
            self.stats["stash_id_tag_reuse"] += 1
            log(
                "INFO",
                f"Tag identity reuse: '{name}' -> '{match.entity.get('name')}' "
                "by exact endpoint + Stash ID.",
            )
        elif stash_id_ambiguous:
            self.stats["ambiguous_entities_skipped"] += 1
            log(
                "WARNING",
                f"Tag '{name}' has multiple current Tags with the same external "
                "Stash ID; relationship skipped and no new Tag will be created.",
            )
            self.resolved_tags[key] = None
            return None
        else:
            decision = decide_entity_match(
                    name,
                    self.tags,
                    alias_field="aliases",
                    allow_fuzzy=True,
                    threshold=TAG_THRESHOLD,
                    margin=TAG_MARGIN,
                )
            if decision.ambiguous:
                merged_entity = self._safe_merge_ambiguous_tag(name)
                if merged_entity is None:
                    self.stats["ambiguous_entities_skipped"] += 1
                    log(
                        "WARNING",
                        f"Tag '{name}' has ambiguous alias/fuzzy current matches and no "
                        "unique primary-name match; relationship skipped.",
                    )
                    self.resolved_tags[key] = None
                    return None
                match = EntityMatch(
                    merged_entity,
                    "safe-duplicate-merge",
                    1.0,
                    0.0,
                )
            else:
                match = decision.match
        if match and match.kind == "fuzzy":
            source_tokens = set(normalize_name(name).split())
            target_tokens = set(normalize_name(str(match.entity.get("name") or "")).split())
            if source_tokens and target_tokens and (
                source_tokens < target_tokens or target_tokens < source_tokens
            ):
                match = None
            else:
                self.stats["fuzzy_tag_reuse"] += 1
                log(
                    "INFO",
                    f"Tag fuzzy reuse: '{name}' -> '{match.entity.get('name')}' "
                    f"({match.score * 100:.1f}%, next {match.second_score * 100:.1f}%).",
                )
        if match:
            if relationship_only_due_identity_disagreement:
                self.stats["tag_identity_conflict_relationship_reuse"] += 1
                self.stats["reused_tags"] += 1
                tag_id = str(match.entity["id"])
                self.resolved_tags[key] = tag_id
                return tag_id

            identity_conflict = _same_endpoint_identity_conflict(
                match.entity.get("stash_ids") or [], source.get("stash_ids") or []
            )
            if identity_conflict:
                self.stats["entity_identity_conflicts"] += 1
                if match.kind in {"primary-exact", "primary-normalized", "stash-id"}:
                    self.stats["tag_identity_conflict_relationship_reuse"] += 1
                    self.stats["reused_tags"] += 1
                    tag_id = str(match.entity["id"])
                    self.resolved_tags[key] = tag_id
                    log(
                        "WARNING",
                        f"Tag '{name}' matches current primary Tag "
                        f"'{match.entity.get('name')}'. The relationship will use that existing "
                        "Tag, but conflicting same-endpoint Stash ID metadata will not be merged.",
                    )
                    return tag_id
                log(
                    "WARNING",
                    f"Tag '{name}' has a conflicting same-endpoint Stash ID and only an "
                    "alias/fuzzy match; relationship skipped.",
                )
                self.resolved_tags[key] = None
                return None

            self.stats["reused_tags"] += 1
            entity = match.entity
            if not self.dry_run:
                entity = self._merge_tag_metadata(entity, source)
                self._replace_entity(self.tags, entity)
            tag_id = str(entity["id"])
            self.resolved_tags[key] = tag_id
            return tag_id

        plausible = self._plausible_existing_tag_candidates(name)
        if plausible:
            self.stats["tag_creation_guard_skips"] += 1
            self.stats["ambiguous_entities_skipped"] += 1
            preview = ", ".join(
                f"'{tag.get('name')}' ({score * 100:.1f}%)"
                for score, tag in plausible[:3]
            )
            log(
                "WARNING",
                f"Tag '{name}' has plausible existing current Tag candidate(s): "
                f"{preview}. To prevent duplicate Tags, creation and this relationship "
                "are skipped for review.",
            )
            self.resolved_tags[key] = None
            return None

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

    def _safe_tag_aliases(
        self,
        aliases: Iterable[str],
        *,
        destination_id: Optional[str] = None,
        destination_name: Optional[str] = None,
    ) -> List[str]:
        """Filter aliases that would collide with another current Tag.

        Stash requires Tag names and aliases to remain globally non-conflicting.
        A metadata merge must never fail just because an old alias is already a
        current Tag's primary name or alias.
        """
        destination_id = str(destination_id or "")
        destination_name_key = str(destination_name or "").casefold().strip()

        primary_owners: Dict[str, str] = {}
        alias_owners: Dict[str, str] = {}
        for tag in self.tags:
            tag_id = str(tag.get("id") or "")
            name_key = str(tag.get("name") or "").casefold().strip()
            if name_key:
                primary_owners[name_key] = tag_id
            for alias in tag.get("aliases") or []:
                alias_key = str(alias or "").casefold().strip()
                if alias_key:
                    alias_owners[alias_key] = tag_id

        out: List[str] = []
        seen = set()
        skipped: List[str] = []
        for alias in aliases or []:
            text = str(alias or "").strip()
            key = text.casefold()
            if not text or key in seen or key == destination_name_key:
                continue

            primary_owner = primary_owners.get(key)
            alias_owner = alias_owners.get(key)
            if (
                (primary_owner and primary_owner != destination_id)
                or (alias_owner and alias_owner != destination_id)
            ):
                skipped.append(text)
                continue

            seen.add(key)
            out.append(text)

        if skipped:
            self.stats["tag_alias_collision_skips"] += len(set(v.casefold() for v in skipped))
            log(
                "WARNING",
                "Skipped conflicting Tag alias(es) while preserving current Tag identities: "
                + ", ".join(sorted(set(skipped), key=str.casefold)),
            )
        return out

    def _tag_create_input(self, source: Dict[str, Any], fallback_name: str) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "name": str(source.get("name") or fallback_name).strip(),
            "aliases": self._safe_tag_aliases(
                merge_strings(source.get("aliases") or []),
                destination_name=str(source.get("name") or fallback_name).strip(),
            ),
            "favorite": bool(source.get("favorite")),
            "ignore_auto_tag": bool(source.get("ignore_auto_tag")),
            "stash_ids": merge_stash_ids(source.get("stash_ids") or []),
        }
        for src, dst in (("sort_name", "sort_name"), ("description", "description")):
            value = str(source.get(src) or "").strip()
            if value:
                data[dst] = value
        if source.get("image"):
            artwork = _normalize_artwork_value(source.get("image"))
            if artwork:
                data["image"] = artwork
                self.stats["entity_images_restored"] += 1
            else:
                self.stats["entity_images_skipped_invalid"] += 1
                log("WARNING", f"Tag '{data['name']}' has unsupported/invalid artwork; artwork skipped.")
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
        aliases = self._safe_tag_aliases(
            aliases,
            destination_id=str(current.get("id") or ""),
            destination_name=str(current.get("name") or ""),
        )
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
        if not _has_custom_image(current.get("image_path")) and source.get("image"):
            artwork = _normalize_artwork_value(source.get("image"))
            if artwork:
                input_data["image"] = artwork
                self.stats["entity_images_restored"] += 1
            else:
                self.stats["entity_images_skipped_invalid"] += 1
                log(
                    "WARNING",
                    f"Tag '{current.get('name') or source.get('name')}' has unsupported/invalid artwork; artwork skipped.",
                )
        if delta:
            input_data["custom_fields"] = {"partial": delta}
        return self.stash.update_tag(input_data)

    def _safe_entity_aliases(
        self,
        collection: Sequence[Dict[str, Any]],
        alias_field: str,
        aliases: Iterable[str],
        *,
        destination_id: Optional[str],
        destination_name: str,
        entity_label: str,
        stat_key: str,
    ) -> List[str]:
        """Filter aliases that collide with another current entity name or alias."""
        destination_id = str(destination_id or "")
        destination_name_key = str(destination_name or "").casefold().strip()

        name_owners: Dict[str, str] = {}
        alias_owners: Dict[str, str] = {}
        for entity in collection:
            entity_id = str(entity.get("id") or "")
            name_key = str(entity.get("name") or "").casefold().strip()
            if name_key:
                name_owners[name_key] = entity_id
            for alias in entity.get(alias_field) or []:
                alias_key = str(alias or "").casefold().strip()
                if alias_key:
                    alias_owners[alias_key] = entity_id

        out: List[str] = []
        seen = set()
        skipped: List[str] = []
        for alias in aliases or []:
            text = str(alias or "").strip()
            key = text.casefold()
            if not text or key in seen or key == destination_name_key:
                continue

            name_owner = name_owners.get(key)
            alias_owner = alias_owners.get(key)
            if (
                (name_owner and name_owner != destination_id)
                or (alias_owner and alias_owner != destination_id)
            ):
                skipped.append(text)
                continue

            seen.add(key)
            out.append(text)

        if skipped:
            unique_skipped = sorted(set(skipped), key=str.casefold)
            self.stats[stat_key] += len({value.casefold() for value in skipped})
            log(
                "WARNING",
                f"Skipped conflicting {entity_label} alias(es) while preserving current "
                f"{entity_label} identities: " + ", ".join(unique_skipped),
            )
        return out

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
            merged_entity = self._safe_merge_ambiguous_performer(name)
            if merged_entity is None:
                self.stats["ambiguous_entities_skipped"] += 1
                log("WARNING", f"Performer '{name}' has ambiguous current matches that cannot be safely collapsed; relationship skipped.")
                self.resolved_performers[key] = None
                return None
            match = type("_Match", (), {
                "entity": merged_entity,
                "kind": "safe-duplicate-merge",
                "score": 1.0,
                "second_score": 0.0,
            })()
        else:
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
        aliases = self._safe_entity_aliases(
            self.performers,
            "alias_list",
            aliases,
            destination_id=str(current.get("id") or ""),
            destination_name=current_name,
            entity_label="Performer",
            stat_key="performer_alias_collision_skips",
        )

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

        if source.get("image") and (not current or not _has_custom_image(current.get("image_path"))):
            artwork = _normalize_artwork_value(source.get("image"))
            if artwork:
                data["image"] = artwork
                self.stats["entity_images_restored"] += 1
            else:
                self.stats["entity_images_skipped_invalid"] += 1
                log(
                    "WARNING",
                    f"Performer '{current_name}' has unsupported/invalid artwork; artwork skipped.",
                )
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
        try:
            return self.stash.update_performer(data)
        except RuntimeError as exc:
            message = str(exc).casefold()
            if (
                data.get("alias_list")
                and "performer with name" in message
                and "already exists" in message
            ):
                skipped = list(data.pop("alias_list"))
                self.stats["performer_alias_collision_skips"] += len(skipped)
                log(
                    "WARNING",
                    f"Stash rejected Performer aliases for '{current.get('name')}' as "
                    "duplicate names; retrying the metadata update without alias changes.",
                )
                return self.stash.update_performer(data)
            raise

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
                merged_entity = self._safe_merge_ambiguous_studio(name)
                if merged_entity is None:
                    self.stats["ambiguous_entities_skipped"] += 1
                    log("WARNING", f"Studio '{name}' has ambiguous current matches that cannot be safely collapsed; relationship skipped.")
                    self.resolved_studios[key] = None
                    return None
                match = type("_Match", (), {
                    "entity": merged_entity,
                    "kind": "safe-duplicate-merge",
                    "score": 1.0,
                    "second_score": 0.0,
                })()
            else:
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
        aliases = self._safe_entity_aliases(
            self.studios,
            "aliases",
            aliases,
            destination_id=str(current.get("id") or ""),
            destination_name=current_name,
            entity_label="Studio",
            stat_key="studio_alias_collision_skips",
        )
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

        if source.get("image") and (not current or not _has_custom_image(current.get("image_path"))):
            artwork = _normalize_artwork_value(source.get("image"))
            if artwork:
                data["image"] = artwork
                self.stats["entity_images_restored"] += 1
            else:
                self.stats["entity_images_skipped_invalid"] += 1
                log(
                    "WARNING",
                    f"Studio '{current_name}' has unsupported/invalid artwork; artwork skipped.",
                )

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
        try:
            return self.stash.update_studio(data)
        except RuntimeError as exc:
            message = str(exc).casefold()
            if (
                data.get("aliases")
                and "studio with name" in message
                and "already exists" in message
            ):
                skipped = list(data.pop("aliases"))
                self.stats["studio_alias_collision_skips"] += len(skipped)
                log(
                    "WARNING",
                    f"Stash rejected Studio aliases for '{current.get('name')}' as "
                    "duplicate names; retrying the metadata update without alias changes.",
                )
                return self.stash.update_studio(data)
            raise

    @staticmethod
    def _group_alias_values(entity: Mapping[str, Any]) -> List[str]:
        raw = str(entity.get("aliases") or "").strip()
        if not raw:
            return []
        values = [raw]
        for separator in (";", "\n", ","):
            if separator in raw:
                values.extend(part.strip() for part in raw.split(separator))
        return merge_strings(values)

    @staticmethod
    def _gallery_relation_key(ref: Mapping[str, Any]) -> str:
        folder = str(ref.get("folder_path") or "").strip()
        if folder:
            return "folder:" + normalized_path(folder)
        zip_files = sorted(
            normalized_path(str(v))
            for v in (ref.get("zip_files") or [])
            if str(v).strip()
        )
        if zip_files:
            return "zip:" + "|".join(zip_files)
        title = str(ref.get("title") or "").strip()
        if title:
            return "title:" + normalize_name(title)
        return ""

    def _gallery_cache_key(self, source: Mapping[str, Any]) -> str:
        relation_key = self._gallery_relation_key(source)
        if relation_key:
            return relation_key
        return str(source.get("_migration_source_file") or "")

    def _old_gallery_for_ref(self, ref: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        folder = str(ref.get("folder_path") or "").strip()
        zip_files = [str(v) for v in (ref.get("zip_files") or []) if str(v).strip()]
        title = str(ref.get("title") or "").strip()
        for source in self.old_galleries:
            if folder and normalized_path(str(source.get("folder_path") or "")) == normalized_path(folder):
                return source
            source_zips = {
                normalized_path(str(v))
                for v in (source.get("zip_files") or [])
                if str(v).strip()
            }
            if zip_files and source_zips.intersection(normalized_path(v) for v in zip_files):
                return source
            if title and normalize_name(str(source.get("title") or "")) == normalize_name(title):
                return source
        return None

    def _match_current_gallery(self, source: Mapping[str, Any]) -> Tuple[Optional[str], str]:
        inferred = self.inferred_gallery_map.get(self._gallery_relation_key(source))
        if inferred:
            return inferred, "media-consensus"
        zip_files = [str(v) for v in (source.get("zip_files") or []) if str(v).strip()]
        if zip_files:
            match = match_old_media(
                zip_files,
                self.old_files,
                self.gallery_fp_index,
                self.gallery_path_index,
            )
            if match.object_id:
                return match.object_id, match.kind
            if match.kind == "ambiguous":
                return None, "ambiguous"

        folder = str(source.get("folder_path") or "").strip()
        if folder:
            ids = self.gallery_folder_index.get(normalized_path(folder), [])
            if len(ids) == 1:
                return ids[0], "exact-folder"
            if len(ids) > 1:
                return None, "ambiguous"
            return None, "unmatched-file-backed"

        title = str(source.get("title") or "").strip()
        if title:
            hits = [
                str(gallery["id"])
                for gallery in self.galleries
                if normalize_name(str(gallery.get("title") or "")) == normalize_name(title)
            ]
            if len(hits) == 1:
                return hits[0], "normalized-title"
            if len(hits) > 1:
                return None, "ambiguous"
        return None, "missing"

    def _gallery_input(
        self,
        source: Mapping[str, Any],
        current: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        current = current or {}
        data: Dict[str, Any] = {}
        for src, dst in (
            ("title", "title"), ("code", "code"), ("date", "date"),
            ("details", "details"), ("photographer", "photographer"),
        ):
            value = scalar_fill(current.get(dst), source.get(src))
            if value not in (None, ""):
                data[dst] = value
        rating = scalar_fill(current.get("rating100"), _clean_int(source.get("rating")))
        if rating not in (None, "", 0):
            data["rating100"] = rating
        data["organized"] = bool(current.get("organized") or source.get("organized"))
        data["urls"] = merge_strings(
            current.get("urls") or [], source.get("urls") or [], [source.get("url") or ""]
        )
        tag_ids = [str(t.get("id")) for t in (current.get("tags") or []) if t.get("id")]
        for tag_name in source.get("tags") or []:
            resolved = self.resolve_tag(str(tag_name))
            if resolved:
                tag_ids.append(resolved)
        if tag_ids:
            data["tag_ids"] = _unique_ids(tag_ids)
        performer_ids = [str(p.get("id")) for p in (current.get("performers") or []) if p.get("id")]
        for performer_name in source.get("performers") or []:
            resolved = self.resolve_performer(str(performer_name))
            if resolved:
                performer_ids.append(resolved)
        if performer_ids:
            data["performer_ids"] = _unique_ids(performer_ids)
        studio_name = str(source.get("studio") or "").strip()
        if studio_name and not current.get("studio"):
            studio_id = self.resolve_studio(studio_name)
            if studio_id:
                data["studio_id"] = studio_id
        custom_delta = _custom_field_delta(current.get("custom_fields"), source.get("custom_fields"))
        if current:
            if custom_delta:
                data["custom_fields"] = {"partial": custom_delta}
        elif isinstance(source.get("custom_fields"), dict) and source.get("custom_fields"):
            data["custom_fields"] = source["custom_fields"]
        return data

    def _restore_gallery_chapters(
        self,
        gallery_id: str,
        source: Mapping[str, Any],
        current: Mapping[str, Any],
    ) -> None:
        existing = {
            (normalize_name(str(chapter.get("title") or "")), int(chapter.get("image_index") or 0))
            for chapter in (current.get("chapters") or [])
        }
        for chapter in source.get("chapters") or []:
            title = str(chapter.get("title") or "").strip()
            index = _clean_int(chapter.get("image_index"))
            if not title or index is None:
                continue
            key = (normalize_name(title), index)
            if key in existing:
                continue
            self.stats["gallery_chapters_created"] += 1
            if not self.dry_run:
                self.stash.create_gallery_chapter(gallery_id, title, index)
            existing.add(key)

    def resolve_gallery_source(self, source: Dict[str, Any]) -> Optional[str]:
        key = self._gallery_cache_key(source)
        if key in self.resolved_galleries:
            return self.resolved_galleries[key]
        current_id, match_kind = self._match_current_gallery(source)
        current: Optional[Dict[str, Any]] = None
        if current_id:
            current = self.galleries_by_id.get(str(current_id))
            self.stats["reused_galleries"] += 1
        elif match_kind == "ambiguous":
            self.stats["ambiguous_galleries"] += 1
            log("WARNING", f"Gallery '{source.get('title') or source.get('folder_path') or '(untitled)'}' has multiple current matches; skipped.")
            self.resolved_galleries[key] = None
            return None
        elif source.get("zip_files") or source.get("folder_path"):
            self.stats["unresolved_galleries"] += 1
            log("WARNING", f"File-backed Gallery '{source.get('title') or source.get('folder_path') or '(untitled)'}' cannot be matched safely in the new Stash; skipped instead of creating a disconnected Gallery.")
            self.resolved_galleries[key] = None
            return None
        else:
            title = str(source.get("title") or "").strip()
            if not title:
                self.stats["unresolved_galleries"] += 1
                self.resolved_galleries[key] = None
                return None
            self.stats["created_galleries"] += 1
            if self.dry_run:
                synthetic = "DRYRUN:GALLERY:" + normalize_name(title)
                self.resolved_galleries[key] = synthetic
                return synthetic
            created = self.stash.create_gallery(self._gallery_input(source))
            self.galleries.append(created)
            self.galleries_by_id[str(created["id"])] = created
            current = created
            current_id = str(created["id"])

        if current is None or current_id is None:
            self.resolved_galleries[key] = None
            return None
        if not self.dry_run:
            update = self._gallery_input(source, current)
            update["id"] = str(current_id)
            current = self.stash.update_gallery(update)
            self._replace_entity(self.galleries, current)
            self.galleries_by_id[str(current_id)] = current
        self._restore_gallery_chapters(str(current_id), source, current)
        self.resolved_galleries[key] = str(current_id)
        return str(current_id)

    def resolve_gallery_ref(self, ref: Mapping[str, Any]) -> Optional[str]:
        source = self._old_gallery_for_ref(ref)
        if source is not None:
            return self.resolve_gallery_source(source)
        title = str(ref.get("title") or "").strip()
        if title:
            hits = [
                gallery for gallery in self.galleries
                if normalize_name(str(gallery.get("title") or "")) == normalize_name(title)
            ]
            if len(hits) == 1:
                return str(hits[0]["id"])
        self.stats["unresolved_galleries"] += 1
        return None

    def _group_match(self, name: str) -> Tuple[Optional[Dict[str, Any]], bool]:
        target = normalize_name(name)
        compact_target = "".join(ch for ch in target if ch.isalnum())
        hits: List[Dict[str, Any]] = []
        for group in self.groups:
            values = [str(group.get("name") or ""), *self._group_alias_values(group)]
            if any(
                normalize_name(value) == target
                or (
                    len(compact_target) >= 4
                    and "".join(ch for ch in normalize_name(value) if ch.isalnum()) == compact_target
                )
                for value in values
            ):
                hits.append(group)
        if len(hits) == 1:
            return hits[0], False
        return None, len(hits) > 1

    def _group_input(
        self,
        source: Mapping[str, Any],
        current: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        current = current or {}
        data: Dict[str, Any] = {}
        name = str(source.get("name") or "").strip()
        if name:
            data["name"] = str(current.get("name") or name)
        current_alias = str(current.get("aliases") or "").strip()
        old_alias = str(source.get("aliases") or "").strip()
        aliases = merge_strings([current_alias], [old_alias])
        if aliases:
            data["aliases"] = "; ".join(aliases)
        for src, dst in (("date", "date"), ("director", "director"), ("synopsis", "synopsis")):
            value = scalar_fill(current.get(dst), source.get(src))
            if value not in (None, ""):
                data[dst] = value
        duration = scalar_fill(current.get("duration"), _clean_int(source.get("duration")))
        if duration not in (None, "", 0):
            data["duration"] = duration
        rating = scalar_fill(current.get("rating100"), _clean_int(source.get("rating")))
        if rating not in (None, "", 0):
            data["rating100"] = rating
        data["urls"] = merge_strings(current.get("urls") or [], source.get("urls") or [], [source.get("url") or ""])
        tag_ids = [str(t.get("id")) for t in (current.get("tags") or []) if t.get("id")]
        for tag_name in source.get("tags") or []:
            resolved = self.resolve_tag(str(tag_name))
            if resolved:
                tag_ids.append(resolved)
        if tag_ids:
            data["tag_ids"] = _unique_ids(tag_ids)
        studio_name = str(source.get("studio") or "").strip()
        if studio_name and not current.get("studio"):
            studio_id = self.resolve_studio(studio_name)
            if studio_id:
                data["studio_id"] = studio_id
        if not current.get("front_image_path") and source.get("front_image"):
            artwork = _normalize_artwork_value(source.get("front_image"))
            if artwork:
                data["front_image"] = artwork
                self.stats["entity_images_restored"] += 1
            else:
                self.stats["entity_images_skipped_invalid"] += 1
                log("WARNING", f"Group '{name}' has unsupported/invalid front artwork; artwork skipped.")
        if not current.get("back_image_path") and source.get("back_image"):
            artwork = _normalize_artwork_value(source.get("back_image"))
            if artwork:
                data["back_image"] = artwork
                self.stats["entity_images_restored"] += 1
            else:
                self.stats["entity_images_skipped_invalid"] += 1
                log("WARNING", f"Group '{name}' has unsupported/invalid back artwork; artwork skipped.")
        custom_delta = _custom_field_delta(current.get("custom_fields"), source.get("custom_fields"))
        if current:
            if custom_delta:
                data["custom_fields"] = {"partial": custom_delta}
        elif isinstance(source.get("custom_fields"), dict) and source.get("custom_fields"):
            data["custom_fields"] = source["custom_fields"]
        return data

    def resolve_group(self, name: str) -> Optional[str]:
        key = normalize_name(name)
        if not key:
            return None
        if key in self.resolved_groups:
            return self.resolved_groups[key]
        if key in self._group_resolution_stack:
            return None
        self._group_resolution_stack.add(key)
        try:
            source = self._source_entity(self.old_groups_by_name, name)
            current, ambiguous = self._group_match(name)
            if ambiguous:
                self.stats["ambiguous_entities_skipped"] += 1
                log("WARNING", f"Group '{name}' has ambiguous current matches; relationship skipped.")
                self.resolved_groups[key] = None
                return None
            if current is None:
                self.stats["created_groups"] += 1
                if self.dry_run:
                    synthetic = "DRYRUN:GROUP:" + key
                    self.resolved_groups[key] = synthetic
                    return synthetic
                created = self.stash.create_group(self._group_input(source))
                self.groups.append(created)
                self.groups_by_id[str(created["id"])] = created
                current = created
            else:
                self.stats["reused_groups"] += 1

            group_id = str(current["id"])
            if not self.dry_run:
                update = self._group_input(source, current)
                update["id"] = group_id
                current = self.stash.update_group(update)
                self._replace_entity(self.groups, current)
                self.groups_by_id[group_id] = current
            self.resolved_groups[key] = group_id
            return group_id
        finally:
            self._group_resolution_stack.discard(key)

    def restore_group_hierarchy(self) -> None:
        for source in self.old_groups:
            name = str(source.get("name") or "").strip()
            group_id = self.resolve_group(name)
            if not group_id or group_id.startswith("DRYRUN:"):
                continue
            current = self.groups_by_id.get(group_id) or {}
            existing: Dict[str, Optional[str]] = {
                str(item.get("group", {}).get("id") or ""): item.get("description")
                for item in (current.get("sub_groups") or [])
                if str(item.get("group", {}).get("id") or "")
            }
            changed = False
            for item in source.get("sub_groups") or []:
                child_name = str(item.get("name") or "").strip()
                child_id = self.resolve_group(child_name)
                if not child_id or child_id == group_id or child_id.startswith("DRYRUN:"):
                    continue
                if child_id not in existing:
                    existing[child_id] = item.get("description")
                    changed = True
            if changed and not self.dry_run:
                self.stash.update_group({
                    "id": group_id,
                    "sub_groups": [
                        {"group_id": child_id, "description": description}
                        for child_id, description in existing.items()
                    ],
                })

    def restore_tag_hierarchy(self) -> None:
        tags_by_id = {str(tag.get("id") or ""): tag for tag in self.tags}
        for source in self.old_tags:
            name = str(source.get("name") or "").strip()
            tag_id = self.resolve_tag(name)
            if not tag_id or tag_id.startswith("DRYRUN:"):
                continue
            current = tags_by_id.get(tag_id) or {}
            parent_ids = [
                str(parent.get("id"))
                for parent in (current.get("parents") or [])
                if parent.get("id")
            ]
            before = set(parent_ids)
            for parent_name in source.get("parents") or []:
                parent_id = self.resolve_tag(str(parent_name))
                if parent_id and not parent_id.startswith("DRYRUN:") and parent_id != tag_id:
                    parent_ids.append(parent_id)
            parent_ids = _unique_ids(parent_ids)
            added = len(set(parent_ids) - before)
            if added:
                self.stats["tag_parent_links_added"] += added
                if not self.dry_run:
                    updated = self.stash.update_tag({"id": tag_id, "parent_ids": parent_ids})
                    self._replace_entity(self.tags, updated)
                    tags_by_id[tag_id] = updated

    def prepare_metadata_entities(self) -> None:
        for source in self.old_tags:
            self.resolve_tag(str(source.get("name") or ""))
        for source in self.old_performers:
            self.resolve_performer(str(source.get("name") or ""))
        for source in self.old_studios:
            self.resolve_studio(str(source.get("name") or ""))
        self.restore_tag_hierarchy()
        for source in self.old_groups:
            self.resolve_group(str(source.get("name") or ""))
        for source in self.old_galleries:
            self.resolve_gallery_source(source)
        self.restore_group_hierarchy()

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

    def _restore_scene_markers(
        self,
        old: Mapping[str, Any],
        current: Mapping[str, Any],
    ) -> bool:
        changed = False
        existing = list(current.get("scene_markers") or [])
        for marker in old.get("markers") or []:
            title = str(marker.get("title") or "").strip()
            seconds = _clean_float(marker.get("seconds"))
            if not title or seconds is None:
                continue
            primary_name = str(marker.get("primary_tag") or "").strip()
            primary_id = self.resolve_tag(primary_name) if primary_name else None
            if not primary_id:
                continue

            tag_ids: List[str] = []
            for tag_name in marker.get("tags") or []:
                tag_id = self.resolve_tag(str(tag_name))
                if tag_id and not tag_id.startswith("DRYRUN:"):
                    tag_ids.append(tag_id)
            tag_ids.append(primary_id)
            tag_ids = _unique_ids(tag_ids)

            current_marker = next(
                (
                    item for item in existing
                    if normalize_name(str(item.get("title") or "")) == normalize_name(title)
                    and abs(float(item.get("seconds") or 0.0) - float(seconds)) < 0.001
                ),
                None,
            )
            end_seconds = _clean_float(marker.get("end_seconds"))
            if current_marker is None:
                self.stats["scene_markers_created"] += 1
                changed = True
                if not self.dry_run:
                    created = self.stash.create_scene_marker({
                        "title": title,
                        "seconds": seconds,
                        "end_seconds": end_seconds,
                        "scene_id": str(current["id"]),
                        "primary_tag_id": primary_id,
                        "tag_ids": tag_ids,
                    })
                    existing.append(created)
                continue

            existing_tag_ids = [
                str(tag.get("id"))
                for tag in (current_marker.get("tags") or [])
                if tag.get("id")
            ]
            current_primary = current_marker.get("primary_tag") or {}
            current_primary_id = str(current_primary.get("id") or "")
            merged_tags = _unique_ids(
                existing_tag_ids
                + tag_ids
                + ([current_primary_id] if current_primary_id else [])
            )
            update: Dict[str, Any] = {"id": str(current_marker["id"])}
            if merged_tags != existing_tag_ids:
                update["tag_ids"] = merged_tags
            if current_marker.get("end_seconds") is None and end_seconds is not None:
                update["end_seconds"] = end_seconds
            if len(update) > 1:
                self.stats["scene_markers_updated"] += 1
                changed = True
                if not self.dry_run:
                    self.stash.update_scene_marker(update)
        return changed

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

        gallery_ids = [
            str(item.get("id"))
            for item in (current.get("galleries") or [])
            if item.get("id")
        ]
        for ref in old.get("galleries") or []:
            if isinstance(ref, dict):
                gallery_id = self.resolve_gallery_ref(ref)
                if gallery_id:
                    gallery_ids.append(gallery_id)
        gallery_ids = _unique_ids(gallery_ids)
        current_gallery_ids = [
            str(item.get("id"))
            for item in (current.get("galleries") or [])
            if item.get("id")
        ]
        if gallery_ids != current_gallery_ids:
            input_data["gallery_ids"] = gallery_ids

        old_studio = str(old.get("studio") or "").strip()
        if old_studio and not current.get("studio"):
            studio_id = self.resolve_studio(old_studio)
            if studio_id:
                input_data["studio_id"] = studio_id

        scene_groups: Dict[str, Optional[int]] = {}
        for relation in current.get("groups") or []:
            group = relation.get("group") or {}
            group_id = str(group.get("id") or "")
            if group_id:
                scene_groups[group_id] = _clean_int(relation.get("scene_index"))
        for relation in old.get("movies") or []:
            if not isinstance(relation, dict):
                continue
            group_name = str(relation.get("movieName") or relation.get("groupName") or "").strip()
            if not group_name:
                continue
            group_id = self.resolve_group(group_name)
            if not group_id:
                continue
            old_index = _clean_int(relation.get("scene_index"))
            if group_id not in scene_groups:
                scene_groups[group_id] = old_index
            elif scene_groups[group_id] is None and old_index is not None:
                scene_groups[group_id] = old_index
        current_group_map = {
            str((relation.get("group") or {}).get("id") or ""): _clean_int(relation.get("scene_index"))
            for relation in (current.get("groups") or [])
            if str((relation.get("group") or {}).get("id") or "")
        }
        if scene_groups != current_group_map:
            input_data["groups"] = [
                {"group_id": group_id, "scene_index": scene_index}
                for group_id, scene_index in scene_groups.items()
            ]

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

        changed = len(input_data) > 1 or bool(old_plays) or bool(old_os)
        if len(input_data) > 1 and not self.dry_run:
            self.stash.update_scene(input_data)
        if not self.dry_run:
            if old_plays:
                self.stash.add_scene_plays(str(current["id"]), old_plays)
            if old_os:
                self.stash.add_scene_os(str(current["id"]), old_os)

        if self._restore_scene_markers(old, current):
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

        gallery_ids = [
            str(item.get("id"))
            for item in (current.get("galleries") or [])
            if item.get("id")
        ]
        for ref in old.get("galleries") or []:
            if isinstance(ref, dict):
                gallery_id = self.resolve_gallery_ref(ref)
                if gallery_id:
                    gallery_ids.append(gallery_id)
        gallery_ids = _unique_ids(gallery_ids)
        current_gallery_ids = [
            str(item.get("id"))
            for item in (current.get("galleries") or [])
            if item.get("id")
        ]
        if gallery_ids != current_gallery_ids:
            input_data["gallery_ids"] = gallery_ids

        old_studio = str(old.get("studio") or "").strip()
        if old_studio and not current.get("studio"):
            studio_id = self.resolve_studio(old_studio)
            if studio_id:
                input_data["studio_id"] = studio_id

        custom_delta = _custom_field_delta(current.get("custom_fields"), old.get("custom_fields"))
        if custom_delta:
            input_data["custom_fields"] = {"partial": custom_delta}

        current_o = _clean_int(current.get("o_counter")) or 0
        old_o = _clean_int(old.get("o_counter")) or 0
        o_delta = max(0, old_o - current_o)
        changed = len(input_data) > 1 or o_delta > 0
        if len(input_data) > 1 and not self.dry_run:
            self.stash.update_image(input_data)
        if o_delta:
            self.stats["image_o_increments"] += o_delta
            if not self.dry_run:
                self.stash.increment_image_o(str(current["id"]), o_delta)
        return changed

    def _preflight_media_and_infer_galleries(self) -> Dict[str, int]:
        summary = {
            "matched": 0,
            "unmatched": 0,
            "ambiguous": 0,
            "collisions": 0,
        }
        gallery_evidence: Dict[str, List[set]] = {}
        used_scene_targets = set()
        used_image_targets = set()

        def inspect(
            old_items: Sequence[Dict[str, Any]],
            current_by_id: Mapping[str, Dict[str, Any]],
            fp_index,
            path_index,
            used_targets: set,
        ) -> None:
            for old in old_items:
                match = match_old_media(
                    old.get("files") or [],
                    self.old_files,
                    fp_index,
                    path_index,
                )
                if not match.object_id:
                    summary["ambiguous" if match.kind == "ambiguous" else "unmatched"] += 1
                    continue
                if match.object_id in used_targets:
                    summary["collisions"] += 1
                    continue
                used_targets.add(match.object_id)
                summary["matched"] += 1
                current = current_by_id.get(str(match.object_id)) or {}
                current_gallery_ids = {
                    str(gallery.get("id") or "")
                    for gallery in (current.get("galleries") or [])
                    if str(gallery.get("id") or "")
                }
                if not current_gallery_ids:
                    continue
                for ref in old.get("galleries") or []:
                    if not isinstance(ref, dict):
                        continue
                    relation_key = self._gallery_relation_key(ref)
                    if relation_key:
                        gallery_evidence.setdefault(relation_key, []).append(set(current_gallery_ids))

        inspect(
            self.old_scenes,
            self.scenes_by_id,
            self.scene_fp_index,
            self.scene_path_index,
            used_scene_targets,
        )
        inspect(
            self.old_images,
            self.images_by_id,
            self.image_fp_index,
            self.image_path_index,
            used_image_targets,
        )

        for relation_key, evidence_sets in gallery_evidence.items():
            if not evidence_sets:
                continue
            intersection = set.intersection(*evidence_sets)
            if len(intersection) == 1:
                self.inferred_gallery_map[relation_key] = next(iter(intersection))

        total = len(self.old_scenes) + len(self.old_images)
        if total:
            rate = summary["matched"] / total
            log(
                "INFO",
                f"Preflight media identity: {summary['matched']}/{total} uniquely matched "
                f"({rate * 100:.1f}%); {summary['unmatched']} unmatched, "
                f"{summary['ambiguous']} ambiguous, {summary['collisions']} source-target collision(s).",
            )
            if self.inferred_gallery_map:
                log(
                    "INFO",
                    f"Inferred {len(self.inferred_gallery_map)} changed-path Gallery mapping(s) "
                    "from consistent matched-media relationships.",
                )
            if not self.dry_run and total >= 100 and rate < 0.70:
                raise RuntimeError(
                    "Restore aborted before making changes: fewer than 70% of old media records "
                    "match uniquely to this Stash. This usually means the wrong export/library "
                    "was selected or the new Stash has not finished scanning."
                )
        return summary

    def run(self) -> Dict[str, int]:
        mode = "ANALYZE" if self.dry_run else "RESTORE"
        log(
            "INFO",
            f"{mode}: old export has {len(self.old_scenes)} scene JSON file(s), "
            f"{len(self.old_images)} image JSON file(s), {len(self.old_galleries)} gallery JSON file(s), "
            f"{len(self.old_groups)} group JSON file(s), and {len(self.old_files)} file record(s).",
        )

        self._preflight_media_and_infer_galleries()
        if not self.dry_run:
            log("INFO", "Creating automatic Stash database backup before migration changes...")
            self.stash.backup_database()
            log("INFO", "Database backup completed successfully.")

        self.prepare_metadata_entities()

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
                display = str(old.get("title") or "").strip()
                if not display:
                    files = old.get("files") or []
                    display = str(files[0]) if files else "(untitled)"
                log("WARNING", f"Scene '{display}': {match.detail}; skipped.")
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
                display = str(old.get("title") or "").strip()
                if not display:
                    files = old.get("files") or []
                    display = str(files[0]) if files else "(untitled)"
                log("WARNING", f"Image '{display}': {match.detail}; skipped.")
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
        log(
            "INFO",
            f"Entity/relationship summary: Tags reused/created="
            f"{self.stats['reused_tags']}/{self.stats['created_tags']} "
            f"(external-ID reuse={self.stats['stash_id_tag_reuse']}, "
            f"creation-guard skips={self.stats['tag_creation_guard_skips']}, "
            f"alias-collision skips={self.stats['tag_alias_collision_skips']}); "
            f"Performer/Studio alias-collision skips="
            f"{self.stats['performer_alias_collision_skips']}/{self.stats['studio_alias_collision_skips']}; "
            f"safe duplicate merges Tags={self.stats['merged_duplicate_tags']}, "
            f"Performers={self.stats['merged_duplicate_performers']}, "
            f"Studios={self.stats['merged_duplicate_studios']}; "
            f"Galleries reused/created={self.stats['reused_galleries']}/{self.stats['created_galleries']}; "
            f"Groups reused/created={self.stats['reused_groups']}/{self.stats['created_groups']}; "
            f"Gallery chapters={self.stats['gallery_chapters_created']}; "
            f"Scene markers created/updated={self.stats['scene_markers_created']}/{self.stats['scene_markers_updated']}; "
            f"Entity artwork restored/skipped={self.stats['entity_images_restored']}/{self.stats['entity_images_skipped_invalid']}.",
        )
        if self.dry_run:
            log(
                "INFO",
                "No changes were made. Restore will update only already-existing matched "
                "Scenes/Images; metadata-only Groups/user Galleries may be created, while "
                "file-backed Galleries must resolve safely. Scene/Image/File records are never created.",
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
