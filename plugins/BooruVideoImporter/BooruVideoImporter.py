#!/usr/bin/env python3
"""Standalone reverse-video metadata importer for Stash scenes.

Search order:
1. Exact local video MD5 against e621 and Rule34 (fast, authoritative when available).
2. Extract representative frames from the local video.
3. Search e621 IQDB directly; optionally use SauceNAO only as a locator for
   e621/Rule34 post IDs.
4. Require the candidate post itself to contain a video.
5. Generate a Stash-style 25-frame video pHash for the candidate and compare it
   to the local Stash pHash (or a locally generated fallback).
6. Only verified video matches receive source metadata.

This plugin is intentionally separate from Booru Importer and never processes
Stash Image objects.
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
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from stash_client import Stash, fingerprint, primary_video
from video_match import (
    extract_jpeg_frame,
    generate_video_phash,
    hamming_distance,
    parse_phash,
    probe_duration,
)

VERSION = "1.0.0"
USER_AGENT = f"stash-booru-video-importer/{VERSION}"
E621_BASE = "https://e621.net"
RULE34_API = "https://api.rule34.xxx/index.php"
SAUCENAO_API = "https://saucenao.com/search.php"

STATUS_IMPORTED = "Booru Video Imported"
STATUS_REVIEW = "Booru Video Review"
STATUS_NO_MATCH = "Booru Video No Match"
STATUS_RETRY = "Booru Video Retry Later"
STATUS_NAMES = {STATUS_IMPORTED, STATUS_REVIEW, STATUS_NO_MATCH, STATUS_RETRY}

VIDEO_EXTENSIONS = {"mp4", "webm", "m4v", "mov", "mkv", "avi"}
DISCOVERY_RATIOS = (0.12, 0.30, 0.50, 0.70, 0.88)
MAX_CANDIDATES = 6
E621_IQDB_MIN_SCORE = 80.0
SAUCENAO_MIN_SCORE = 80.0

_LAST_E621_REQUEST = 0.0
_LAST_E621_IQDB_REQUEST = 0.0
_LAST_RULE34_REQUEST = 0.0
_LAST_SAUCENAO_REQUEST = 0.0
_SAUCENAO_SHORT_LIMIT = 4.0


def _prefix(level: str) -> str:
    char = {"DEBUG": "d", "INFO": "i", "WARNING": "w", "ERROR": "e"}.get(level.upper(), "i")
    return "\x01" + char + "\x02"


def log(level: str, message: str) -> None:
    print(f"{_prefix(level)}{message}", file=sys.stderr, flush=True)


def progress(value: float) -> None:
    value = max(0.0, min(1.0, value))
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


def as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _wait(last: float, interval: float) -> float:
    now = time.monotonic()
    remaining = last + max(0.0, interval) - now
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
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail[:300]}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Provider returned non-JSON data: {' '.join(raw.split())[:240]}") from exc


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
    payload = e621_request(f"{E621_BASE}/posts/{urllib.parse.quote(str(post_id))}.json", username, api_key)
    if isinstance(payload, dict):
        post = payload.get("post")
        if isinstance(post, dict):
            return post
