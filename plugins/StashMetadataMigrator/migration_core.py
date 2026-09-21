"""Pure planning helpers for Stash Metadata Migrator."""

from __future__ import annotations

import difflib
import json
import os
import re
import tempfile
import unicodedata
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple


STRONG_FINGERPRINT_TYPES = ("md5", "oshash")


def normalize_name(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    text = text.replace("_", " ").replace("-", " ")
    text = re.sub(r"[^\w\s]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def compact_name(value: str) -> str:
    return "".join(ch for ch in normalize_name(value) if ch.isalnum())


def normalized_path(value: str) -> str:
    return os.path.normcase(os.path.normpath(str(value or "").strip()))


def _dedupe_strings(values: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            out.append(text)
    return out


def merge_strings(*collections: Iterable[str]) -> List[str]:
    values: List[str] = []
    for collection in collections:
        values.extend(collection or [])
    return _dedupe_strings(values)


def merge_stash_ids(*collections: Iterable[Mapping[str, Any]]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    seen = set()
    for collection in collections:
        for item in collection or []:
            endpoint = str(item.get("endpoint") or "").strip()
            stash_id = str(item.get("stash_id") or "").strip()
            key = (endpoint.casefold(), stash_id)
            if endpoint and stash_id and key not in seen:
                seen.add(key)
                out.append({"endpoint": endpoint, "stash_id": stash_id})
    return out


@dataclass(frozen=True)
class EntityMatch:
    entity: Dict[str, Any]
    kind: str
    score: float
    second_score: float


def find_entity_match(
    source_name: str,
    entities: Sequence[Dict[str, Any]],
    *,
    alias_field: str,
    allow_fuzzy: bool,
    threshold: float,
    margin: float,
) -> Optional[EntityMatch]:
    source_norm = normalize_name(source_name)
    if not source_norm:
        return None

    normalized_hits: List[Dict[str, Any]] = []
    for entity in entities:
        values = [str(entity.get("name") or "")]
        values.extend(str(v or "") for v in (entity.get(alias_field) or []))
        if any(normalize_name(value) == source_norm for value in values):
            normalized_hits.append(entity)

    if len(normalized_hits) == 1:
        return EntityMatch(normalized_hits[0], "normalized", 1.0, 0.0)
    if len(normalized_hits) > 1:
        return None

    source_compact = compact_name(source_name)
    if len(source_compact) >= 4:
        compact_hits: List[Dict[str, Any]] = []
        for entity in entities:
            values = [str(entity.get("name") or "")]
            values.extend(str(v or "") for v in (entity.get(alias_field) or []))
            if any(compact_name(value) == source_compact for value in values):
                compact_hits.append(entity)
        if len(compact_hits) == 1:
            return EntityMatch(compact_hits[0], "formatting", 1.0, 0.0)
        if len(compact_hits) > 1:
            return None

    if not allow_fuzzy or len(source_norm) < 5:
        return None

    scored: List[Tuple[float, Dict[str, Any]]] = []
    for entity in entities:
        best = 0.0
        values = [str(entity.get("name") or "")]
        values.extend(str(v or "") for v in (entity.get(alias_field) or []))
        for value in values:
            candidate = normalize_name(value)
            if not candidate or candidate[0] != source_norm[0]:
                continue
            if abs(len(candidate) - len(source_norm)) > 4:
                continue
            best = max(best, difflib.SequenceMatcher(None, source_norm, candidate).ratio())
        if best:
            scored.append((best, entity))

    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    best_score, best_entity = scored[0]
    second = scored[1][0] if len(scored) > 1 else 0.0
    if best_score < threshold:
        return None
    if len(scored) > 1 and (best_score - second) < margin:
        return None
    return EntityMatch(best_entity, "fuzzy", best_score, second)


def load_json_files(directory: Path) -> List[Dict[str, Any]]:
    if not directory.exists():
        return []
    out: List[Dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            data["_migration_source_file"] = str(path)
            out.append(data)
    return out


def build_old_file_index(root: Path) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for item in load_json_files(root / "files"):
        if str(item.get("type") or "").casefold() == "folder":
            continue
        path = str(item.get("path") or "").strip()
        if not path:
            continue
        fps: Dict[str, str] = {}
        for fp in item.get("fingerprints") or []:
            fp_type = str(fp.get("type") or "").casefold().strip()
            value = str(fp.get("fingerprint") or "").casefold().strip()
            if fp_type and value:
                fps[fp_type] = value
        out[path] = {
            "path": path,
            "size": int(item.get("size") or 0),
            "fingerprints": fps,
        }
    return out


def build_current_media_indexes(
    objects: Sequence[Dict[str, Any]],
) -> Tuple[Dict[Tuple[str, str], List[str]], Dict[str, List[str]]]:
    fp_index: Dict[Tuple[str, str], List[str]] = {}
    path_index: Dict[str, List[str]] = {}
    for obj in objects:
        obj_id = str(obj.get("id") or "")
        if not obj_id:
            continue
        for file in obj.get("files") or []:
            path = str(file.get("path") or "").strip()
            if path:
                path_index.setdefault(normalized_path(path), []).append(obj_id)
            for fp in file.get("fingerprints") or []:
                fp_type = str(fp.get("type") or "").casefold().strip()
                value = str(fp.get("value") or "").casefold().strip()
                if fp_type in STRONG_FINGERPRINT_TYPES and value:
                    fp_index.setdefault((fp_type, value), []).append(obj_id)
    return fp_index, path_index


@dataclass(frozen=True)
class MediaMatch:
    object_id: Optional[str]
    kind: str
    detail: str


def match_old_media(
    old_paths: Sequence[str],
    old_files: Mapping[str, Dict[str, Any]],
    current_fp_index: Mapping[Tuple[str, str], Sequence[str]],
    current_path_index: Mapping[str, Sequence[str]],
) -> MediaMatch:
    candidate_sets: List[set[str]] = []
    evidence: List[str] = []

    for old_path in old_paths or []:
        info = old_files.get(str(old_path))
        if not info:
            continue
        fingerprints = info.get("fingerprints") or {}
        for fp_type in STRONG_FINGERPRINT_TYPES:
            value = str(fingerprints.get(fp_type) or "").casefold().strip()
            if not value:
                continue
            ids = set(str(v) for v in current_fp_index.get((fp_type, value), ()) if str(v))
            if ids:
                candidate_sets.append(ids)
                evidence.append(f"{fp_type}:{value[:12]}")

    if candidate_sets:
        intersection = set.intersection(*candidate_sets)
        if len(intersection) == 1:
            return MediaMatch(next(iter(intersection)), "fingerprint", ", ".join(evidence))
        union = set.union(*candidate_sets)
        if len(union) == 1:
            return MediaMatch(next(iter(union)), "fingerprint", ", ".join(evidence))
        if len(union) > 1:
            return MediaMatch(None, "ambiguous", f"fingerprints point to {len(union)} objects")

    path_candidates: set[str] = set()
    for old_path in old_paths or []:
        ids = current_path_index.get(normalized_path(str(old_path)), ())
        path_candidates.update(str(v) for v in ids if str(v))
    if len(path_candidates) == 1:
        return MediaMatch(next(iter(path_candidates)), "exact-path", "exact current path")
    if len(path_candidates) > 1:
        return MediaMatch(None, "ambiguous", "exact path maps to multiple objects")
    return MediaMatch(None, "unmatched", "no exact fingerprint or exact-path match")


def _safe_zip_members(archive: zipfile.ZipFile, target: Path) -> None:
    target_abs = target.resolve()
    for member in archive.infolist():
        destination = (target / member.filename).resolve()
        if destination != target_abs and target_abs not in destination.parents:
            raise ValueError(f"Unsafe path in migration ZIP: {member.filename}")
    archive.extractall(target)


def _find_export_root(base: Path) -> Path:
    if (base / "files").is_dir() and ((base / "scenes").is_dir() or (base / "images").is_dir()):
        return base
    children = [p for p in base.iterdir() if p.is_dir()] if base.exists() else []
    candidates = [
        child for child in children
        if (child / "files").is_dir()
        and ((child / "scenes").is_dir() or (child / "images").is_dir())
    ]
    if len(candidates) == 1:
        return candidates[0]
    raise ValueError(
        "Migration source does not look like a Stash JSON export. "
        "Expected files/ plus scenes/ and/or images/."
    )


@contextmanager
def open_export_source(source_path: str) -> Iterator[Path]:
    source = Path(os.path.expandvars(os.path.expanduser(str(source_path or "").strip())))
    if not source.exists():
        raise FileNotFoundError(f"Migration source not found: {source}")

    if source.is_dir():
        yield _find_export_root(source)
        return

    if source.suffix.casefold() != ".zip":
        raise ValueError("Migration source must be a Stash export ZIP or extracted export directory")

    with tempfile.TemporaryDirectory(prefix="stash-metadata-migrator-") as tmp:
        target = Path(tmp)
        with zipfile.ZipFile(source, "r") as archive:
            _safe_zip_members(archive, target)
        yield _find_export_root(target)


def scalar_fill(current: Any, old: Any) -> Any:
    if current not in (None, "", 0):
        return current
    return old


def source_name_map(items: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for item in items:
        name = str(item.get("name") or "").strip()
        if name:
            out[name.casefold()] = item
    return out
