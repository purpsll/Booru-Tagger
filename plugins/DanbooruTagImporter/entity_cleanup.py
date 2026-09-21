"""Conservative duplicate cleanup planning for Stash Performers and Studios."""

from __future__ import annotations

import difflib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Sequence, Tuple


@dataclass(frozen=True)
class EntityCleanupCandidate:
    destination_id: str
    destination_name: str
    source_ids: Tuple[str, ...]
    source_names: Tuple[str, ...]
    score: float
    kind: str
    reason: str
    auto_merge: bool


@dataclass(frozen=True)
class EntityCleanupPlan:
    safe_merges: Tuple[EntityCleanupCandidate, ...]
    review: Tuple[EntityCleanupCandidate, ...]


def normalized_cleanup_name(name: str) -> str:
    value = unicodedata.normalize("NFKC", str(name or "")).casefold().strip()
    value = value.replace("_", " ").replace("-", " ")
    value = re.sub(r"[^\w\s]+", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def compact_cleanup_name(name: str) -> str:
    return "".join(ch for ch in normalized_cleanup_name(name) if ch.isalnum())


def _json_key(value: Any) -> str:
    if not value:
        return ""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _custom_image_key(entity: Dict[str, Any]) -> str:
    path = str(entity.get("image_path") or "").strip()
    if not path or "default=true" in path.casefold():
        return ""
    return path


def _clean_ids(items: Iterable[Dict[str, Any]]) -> Tuple[str, ...]:
    return tuple(sorted({
        str(item.get("id") or "")
        for item in items
        if str(item.get("id") or "")
    }))


def _performer_signature(entity: Dict[str, Any]) -> Tuple[Any, ...]:
    fields = (
        "disambiguation", "gender", "birthdate", "ethnicity", "country",
        "eye_color", "height_cm", "measurements", "fake_tits",
        "penis_length", "circumcised", "career_start", "career_end",
        "tattoos", "piercings", "rating100", "details", "death_date",
        "hair_color", "weight",
    )
    return tuple(entity.get(field) or "" for field in fields) + (
        bool(entity.get("favorite")),
        bool(entity.get("ignore_auto_tag")),
        _custom_image_key(entity),
        _json_key(entity.get("custom_fields")),
    )


def _studio_signature(entity: Dict[str, Any]) -> Tuple[Any, ...]:
    parent = entity.get("parent_studio") or {}
    return (
        str(parent.get("id") or ""),
        _clean_ids(entity.get("child_studios") or []),
        entity.get("rating100") or "",
        str(entity.get("details") or "").strip(),
        bool(entity.get("favorite")),
        bool(entity.get("ignore_auto_tag")),
        bool(entity.get("organized")),
        _custom_image_key(entity),
        _json_key(entity.get("custom_fields")),
    )


def _signature(entity: Dict[str, Any], entity_type: str) -> Tuple[Any, ...]:
    if entity_type == "performer":
        return _performer_signature(entity)
    if entity_type == "studio":
        return _studio_signature(entity)
    raise ValueError(f"Unsupported entity type: {entity_type}")


def _metadata_nontrivial(entity: Dict[str, Any], entity_type: str) -> bool:
    return any(bool(value) for value in _signature(entity, entity_type))


def _usage_count(entity: Dict[str, Any], entity_type: str) -> int:
    fields = (
        ("scene_count", "image_count", "gallery_count", "group_count")
        if entity_type == "performer"
        else ("scene_count", "image_count", "gallery_count", "group_count")
    )
    total = 0
    for field in fields:
        try:
            total += int(entity.get(field) or 0)
        except (TypeError, ValueError):
            pass
    return total


def _booru_style_rank(name: str) -> int:
    return 0 if re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+)*", str(name or "")) else 1


def _rank(entity: Dict[str, Any], entity_type: str) -> Tuple[Any, ...]:
    return (
        -_usage_count(entity, entity_type),
        _booru_style_rank(str(entity.get("name") or "")),
        str(entity.get("name") or "").casefold(),
        str(entity.get("id") or ""),
    )


def _stash_identity_conflict(group: Sequence[Dict[str, Any]]) -> bool:
    by_endpoint: Dict[str, str] = {}
    for entity in group:
        for stash_id in entity.get("stash_ids") or []:
            endpoint = str(stash_id.get("endpoint") or "").casefold().strip()
            sid = str(stash_id.get("stash_id") or "").strip()
            if not endpoint or not sid:
                continue
            previous = by_endpoint.get(endpoint)
            if previous is not None and previous != sid:
                return True
            by_endpoint[endpoint] = sid
    return False


