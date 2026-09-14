"""Perceptual-hash matching for Multi-Booru Tag Importer.

Uses a BK-tree over the full 64-bit pHash. Unlike prefix pruning this remains exact for
all configured Hamming thresholds and does not silently miss valid candidates above four
bits of prefix distance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple


def phash_hamming_distance(a: str, b: str) -> Optional[int]:
    try:
        ai = int(str(a), 16)
        bi = int(str(b), 16)
    except (TypeError, ValueError):
        return None
    return (ai ^ bi).bit_count()


def _normalized_hash(value: Any) -> Optional[str]:
    text = str(value or "").strip().lower()
    if not text:
        return None
    try:
        integer = int(text, 16)
    except ValueError:
        return None
    if integer < 0 or integer.bit_length() > 64:
        return None
    return f"{integer:016x}"


@dataclass
class _Node:
    hash_value: str
    images: List[Dict[str, Any]] = field(default_factory=list)
    children: Dict[int, "_Node"] = field(default_factory=dict)


class PHashIndex:
    def __init__(self, pairs: Optional[Iterable[Tuple[str, Dict[str, Any]]]] = None) -> None:
        self.root: Optional[_Node] = None
        self._ids: set[str] = set()
        if pairs:
            for hash_value, image in pairs:
                self.add(hash_value, image)

    def __len__(self) -> int:
        return len(self._ids)

    def add(self, hash_value: Any, image: Dict[str, Any]) -> None:
        normalized = _normalized_hash(hash_value)
        image_id = str(image.get("id") or "")
        if not normalized or not image_id or image_id in self._ids:
            return
        self._ids.add(image_id)
        if self.root is None:
            self.root = _Node(normalized, [image])
            return

        node = self.root
        while True:
            distance = phash_hamming_distance(normalized, node.hash_value)
            if distance is None:
                return
            if distance == 0:
                node.images.append(image)
                return
            child = node.children.get(distance)
            if child is None:
                node.children[distance] = _Node(normalized, [image])
                return
            node = child

    def query(self, target_hash: Any, max_distance: int) -> List[Tuple[int, Dict[str, Any]]]:
        normalized = _normalized_hash(target_hash)
        if not normalized or self.root is None:
            return []
        radius = max(0, min(64, int(max_distance)))
        found: List[Tuple[int, Dict[str, Any]]] = []
        stack = [self.root]
        while stack:
            node = stack.pop()
            distance = phash_hamming_distance(normalized, node.hash_value)
            if distance is None:
                continue
            if distance <= radius:
                found.extend((distance, image) for image in node.images)
            lower = distance - radius
            upper = distance + radius
            for edge, child in node.children.items():
                if lower <= edge <= upper:
                    stack.append(child)
        found.sort(key=lambda pair: (pair[0], str(pair[1].get("id") or "")))
        return found

    def unique_nearest(
        self,
        target_hash: Any,
        max_distance: int,
        *,
        exclude_image_id: Optional[str] = None,
        min_margin: int = 1,
    ) -> Optional[Tuple[int, Dict[str, Any]]]:
        candidates = [
            pair for pair in self.query(target_hash, max_distance)
            if str(pair[1].get("id") or "") != str(exclude_image_id or "")
        ]
        if not candidates:
            return None
        best_distance = candidates[0][0]
        best = [pair for pair in candidates if pair[0] == best_distance]
        if len(best) != 1:
            return None
        second_distance = next((d for d, _ in candidates if d > best_distance), None)
        required_margin = max(1, int(min_margin))
        if second_distance is not None and second_distance - best_distance < required_margin:
            return None
        return best[0]
