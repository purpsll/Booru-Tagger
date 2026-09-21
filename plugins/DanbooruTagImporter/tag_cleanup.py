"""Conservative similar-tag cleanup planning for Booru Importer."""

from __future__ import annotations

import difflib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Sequence, Tuple

_KNOWN_PREFIXES = {"artist", "character", "copyright", "general", "meta"}


@dataclass(frozen=True)
class TagCleanupCandidate:
    destination_id: str
    destination_name: str
    source_ids: Tuple[str, ...]
    source_names: Tuple[str, ...]
    score: float
    kind: str
    reason: str
    auto_merge: bool


@dataclass(frozen=True)
class TagCleanupPlan:
    safe_merges: Tuple[TagCleanupCandidate, ...]
    review: Tuple[TagCleanupCandidate, ...]


def normalized_cleanup_name(name: str) -> str:
    value = unicodedata.normalize("NFKC", str(name or "")).casefold().strip()
    if ":" in value:
        prefix, rest = value.split(":", 1)
        if prefix.strip() in _KNOWN_PREFIXES:
            value = rest.strip()
    value = value.replace("_", " ").replace("-", " ")
    value = re.sub(r"[^\w\s]+", " ", value, flags=re.UNICODE)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def compact_cleanup_name(name: str) -> str:
    return "".join(ch for ch in normalized_cleanup_name(name) if ch.isalnum())


def _usage_count(tag: Dict[str, Any]) -> int:
    fields = (
        "scene_count", "scene_marker_count", "image_count", "gallery_count",
        "performer_count", "studio_count", "group_count",
    )
    total = 0
    for field in fields:
        try:
            total += int(tag.get(field) or 0)
        except (TypeError, ValueError):
            continue
    return total


def _clean_ids(items: Iterable[Dict[str, Any]]) -> Tuple[str, ...]:
    return tuple(sorted({
        str(item.get("id") or "")
        for item in items
        if str(item.get("id") or "")
    }))


def _custom_fields_key(value: Any) -> str:
    if not value:
        return ""
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    except TypeError:
        return str(value)


def _metadata_signature(tag: Dict[str, Any]) -> Tuple[Any, ...]:
    return (
        str(tag.get("description") or "").strip(),
        str(tag.get("sort_name") or "").strip(),
        bool(tag.get("favorite")),
        bool(tag.get("ignore_auto_tag")),
        (str(tag.get("image_path") or "").strip() if "default=true" not in str(tag.get("image_path") or "").casefold() else ""),
        _custom_fields_key(tag.get("custom_fields")),
        _clean_ids(tag.get("parents") or []),
        _clean_ids(tag.get("children") or []),
    )


def _metadata_nontrivial(tag: Dict[str, Any]) -> bool:
    return any(bool(part) for part in _metadata_signature(tag))


def _booru_style_rank(name: str) -> int:
    value = str(name or "")
    return 0 if re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+)*", value) else 1


def _rank_tag(tag: Dict[str, Any]) -> Tuple[Any, ...]:
    return (
        -_usage_count(tag),
        _booru_style_rank(str(tag.get("name") or "")),
        str(tag.get("name") or "").casefold(),
        str(tag.get("id") or ""),
    )


def _choose_destination(
    group: Sequence[Dict[str, Any]],
) -> Tuple[Dict[str, Any], bool]:
    """Choose the survivor and report whether rich metadata conflicts.

    A single tag carrying description/sort/image/custom-field/hierarchy metadata
    wins even when another formatting duplicate has more media uses. This avoids
    silently deleting metadata that Stash's native tagsMerge does not merge.
    When multiple duplicates contain differing rich metadata, the entire group is
    review-only.
    """
    rich = [tag for tag in group if _metadata_nontrivial(tag)]
    if len(rich) > 1:
        signatures = {_metadata_signature(tag) for tag in rich}
        if len(signatures) > 1:
            return min(group, key=_rank_tag), True
    pool = rich or list(group)
    return min(pool, key=_rank_tag), False


class _UnionFind:
    def __init__(self, ids: Iterable[str]):
        self.parent = {value: value for value in ids}

    def find(self, value: str) -> str:
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: str, right: str) -> None:
        a, b = self.find(left), self.find(right)
        if a != b:
            self.parent[b] = a


