#!/usr/bin/env python3
"""Standalone reverse-video metadata importer for Stash scenes.

The image Booru Importer is intentionally not imported or modified here.

Matching pipeline:
1. Exact video MD5 against e621 and Rule34.
2. Deep mode extracts representative frames from the Stash scene.
3. SauceNAO locates e621/Rule34 candidate posts from those frames.
4. Authenticated e621 ERIS is used as an additional e621-only frame locator.
5. Candidate posts must actually contain a video file.
6. Local and remote videos are compared across multiple perceptual frame hashes.
7. Only high-confidence video verification imports metadata; borderline results go
   to a separate Review queue.
"""
from __future__ import annotations

import base64
import email.utils
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from stash_client import Stash, fingerprint, primary_video
from video_match import (
    DEFAULT_RATIOS,
    extract_jpeg_frame,
    frame_hashes,
    probe_duration,
    verify_video_candidate,
)

VERSION = "1.0.0"
USER_AGENT = f"stash-booru-video-importer/{VERSION}"

E621_BASE = "https://e621.net"
RULE34_API = "https://api.rule34.xxx/index.php"
SAUCENAO_API = "https://saucenao.com/search.php"

STATUS_IMPORTED = "Booru Video Imported"
STATUS_UNRESOLVED = "Booru Video Unresolved"
STATUS_REVIEW = "Booru Video Review"
STATUS_NO_MATCH = "Booru Video No Match"
STATUS_RETRY = "Booru Video Retry Later"
STATUS_NAMES = {
    STATUS_IMPORTED,
    STATUS_UNRESOLVED,
    STATUS_REVIEW,
    STATUS_NO_MATCH,
    STATUS_RETRY,
}

VIDEO_EXTENSIONS = {"mp4", "webm", "m4v", "mov", "mkv", "avi"}
DISCOVERY_RATIOS = (0.12, 0.32, 0.52, 0.72, 0.88)
MAX_CANDIDATES = 8
E621_ERIS_DISCOVERY_SCORE = 60.0
SAUCENAO_DISCOVERY_SCORE = 80.0
VERIFY_FRAME_DISTANCE = 8

_LAST_E621_REQUEST = 0.0
_LAST_E621_ERIS = 0.0
_LAST_RULE34 = 0.0
_LAST_SAUCENAO = 0.0
_SAUCENAO_SHORT_LIMIT = 4.0


def _prefix(level: str) -> str:
    char = {"DEBUG": "d", "INFO": "i", "WARNING": "w", "ERROR": "e"}.get(level.upper(), "i")
    return "\x01" + char + "\x02"


def log(level: str, message: str) -> None:
    print(f"{_prefix(level)}{message}", file=sys.stderr, flush=True)


def progress(value: float) -> None:
    value = max(0.0, min(1.0, float(value)))
    print(f"\x01p\x02{value:.6f}", file=sys.stderr, flush=True)


def read_input() -> Dict[str, Any]:
    raw = sys.stdin.read()
    return json.loads(raw) if raw.strip() else {}


def as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes", "on"}


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _wait(last: float, interval: float) -> float:
    now = time.monotonic()
    remaining = last + max(0.0, float(interval)) - now
    if remaining > 0:
        time.sleep(remaining)
    return time.monotonic()


def _json_request(
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    data: Optional[bytes] = None,
    timeout: int = 45,
) -> Any:
    req = urllib.request.Request(
        url,
        data=data,
        headers=headers or {},
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail[:300]}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Provider returned non-JSON data: {' '.join(raw.split())[:240]}"
        ) from exc


