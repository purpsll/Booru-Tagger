"""Conservative duplicate planning for Stash Metadata Migrator.

Destructive merges are allowed only when primary names are formatting-equivalent
(case, whitespace, punctuation, underscores/hyphens) and identity/rich metadata
does not conflict. Fuzzy similarity is never used for destructive merging.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from migration_core import compact_name, merge_stash_ids, merge_strings, normalize_name


@dataclass(frozen=True)
class SafeMerge:
    destination_id: str
    source_ids: Tuple[str, ...]
    destination_name: str
    source_names: Tuple[str, ...]


def _ids(items: Iterable[Mapping[str, Any]]) -> Tuple[str, ...]:
    return tuple(sorted(
        str(item.get("id") or "")
        for item in (items or [])
        if str(item.get("id") or "")
    ))


def _custom(value: Any) -> str:
    if not value:
        return ""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _custom_image(entity: Mapping[str, Any]) -> str:
    path=str(entity.get("image_path") or "").strip()
    return "" if not path or "default=true" in path.casefold() else path


def _same_endpoint_conflict(group: Sequence[Mapping[str, Any]]) -> bool:
    values: Dict[str,str]={}
    for entity in group:
        for item in entity.get("stash_ids") or []:
            ep=str(item.get("endpoint") or "").casefold().strip()
            sid=str(item.get("stash_id") or "").strip()
            if not ep or not sid:
                continue
            previous=values.get(ep)
            if previous is not None and previous != sid:
                return True
            values[ep]=sid
    return False


def _tag_signature(entity: Mapping[str, Any]) -> Tuple[Any,...]:
    return (
        str(entity.get("description") or "").strip(),
        str(entity.get("sort_name") or "").strip(),
        bool(entity.get("favorite")),
        bool(entity.get("ignore_auto_tag")),
        _custom_image(entity),
        _custom(entity.get("custom_fields")),
        _ids(entity.get("parents") or []),
        _ids(entity.get("children") or []),
    )


def _performer_signature(entity: Mapping[str, Any]) -> Tuple[Any,...]:
    fields=(
        "disambiguation","gender","birthdate","ethnicity","country","eye_color",
        "height_cm","measurements","fake_tits","penis_length","circumcised",
        "career_start","career_end","tattoos","piercings","rating100","details",
        "death_date","hair_color","weight",
    )
    return tuple(entity.get(k) or "" for k in fields)+(
        bool(entity.get("favorite")),
        bool(entity.get("ignore_auto_tag")),
        _custom_image(entity),
        _custom(entity.get("custom_fields")),
    )


def _studio_signature(entity: Mapping[str, Any]) -> Tuple[Any,...]:
    parent=entity.get("parent_studio") or {}
    return (
        str(parent.get("id") or ""),
        _ids(entity.get("child_studios") or []),
        entity.get("rating100") or "",
        str(entity.get("details") or "").strip(),
        bool(entity.get("favorite")),
        bool(entity.get("ignore_auto_tag")),
        bool(entity.get("organized")),
        _custom_image(entity),
        _custom(entity.get("custom_fields")),
    )


def _signature(entity: Mapping[str,Any], kind: str) -> Tuple[Any,...]:
    if kind=="tag":
        return _tag_signature(entity)
    if kind=="performer":
        return _performer_signature(entity)
    if kind=="studio":
        return _studio_signature(entity)
    raise ValueError(kind)


def _usage(entity: Mapping[str,Any], kind: str) -> int:
    fields={
        "tag":("scene_count","scene_marker_count","image_count","gallery_count","performer_count","studio_count","group_count"),
        "performer":("scene_count","image_count","gallery_count","group_count"),
        "studio":("scene_count","image_count","gallery_count","group_count"),
    }[kind]
    total=0
    for field in fields:
        try:
            total+=int(entity.get(field) or 0)
        except (TypeError,ValueError):
            pass
    return total


def _booru_rank(name: str) -> int:
    return 0 if re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+)*",str(name or "")) else 1


def _rank(entity: Mapping[str,Any], kind: str) -> Tuple[Any,...]:
    return (
        -_usage(entity,kind),
        _booru_rank(str(entity.get("name") or "")),
        str(entity.get("name") or "").casefold(),
        str(entity.get("id") or ""),
    )


def safe_formatting_merge(
    source_name: str,
    entities: Sequence[Dict[str,Any]],
    *,
    kind: str,
    alias_field: str,
) -> Optional[SafeMerge]:
    """Return a safe destructive merge only for formatting-equivalent primaries."""
    key=compact_name(source_name)
    if len(key)<4:
        return None

    group=[
        entity for entity in entities
        if compact_name(str(entity.get("name") or ""))==key
    ]
    if len(group)<2:
        return None

    if _same_endpoint_conflict(group):
        return None

    rich=[entity for entity in group if any(bool(x) for x in _signature(entity,kind))]
    if len(rich)>1 and len({_signature(entity,kind) for entity in rich})>1:
        return None

    pool=rich or group
    destination=min(pool,key=lambda entity:_rank(entity,kind))
    sources=sorted(
        [entity for entity in group if str(entity.get("id"))!=str(destination.get("id"))],
        key=lambda entity:_rank(entity,kind),
    )
    return SafeMerge(
        destination_id=str(destination["id"]),
        source_ids=tuple(str(entity["id"]) for entity in sources),
        destination_name=str(destination.get("name") or ""),
        source_names=tuple(str(entity.get("name") or "") for entity in sources),
    )


def merged_entity_values(
    entities: Sequence[Dict[str,Any]],
    merge: SafeMerge,
    *,
    alias_field: str,
) -> Dict[str,Any]:
    by_id={str(e.get("id") or ""):e for e in entities}
    ids=(merge.destination_id,)+merge.source_ids
    group=[by_id[i] for i in ids]
    destination=by_id[merge.destination_id]
    destination_name=str(destination.get("name") or "").casefold().strip()

    aliases: List[str]=[]
    urls: List[str]=[]
    tags: List[str]=[]
    seen_alias=set(); seen_url=set(); seen_tag=set()
    for entity in group:
        for value in [str(entity.get("name") or ""), *[str(v or "") for v in (entity.get(alias_field) or [])]]:
            value=value.strip(); key=value.casefold()
            if value and key!=destination_name and key not in seen_alias:
                seen_alias.add(key); aliases.append(value)
        for value in entity.get("urls") or []:
            value=str(value or "").strip(); key=value.casefold()
            if value and key not in seen_url:
                seen_url.add(key); urls.append(value)
        for tag in entity.get("tags") or []:
            tid=str(tag.get("id") or "")
            if tid and tid not in seen_tag:
                seen_tag.add(tid); tags.append(tid)

    return {
        alias_field: aliases,
        "urls": urls,
        "tag_ids": tags,
        "stash_ids": merge_stash_ids(*[entity.get("stash_ids") or [] for entity in group]),
    }


def tag_alias_preflight(
    tags: Sequence[Dict[str,Any]],
    merge: SafeMerge,
) -> Tuple[Dict[str,Tuple[str,...]],Tuple[Tuple[str,str],...]]:
    by_id={str(t.get("id") or ""):t for t in tags}
    group_ids={merge.destination_id,*merge.source_ids}
    owners: Dict[str,str]={}
    for tag in tags:
        tid=str(tag.get("id") or "")
        for alias in tag.get("aliases") or []:
            key=str(alias or "").casefold().strip()
            if key:
                owners[key]=tid

    removals: Dict[str,set]={}
    conflicts: List[Tuple[str,str]]=[]
    for source_id in merge.source_ids:
        source=by_id.get(source_id) or {}
        name=str(source.get("name") or "").strip()
        owner=owners.get(name.casefold())
        if not owner:
            continue
        if owner in group_ids:
            removals.setdefault(owner,set()).add(name.casefold())
        else:
            conflicts.append((name,str((by_id.get(owner) or {}).get("name") or owner)))
    return (
        {owner:tuple(sorted(keys)) for owner,keys in removals.items()},
        tuple(conflicts),
    )