def native_merge_alias_preflight(
    tags: Sequence[Dict[str, Any]],
    candidate: TagCleanupCandidate,
) -> Tuple[Dict[str, Tuple[str, ...]], Tuple[Tuple[str, str], ...]]:
    """Find alias rows that would make Stash v0.31.1 tagsMerge fail.

    Stash inserts each source tag name into tag_aliases before moving source
    aliases. The alias column is globally UNIQUE. An alias already owned by a
    tag inside this same merge group can be removed temporarily because the
    native merge immediately recreates the source name on the destination.
    An alias owned by an unrelated tag is a semantic conflict and must block
    unattended merging.
    """
    tags_by_id = {
        str(tag.get("id") or ""): tag
        for tag in tags
        if str(tag.get("id") or "")
    }
    group_ids = {
        str(candidate.destination_id),
        *[str(tag_id) for tag_id in candidate.source_ids],
    }

    alias_owners: Dict[str, str] = {}
    for tag in tags:
        tag_id = str(tag.get("id") or "")
        for alias in tag.get("aliases") or []:
            key = str(alias or "").casefold().strip()
            if key:
                alias_owners[key] = tag_id

    removals: Dict[str, set] = {}
    conflicts: List[Tuple[str, str]] = []
    for source_id in candidate.source_ids:
        source = tags_by_id.get(str(source_id)) or {}
        source_name = str(source.get("name") or "").strip()
        if not source_name:
            continue
        key = source_name.casefold()
        owner_id = alias_owners.get(key)
        if not owner_id:
            continue
        if owner_id in group_ids:
            removals.setdefault(owner_id, set()).add(key)
        else:
            owner = tags_by_id.get(owner_id) or {}
            conflicts.append((source_name, str(owner.get("name") or owner_id)))

    return (
        {
            owner_id: tuple(sorted(keys))
            for owner_id, keys in removals.items()
        },
        tuple(conflicts),
    )


