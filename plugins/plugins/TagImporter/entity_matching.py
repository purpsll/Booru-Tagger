"""Safe normalized/fuzzy matching for Stash Studios and Performers."""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class EntityMatch:
    entity: Dict[str, Any]
    score: float
    second_score: float
    normalized_exact: bool


def normalized_entity_name(name: str) -> str:
    value = unicodedata.normalize("NFKC", str(name or "")).casefold().strip()
    value = value.replace("_", " ").replace("-", " ")
    value = re.sub(r"[^\w\s()]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def unique_entities(cache: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    result: List[Dict[str, Any]] = []
    for obj in cache.values():
        obj_id = str(obj.get("id") or "")
        if not obj_id or obj_id in seen:
            continue
        seen.add(obj_id)
        result.append(obj)
    return result


def find_entity_match(
    incoming_name: str,
    cache: Dict[str, Dict[str, Any]],
    alias_field: str,
    *,
    allow_normalized: bool,
    allow_fuzzy: bool,
    threshold: float,
    margin: float,
) -> Optional[EntityMatch]:
    incoming_norm = normalized_entity_name(incoming_name)
    if not incoming_norm:
        return None
    entities = unique_entities(cache)

    if allow_normalized:
        normalized_matches: List[Dict[str, Any]] = []
        for obj in entities:
            values = [str(obj.get("name") or "")]
            values.extend(str(a or "") for a in (obj.get(alias_field) or []))
            if any(normalized_entity_name(value) == incoming_norm for value in values):
                normalized_matches.append(obj)
        # Normalization is safer than fuzzy matching, but it can still collapse two
        # distinct existing entities to the same key. Never choose arbitrarily.
        if len(normalized_matches) == 1:
            return EntityMatch(normalized_matches[0], 1.0, 0.0, True)
        if len(normalized_matches) > 1:
            return None

    if not allow_fuzzy or len(incoming_norm) < 5:
        return None

    scored: List[tuple[float, Dict[str, Any]]] = []
    for obj in entities:
        best_for_entity = 0.0
        values = [str(obj.get("name") or "")]
        values.extend(str(a or "") for a in (obj.get(alias_field) or []))
        for value in values:
            candidate_norm = normalized_entity_name(value)
            if (
                not candidate_norm
                or abs(len(candidate_norm) - len(incoming_norm)) > 3
                or candidate_norm[0] != incoming_norm[0]
            ):
                continue
            best_for_entity = max(
                best_for_entity,
                difflib.SequenceMatcher(None, incoming_norm, candidate_norm).ratio(),
            )
        if best_for_entity:
            scored.append((best_for_entity, obj))

    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    best_score, best_entity = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0.0
    if best_score < threshold:
        return None
    if len(scored) > 1 and (best_score - second_score) < max(0.0, margin):
        return None
    return EntityMatch(best_entity, best_score, second_score, False)