def _choose_destination(
    group: Sequence[Dict[str, Any]],
    entity_type: str,
) -> Tuple[Dict[str, Any], str]:
    if _stash_identity_conflict(group):
        return min(group, key=lambda obj: _rank(obj, entity_type)), "stash-id-conflict"

    rich = [obj for obj in group if _metadata_nontrivial(obj, entity_type)]
    if len(rich) > 1:
        signatures = {_signature(obj, entity_type) for obj in rich}
        if len(signatures) > 1:
            return min(group, key=lambda obj: _rank(obj, entity_type)), "metadata-conflict"

    pool = rich or list(group)
    return min(pool, key=lambda obj: _rank(obj, entity_type)), ""


class _UnionFind:
    def __init__(self, ids: Iterable[str]):
        self.parent = {item: item for item in ids}

    def find(self, item: str) -> str:
        parent = self.parent[item]
        if parent != item:
            self.parent[item] = self.find(parent)
        return self.parent[item]

    def union(self, left: str, right: str) -> None:
        a, b = self.find(left), self.find(right)
        if a != b:
            self.parent[b] = a


def _external_alias_conflict(
    group: Sequence[Dict[str, Any]],
    all_entities: Sequence[Dict[str, Any]],
    alias_field: str,
) -> bool:
    group_ids = {str(obj.get("id") or "") for obj in group}
    desired = set()
    for obj in group:
        desired.add(str(obj.get("name") or "").casefold().strip())
        desired.update(
            str(alias or "").casefold().strip()
            for alias in (obj.get(alias_field) or [])
            if str(alias or "").strip()
        )

    for obj in all_entities:
        if str(obj.get("id") or "") in group_ids:
            continue
        claims = {str(obj.get("name") or "").casefold().strip()}
        claims.update(
            str(alias or "").casefold().strip()
            for alias in (obj.get(alias_field) or [])
            if str(alias or "").strip()
        )
        if desired & claims:
            return True
    return False