def build_tag_cleanup_plan(
    tags: Sequence[Dict[str, Any]],
    *,
    protected_names: Iterable[str] = (),
    fuzzy_threshold: float = 0.96,
) -> TagCleanupPlan:
    """Return safe auto-merges and review-only fuzzy/conflicting candidates.

    Safe groups contain exactly the same letters/numbers after only harmless
    formatting normalization (case, whitespace, underscores, hyphens,
    punctuation, and legacy source-category prefixes), or an existing alias
    explicitly points at another tag's primary name.

    Fuzzy similarity is never auto-merged.
    """
    unique: Dict[str, Dict[str, Any]] = {}
    for tag in tags:
        tag_id = str(tag.get("id") or "")
        name = str(tag.get("name") or "").strip()
        if tag_id and name:
            unique[tag_id] = tag
    ordered = list(unique.values())

    protected = {str(name).casefold().strip() for name in protected_names}
    protected_ids = {
        str(tag["id"])
        for tag in ordered
        if str(tag.get("name") or "").casefold().strip() in protected
    }

    uf = _UnionFind(unique.keys())
    primary: Dict[str, List[str]] = {}
    aliases: List[Tuple[str, str]] = []

    for tag in ordered:
        tag_id = str(tag["id"])
        if tag_id in protected_ids:
            continue

        key = compact_cleanup_name(str(tag.get("name") or ""))
        # Short tags are too collision-prone for unattended cleanup.
        if len(key) >= 4:
            primary.setdefault(key, []).append(tag_id)

        for alias in tag.get("aliases") or []:
            alias_key = compact_cleanup_name(str(alias or ""))
            if len(alias_key) >= 4:
                aliases.append((tag_id, alias_key))

    for ids in primary.values():
        if len(ids) > 1:
            base = ids[0]
            for other in ids[1:]:
                uf.union(base, other)

    # An existing alias is an explicit synonym declaration. Alias-to-primary
    # collisions are therefore safe formatting duplicate signals; alias-to-alias
    # collisions alone are not used.
    for tag_id, alias_key in aliases:
        for other in primary.get(alias_key, ()):
            if other != tag_id:
                uf.union(tag_id, other)

    groups: Dict[str, List[Dict[str, Any]]] = {}
    for tag in ordered:
        tag_id = str(tag["id"])
        if tag_id in protected_ids:
            continue
        groups.setdefault(uf.find(tag_id), []).append(tag)

    safe: List[TagCleanupCandidate] = []
    review: List[TagCleanupCandidate] = []

    for group in groups.values():
        if len(group) < 2:
            continue

        destination, conflict = _choose_destination(group)
        sources = sorted(
            (
                tag
                for tag in group
                if str(tag["id"]) != str(destination["id"])
            ),
            key=_rank_tag,
        )
        norms = {
            normalized_cleanup_name(str(tag.get("name") or ""))
            for tag in group
        }
        kind = "normalized-format" if len(norms) == 1 else "joined-format"

        candidate = TagCleanupCandidate(
            destination_id=str(destination["id"]),
            destination_name=str(destination.get("name") or ""),
            source_ids=tuple(str(tag["id"]) for tag in sources),
            source_names=tuple(str(tag.get("name") or "") for tag in sources),
            score=1.0,
            kind=kind if not conflict else f"{kind}-metadata-conflict",
            reason=(
                "Names differ only by case/separators/punctuation or an existing alias."
                if not conflict
                else "Formatting duplicate detected, but both tags contain differing metadata."
            ),
            auto_merge=not conflict,
        )
        (safe if candidate.auto_merge else review).append(candidate)

    # Fuzzy candidates are intentionally review-only. Bucket by first character
    # and similar length so large tag libraries do not require O(n^2) comparisons.
    threshold = max(0.0, min(1.0, float(fuzzy_threshold)))
    buckets: Dict[Tuple[str, int], List[Tuple[str, Dict[str, Any]]]] = {}

    for tag in ordered:
        tag_id = str(tag["id"])
        if tag_id in protected_ids:
            continue
        norm = normalized_cleanup_name(str(tag.get("name") or ""))
        if len(norm) < 5:
            continue
        buckets.setdefault((norm[0], len(norm)), []).append((norm, tag))

    seen_pairs = set()
    for tag in ordered:
        left_id = str(tag["id"])
        if left_id in protected_ids:
            continue
        left_norm = normalized_cleanup_name(str(tag.get("name") or ""))
        if len(left_norm) < 5:
            continue

        for length in range(max(5, len(left_norm) - 2), len(left_norm) + 3):
            for right_norm, other in buckets.get((left_norm[0], length), []):
                right_id = str(other["id"])
                if (
                    right_id == left_id
                    or uf.find(right_id) == uf.find(left_id)
                ):
                    continue

                pair = tuple(sorted((left_id, right_id)))
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)

                # Do not flag obvious parent/child-style names where one tag simply
                # adds a whole token, such as "anal" versus "anal sex".
                left_tokens = set(left_norm.split())
                right_tokens = set(right_norm.split())
                if left_tokens and right_tokens and (
                    left_tokens < right_tokens
                    or right_tokens < left_tokens
                ):
                    continue

                ratio = difflib.SequenceMatcher(
                    None, left_norm, right_norm
                ).ratio()
                if ratio < threshold:
                    continue

                pair_tags = [tag, other]
                destination, _ = _choose_destination(pair_tags)
                source = (
                    other
                    if str(destination["id"]) == left_id
                    else tag
                )
                review.append(TagCleanupCandidate(
                    destination_id=str(destination["id"]),
                    destination_name=str(destination.get("name") or ""),
                    source_ids=(str(source["id"]),),
                    source_names=(str(source.get("name") or ""),),
                    score=ratio,
                    kind="fuzzy",
                    reason="High textual similarity only; manual review required.",
                    auto_merge=False,
                ))

    safe.sort(
        key=lambda candidate: (
            candidate.destination_name.casefold(),
            candidate.source_names,
        )
    )
    review.sort(
        key=lambda candidate: (
            -candidate.score,
            candidate.destination_name.casefold(),
            candidate.source_names,
        )
    )
    return TagCleanupPlan(tuple(safe), tuple(review))