def _basic_auth(username: str, api_key: str) -> str:
    token = base64.b64encode(f"{username}:{api_key}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def e621_headers(username: str, api_key: str) -> Dict[str, str]:
    ua = f"BooruVideoImporter/{VERSION}"
    if username:
        ua += f" (by {username} on e621)"
    headers = {"User-Agent": ua, "Accept": "application/json"}
    if username and api_key:
        headers["Authorization"] = _basic_auth(username, api_key)
    return headers


def e621_request(url: str, username: str, api_key: str) -> Any:
    global _LAST_E621_REQUEST
    _LAST_E621_REQUEST = _wait(_LAST_E621_REQUEST, 1.0)
    return _json_request(url, headers=e621_headers(username, api_key))


def e621_post_by_id(post_id: str, username: str, api_key: str) -> Optional[Dict[str, Any]]:
    payload = e621_request(
        f"{E621_BASE}/posts/{urllib.parse.quote(str(post_id))}.json",
        username,
        api_key,
    )
    if isinstance(payload, dict):
        post = payload.get("post")
        if isinstance(post, dict):
            return post
        if payload.get("id"):
            return payload
    return None


def e621_post_by_md5(md5: str, username: str, api_key: str) -> Optional[Dict[str, Any]]:
    params = urllib.parse.urlencode({"tags": f"md5:{md5}", "limit": "1"})
    payload = e621_request(f"{E621_BASE}/posts.json?{params}", username, api_key)
    posts = payload.get("posts") if isinstance(payload, dict) else None
    if not isinstance(posts, list):
        return None
    for post in posts:
        if not isinstance(post, dict):
            continue
        if str((post.get("file") or {}).get("md5") or "").casefold() == md5.casefold():
            return post
    return None


def _multipart(boundary: str, fields: Dict[str, str], data: bytes, filename: str) -> bytes:
    chunks: List[bytes] = []
    for key, value in fields.items():
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )
    chunks.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            "Content-Type: image/jpeg\r\n\r\n"
        ).encode("utf-8")
        + data
        + b"\r\n"
    )
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks)