def build_entity_cleanup_plan(
    entities: Sequence[Dict[str, Any]],
    *,
    entity_type: str,
    alias_field: str,
    fuzzy_threshold: float,
) -> EntityCleanupPlan:
    if entity_type not in {"performer", "studio"}:
        raise ValueError("entity_type must be performer or studio")

    unique = {
        str(obj.get("id")): obj
        for obj in entities
        if str(obj.get("id") or "") and str(obj.get("name") or "").strip()
    }
    ordered = list(unique.values())
    uf = _UnionFind(unique.keys())
    primary: Dict[str, List[str]] = {}
    aliases: List[Tuple[str, str]] = []

    for obj in ordered:
        obj_id = str(obj["id"])
        key = compact_cleanup_name(str(obj.get("name") or ""))
        if len(key) >= 4:
            primary.setdefault(key, []).append(obj_id)
        for alias in obj.get(alias_field) or []:
            alias_key = compact_cleanup_name(str(alias or ""))
            if len(alias_key) >= 4:
                aliases.append((obj_id, alias_key))

    for ids in primary.values():
        if len(ids) > 1:
            for other in ids[1:]:
                uf.union(ids[0], other)

    for obj_id, alias_key in aliases:
        for other in primary.get(alias_key, ()):
            if obj_id != other:
                uf.union(obj_id, other)

    groups: Dict[str, List[Dict[str, Any]]] = {}
    for obj in ordered:
        groups.setdefault(uf.find(str(obj["id"])), []).append(obj)

    safe: List[EntityCleanupCandidate] = []
    review: List[EntityCleanupCandidate] = []

    for group in groups.values():
        if len(group) < 2:
            continue
        destination, conflict = _choose_destination(group, entity_type)
        if not conflict and _external_alias_conflict(group, ordered, alias_field):
            conflict = "external-name-conflict"

        sources = sorted(
            (obj for obj in group if str(obj["id"]) != str(destination["id"])),
            key=lambda obj: _rank(obj, entity_type),
        )
        normalized = {
            normalized_cleanup_name(str(obj.get("name") or "")) for obj in group
        }
        kind = "normalized-format" if len(normalized) == 1 else "joined-format"
        reason = "Names differ only by case/separators/punctuation or an existing alias."
        if conflict == "stash-id-conflict":
            reason = "Same-endpoint Stash IDs disagree; these may be different entities."
        elif conflict == "metadata-conflict":
            reason = "Formatting duplicate detected, but rich metadata disagrees."
        elif conflict == "external-name-conflict":
            reason = "A resulting name/alias is already claimed by another entity."

        candidate = EntityCleanupCandidate(
            destination_id=str(destination["id"]),
            destination_name=str(destination.get("name") or ""),
            source_ids=tuple(str(obj["id"]) for obj in sources),
            source_names=tuple(str(obj.get("name") or "") for obj in sources),
            score=1.0,
            kind=kind if not conflict else f"{kind}-{conflict}",
            reason=reason,
            auto_merge=not conflict,
        )
        (safe if candidate.auto_merge else review).append(candidate)

    threshold = max(0.0, min(1.0, float(fuzzy_threshold)))
    buckets: Dict[Tuple[str, int], List[Tuple[str, Dict[str, Any]]]] = {}
    for obj in ordered:
        norm = normalized_cleanup_name(str(obj.get("name") or ""))
        if len(norm) >= 5:
            buckets.setdefault((norm[0], len(norm)), []).append((norm, obj))

    seen_pairs = set()
    for obj in ordered:
        left_id = str(obj["id"])
        left_norm = normalized_cleanup_name(str(obj.get("name") or ""))
        if len(left_norm) < 5:
            continue
        for length in range(max(5, len(left_norm) - 3), len(left_norm) + 4):
            for right_norm, other in buckets.get((left_norm[0], length), []):
                right_id = str(other["id"])
                if right_id == left_id or uf.find(right_id) == uf.find(left_id):
                    continue
                pair = tuple(sorted((left_id, right_id)))
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                ratio = difflib.SequenceMatcher(None, left_norm, right_norm).ratio()
                if ratio < threshold:
                    continue
                destination, _ = _choose_destination([obj, other], entity_type)
                source = other if str(destination["id"]) == left_id else obj
                review.append(EntityCleanupCandidate(
                    destination_id=str(destination["id"]),
                    destination_name=str(destination.get("name") or ""),
                    source_ids=(str(source["id"]),),
                    source_names=(str(source.get("name") or ""),),
                    score=ratio,
                    kind="fuzzy",
                    reason="High textual similarity only; manual review required.",
                    auto_merge=False,
                ))

    safe.sort(key=lambda c: (c.destination_name.casefold(), c.source_names))
    review.sort(key=lambda c: (-c.score, c.destination_name.casefold(), c.source_names))
    return EntityCleanupPlan(tuple(safe), tuple(review))


def merged_entity_values(
    entities_by_id: Dict[str, Dict[str, Any]],
    candidate: EntityCleanupCandidate,
    *,
    alias_field: str,
) -> Dict[str, Any]:
    ids = (candidate.destination_id,) + candidate.source_ids
    group = [entities_by_id[item] for item in ids]
    destination = entities_by_id[candidate.destination_id]
    destination_name = str(destination.get("name") or "").casefold().strip()

    aliases: List[str] = []
    urls: List[str] = []
    tag_ids: List[str] = []
    stash_ids: List[Dict[str, str]] = []
    seen_aliases = set()
    seen_urls = set()
    seen_tags = set()
    seen_stash = set()

    for obj in group:
        values = [str(obj.get("name") or "")]
        values.extend(str(alias or "") for alias in (obj.get(alias_field) or []))
        for value in values:
            value = value.strip()
            key = value.casefold()
            if not value or key == destination_name or key in seen_aliases:
                continue
            seen_aliases.add(key)
            aliases.append(value)

        for value in obj.get("urls") or []:
            value = str(value or "").strip()
            key = value.casefold()
            if value and key not in seen_urls:
                seen_urls.add(key)
                urls.append(value)

        for tag in obj.get("tags") or []:
            tag_id = str(tag.get("id") or "")
            if tag_id and tag_id not in seen_tags:
                seen_tags.add(tag_id)
                tag_ids.append(tag_id)

        for stash_id in obj.get("stash_ids") or []:
            endpoint = str(stash_id.get("endpoint") or "").strip()
            sid = str(stash_id.get("stash_id") or "").strip()
            key = (endpoint.casefold(), sid)
            if endpoint and sid and key not in seen_stash:
                seen_stash.add(key)
                stash_ids.append({"endpoint": endpoint, "stash_id": sid})

    return {
        alias_field: aliases,
        "urls": urls,
        "tag_ids": tag_ids,
        "stash_ids": stash_ids,
    }