def e621_eris_candidates(
    frame: bytes,
    username: str,
    api_key: str,
) -> List[Tuple[float, str]]:
    """Authenticated ERIS frame lookup. Anonymous heavy queries are intentionally skipped."""
    global _LAST_E621_ERIS
    if not (username and api_key):
        return []
    _LAST_E621_ERIS = _wait(_LAST_E621_ERIS, 3.0)
    boundary = "----BooruVideoERISBoundary7MA4YWxkTrZu0gW"
    body = _multipart(
        boundary,
        {"score_cutoff": f"{E621_ERIS_DISCOVERY_SCORE:.1f}"},
        frame,
        "stash-video-frame.jpg",
    )
    headers = e621_headers(username, api_key)
    headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    payload = _json_request(
        f"{E621_BASE}/iqdb_queries.json?v2=true",
        headers=headers,
        data=body,
        timeout=60,
    )
    rows = payload if isinstance(payload, list) else (
        payload.get("results") if isinstance(payload, dict) else None
    )
    if not isinstance(rows, list):
        return []
    out: List[Tuple[float, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            score = float(row.get("score") or row.get("similarity") or 0)
        except (TypeError, ValueError):
            score = 0.0
        if 0 < score <= 1:
            score *= 100.0
        post_id = row.get("post_id")
        if post_id is None and isinstance(row.get("post"), dict):
            post_id = row["post"].get("id")
        if post_id and score >= E621_ERIS_DISCOVERY_SCORE:
            out.append((score, str(post_id)))
    return out


def _normalize_rule34_posts(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("post", "posts"):
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
            if isinstance(value, dict):
                return [value]
        if payload.get("id"):
            return [payload]
    return []


def rule34_request(params: Dict[str, str], api_key: str, user_id: str) -> Any:
    global _LAST_RULE34
    _LAST_RULE34 = _wait(_LAST_RULE34, 1.0)
    query = {
        "page": "dapi",
        "s": "post",
        "q": "index",
        "json": "1",
        "api_key": api_key,
        "user_id": user_id,
        **params,
    }
    return _json_request(
        f"{RULE34_API}?{urllib.parse.urlencode(query)}",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )


def rule34_post_by_id(post_id: str, api_key: str, user_id: str) -> Optional[Dict[str, Any]]:
    rows = _normalize_rule34_posts(
        rule34_request({"id": str(post_id), "limit": "1"}, api_key, user_id)
    )
    return rows[0] if rows else None


def rule34_post_by_md5(md5: str, api_key: str, user_id: str) -> Optional[Dict[str, Any]]:
    rows = _normalize_rule34_posts(
        rule34_request({"tags": f"md5:{md5}", "limit": "1"}, api_key, user_id)
    )
    for post in rows:
        if str(post.get("hash") or post.get("md5") or "").casefold() == md5.casefold():
            return post
    return rows[0] if rows else None


def _rule34_tag_types(
    names: Sequence[str],
    api_key: str,
    user_id: str,
) -> Dict[str, int]:
    """Best-effort category enrichment without one network request per tag."""
    global _LAST_RULE34
    result: Dict[str, int] = {}
    names = [str(x) for x in names if str(x).strip()]
    for start in range(0, len(names), 50):
        chunk = names[start:start + 50]
        _LAST_RULE34 = _wait(_LAST_RULE34, 1.0)
        query = {
            "page": "dapi",
            "s": "tag",
            "q": "index",
            "json": "1",
            "names": " ".join(chunk),
            "limit": "100",
            "api_key": api_key,
            "user_id": user_id,
        }
        try:
            payload = _json_request(
                f"{RULE34_API}?{urllib.parse.urlencode(query)}",
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
        except Exception:
            continue
        rows = payload if isinstance(payload, list) else (
            payload.get("tag") if isinstance(payload, dict) else None
        )
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "").strip()
            if not name:
                continue
            try:
                result[name] = int(row.get("type"))
            except (TypeError, ValueError):
                continue
    return result


def _saucenao_wait(settings: Dict[str, Any]) -> None:
    global _LAST_SAUCENAO
    requested = as_float(settings.get("saucenao_requests_per_30_seconds"), 0.0)
    effective = requested if requested > 0 else max(1.0, _SAUCENAO_SHORT_LIMIT)
    _LAST_SAUCENAO = _wait(_LAST_SAUCENAO, 30.0 / effective)


def saucenao_candidates(
    frame: bytes,
    settings: Dict[str, Any],
) -> List[Tuple[float, str, str]]:
    """Return only e621/Rule34 source IDs; SauceNAO is discovery, never metadata authority."""
    global _SAUCENAO_SHORT_LIMIT
    api_key = str(settings.get("saucenao_api_key") or "").strip()
    if not api_key:
        return []
    _saucenao_wait(settings)
    boundary = "----BooruVideoSauceBoundary7MA4YWxkTrZu0gW"
    body = _multipart(
        boundary,
        {"api_key": api_key, "output_type": "2", "numres": "12", "db": "999"},
        frame,
        "stash-video-frame.jpg",
    )
    payload = _json_request(
        SAUCENAO_API,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        data=body,
        timeout=60,
    )
    if isinstance(payload, dict):
        header = payload.get("header") or {}
        try:
            limit = float(header.get("short_limit") or 0)
        except (TypeError, ValueError):
            limit = 0.0
        if limit > 0:
            _SAUCENAO_SHORT_LIMIT = limit

    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        return []

    out: List[Tuple[float, str, str]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        try:
            score = round(float((item.get("header") or {}).get("similarity") or 0), 1)
        except (TypeError, ValueError):
            score = 0.0
        if score < SAUCENAO_DISCOVERY_SCORE:
            continue
        data = item.get("data") or {}
        if not isinstance(data, dict):
            continue
        if data.get("e621_id"):
            out.append((score, "e621", str(data["e621_id"])))
        urls = data.get("ext_urls") or []
        if isinstance(urls, str):
            urls = [urls]
        for url in urls:
            text = str(url)
            m = re.search(r"e621\.net/posts/(\d+)", text)
            if m:
                out.append((score, "e621", m.group(1)))
            m = re.search(r"rule34\.xxx/.*[?&]id=(\d+)", text)
            if m:
                out.append((score, "rule34", m.group(1)))
    return out


def media_url(source: str, post: Dict[str, Any]) -> Optional[str]:
    if source == "e621":
        file_obj = post.get("file") or {}
        ext = str(file_obj.get("ext") or "").casefold().lstrip(".")
        url = str(file_obj.get("url") or "").strip()
        return url if ext in VIDEO_EXTENSIONS and url else None
    url = str(post.get("file_url") or "").strip()
    if not url:
        return None
    path = urllib.parse.urlparse(url).path
    ext = path.rsplit(".", 1)[-1].casefold() if "." in path else ""
    return url if ext in VIDEO_EXTENSIONS else None


def canonical_post_url(source: str, post: Dict[str, Any]) -> str:
    post_id = str(post.get("id") or "")
    if source == "e621":
        return f"https://e621.net/posts/{post_id}"
    return f"https://rule34.xxx/index.php?page=post&s=view&id={post_id}"


def _date_only(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("s") or value.get("date") or value.get("created_at")
    text = str(value or "").strip()
    if not text:
        return None
    if re.match(r"^\d{4}-\d{2}-\d{2}", text):
        return text[:10]
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        pass
    try:
        return email.utils.parsedate_to_datetime(text).date().isoformat()
    except (TypeError, ValueError):
        return None


def post_metadata(source: str, post: Dict[str, Any], settings: Dict[str, Any]) -> Dict[str, Any]:
    tags: List[str] = []
    artists: List[str] = []
    characters: List[str] = []
    if source == "e621":
        typed = post.get("tags") or {}
        if isinstance(typed, dict):
            artists = [str(x).strip() for x in typed.get("artist") or [] if str(x).strip()]
            characters = [str(x).strip() for x in typed.get("character") or [] if str(x).strip()]
            for category in ("general", "species", "copyright", "lore"):
                tags.extend(str(x).strip() for x in typed.get(category) or [] if str(x).strip())
        date = _date_only(post.get("created_at"))
        raw_sources = post.get("sources") or []
        if isinstance(raw_sources, str):
            raw_sources = [raw_sources]
    else:
        flat = [x for x in str(post.get("tags") or "").split() if x]
        api_key = str(settings.get("rule34_api_key") or "")
        user_id = str(settings.get("rule34_user_id") or "")
        typed = _rule34_tag_types(flat, api_key, user_id) if api_key and user_id else {}
        artists = [name for name in flat if typed.get(name) == 1]
        characters = [name for name in flat if typed.get(name) == 4]
        tags = [name for name in flat if typed.get(name) not in {1, 4}]
        date = _date_only(post.get("created_at"))
        raw_sources = re.split(r"\s+", str(post.get("source") or "").strip()) if post.get("source") else []

    urls = [canonical_post_url(source, post)]
    for item in raw_sources:
        url = str(item or "").strip()
        if url.startswith(("http://", "https://")) and url not in urls:
            urls.append(url)
        if len(urls) >= 6:
            break

    # Additional artists remain visible as tags when Stash can only hold one studio.
    if len(artists) > 1:
        tags.extend(artists[1:])

    return {
        "tags": list(dict.fromkeys(tags)),
        "artists": list(dict.fromkeys(artists)),
        "characters": list(dict.fromkeys(characters)),
        "date": date,
        "urls": urls,
    }


def _status_names(scene: Dict[str, Any]) -> set[str]:
    return {
        str(tag.get("name") or "").casefold()
        for tag in scene.get("tags") or []
        if str(tag.get("name") or "") in STATUS_NAMES
    }


def ensure_tag(stash: Stash, name: str, cache: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    key = name.casefold()
    if key in cache:
        return cache[key]
    found = stash.find_tag_by_name(name)
    if not found:
        found = stash.create_tag(name)
    cache[key] = found
    return found


def ensure_entity(
    stash: Stash,
    name: str,
    cache: Dict[str, Dict[str, Any]],
    kind: str,
) -> Dict[str, Any]:
    key = name.casefold().strip()
    if key in cache:
        return cache[key]
    if kind == "studio":
        obj = stash.create_studio(name)
    else:
        obj = stash.create_performer(name)
    cache[key] = obj
    return obj


def transition_status(
    stash: Stash,
    scene: Dict[str, Any],
    status: str,
    tag_cache: Dict[str, Dict[str, Any]],
    extra_url: Optional[str] = None,
) -> None:
    marker = ensure_tag(stash, status, tag_cache)
    keep = [
        str(tag.get("id"))
        for tag in scene.get("tags") or []
        if str(tag.get("name") or "") not in STATUS_NAMES
    ]
    keep.append(str(marker["id"]))
    urls = list(scene.get("urls") or [])
    if extra_url and extra_url not in urls:
        urls.append(extra_url)
    stash.update_scene(str(scene["id"]), tag_ids=list(dict.fromkeys(keep)), urls=urls)
    scene["tags"] = [
        tag for tag in scene.get("tags") or []
        if str(tag.get("name") or "") not in STATUS_NAMES
    ] + [{"id": marker["id"], "name": status}]
    scene["urls"] = urls


def apply_metadata(
    stash: Stash,
    scene: Dict[str, Any],
    source: str,
    post: Dict[str, Any],
    settings: Dict[str, Any],
    tag_cache: Dict[str, Dict[str, Any]],
    performer_cache: Dict[str, Dict[str, Any]],
    studio_cache: Dict[str, Dict[str, Any]],
    dry_run: bool,
) -> None:
    metadata = post_metadata(source, post, settings)

    existing_tag_ids = [
        str(tag.get("id"))
        for tag in scene.get("tags") or []
        if str(tag.get("name") or "") not in STATUS_NAMES
    ]
    for name in metadata["tags"]:
        tag = ensure_tag(stash, name, tag_cache) if not dry_run else tag_cache.get(name.casefold())
        if tag and str(tag.get("id")) not in existing_tag_ids:
            existing_tag_ids.append(str(tag["id"]))

    marker = ensure_tag(stash, STATUS_IMPORTED, tag_cache) if not dry_run else None
    if marker:
        existing_tag_ids.append(str(marker["id"]))

    performer_ids = [str(x.get("id")) for x in scene.get("performers") or []]
    for name in metadata["characters"]:
        obj = performer_cache.get(name.casefold())
        if not obj and not dry_run:
            obj = ensure_entity(stash, name, performer_cache, "performer")
        if obj and str(obj.get("id")) not in performer_ids:
            performer_ids.append(str(obj["id"]))

    studio_id: Optional[str] = None
    if metadata["artists"] and not scene.get("studio"):
        name = metadata["artists"][0]
        obj = studio_cache.get(name.casefold())
        if not obj and not dry_run:
            obj = ensure_entity(stash, name, studio_cache, "studio")
        if obj:
            studio_id = str(obj["id"])

    urls = list(scene.get("urls") or [])
    for url in metadata["urls"]:
        if url not in urls:
            urls.append(url)

    date = None if scene.get("date") else metadata.get("date")

    if dry_run:
        return

    stash.update_scene(
        str(scene["id"]),
        tag_ids=list(dict.fromkeys(existing_tag_ids)),
        performer_ids=list(dict.fromkeys(performer_ids)),
        studio_id=studio_id,
        date=date,
        urls=urls,
    )


def fetch_candidate(
    source: str,
    post_id: str,
    settings: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    if source == "e621":
        return e621_post_by_id(
            post_id,
            str(settings.get("e621_username") or ""),
            str(settings.get("e621_api_key") or ""),
        )
    api_key = str(settings.get("rule34_api_key") or "")
    user_id = str(settings.get("rule34_user_id") or "")
    if not (api_key and user_id):
        raise RuntimeError("Rule34 user ID and API key are required to resolve this candidate")
    return rule34_post_by_id(post_id, api_key, user_id)


def exact_candidates(md5: str, settings: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    out: List[Tuple[str, Dict[str, Any]]] = []
    try:
        post = e621_post_by_md5(
            md5,
            str(settings.get("e621_username") or ""),
            str(settings.get("e621_api_key") or ""),
        )
        if post and media_url("e621", post):
            out.append(("e621", post))
    except Exception as exc:
        log("WARNING", f"e621 exact video lookup failed: {exc}")

    api_key = str(settings.get("rule34_api_key") or "")
    user_id = str(settings.get("rule34_user_id") or "")
    if api_key and user_id:
        try:
            post = rule34_post_by_md5(md5, api_key, user_id)
            if post and media_url("rule34", post):
                out.append(("rule34", post))
        except Exception as exc:
            log("WARNING", f"Rule34 exact video lookup failed: {exc}")
    return out


def discover_candidates(
    local_path: str,
    duration: float,
    ffmpeg_path: str,
    settings: Dict[str, Any],
) -> Tuple[List[Tuple[str, str, int, float]], bool]:
    found: Dict[Tuple[str, str], Dict[str, float]] = {}
    had_error = False
    username = str(settings.get("e621_username") or "")
    api_key = str(settings.get("e621_api_key") or "")
    have_sauce = bool(str(settings.get("saucenao_api_key") or "").strip())
    have_eris = bool(username and api_key)

    if not have_sauce and not have_eris:
        return [], True

    for ratio in DISCOVERY_RATIOS:
        try:
            frame = extract_jpeg_frame(local_path, duration, ratio, ffmpeg_path, timeout=45)
        except Exception as exc:
            log("WARNING", f"Could not extract discovery frame at {ratio:.0%}: {exc}")
            had_error = True
            continue

        frame_hits: Dict[Tuple[str, str], float] = {}
        if have_sauce:
            try:
                for score, source, post_id in saucenao_candidates(frame, settings):
                    key = (source, post_id)
                    frame_hits[key] = max(frame_hits.get(key, 0.0), float(score))
            except Exception as exc:
                log("WARNING", f"SauceNAO video-frame lookup failed: {exc}")
                had_error = True

        if have_eris:
            try:
                for score, post_id in e621_eris_candidates(frame, username, api_key):
                    key = ("e621", post_id)
                    frame_hits[key] = max(frame_hits.get(key, 0.0), float(score))
            except Exception as exc:
                log("WARNING", f"e621 ERIS video-frame lookup failed: {exc}")
                had_error = True

        for key, score in frame_hits.items():
            state = found.setdefault(key, {"hits": 0.0, "score": 0.0})
            state["hits"] += 1
            state["score"] = max(state["score"], score)

    ranked = [
        (source, post_id, int(state["hits"]), float(state["score"]))
        for (source, post_id), state in found.items()
    ]
    ranked.sort(key=lambda item: (item[2], item[3]), reverse=True)
    return ranked[:MAX_CANDIDATES], had_error


def process_scene(
    stash: Stash,
    scene: Dict[str, Any],
    settings: Dict[str, Any],
    *,
    deep: bool,
    dry_run: bool,
    tag_cache: Dict[str, Dict[str, Any]],
    performer_cache: Dict[str, Dict[str, Any]],
    studio_cache: Dict[str, Dict[str, Any]],
) -> str:
    sid = str(scene.get("id") or "")
    video = primary_video(scene)
    if not video:
        return "skipped"
    local_path = str(video.get("path") or "").strip()
    if not local_path:
        return "skipped"

    md5 = fingerprint(scene, "md5")
    if md5:
        exact = exact_candidates(md5, settings)
        if exact:
            source, post = exact[0]
            log("INFO", f"Scene {sid}: exact video MD5 matched {source} #{post.get('id')}")
            if not dry_run:
                apply_metadata(
                    stash, scene, source, post, settings,
                    tag_cache, performer_cache, studio_cache, False,
                )
            return "imported"

    if not deep:
        log("INFO", f"Scene {sid}: no exact video match; queued for Deep Reverse Search")
        if not dry_run:
            transition_status(stash, scene, STATUS_UNRESOLVED, tag_cache)
        return "unresolved"

    ffmpeg_path = stash.ffmpeg_path()
    duration = as_float(video.get("duration"), 0.0)
    try:
        if duration <= 0:
            duration = probe_duration(local_path, ffmpeg_path, 30)
        local_hashes = frame_hashes(
            local_path, duration, ffmpeg_path, DEFAULT_RATIOS, timeout=45
        )
    except Exception as exc:
        log("WARNING", f"Scene {sid}: local video frame analysis failed: {exc}")
        if not dry_run:
            transition_status(stash, scene, STATUS_RETRY, tag_cache)
        return "retry_later"

    candidates, discovery_error = discover_candidates(
        local_path, duration, ffmpeg_path, settings
    )
    if not candidates:
        status = STATUS_RETRY if discovery_error else STATUS_NO_MATCH
        log("INFO", f"Scene {sid}: {'RETRY LATER' if discovery_error else 'NO MATCH'}; no video candidates found")
        if not dry_run:
            transition_status(stash, scene, status, tag_cache)
        return "retry_later" if discovery_error else "no_match"

    best_review: Optional[Tuple[float, str, str, Dict[str, Any], Dict[str, object]]] = None
    authoritative_checks = 0
    verification_errors = 0

    for source, post_id, hits, discovery_score in candidates:
        try:
            post = fetch_candidate(source, post_id, settings)
        except Exception as exc:
            log("WARNING", f"Scene {sid}: could not resolve {source} #{post_id}: {exc}")
            verification_errors += 1
            continue
        if not post:
            continue
        remote = media_url(source, post)
        if not remote:
            continue

        authoritative_checks += 1
        try:
            verification = verify_video_candidate(
                local_path,
                remote,
                ffmpeg_path=ffmpeg_path,
                ratios=DEFAULT_RATIOS,
                frame_distance=VERIFY_FRAME_DISTANCE,
                timeout=60,
                local_duration=duration,
                local_hashes=local_hashes,
            )
        except Exception as exc:
            log("WARNING", f"Scene {sid}: video verification failed for {source} #{post_id}: {exc}")
            verification_errors += 1
            continue

        matched_frames = int(verification.get("matched_frames") or 0)
        total_frames = int(verification.get("total_frames") or 0)
        median = float(verification.get("median_distance") or 64.0)
        log(
            "INFO",
            f"Scene {sid}: candidate {source} #{post_id} discovered on {hits} frame(s) "
            f"({discovery_score:.1f}% best); verification {matched_frames}/{total_frames} "
            f"frames, median distance {median:.1f}",
        )

        if verification.get("high"):
            log("INFO", f"Scene {sid}: MATCH {source} #{post_id}; importing authoritative metadata")
            if not dry_run:
                apply_metadata(
                    stash, scene, source, post, settings,
                    tag_cache, performer_cache, studio_cache, False,
                )
            return "imported"

        if verification.get("review"):
            rank = matched_frames * 100.0 - median
            candidate = (rank, source, post_id, post, verification)
            if best_review is None or candidate[0] > best_review[0]:
                best_review = candidate

    if best_review is not None:
        _, source, post_id, post, verification = best_review
        url = canonical_post_url(source, post)
        log(
            "INFO",
            f"Scene {sid}: REVIEW CANDIDATE {source} #{post_id}; "
            f"{verification.get('matched_frames')}/{verification.get('total_frames')} frames agree",
        )
        if not dry_run:
            transition_status(stash, scene, STATUS_REVIEW, tag_cache, extra_url=url)
        return "review_candidate"

    if verification_errors and authoritative_checks == 0:
        status = STATUS_RETRY
        result = "retry_later"
    else:
        status = STATUS_NO_MATCH
        result = "no_match"
    log("INFO", f"Scene {sid}: {'RETRY LATER' if result == 'retry_later' else 'NO MATCH'} after video verification")
    if not dry_run:
        transition_status(stash, scene, status, tag_cache)
    return result


def import_all(stash: Stash, settings: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, int]:
    deep = str(args.get("lookup_mode") or "fast").casefold() == "deep"
    dry_run = as_bool(args.get("dry_run"), False)
    only_review = as_bool(args.get("only_review"), False)
    only_no_match = as_bool(args.get("only_no_match"), False)
    limit = max(0, as_int(args.get("limit"), 0))

    tag_cache = stash.all_tags()
    performer_cache = stash.all_performers()
    studio_cache = stash.all_studios()

    target_tag_id: Optional[str] = None
    if only_review:
        tag = ensure_tag(stash, STATUS_REVIEW, tag_cache)
        target_tag_id = str(tag["id"])
    elif only_no_match:
        tag = ensure_tag(stash, STATUS_NO_MATCH, tag_cache)
        target_tag_id = str(tag["id"])
    elif deep:
        tag = ensure_tag(stash, STATUS_UNRESOLVED, tag_cache)
        target_tag_id = str(tag["id"])

    stats = {
        "seen": 0,
        "imported": 0,
        "unresolved": 0,
        "review_candidate": 0,
        "no_match": 0,
        "retry_later": 0,
        "skipped": 0,
    }

    page = 1
    per_page = 100
    stop = False
    while not stop:
        count, scenes = stash.find_scenes(page, per_page, tag_id=target_tag_id)
        if not scenes:
            break
        for scene in scenes:
            statuses = _status_names(scene)
            if not target_tag_id and statuses:
                continue
            if limit and stats["seen"] >= limit:
                stop = True
                break
            stats["seen"] += 1
            result = process_scene(
                stash,
                scene,
                settings,
                deep=deep,
                dry_run=dry_run,
                tag_cache=tag_cache,
                performer_cache=performer_cache,
                studio_cache=studio_cache,
            )
            stats[result] = stats.get(result, 0) + 1
            if limit:
                progress(stats["seen"] / max(1, limit))
            elif count:
                progress(min(1.0, stats["seen"] / max(1, count)))
        if target_tag_id:
            # Queue membership shrinks as status tags are replaced. Re-read page 1
            # so changing pagination cannot skip scenes.
            if dry_run:
                break
            page = 1
            continue
        if page * per_page >= count:
            break
        page += 1

    return stats


def main() -> None:
    payload = read_input()
    stash = Stash(payload.get("server_connection") or {})
    settings = stash.settings()
    args = payload.get("args") or {}
    mode = str(args.get("mode") or "import_all")
    if mode != "import_all":
        raise RuntimeError(f"Unsupported mode: {mode}")
    stats = import_all(stash, settings, args)
    log("INFO", f"Finished Booru Video Importer: {stats}")
    print(json.dumps({"output": "ok", "stats": stats}))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("ERROR", str(exc))
        print(json.dumps({"output": "error", "error": str(exc)}))
        sys.exit(1)
