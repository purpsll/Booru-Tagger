#!/usr/bin/env python3
"""Danbooru Tag Importer for Stash.

Metadata only by design:
- Reads MD5 fingerprints already stored by Stash.
- FAST mode tries exact-MD5 metadata lookups first (Danbooru, Gelbooru, Rule34, e621), then local pHash reuse and stops.
- DEEP mode adds Danbooru/e621 IQDB and SauceNAO fallbacks for unresolved images.
- Reads only metadata returned by the configured booru APIs.
- Never requests Danbooru file_url, large_file_url, previews, samples, thumbnails,
  or other remote image bytes. The IQDB fallback sends the existing Stash image
  to Danbooru in-memory only; it never saves a duplicate image locally.
- Creates missing Stash tags and merges them onto the existing Stash image.
- Never removes ordinary user tags; it only replaces its own mutually-exclusive workflow marker during state transitions.

Uses only Python's standard library so no pip install is required.
"""

from __future__ import annotations

import base64
import difflib
import email.utils
import re
import unicodedata
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from entity_matching import find_entity_match, normalized_entity_name, unique_entities
from lookup_state import LookupOutcome, LookupStatus, can_mark_no_match
from matching import PHashIndex
from network import HTTP, is_retryable_exception

from constants import (
    ARTIST_MAPPING, AUTO_IMPORT_NEW_IMAGES, CHARACTER_MAPPING,
    CREATE_SECONDARY_ARTIST_STUDIOS, DANBOORU_BASE, DANBOORU_IQDB_MIN_SCORE,
    DEEP_VISUAL_HTTP_RETRIES, DEEP_VISUAL_MAX_WORKERS, DEEP_VISUAL_TIMEOUT_SECONDS,
    E621_BASE, E621_GENERAL_MIN_INTERVAL_SECONDS, E621_IQDB_ANON_MIN_INTERVAL_SECONDS,
    E621_IQDB_AUTH_MIN_INTERVAL_SECONDS, E621_IQDB_MIN_SCORE,
    E621_IQDB_RATE_LIMIT_BACKOFF_SECONDS, E621_IQDB_RATE_LIMIT_MAX_BACKOFF_SECONDS,
    E621_IQDB_RECOVERY_DECAY_SECONDS,
    ENABLE_DANBOORU, ENABLE_DANBOORU_IQDB,
    ENABLE_E621, ENABLE_E621_IQDB, ENABLE_GELBOORU, ENABLE_LOCAL_PHASH_REUSE,
    ENABLE_RULE34, ENABLE_SAUCENAO, ENTITY_SIMILARITY_MARGIN, EXCLUDED_EXTENSIONS,
    GELBOORU_BASE, HTTP_CIRCUIT_COOLDOWN_SECONDS, HTTP_CIRCUIT_FAILURE_THRESHOLD,
    HTTP_MAX_RETRIES, HTTP_RETRY_BACKOFF_SECONDS, IMPORT_MARKER_TAG, REVIEW_MARKER_TAG,
    IGNORED_ARTIST_TAGS, INCLUDE_META_TAGS, LOCAL_PHASH_MAX_DISTANCE, LOCAL_PHASH_MIN_MARGIN,
    MAX_IMAGES_PER_RUN, MERGE_NORMALIZED_PERFORMERS, MERGE_NORMALIZED_STUDIOS,
    MERGE_SIMILAR_PERFORMERS, MERGE_SIMILAR_STUDIOS, MERGE_SIMILAR_TAGS,
    NO_MATCH_MARKER_TAG, STATUS_MARKER_TAGS, UNRESOLVED_MARKER_TAG, PERFORMER_SIMILARITY_THRESHOLD, PROVIDER_REQUEST_INTERVAL_MS,
    REQUEST_DELAY_MS, RULE34_BASE, RULE34_RATE_LIMIT_COOLDOWN_SECONDS, SAUCENAO_ACCEPT_REVIEW_BAND, SAUCENAO_BASE,
    SAUCENAO_DEFAULT_REQUESTS_PER_30_SECONDS, SAUCENAO_HIGH_CONFIDENCE,
    SAUCENAO_OUTAGE_BACKOFF_SECONDS, SAUCENAO_OUTAGE_MAX_BACKOFF_SECONDS,
    SAUCENAO_QUOTA_WINDOW_SECONDS,
    SAUCENAO_RATE_LIMIT_FALLBACK_SECONDS,
    SAUCENAO_REVIEW_MINIMUM, SIMILAR_TAG_MARGIN, SIMILAR_TAG_THRESHOLD,
    STUDIO_SIMILARITY_THRESHOLD, USER_AGENT, VERBOSE_FAST_DECISION_LOGGING, VERSION,
)
from stash_client import Stash

# Per-process provider state. SauceNAO publishes account-specific quota fields in
# each JSON response, so its pacing is adaptive instead of assuming the free tier.
_SAUCENAO_RATE_LIMIT_HITS = 0
_SAUCENAO_DAILY_EXHAUSTED = False
_SAUCENAO_DISABLED_REASON = ""
_SAUCENAO_ACCOUNT_TYPE = ""
_SAUCENAO_SHORT_LIMIT = 0
_SAUCENAO_SHORT_REMAINING = -1
_SAUCENAO_LONG_LIMIT = 0
_SAUCENAO_LONG_REMAINING = -1
_SAUCENAO_LAST_REQUEST_AT = 0.0
_SAUCENAO_PAUSE_UNTIL = 0.0
_SAUCENAO_QUOTA_PAUSES = 0
_SAUCENAO_QUOTA_ANNOUNCED = False
_SAUCENAO_OUTAGE_HITS = 0
_SAUCENAO_OUTAGE_STREAK = 0
_E621_IQDB_RATE_LIMIT_HITS = 0
_E621_IQDB_LAST_REQUEST_AT = 0.0
_E621_IQDB_BACKOFF_UNTIL = 0.0
_E621_IQDB_CONSECUTIVE_RATE_LIMITS = 0
_E621_IQDB_RECOVERY_INTERVAL_SECONDS = 0.0
_RULE34_COOLDOWN_ANNOUNCED = False
_PROVIDER_WARNING_ONCE: set[tuple[str, str]] = set()

# Keep ordinary e621 API traffic below the documented hard ceiling. IQDB file
# uploads use their own stricter pacing below.
HTTP.set_host_interval(
    (urllib.parse.urlparse(E621_BASE).hostname or "e621.net").casefold(),
    E621_GENERAL_MIN_INTERVAL_SECONDS,
)


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
    # Raw Stash plugins must reserve stdout for the final JSON response.
    level_char = _STASH_LOG_LEVEL_CHARS.get(str(level).upper(), "i")
    print(f"{_stash_log_prefix(level_char)}{message}", file=sys.stderr, flush=True)


def progress(value: float) -> None:
    value = max(0.0, min(1.0, value))
    # Native Stash plugin progress encoding: SOH + 'p' + STX + float 0..1.
    print(f"{_stash_log_prefix('p')}{value:.6f}", file=sys.stderr, flush=True)


def read_input() -> Dict[str, Any]:
    raw = sys.stdin.read()
    return json.loads(raw) if raw.strip() else {}


def as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


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


def _saucenao_host() -> str:
    return (urllib.parse.urlparse(SAUCENAO_BASE).hostname or "saucenao.com").casefold()


def _saucenao_requested_rate(settings: Dict[str, Any]) -> float:
    """Return the user's SauceNAO ceiling in requests/30s; 0 means auto."""
    value = as_float(
        settings.get("saucenao_requests_per_30_seconds"),
        SAUCENAO_DEFAULT_REQUESTS_PER_30_SECONDS,
    )
    return max(0.0, min(10000.0, value))


def _saucenao_effective_rate(requested_rate: float) -> float:
    """Use the account-reported short limit, optionally capped by the user."""
    reported = max(0.0, float(_SAUCENAO_SHORT_LIMIT or 0))
    requested = max(0.0, float(requested_rate or 0))
    if reported > 0 and requested > 0:
        return min(reported, requested)
    if reported > 0:
        return reported
    return requested


def _saucenao_wait_for_slot(requested_rate: float) -> float:
    """Smooth SauceNAO requests to the configured/account allowance.

    The first request in Auto mode is immediate so the account quota can be
    learned. Subsequent requests are paced from the returned ``short_limit``.
    """
    global _SAUCENAO_LAST_REQUEST_AT, _SAUCENAO_QUOTA_ANNOUNCED
    now = time.monotonic()
    wait_for = max(0.0, _SAUCENAO_PAUSE_UNTIL - now)
    effective_rate = _saucenao_effective_rate(requested_rate)
    if effective_rate > 0 and _SAUCENAO_LAST_REQUEST_AT > 0:
        interval = float(SAUCENAO_QUOTA_WINDOW_SECONDS) / effective_rate
        wait_for = max(wait_for, (_SAUCENAO_LAST_REQUEST_AT + interval) - now)
    if wait_for > 0:
        if _SAUCENAO_PAUSE_UNTIL > now and not _SAUCENAO_QUOTA_ANNOUNCED:
            log("INFO", f"SauceNAO quota window exhausted; pausing searches for {wait_for:.1f}s before continuing.")
            _SAUCENAO_QUOTA_ANNOUNCED = True
        time.sleep(wait_for)
    if _SAUCENAO_PAUSE_UNTIL <= time.monotonic():
        _SAUCENAO_QUOTA_ANNOUNCED = False
    _SAUCENAO_LAST_REQUEST_AT = time.monotonic()
    return effective_rate


def _saucenao_int(value: Any, default: int = -1) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _saucenao_update_quota(
    payload: Any, requested_rate: float, diagnostics: Optional[Dict[str, Any]] = None
) -> None:
    """Learn SauceNAO account limits from a successful JSON response."""
    global _SAUCENAO_ACCOUNT_TYPE, _SAUCENAO_SHORT_LIMIT, _SAUCENAO_SHORT_REMAINING
    global _SAUCENAO_LONG_LIMIT, _SAUCENAO_LONG_REMAINING, _SAUCENAO_DAILY_EXHAUSTED
    global _SAUCENAO_DISABLED_REASON, _SAUCENAO_PAUSE_UNTIL, _SAUCENAO_QUOTA_PAUSES

    if not isinstance(payload, dict):
        return
    hdr = payload.get("header") or {}
    if not isinstance(hdr, dict):
        return
    account_type = str(hdr.get("account_type") or "").strip()
    short_limit = _saucenao_int(hdr.get("short_limit"), 0)
    short_remaining = _saucenao_int(hdr.get("short_remaining"), -1)
    long_limit = _saucenao_int(hdr.get("long_limit"), 0)
    long_remaining = _saucenao_int(hdr.get("long_remaining"), -1)
    if account_type:
        _SAUCENAO_ACCOUNT_TYPE = account_type
    if short_limit > 0:
        _SAUCENAO_SHORT_LIMIT = short_limit
    if short_remaining >= 0:
        _SAUCENAO_SHORT_REMAINING = short_remaining
    if long_limit > 0:
        _SAUCENAO_LONG_LIMIT = long_limit
    if long_remaining >= 0:
        _SAUCENAO_LONG_REMAINING = long_remaining

    if long_limit > 0 and long_remaining == 0:
        _SAUCENAO_DAILY_EXHAUSTED = True
        _SAUCENAO_DISABLED_REASON = "daily SauceNAO quota exhausted"
    if short_limit > 0 and short_remaining == 0:
        new_until = time.monotonic() + float(SAUCENAO_QUOTA_WINDOW_SECONDS)
        if new_until > _SAUCENAO_PAUSE_UNTIL:
            _SAUCENAO_PAUSE_UNTIL = new_until
            _SAUCENAO_QUOTA_PAUSES += 1

    if diagnostics is not None:
        diagnostics["account_type"] = _SAUCENAO_ACCOUNT_TYPE
        diagnostics["short_limit"] = _SAUCENAO_SHORT_LIMIT
        diagnostics["short_remaining"] = _SAUCENAO_SHORT_REMAINING
        diagnostics["long_limit"] = _SAUCENAO_LONG_LIMIT
        diagnostics["long_remaining"] = _SAUCENAO_LONG_REMAINING
        diagnostics["effective_requests_per_30_seconds"] = _saucenao_effective_rate(requested_rate)


def _saucenao_note_rate_limit(reason: str, retry_after: float = 0.0) -> None:
    """Pause SauceNAO temporarily after a real rate-limit response; do not disable it."""
    global _SAUCENAO_RATE_LIMIT_HITS, _SAUCENAO_PAUSE_UNTIL, _SAUCENAO_QUOTA_PAUSES
    _SAUCENAO_RATE_LIMIT_HITS += 1
    delay = max(float(SAUCENAO_RATE_LIMIT_FALLBACK_SECONDS), float(retry_after or 0.0))
    _SAUCENAO_PAUSE_UNTIL = max(_SAUCENAO_PAUSE_UNTIL, time.monotonic() + delay)
    _SAUCENAO_QUOTA_PAUSES += 1
    key = ("SauceNAO rate limit", "429")
    if key not in _PROVIDER_WARNING_ONCE:
        _PROVIDER_WARNING_ONCE.add(key)
        log("WARNING", f"SauceNAO rate limit hit; pausing SauceNAO for {delay:.0f}s and continuing afterward ({reason}). Repeated rate-limit warnings are suppressed.")


def _saucenao_is_disabled() -> bool:
    # Only a genuinely exhausted long-term quota disables SauceNAO. Temporary
    # 52x origin failures never skip SauceNAO for later images.
    return bool(_SAUCENAO_DAILY_EXHAUSTED)


def _saucenao_note_success() -> None:
    """Recover gradually from an outage streak after a successful request."""
    global _SAUCENAO_OUTAGE_STREAK
    _SAUCENAO_OUTAGE_STREAK = max(0, int(_SAUCENAO_OUTAGE_STREAK) - 1)


def _saucenao_note_outage(status_code: int) -> float:
    """Back off briefly after 52x, but keep SauceNAO eligible for every image."""
    global _SAUCENAO_OUTAGE_HITS, _SAUCENAO_OUTAGE_STREAK
    global _SAUCENAO_PAUSE_UNTIL, _SAUCENAO_QUOTA_PAUSES

    _SAUCENAO_OUTAGE_HITS += 1
    _SAUCENAO_OUTAGE_STREAK += 1
    base = max(1.0, float(SAUCENAO_OUTAGE_BACKOFF_SECONDS))
    maximum = max(base, float(SAUCENAO_OUTAGE_MAX_BACKOFF_SECONDS))
    delay = min(maximum, base * (2 ** min(6, _SAUCENAO_OUTAGE_STREAK - 1)))
    _SAUCENAO_PAUSE_UNTIL = max(
        _SAUCENAO_PAUSE_UNTIL, time.monotonic() + delay
    )
    _SAUCENAO_QUOTA_PAUSES += 1

    key = ("SauceNAO outage", str(status_code))
    if key not in _PROVIDER_WARNING_ONCE:
        _PROVIDER_WARNING_ONCE.add(key)
        log(
            "WARNING",
            f"SauceNAO HTTP {status_code}; keeping SauceNAO enabled and waiting "
            f"{delay:.0f}s before its next image search. Repeated identical "
            "outage warnings are suppressed.",
        )
    return delay


def _rule34_host() -> str:
    return (urllib.parse.urlparse(RULE34_BASE).hostname or "api.rule34.xxx").casefold()


def _maybe_log_rule34_resumed() -> None:
    global _RULE34_COOLDOWN_ANNOUNCED
    if _RULE34_COOLDOWN_ANNOUNCED and HTTP.cooldown_remaining(_rule34_host()) <= 0:
        log("INFO", "Rule34 cooldown expired; Rule34 lookups resumed.")
        _RULE34_COOLDOWN_ANNOUNCED = False


def _log_lookup_problem_once(provider: str, detail: str) -> None:
    key = (str(provider), str(detail).split("(", 1)[0].strip())
    if key in _PROVIDER_WARNING_ONCE:
        return
    _PROVIDER_WARNING_ONCE.add(key)
    log("WARNING", f"{provider} lookup did not complete authoritatively: {detail}")


def _observe_rule34_problem(outcome: LookupOutcome, metrics: Optional[Dict[str, Any]]) -> bool:
    """Record/log Rule34 cooldown behavior. Returns True when handled specially."""
    global _RULE34_COOLDOWN_ANNOUNCED
    if outcome.provider != "Rule34" or outcome.status not in {LookupStatus.RETRYABLE_ERROR, LookupStatus.UNAVAILABLE}:
        return False
    detail = str(outcome.detail or "")
    remaining = HTTP.cooldown_remaining(_rule34_host())
    if "429" in detail:
        _metric(metrics, "rule34_rate_limit_hits")
        _metric(metrics, "rule34_cooldown_activations")
        if not _RULE34_COOLDOWN_ANNOUNCED:
            _RULE34_COOLDOWN_ANNOUNCED = True
            minutes = max(1, int(round(max(remaining, RULE34_RATE_LIMIT_COOLDOWN_SECONDS) / 60.0)))
            log(
                "WARNING",
                f"Rule34 rate limit detected; pausing Rule34 lookups for about {minutes} minute(s). "
                "The scan will continue with Danbooru, Gelbooru, e621, and local pHash. "
                "Images that still require Rule34 confirmation will remain pending for the next Fast scan.",
            )
        return True
    if "cooldown" in detail.casefold() or "temporarily paused" in detail.casefold():
        _metric(metrics, "rule34_cooldown_skips")
        if not _RULE34_COOLDOWN_ANNOUNCED:
            _RULE34_COOLDOWN_ANNOUNCED = True
            minutes = max(1, int(round(max(remaining, 1.0) / 60.0)))
            log(
                "WARNING",
                f"Rule34 is in a rate-limit cooldown (~{minutes} minute(s) remaining). "
                "Continuing the scan with the other providers; affected images stay pending.",
            )
        return True
    return False


def danbooru_headers(login: str = "", api_key: str = "") -> Dict[str, str]:
    """Build Danbooru request headers without placing API credentials in URLs."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if login and api_key:
        token = base64.b64encode(f"{login}:{api_key}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {token}"
    return headers


def danbooru_post(md5: str, login: str, api_key: str) -> Optional[Dict[str, Any]]:
    # Intentionally request JSON post metadata only. No image URL is ever fetched.
    params: Dict[str, str] = {"tags": f"md5:{md5}", "limit": "1"}
    url = f"{DANBOORU_BASE}/posts.json?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers=danbooru_headers(login, api_key))
    try:
        with HTTP.urlopen(req, timeout=30) as resp:
            posts = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        if exc.code == 404:
            return None
        raise RuntimeError(f"Danbooru HTTP {exc.code}: {detail[:300]}") from exc
    if not isinstance(posts, list):
        raise RuntimeError("Danbooru returned an unexpected posts response")
    if not posts:
        return None
    for post in posts:
        if str(post.get("md5") or "").lower() == md5.lower():
            return post
    return None




def danbooru_post_by_id(post_id: str, login: str, api_key: str) -> Optional[Dict[str, Any]]:
    req = urllib.request.Request(
        f"{DANBOORU_BASE}/posts/{urllib.parse.quote(str(post_id))}.json",
        headers=danbooru_headers(login, api_key),
    )
    try:
        with HTTP.urlopen(req, timeout=30) as resp:
            obj = json.loads(resp.read().decode("utf-8"))
            return obj if isinstance(obj, dict) else None
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise RuntimeError(f"Danbooru HTTP {exc.code} resolving SauceNAO result") from exc


def gelbooru_style_post_by_id(
    base_url: str,
    post_id: str,
    source_name: str,
    api_key: str = "",
    user_id: str = "",
    require_auth: bool = False,
) -> Optional[Dict[str, Any]]:
    """Resolve a SauceNAO post ID without collapsing provider errors into misses."""
    if require_auth and (not api_key or not user_id):
        raise RuntimeError(f"{source_name} credentials are required to resolve the visual-search result")
    params = {"page": "dapi", "s": "post", "q": "index", "json": "1", "id": str(post_id)}
    if api_key and user_id:
        params.update({"api_key": api_key, "user_id": user_id})
    req = urllib.request.Request(
        f"{base_url}?{urllib.parse.urlencode(params)}",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    try:
        with HTTP.urlopen(req, timeout=30) as resp:
            payload = _safe_json_response(
                resp.read(),
                source_name,
                empty_is_miss=(source_name.casefold() == "rule34"),
            )
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"{source_name} HTTP {exc.code} resolving visual-search result: {detail[:240]}"
        ) from exc
    if payload is None:
        return None
    posts = _normalize_gelbooru_posts(payload)
    if posts:
        return posts[0]
    # Recognized empty result shapes are an authoritative miss. Unknown schema is not.
    if payload == [] or (isinstance(payload, dict) and not payload.get("post") and str((payload.get("@attributes") or {}).get("count", "")) == "0"):
        return None
    raise RuntimeError(f"{source_name} returned an unexpected post response while resolving visual-search result")



def e621_user_agent(username: str = "") -> str:
    username = str(username or "").strip()
    if username:
        return f"StashMultiBooru/{VERSION} (by {username} on e621)"
    return f"StashMultiBooru/{VERSION} (local Stash plugin)"


def e621_request(url: str, username: str = "", api_key: str = "") -> Any:
    headers = {
        "User-Agent": e621_user_agent(username),
        "Accept": "application/json",
    }
    if username and api_key:
        token = base64.b64encode(f"{username}:{api_key}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {token}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with HTTP.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        if exc.code == 404:
            return None
        raise RuntimeError(f"e621 HTTP {exc.code}: {detail[:300]}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"e621 returned non-JSON response: {' '.join(raw.split())[:240]}") from exc


def e621_post(md5: str, username: str = "", api_key: str = "") -> Optional[Dict[str, Any]]:
    params = {"tags": f"md5:{md5}", "limit": "1"}
    payload = e621_request(f"{E621_BASE}/posts.json?{urllib.parse.urlencode(params)}", username, api_key)
    if payload is None:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("posts"), list):
        raise RuntimeError("e621 returned an unexpected posts response")
    posts = payload["posts"]
    for post in posts:
        if not isinstance(post, dict):
            continue
        file_obj = post.get("file") or {}
        if str(file_obj.get("md5") or "").strip().lower() == md5.lower():
            return post
    return None


def e621_post_by_id(post_id: str, username: str = "", api_key: str = "") -> Optional[Dict[str, Any]]:
    payload = e621_request(f"{E621_BASE}/posts/{urllib.parse.quote(str(post_id))}.json", username, api_key)
    if isinstance(payload, dict):
        post = payload.get("post")
        if isinstance(post, dict):
            return post
        # Be tolerant of wrappers/alternate shapes.
        if payload.get("id"):
            return payload
    return None



def _e621_iqdb_is_authenticated(username: str, api_key: str) -> bool:
    return bool(str(username or "").strip() and str(api_key or "").strip())


def _e621_iqdb_base_interval(username: str, api_key: str) -> float:
    return float(
        E621_IQDB_AUTH_MIN_INTERVAL_SECONDS
        if _e621_iqdb_is_authenticated(username, api_key)
        else E621_IQDB_ANON_MIN_INTERVAL_SECONDS
    )


def _e621_iqdb_min_interval(username: str, api_key: str) -> float:
    return max(
        _e621_iqdb_base_interval(username, api_key),
        float(_E621_IQDB_RECOVERY_INTERVAL_SECONDS or 0.0),
    )


def _e621_retry_after_seconds(exc: urllib.error.HTTPError) -> float:
    raw = exc.headers.get("Retry-After") if exc.headers else None
    if not raw:
        return 0.0
    raw = str(raw).strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _e621_iqdb_wait_for_slot(username: str, api_key: str) -> float:
    """Pace heavy e621 IQDB file uploads without disabling later images."""
    global _E621_IQDB_LAST_REQUEST_AT
    interval = _e621_iqdb_min_interval(username, api_key)
    now = time.monotonic()
    wait_for = max(0.0, _E621_IQDB_BACKOFF_UNTIL - now)
    if _E621_IQDB_LAST_REQUEST_AT > 0:
        wait_for = max(wait_for, (_E621_IQDB_LAST_REQUEST_AT + interval) - now)
    if wait_for > 0:
        time.sleep(wait_for)
    _E621_IQDB_LAST_REQUEST_AT = time.monotonic()
    return interval


def _e621_iqdb_note_rate_limit(
    exc: urllib.error.HTTPError, username: str, api_key: str
) -> float:
    """Delay only the next e621 IQDB attempt after a 429/challenge."""
    global _E621_IQDB_BACKOFF_UNTIL, _E621_IQDB_CONSECUTIVE_RATE_LIMITS
    global _E621_IQDB_RECOVERY_INTERVAL_SECONDS
    _E621_IQDB_CONSECUTIVE_RATE_LIMITS += 1
    minimum = _e621_iqdb_base_interval(username, api_key)
    adaptive = min(
        float(E621_IQDB_RATE_LIMIT_MAX_BACKOFF_SECONDS),
        float(E621_IQDB_RATE_LIMIT_BACKOFF_SECONDS)
        * (2 ** max(0, _E621_IQDB_CONSECUTIVE_RATE_LIMITS - 1)),
    )
    delay = max(minimum, adaptive, _e621_retry_after_seconds(exc))
    _E621_IQDB_BACKOFF_UNTIL = max(
        _E621_IQDB_BACKOFF_UNTIL, time.monotonic() + delay
    )
    _E621_IQDB_RECOVERY_INTERVAL_SECONDS = max(
        float(_E621_IQDB_RECOVERY_INTERVAL_SECONDS or 0.0), delay
    )
    return delay


def _e621_iqdb_note_success(username: str, api_key: str) -> None:
    """Recover gradually after throttling instead of snapping back to the base rate."""
    global _E621_IQDB_BACKOFF_UNTIL, _E621_IQDB_CONSECUTIVE_RATE_LIMITS
    global _E621_IQDB_RECOVERY_INTERVAL_SECONDS
    _E621_IQDB_BACKOFF_UNTIL = 0.0
    _E621_IQDB_CONSECUTIVE_RATE_LIMITS = max(
        0, _E621_IQDB_CONSECUTIVE_RATE_LIMITS - 1
    )
    base = _e621_iqdb_base_interval(username, api_key)
    recovery = float(_E621_IQDB_RECOVERY_INTERVAL_SECONDS or 0.0)
    if recovery > base:
        recovery = max(
            base, recovery - max(0.1, float(E621_IQDB_RECOVERY_DECAY_SECONDS))
        )
        _E621_IQDB_RECOVERY_INTERVAL_SECONDS = (
            0.0 if recovery <= base else recovery
        )
    else:
        _E621_IQDB_RECOVERY_INTERVAL_SECONDS = 0.0


def e621_iqdb(
    image_bytes: bytes,
    username: str,
    api_key: str,
    minimum_score: float,
    diagnostics: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Reverse-search the existing Stash image against e621's IQDB endpoint.

    The image is uploaded in memory only; no duplicate image is written to disk.
    A failure on one image never disables IQDB for the next image in the queue.
    """
    e621_host = (urllib.parse.urlparse(E621_BASE).hostname or "e621.net").casefold()
    HTTP.clear_host_failures(e621_host)
    _e621_iqdb_wait_for_slot(username, api_key)
    boundary = "----StashE621IQDBBoundary7MA4YWxkTrZu0gW"
    chunks: List[bytes] = []

    chunks.append(
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="stash-image"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8")
        + image_bytes
        + b"\r\n"
    )
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(chunks)

    headers = {
        "User-Agent": e621_user_agent(username),
        "Accept": "application/json",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }

    if username and api_key:
        token = base64.b64encode(f"{username}:{api_key}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {token}"

    req = urllib.request.Request(
        f"{E621_BASE}/iqdb_queries.json",
        data=body,
        method="POST",
        headers=headers,
    )

    try:
        with HTTP.urlopen(
            req,
            timeout=DEEP_VISUAL_TIMEOUT_SECONDS,
            min_interval=0.0,
            retries=DEEP_VISUAL_HTTP_RETRIES,
        ) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        _e621_iqdb_note_success(username, api_key)
    except urllib.error.HTTPError as exc:
        global _E621_IQDB_RATE_LIMIT_HITS
        detail = exc.read().decode("utf-8", errors="replace")

        if exc.code in {404, 422}:
            return None

        detail_lower = detail.lower()
        cloudflare_challenge = (
            "just a moment" in detail_lower
            or "cloudflare" in detail_lower
            or "cf-chl-" in detail_lower
        )

        if exc.code == 429 or cloudflare_challenge:
            _E621_IQDB_RATE_LIMIT_HITS += 1
            _e621_iqdb_note_rate_limit(exc, username, api_key)
            reason = f"HTTP {exc.code}" + (
                " / Cloudflare challenge" if cloudflare_challenge else ""
            )
            raise RuntimeError(
                f"e621 IQDB temporarily unavailable for this image: {reason}"
            ) from exc

        raise RuntimeError(f"e621 IQDB HTTP {exc.code}: {detail[:300]}") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"e621 IQDB returned non-JSON response: {' '.join(raw.split())[:240]}"
        ) from exc

    # e621/Danbooru-family IQDB results have historically appeared as either
    # a top-level list or a dict containing result arrays.
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = None
        for key in ("posts", "matches", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                rows = value
                break
        if rows is None and (payload.get("post") or payload.get("id")):
            rows = [payload]
        if rows is None:
            raise RuntimeError("e621 IQDB returned an unexpected response shape")
    else:
        raise RuntimeError("e621 IQDB returned an unexpected response shape")

    ranked: List[Tuple[float, Dict[str, Any]]] = []
    best_seen_score = 0.0

    for row in rows:
        if not isinstance(row, dict):
            continue

        post_obj = row.get("post")
        if not isinstance(post_obj, dict):
            post_obj = row if row.get("id") else None
        if not isinstance(post_obj, dict):
            continue

        score_value = (
            row.get("score")
            if row.get("score") is not None
            else row.get("similarity")
        )
        if score_value is None:
            score_value = post_obj.get("iqdb_score")

        try:
            score = float(score_value or 0)
        except (TypeError, ValueError):
            score = 0.0

        # Some clients expose similarity as 0..1, others as 0..100.
        if 0 < score <= 1:
            score *= 100.0

        best_seen_score = max(best_seen_score, score)
        if score >= minimum_score:
            ranked.append((score, post_obj))

    if diagnostics is not None:
        diagnostics["best_score"] = best_seen_score
        diagnostics["candidate_count"] = len(rows)
        diagnostics["minimum_score"] = minimum_score

    if not ranked:
        return None

    ranked.sort(key=lambda pair: pair[0], reverse=True)

    for score, post_obj in ranked:
        # Prefer resolving the full current post payload by ID so tag/category
        # structure is consistent with normal e621 MD5 matches.
        post_id = post_obj.get("id")
        if post_id:
            resolved = e621_post_by_id(str(post_id), username, api_key)
            if resolved:
                resolved["_e621_iqdb_score"] = score
                return resolved

        # Fall back to the returned post object if it is already complete enough.
        if post_obj.get("tags") and post_obj.get("file"):
            post_obj = dict(post_obj)
            post_obj["_e621_iqdb_score"] = score
            return post_obj

    return None


def _saucenao_data_has_supported_booru(data: Dict[str, Any]) -> bool:
    if data.get("danbooru_id") or data.get("e621_id"):
        return True
    urls = data.get("ext_urls") or []
    if isinstance(urls, str):
        urls = [urls]
    supported = (
        "danbooru.donmai.us/posts/",
        "gelbooru.com/",
        "rule34.xxx/",
        "e621.net/posts/",
    )
    return any(any(domain in str(url) for domain in supported) for url in urls)


def _saucenao_supported_candidate_url(data: Dict[str, Any]) -> str:
    """Return a stable source URL for the best supported SauceNAO candidate."""
    danbooru_id = data.get("danbooru_id")
    if danbooru_id:
        return f"https://danbooru.donmai.us/posts/{danbooru_id}"
    e621_id = data.get("e621_id")
    if e621_id:
        return f"https://e621.net/posts/{e621_id}"
    urls = data.get("ext_urls") or []
    if isinstance(urls, str):
        urls = [urls]
    supported = (
        "danbooru.donmai.us/posts/",
        "gelbooru.com/",
        "rule34.xxx/",
        "e621.net/posts/",
    )
    for url in urls:
        text = str(url or "").strip()
        if text and any(domain in text for domain in supported):
            return text
    return ""


def saucenao_resolve(image_bytes: bytes, api_key: str, minimum_similarity: float, danbooru_login: str, danbooru_api_key: str, gelbooru_api_key: str, gelbooru_user_id: str, rule34_api_key: str, rule34_user_id: str, e621_username: str = '', e621_api_key: str = '', requests_per_30_seconds: float = 0.0, diagnostics: Optional[Dict[str, Any]] = None) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Search SauceNAO with the existing Stash image, then resolve a supported booru post.

    SauceNAO is used only as a similarity/index resolver. Tags are fetched from
    Danbooru/Gelbooru/Rule34/e621 metadata APIs; remote source image files are never downloaded.
    """
    if not api_key or _saucenao_is_disabled():
        return None

    _saucenao_wait_for_slot(requests_per_30_seconds)
    fields = {"api_key": api_key, "output_type":"2", "numres":"8", "db":"999"}
    boundary = "----StashSauceNAOBoundary7MA4YWxkTrZu0gW"
    chunks=[]
    for k,v in fields.items():
        chunks.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode())
    chunks.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"stash-image\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode()+image_bytes+b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    body=b"".join(chunks)
    req=urllib.request.Request(SAUCENAO_BASE,data=body,method="POST",headers={"User-Agent":USER_AGENT,"Accept":"application/json","Content-Type":f"multipart/form-data; boundary={boundary}"})
    try:
        with HTTP.urlopen(
            req,
            timeout=DEEP_VISUAL_TIMEOUT_SECONDS,
            min_interval=0.0,
            retries=DEEP_VISUAL_HTTP_RETRIES,
        ) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            payload=json.loads(raw)
    except urllib.error.HTTPError as exc:
        detail=exc.read().decode("utf-8",errors="replace")
        if exc.code in {500, 520, 521, 522, 523, 524}:
            _saucenao_note_outage(exc.code)
            raise RuntimeError(
                f"SauceNAO temporarily unavailable: HTTP {exc.code}"
            ) from exc
        if exc.code == 429:
            retry_after_raw = exc.headers.get("Retry-After") if exc.headers else None
            try:
                retry_after_seconds = max(0.0, float(retry_after_raw or 0.0))
            except (TypeError, ValueError):
                retry_after_seconds = 0.0
            detail_reason = f"HTTP 429; Retry-After={retry_after_raw}" if retry_after_raw else "HTTP 429"
            _saucenao_note_rate_limit(detail_reason, retry_after_seconds)
            raise RuntimeError(f"SauceNAO temporarily unavailable: {detail_reason}") from exc
        raise RuntimeError(f"SauceNAO HTTP {exc.code}: {detail[:240]}") from exc

    # SauceNAO reports account-specific quotas in every JSON response. Learn the
    # actual allowance (including paid/upgraded accounts) before interpreting status.
    _saucenao_update_quota(payload, requests_per_30_seconds, diagnostics)
    if isinstance(payload, dict):
        hdr = payload.get("header") or {}
        try:
            status = int(hdr.get("status", 0))
        except (TypeError, ValueError):
            status = 0
        message = str(hdr.get("message") or "")
        if status == -2 or "rate" in message.casefold():
            if _SAUCENAO_LONG_LIMIT > 0 and _SAUCENAO_LONG_REMAINING == 0:
                raise RuntimeError("SauceNAO unavailable: daily quota exhausted")
            _saucenao_note_rate_limit("API rate status")
            raise RuntimeError("SauceNAO temporarily unavailable: API rate status")
    _saucenao_note_success()
    results=payload.get("results") if isinstance(payload,dict) else None
    if not isinstance(results,list): return None
    ranked=[]
    best_seen_similarity = 0.0
    best_supported_similarity = 0.0
    best_supported_url = ""
    for item in results:
        if not isinstance(item,dict): continue
        try:
            raw_similarity = float((item.get("header") or {}).get("similarity") or 0)
        except (TypeError, ValueError):
            raw_similarity = 0.0
        sim = _saucenao_policy_score(raw_similarity)
        data = item.get("data") or {}
        best_seen_similarity = max(best_seen_similarity, sim)
        if isinstance(data, dict) and _saucenao_data_has_supported_booru(data):
            if sim > best_supported_similarity:
                best_supported_similarity = sim
                best_supported_url = _saucenao_supported_candidate_url(data)
        if sim>=minimum_similarity: ranked.append((sim,data))
    if diagnostics is not None:
        diagnostics["best_similarity"] = best_seen_similarity
        diagnostics["best_supported_similarity"] = best_supported_similarity
        diagnostics["best_supported_url"] = best_supported_url
        diagnostics["candidate_count"] = len(results)
        diagnostics["minimum_similarity"] = minimum_similarity
    ranked.sort(key=lambda x:x[0],reverse=True)
    for sim,data in ranked:
        did=data.get("danbooru_id")
        if did:
            post=danbooru_post_by_id(str(did),danbooru_login,danbooru_api_key)
            if post:
                post["_saucenao_score"]=sim; return "danbooru",post
        eid=data.get("e621_id")
        if eid:
            post=e621_post_by_id(str(eid),e621_username,e621_api_key)
            if post:
                post["_saucenao_score"]=sim; return "e621",post
        urls=data.get("ext_urls") or []
        if isinstance(urls,str): urls=[urls]
        for u in urls:
            u=str(u)
            import re
            m=re.search(r'danbooru\\.donmai\\.us/posts/(\\d+)',u)
            if m:
                post=danbooru_post_by_id(m.group(1),danbooru_login,danbooru_api_key)
                if post: post["_saucenao_score"]=sim; return "danbooru",post
            m=re.search(r'gelbooru\\.com/.*[?&]id=(\\d+)',u)
            if m:
                post=gelbooru_style_post_by_id(GELBOORU_BASE,m.group(1),"Gelbooru",gelbooru_api_key,gelbooru_user_id)
                if post: post["_saucenao_score"]=sim; return "gelbooru",post
            m=re.search(r'rule34\\.xxx/.*[?&]id=(\\d+)',u)
            if m and rule34_api_key and rule34_user_id:
                post=gelbooru_style_post_by_id(RULE34_BASE,m.group(1),"Rule34",rule34_api_key,rule34_user_id,True)
                if post: post["_saucenao_score"]=sim; return "rule34",post
            m=re.search(r'e621\\.net/posts/(\\d+)',u)
            if m:
                post=e621_post_by_id(m.group(1),e621_username,e621_api_key)
                if post: post["_saucenao_score"]=sim; return "e621",post
    return None


def _normalize_gelbooru_posts(payload: Any) -> List[Dict[str, Any]]:
    """Normalize Gelbooru-style JSON responses to a list of post dicts."""
    if isinstance(payload, list):
        return [p for p in payload if isinstance(p, dict)]
    if isinstance(payload, dict):
        posts = payload.get("post")
        if isinstance(posts, list):
            return [p for p in posts if isinstance(p, dict)]
        if isinstance(posts, dict):
            return [posts]
    return []



_SOURCE_DISABLED: Dict[str, str] = {}
_TYPED_TAG_CACHE: Dict[Tuple[str, str], Optional[int]] = {}


def disable_source(source_name: str, reason: str) -> None:
    if source_name not in _SOURCE_DISABLED:
        _SOURCE_DISABLED[source_name] = reason
        log("WARNING", f"{source_name} disabled for remainder of this run: {reason}")


def source_disabled(source_name: str) -> bool:
    return source_name in _SOURCE_DISABLED


def _safe_json_response(
    raw: bytes,
    source_name: str,
    *,
    empty_is_miss: bool = False,
) -> Any:
    """Decode an upstream JSON body without collapsing provider failures into misses.

    Most providers returning HTTP 200 with an empty body is suspicious and must stay
    retryable/unavailable. Rule34 is a documented/observed exception: its DAPI can
    return an empty HTTP-200 body for a valid search with zero results. Callers opt
    into that behavior explicitly with ``empty_is_miss=True``.
    """
    if not raw:
        if empty_is_miss:
            return None
        raise RuntimeError(f"{source_name} returned an empty response")
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        if empty_is_miss:
            return None
        raise RuntimeError(f"{source_name} returned an empty response")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        preview = " ".join(text.split())[:240]
        raise RuntimeError(
            f"{source_name} returned non-JSON response"
            + (f": {preview}" if preview else "")
        ) from exc


def gelbooru_style_md5_post(
    base_url: str,
    md5: str,
    source_name: str,
    api_key: str = "",
    user_id: str = "",
    require_auth: bool = False,
) -> Optional[Dict[str, Any]]:
    """Exact-MD5 metadata lookup for Gelbooru-compatible DAPI services."""
    if source_disabled(source_name):
        raise RuntimeError(
            f"{source_name} unavailable: {_SOURCE_DISABLED.get(source_name, 'disabled')}"
        )

    if require_auth and (not api_key or not user_id):
        disable_source(source_name, "API credentials are required but not configured")
        raise RuntimeError(f"{source_name} API credentials are required but not configured")

    params: Dict[str, str] = {
        "page": "dapi",
        "s": "post",
        "q": "index",
        "json": "1",
        "limit": "1",
        "tags": f"md5:{md5}",
    }
    if api_key and user_id:
        params["api_key"] = api_key
        params["user_id"] = user_id

    url = f"{base_url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        method="GET",
    )

    try:
        with HTTP.urlopen(req, timeout=30) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        if exc.code in (401, 403):
            disable_source(
                source_name,
                f"HTTP {exc.code} authentication/authorization failure"
            )
            raise RuntimeError(
                f"{source_name} HTTP {exc.code} authentication/authorization failure"
            ) from exc
        if exc.code == 404:
            return None
        preview = " ".join(detail.split())[:240]
        raise RuntimeError(
            f"{source_name} HTTP {exc.code}" + (f": {preview}" if preview else "")
        ) from exc
    except Exception:
        raise

    payload = _safe_json_response(
        raw,
        source_name,
        empty_is_miss=(source_name.casefold() == "rule34"),
    )
    if payload is None:
        return None

    for post in _normalize_gelbooru_posts(payload):
        if str(post.get("md5") or "").strip().lower() == md5.lower():
            return post
    return None


def gelbooru_post(md5: str, api_key: str, user_id: str) -> Optional[Dict[str, Any]]:
    return gelbooru_style_md5_post(
        GELBOORU_BASE,
        md5,
        "Gelbooru",
        api_key=api_key,
        user_id=user_id,
        require_auth=False,
    )


def rule34_post(md5: str, api_key: str, user_id: str) -> Optional[Dict[str, Any]]:
    return gelbooru_style_md5_post(
        RULE34_BASE,
        md5,
        "Rule34",
        api_key=api_key,
        user_id=user_id,
        require_auth=True,
    )


def _multipart_file(field_name: str, filename: str, data: bytes, content_type: str = "application/octet-stream") -> Tuple[bytes, str]:
    boundary = "----StashDanbooruIQDBBoundary7MA4YWxkTrZu0gW"
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8")
    body = head + data + f"\r\n--{boundary}--\r\n".encode("utf-8")
    return body, boundary


def danbooru_iqdb(image_bytes: bytes, login: str, api_key: str, minimum_score: float, debug_label: Optional[str] = None, diagnostics: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Use Danbooru IQDB for a conservative visual fallback.

    Danbooru exposes POST /iqdb_queries as an authenticated API route. The
    existing Stash image is transmitted in memory only. No duplicate is written
    locally and no Danbooru image file is downloaded.
    """
    if not login or not api_key:
        # Anonymous multipart POSTs are treated as browser form submissions and
        # can be rejected by Rails CSRF protection. Require API credentials.
        raise RuntimeError(
            "IQDB fallback requires both Danbooru login and API key in plugin settings"
        )

    url = f"{DANBOORU_BASE}/iqdb_queries.json"
    body, boundary = _multipart_file("file", "stash-image", image_bytes)
    basic = base64.b64encode(f"{login}:{api_key}".encode("utf-8")).decode("ascii")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Authorization": f"Basic {basic}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
    )
    try:
        with HTTP.urlopen(
            req,
            timeout=DEEP_VISUAL_TIMEOUT_SECONDS,
            retries=DEEP_VISUAL_HTTP_RETRIES,
        ) as resp:
            raw_response = resp.read().decode("utf-8")
            if debug_label is not None:
                # Diagnostic only: log the first few IQDB API responses during
                # dry runs. Request credentials are never included in this data.
                # Truncate to keep Stash logs manageable.
                compact = raw_response.replace("\r", " ").replace("\n", " ")
                log("INFO", f"IQDB DEBUG image {debug_label}: {compact[:4000]}")
            result = json.loads(raw_response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Danbooru IQDB HTTP {exc.code}: {detail[:300]}") from exc

    if isinstance(result, dict) and result.get("success") is False:
        return None
    if not isinstance(result, list):
        raise RuntimeError("Danbooru IQDB returned an unexpected response shape")
    if not result:
        return None

    candidates = []
    for item in result:
        if not isinstance(item, dict):
            continue
        try:
            score = float(item.get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        post = item.get("post")
        if isinstance(post, dict):
            candidates.append((score, post))

    if not candidates:
        if diagnostics is not None:
            diagnostics["best_score"] = 0.0
            diagnostics["candidate_count"] = 0
            diagnostics["minimum_score"] = minimum_score
        return None
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    score, post = candidates[0]
    if diagnostics is not None:
        diagnostics["best_score"] = score
        diagnostics["candidate_count"] = len(candidates)
        diagnostics["minimum_score"] = minimum_score
    if score < minimum_score:
        return None
    post = dict(post)
    post["_iqdb_score"] = score
    return post


def artist_names(
    post: Dict[str, Any],
    source: str,
    gelbooru_api_key: str = "",
    gelbooru_user_id: str = "",
    rule34_api_key: str = "",
    rule34_user_id: str = "",
) -> List[str]:
    """Extract explicit artist-category values for Studio assignment.

    Danbooru and e621 expose structured artist fields directly. Gelbooru-style
    post payloads expose a flat tag list, so resolve those tags through the tag
    metadata endpoint and keep only type 1 (artist). This is category metadata,
    not name-based guessing.
    """
    values: List[str] = []

    if source == "e621":
        groups = post.get("tags") or {}
        raw_values = groups.get("artist") if isinstance(groups, dict) else None
        if isinstance(raw_values, list):
            values.extend(str(v or "").strip() for v in raw_values)

    elif source == "danbooru":
        values.extend(str(post.get("tag_string_artist") or "").split())

    elif source in {"gelbooru", "rule34"}:
        raw_tags = [t.strip() for t in str(post.get("tags") or "").split() if t.strip()]
        if raw_tags:
            if source == "gelbooru":
                typed = gelbooru_style_tag_metadata(
                    GELBOORU_BASE, raw_tags, "Gelbooru",
                    gelbooru_api_key, gelbooru_user_id, False,
                )
            else:
                typed = gelbooru_style_tag_metadata(
                    RULE34_BASE, raw_tags, "Rule34",
                    rule34_api_key, rule34_user_id, True,
                )
            values.extend(name for name in raw_tags if typed.get(name) == 1)

    result: List[str] = []
    seen = set()
    for value in values:
        value = value.strip()
        if not value:
            continue
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def artist_studio_name(names: List[str]) -> Optional[str]:
    """Return the first explicit artist for the image's single Studio field."""
    if not names:
        return None
    return names[0]



def append_alias(existing_aliases: List[str], primary_name: str, alias: str) -> List[str]:
    primary_key = str(primary_name or "").casefold().strip()
    alias = str(alias or "").strip()
    if not alias or alias.casefold() == primary_key:
        return list(existing_aliases or [])

    result = []
    seen = set()
    for value in list(existing_aliases or []) + [alias]:
        value = str(value or "").strip()
        key = value.casefold()
        if not value or key == primary_key or key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def refresh_entity_cache(
    cache: Dict[str, Dict[str, Any]],
    obj: Dict[str, Any],
    alias_field: str,
) -> None:
    # Remove stale references to this object, then rebuild name+alias keys.
    obj_id = str(obj.get("id") or "")
    stale = [k for k, v in cache.items() if str(v.get("id") or "") == obj_id]
    for key in stale:
        cache.pop(key, None)

    values = [str(obj.get("name") or "")]
    values.extend(str(a or "") for a in (obj.get(alias_field) or []))
    for value in values:
        key = value.casefold().strip()
        if key:
            cache[key] = obj


def ensure_named_entity(
    *,
    stash: Stash,
    cache: Dict[str, Dict[str, Any]],
    name: str,
    dry_run: bool,
    merge_normalized: bool,
    merge_similar: bool,
    similarity_threshold: float,
    similarity_margin: float,
    alias_field: str,
    entity_label: str,
    create_fn: Any,
    update_aliases_fn: Any,
    reload_fn: Any,
) -> Optional[Dict[str, Any]]:
    key = name.casefold().strip()
    existing = cache.get(key)
    if existing:
        return existing

    match = find_entity_match(
        name,
        cache,
        alias_field,
        allow_normalized=merge_normalized,
        allow_fuzzy=merge_similar,
        threshold=similarity_threshold,
        margin=similarity_margin,
    )

    if match:
        similar = match.entity
        match_kind = "normalized" if match.normalized_exact else "fuzzy"
        if dry_run:
            log(
                "INFO",
                f"DRY RUN: {entity_label} '{name}' would reuse "
                f"'{similar.get('name')}' via {match_kind} match "
                f"({match.score * 100:.1f}%"
                + (
                    f", next-best {match.second_score * 100:.1f}%"
                    if not match.normalized_exact and match.second_score
                    else ""
                )
                + ")",
            )
            return similar

        current_aliases = list(similar.get(alias_field) or [])
        updated_aliases = append_alias(
            current_aliases, str(similar.get("name") or ""), name
        )
        if updated_aliases != current_aliases:
            similar = update_aliases_fn(str(similar["id"]), updated_aliases)
            log(
                "INFO",
                f"Merged {match_kind} {entity_label} '{name}' into "
                f"'{similar['name']}' as alias ({match.score * 100:.1f}%)",
            )
        refresh_entity_cache(cache, similar, alias_field)
        return similar

    if dry_run:
        return None

    try:
        created = create_fn(name)
        created[alias_field] = created.get(alias_field) or []
    except RuntimeError as exc:
        if "already" in str(exc).lower() or "unique" in str(exc).lower():
            cache.clear()
            cache.update(reload_fn())
            # Reloading can invalidate previous name/alias references; retry exact key
            # then normalized-only matching so a concurrent creator is reused safely.
            concurrent = cache.get(key)
            if concurrent:
                return concurrent
            concurrent_match = find_entity_match(
                name, cache, alias_field,
                allow_normalized=True, allow_fuzzy=False,
                threshold=1.0, margin=0.0,
            )
            return concurrent_match.entity if concurrent_match else None
        raise

    refresh_entity_cache(cache, created, alias_field)
    log("INFO", f"Created Stash {entity_label}: {name}")
    return created


def ensure_studio(
    stash: Stash,
    studio_cache: Dict[str, Dict[str, Any]],
    name: str,
    dry_run: bool,
    merge_normalized: bool = True,
    merge_similar: bool = True,
    similarity_threshold: float = 0.96,
    similarity_margin: float = 0.03,
) -> Optional[Dict[str, Any]]:
    return ensure_named_entity(
        stash=stash,
        cache=studio_cache,
        name=name,
        dry_run=dry_run,
        merge_normalized=merge_normalized,
        merge_similar=merge_similar,
        similarity_threshold=similarity_threshold,
        similarity_margin=similarity_margin,
        alias_field="aliases",
        entity_label="studio",
        create_fn=stash.create_studio,
        update_aliases_fn=stash.update_studio_aliases,
        reload_fn=stash.all_studios,
    )

def _normalize_tag_api_payload(payload: Any) -> List[Dict[str, Any]]:
    """Normalize Gelbooru-style tag API JSON into a list."""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("tag", "tags"):
        value = payload.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
        if isinstance(value, dict):
            return [value]
    return []


def gelbooru_style_tag_metadata(
    base_url: str,
    tag_names: List[str],
    source_name: str,
    api_key: str = "",
    user_id: str = "",
    require_auth: bool = False,
) -> Dict[str, int]:
    """Resolve tag type metadata for Gelbooru/Rule34 flat post tags.

    Both sites expose a DAPI tag-list endpoint. We batch `names` first and fall back
    to individual `name` requests if the server does not support the batch parameter.
    Type 4 is the Character category on Gelbooru-style tag metadata.
    """
    result: Dict[str, int] = {}
    pending = []
    for name in tag_names:
        key = (source_name.casefold(), name.casefold())
        if key in _TYPED_TAG_CACHE:
            cached = _TYPED_TAG_CACHE[key]
            if cached is not None:
                result[name] = int(cached)
        else:
            pending.append(name)

    if not pending:
        return result

    auth: Dict[str, str] = {}
    if api_key and user_id:
        auth["api_key"] = api_key
        auth["user_id"] = user_id
    elif require_auth:
        # Do not make unauthenticated calls to sources configured as auth-required.
        for name in pending:
            _TYPED_TAG_CACHE[(source_name.casefold(), name.casefold())] = None
        return result

    def fetch(params: Dict[str, str]) -> List[Dict[str, Any]]:
        q = {
            "page": "dapi",
            "s": "tag",
            "q": "index",
            "json": "1",
            "limit": "100",
            **params,
            **auth,
        }
        url = f"{base_url}?{urllib.parse.urlencode(q)}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        with HTTP.urlopen(req, timeout=30) as resp:
            raw = resp.read()
        payload = _safe_json_response(
            raw,
            source_name,
            empty_is_miss=(source_name.casefold() == "rule34"),
        )
        if payload is None:
            return []
        return _normalize_tag_api_payload(payload)

    # Most posts are comfortably under 100 tags. Gelbooru documents the `names`
    # parameter for multi-tag lookup, so use one request per chunk rather than one
    # request per tag. Rule34 is less explicit in its documentation; it gets a
    # bounded single-tag fallback only when the batch request itself succeeded.
    for start in range(0, len(pending), 50):
        chunk = pending[start:start + 50]
        try:
            rows = fetch({"names": " ".join(chunk)})
        except Exception as exc:
            log(
                "WARNING",
                f"{source_name} tag-type batch lookup failed; skipping category enrichment "
                f"for {len(chunk)} tag(s): {exc}",
            )
            # Do not cache a transient enrichment failure as a permanent unknown and
            # do not fan it out into dozens of individual retries.
            continue

        seen = set()
        for row in rows:
            name = str(row.get("name") or "").strip()
            if not name:
                continue
            try:
                type_id = int(row.get("type"))
            except (TypeError, ValueError):
                continue
            seen.add(name.casefold())
            _TYPED_TAG_CACHE[(source_name.casefold(), name.casefold())] = type_id
            result[name] = type_id

        missing = [name for name in chunk if name.casefold() not in seen]
        if source_name.casefold() != "rule34":
            # For Gelbooru, `names` is the documented multi-name endpoint. Missing
            # rows are genuine unknowns/nonexistent tag metadata for this run.
            for name in missing:
                _TYPED_TAG_CACHE[(source_name.casefold(), name.casefold())] = None
            continue

        # Rule34's public DAPI page does not document `names`. Keep compatibility
        # with engines that require `name`, but cap the fallback so one post cannot
        # explode into an unbounded number of requests.
        fallback_limit = 20
        for name in missing[:fallback_limit]:
            try:
                single = fetch({"name": name})
            except Exception as exc:
                log("WARNING", f"Rule34 tag-type lookup stopped after provider error: {exc}")
                break
            type_id: Optional[int] = None
            for row in single:
                row_name = str(row.get("name") or "").strip()
                if row_name.casefold() != name.casefold():
                    continue
                try:
                    type_id = int(row.get("type"))
                except (TypeError, ValueError):
                    type_id = None
                break
            _TYPED_TAG_CACHE[(source_name.casefold(), name.casefold())] = type_id
            if type_id is not None:
                result[name] = type_id
        if len(missing) > fallback_limit:
            log(
                "DEBUG",
                f"Rule34 tag-type fallback capped at {fallback_limit} of {len(missing)} missing tags",
            )

    return result


def flat_source_character_names(
    post: Dict[str, Any],
    source: str,
    gelbooru_api_key: str,
    gelbooru_user_id: str,
    rule34_api_key: str,
    rule34_user_id: str,
) -> List[str]:
    raw_tags = [t.strip() for t in str(post.get("tags") or "").split() if t.strip()]
    if not raw_tags:
        return []

    if source == "gelbooru":
        typed = gelbooru_style_tag_metadata(
            GELBOORU_BASE, raw_tags, "Gelbooru",
            gelbooru_api_key, gelbooru_user_id, False
        )
    elif source == "rule34":
        typed = gelbooru_style_tag_metadata(
            RULE34_BASE, raw_tags, "Rule34",
            rule34_api_key, rule34_user_id, True
        )
    else:
        return []

    return [name for name in raw_tags if typed.get(name) == 4]


def performer_names(
    post: Dict[str, Any],
    source: str,
    gelbooru_api_key: str = "",
    gelbooru_user_id: str = "",
    rule34_api_key: str = "",
    rule34_user_id: str = "",
) -> List[str]:
    values: List[str] = []
    if source == "e621":
        groups = post.get("tags") or {}
        raw_values = groups.get("character") if isinstance(groups, dict) else None
        if isinstance(raw_values, list):
            values.extend(str(v or "").strip() for v in raw_values)
    elif source == "danbooru":
        values.extend(str(post.get("tag_string_character") or "").split())
    elif source in {"gelbooru", "rule34"}:
        values.extend(
            flat_source_character_names(
                post, source,
                gelbooru_api_key, gelbooru_user_id,
                rule34_api_key, rule34_user_id,
            )
        )

    result: List[str] = []
    seen = set()
    for value in values:
        value = value.strip()
        if value and value.casefold() not in seen:
            seen.add(value.casefold())
            result.append(value)
    return result


def ensure_performer(
    stash: Stash,
    performer_cache: Dict[str, Dict[str, Any]],
    name: str,
    dry_run: bool,
    merge_normalized: bool = True,
    merge_similar: bool = True,
    similarity_threshold: float = 0.98,
    similarity_margin: float = 0.03,
) -> Optional[Dict[str, Any]]:
    return ensure_named_entity(
        stash=stash,
        cache=performer_cache,
        name=name,
        dry_run=dry_run,
        merge_normalized=merge_normalized,
        merge_similar=merge_similar,
        similarity_threshold=similarity_threshold,
        similarity_margin=similarity_margin,
        alias_field="alias_list",
        entity_label="performer",
        create_fn=stash.create_performer,
        update_aliases_fn=stash.update_performer_aliases,
        reload_fn=stash.all_performers,
    )


def _normalized_performer_names(performer: Dict[str, Any]) -> set[str]:
    """Return normalized primary/alias names for one Stash performer."""
    values = [str(performer.get("name") or "")]
    values.extend(str(alias or "") for alias in (performer.get("alias_list") or []))
    return {
        normalized_entity_name(value)
        for value in values
        if normalized_entity_name(value)
    }


def _equivalent_attached_performer_ids(
    image: Dict[str, Any],
    performer_cache: Dict[str, Dict[str, Any]],
    incoming_name: str,
    canonical: Dict[str, Any],
) -> set[str]:
    """Find stale duplicate performer attachments that should yield to canonical.

    This is intentionally conservative: an attached performer is replaced only when
    the current source name normalizes exactly to both the canonical performer and
    that attached performer. Fuzzy similarity alone never removes an attachment.
    """
    canonical_id = str(canonical.get("id") or "")
    incoming_norm = normalized_entity_name(incoming_name)
    if not canonical_id or not incoming_norm:
        return set()
    if incoming_norm not in _normalized_performer_names(canonical):
        return set()

    by_id = {
        str(obj.get("id")): obj
        for obj in unique_entities(performer_cache)
        if obj.get("id") is not None
    }
    duplicates: set[str] = set()
    for attached in image.get("performers") or []:
        attached_id = str(attached.get("id") or "")
        if not attached_id or attached_id == canonical_id:
            continue
        candidate = by_id.get(attached_id) or attached
        if incoming_norm in _normalized_performer_names(candidate):
            duplicates.add(attached_id)
    return duplicates

def tag_names(post: Dict[str, Any], source: str, include_meta: bool, prefixes: bool = False) -> List[str]:
    """Extract tag names from supported source post metadata."""
    if source in {"gelbooru", "rule34"}:
        raw = str(post.get("tags") or "")
        result: List[str] = []
        seen = set()
        for tag in raw.split():
            tag = tag.strip()
            if not tag:
                continue
            key = tag.casefold()
            if key not in seen:
                seen.add(key)
                result.append(tag)
        return result

    if source == "e621":
        groups_obj = post.get("tags") or {}
        result: List[str] = []
        seen = set()
        if isinstance(groups_obj, dict):
            for category, values in groups_obj.items():
                if category in {"artist", "character"}:
                    continue
                if category == "meta" and not include_meta:
                    continue
                if not isinstance(values, list):
                    continue
                for raw in values:
                    raw = str(raw or "").strip()
                    if not raw:
                        continue
                    name = raw
                    key = name.casefold()
                    if key not in seen:
                        seen.add(key)
                        result.append(name)
        return result

    groups = [
        ("copyright", post.get("tag_string_copyright") or ""),
        ("general", post.get("tag_string_general") or ""),
    ]
    if include_meta:
        groups.append(("meta", post.get("tag_string_meta") or ""))

    result: List[str] = []
    seen = set()
    for category, raw_group in groups:
        for raw in str(raw_group).split():
            raw = raw.strip()
            if not raw:
                continue
            name = raw
            key = name.casefold()
            if key not in seen:
                seen.add(key)
                result.append(name)
    return result


def image_file_extensions(image: Dict[str, Any]) -> set[str]:
    """Return every meaningful extension found in each Stash file path.

    Stash may represent archive-backed images with paths such as:
      C:\\library\\book.cbz\\page001.jpg
      /library/book.cbz/page001.jpg
      /library/book.cbz#page001.jpg

    The older implementation only looked at the final suffix (`.jpg`), so excluding
    `.cbz` did not work. This version detects extensions from every path component
    and from archive markers as well.
    """
    exts: set[str] = set()

    for file_obj in image.get("files") or []:
        path = str(file_obj.get("path") or "").strip()
        if not path:
            continue

        normalized = path.replace("\\", "/")

        # Split common archive/member notations and ordinary path components.
        candidates = []
        for chunk in re.split(r"[#!|]", normalized):
            candidates.extend(part for part in chunk.split("/") if part)

        for part in candidates:
            # Extract the last suffix from every path component. This catches
            # book.cbz even when page001.jpg follows it as an archive member.
            name = part.strip().lower()
            if "." not in name:
                continue

            # Also handle components that may contain query-like metadata.
            name = name.split("?", 1)[0]
            suffix = "." + name.rsplit(".", 1)[-1]
            if re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
                exts.add(suffix)

        # Explicitly detect known archive extensions anywhere in the raw path in
        # case Stash uses an unusual member separator.
        lower_path = normalized.lower()
        for archive_ext in (".cbz", ".cbr", ".cb7", ".cbt", ".zip", ".rar", ".7z"):
            if re.search(re.escape(archive_ext) + r"(?=$|[/\\#!|?])", lower_path):
                exts.add(archive_ext)

    return exts


def is_excluded_extension(image: Dict[str, Any], excluded: set[str]) -> bool:
    if not excluded:
        return False
    exts = image_file_extensions(image)
    return any(ext in excluded for ext in exts)


def image_fingerprint(image: Dict[str, Any], kind: str) -> Optional[str]:
    target = str(kind or "").strip().lower()
    for file_obj in image.get("files") or []:
        fps = file_obj.get("fingerprints") or []
        for fp in fps:
            if str(fp.get("type") or "").strip().lower() == target:
                value = str(fp.get("value") or "").strip()
                if value:
                    return value
    return None


def image_md5(image: Dict[str, Any]) -> Optional[str]:
    value = image_fingerprint(image, "md5")
    return value.lower() if value else None


def image_phash(image: Dict[str, Any]) -> Optional[str]:
    value = image_fingerprint(image, "phash")
    return value.lower() if value else None


def normalized_tag_key(name: str) -> str:
    """Normalize tag names so source/category formatting does not create duplicates."""
    value = unicodedata.normalize("NFKC", str(name or "")).casefold().strip()

    # Strip source category prefixes created by older versions of this plugin.
    # This allows "general:long_hair" to match "long_hair" / "long hair".
    known_prefixes = {"artist", "character", "copyright", "general", "meta"}
    if ":" in value:
        prefix, rest = value.split(":", 1)
        if prefix.strip() in known_prefixes:
            value = rest.strip()

    value = re.sub(r"[_\-\s]+", " ", value).strip()
    return value


def build_normalized_tag_index(cache: Dict[str, Dict[str, str]]) -> Dict[str, Dict[str, str]]:
    """Index only unambiguous normalized tag names.

    Two distinct existing tags can collapse to the same separator/case-normalized key. In
    that case neither is a safe automatic reuse target, so the key is deliberately omitted.
    """
    out: Dict[str, Dict[str, str]] = {}
    ambiguous: set[str] = set()
    seen_ids: set[str] = set()
    for tag in cache.values():
        tag_id = str(tag.get("id") or "")
        if not tag_id or tag_id in seen_ids:
            continue
        seen_ids.add(tag_id)
        key = normalized_tag_key(tag.get("name") or "")
        if not key or key in ambiguous:
            continue
        existing = out.get(key)
        if existing is not None and str(existing.get("id") or "") != tag_id:
            out.pop(key, None)
            ambiguous.add(key)
            continue
        out[key] = tag
    return out


def build_similarity_buckets(normalized_index: Dict[str, Dict[str, str]]) -> Dict[Tuple[str, int], List[Tuple[str, Dict[str, str]]]]:
    """Bucket normalized tags by first character and length for cheap fuzzy candidate lookup."""
    buckets: Dict[Tuple[str, int], List[Tuple[str, Dict[str, str]]]] = {}
    for norm, tag in normalized_index.items():
        if not norm:
            continue
        buckets.setdefault((norm[0], len(norm)), []).append((norm, tag))
    return buckets


def find_similar_existing_tag(
    name: str,
    cache: Dict[str, Dict[str, str]],
    normalized_index: Dict[str, Dict[str, str]],
    threshold: float,
    similarity_buckets: Optional[Dict[Tuple[str, int], List[Tuple[str, Dict[str, str]]]]] = None,
    margin: float = 0.02,
) -> Optional[Dict[str, str]]:
    # First merge separator/case variants exactly: red_hair == red hair == red-hair.
    norm = normalized_tag_key(name)
    if not norm:
        return None
    exact_norm = normalized_index.get(norm)
    if exact_norm:
        return exact_norm

    # Conservative fuzzy merge for obvious near-duplicates/typos only.
    body = norm
    if len(body) < 5:
        return None

    best = None
    best_ratio = 0.0
    second_ratio = 0.0
    if similarity_buckets is None:
        candidates = normalized_index.items()
    else:
        candidates = []
        for length in range(max(1, len(body) - 2), len(body) + 3):
            candidates.extend(similarity_buckets.get((body[0], length), []))
    for existing_norm, tag in candidates:
        existing_body = existing_norm
        if not existing_body or abs(len(existing_body) - len(body)) > 2:
            continue
        ratio = difflib.SequenceMatcher(None, body, existing_body).ratio()
        if ratio > best_ratio:
            second_ratio = best_ratio
            best_ratio = ratio
            best = tag
        elif ratio > second_ratio:
            second_ratio = ratio

    if (
        best is not None
        and best_ratio >= threshold
        and (second_ratio == 0.0 or best_ratio - second_ratio >= max(0.0, margin))
    ):
        return best
    return None


def ensure_tags(
    stash: Stash,
    cache: Dict[str, Dict[str, str]],
    names: Iterable[str],
    dry_run: bool,
    merge_similar: bool = True,
    similarity_threshold: float = 0.96,
    similarity_margin: float = 0.02,
    normalized_index: Optional[Dict[str, Dict[str, str]]] = None,
    similarity_buckets: Optional[Dict[Tuple[str, int], List[Tuple[str, Dict[str, str]]]]] = None,
) -> List[str]:
    ids: List[str] = []
    if normalized_index is None:
        normalized_index = build_normalized_tag_index(cache)
    if similarity_buckets is None:
        similarity_buckets = build_similarity_buckets(normalized_index)

    for name in names:
        key = name.casefold()
        current = cache.get(key)
        if current:
            ids.append(str(current["id"]))
            continue

        similar = None
        if merge_similar:
            similar = find_similar_existing_tag(
                name,
                cache,
                normalized_index,
                similarity_threshold,
                similarity_buckets,
                similarity_margin,
            )
        if similar:
            ids.append(str(similar["id"]))
            log("INFO", f"Merged similar tag '{name}' into existing Stash tag '{similar['name']}'")
            continue

        if dry_run:
            continue

        try:
            created = stash.create_tag(name)
        except RuntimeError as exc:
            if any(token in str(exc).lower() for token in ("already", "unique", "used as alias")):
                cache.clear()
                cache.update(stash.all_tags())
                rebuilt_index = build_normalized_tag_index(cache)
                normalized_index.clear()
                normalized_index.update(rebuilt_index)
                rebuilt_buckets = build_similarity_buckets(normalized_index)
                similarity_buckets.clear()
                similarity_buckets.update(rebuilt_buckets)
                existing = cache.get(key) or (
                    find_similar_existing_tag(
                        name, cache, normalized_index, similarity_threshold,
                        similarity_buckets, similarity_margin
                    )
                    if merge_similar else None
                )
                if existing:
                    ids.append(str(existing["id"]))
                    continue
            raise

        cache[key] = created
        created_norm = normalized_tag_key(created["name"])
        normalized_index[created_norm] = created
        if created_norm:
            similarity_buckets.setdefault((created_norm[0], len(created_norm)), []).append((created_norm, created))
        ids.append(str(created["id"]))
        log("INFO", f"Created Stash tag: {name}")

    return ids


def ensure_marker_tag(
    stash: Stash,
    marker_name: str,
    tag_cache: Dict[str, Dict[str, str]],
    normalized_tag_index: Dict[str, Dict[str, str]],
    similarity_buckets: Dict[Tuple[str, int], List[Tuple[str, Dict[str, str]]]],
) -> str:
    key = marker_name.casefold()
    existing = tag_cache.get(key)
    if existing:
        return str(existing["id"])
    try:
        created = stash.create_tag(marker_name)
    except RuntimeError as exc:
        if not any(token in str(exc).lower() for token in ("already", "unique", "used as alias")):
            raise
        tag_cache.clear()
        tag_cache.update(stash.all_tags())
        existing = tag_cache.get(key)
        if not existing:
            raise
        created = existing
    tag_cache[key] = created
    norm = normalized_tag_key(created["name"])
    if norm:
        normalized_tag_index[norm] = created
        bucket = similarity_buckets.setdefault((norm[0], len(norm)), [])
        if not any(str(tag.get("id")) == str(created.get("id")) for _, tag in bucket):
            bucket.append((norm, created))
    log("INFO", f"Ensured Stash marker tag: {marker_name}")
    return str(created["id"])


def ensure_import_marker_tag(
    stash: Stash,
    tag_cache: Dict[str, Dict[str, str]],
    normalized_tag_index: Dict[str, Dict[str, str]],
    similarity_buckets: Dict[Tuple[str, int], List[Tuple[str, Dict[str, str]]]],
) -> str:
    return ensure_marker_tag(
        stash, IMPORT_MARKER_TAG, tag_cache, normalized_tag_index, similarity_buckets
    )


def ensure_no_match_marker_tag(
    stash: Stash,
    tag_cache: Dict[str, Dict[str, str]],
    normalized_tag_index: Dict[str, Dict[str, str]],
    similarity_buckets: Dict[Tuple[str, int], List[Tuple[str, Dict[str, str]]]],
) -> str:
    return ensure_marker_tag(
        stash, NO_MATCH_MARKER_TAG, tag_cache, normalized_tag_index, similarity_buckets
    )

def ensure_unresolved_marker_tag(
    stash: Stash,
    tag_cache: Dict[str, Dict[str, str]],
    normalized_tag_index: Dict[str, Dict[str, str]],
    similarity_buckets: Dict[Tuple[str, int], List[Tuple[str, Dict[str, str]]]],
) -> str:
    return ensure_marker_tag(
        stash, UNRESOLVED_MARKER_TAG, tag_cache, normalized_tag_index, similarity_buckets
    )


def ensure_review_marker_tag(
    stash: Stash,
    tag_cache: Dict[str, Dict[str, str]],
    normalized_tag_index: Dict[str, Dict[str, str]],
    similarity_buckets: Dict[Tuple[str, int], List[Tuple[str, Dict[str, str]]]],
) -> str:
    return ensure_marker_tag(
        stash, REVIEW_MARKER_TAG, tag_cache, normalized_tag_index, similarity_buckets
    )


def _status_marker_keys() -> set[str]:
    return {name.casefold() for name in STATUS_MARKER_TAGS}


def _image_status_names(image: Dict[str, Any]) -> set[str]:
    status_keys = _status_marker_keys()
    return {
        str(tag.get("name") or "").casefold()
        for tag in (image.get("tags") or [])
        if str(tag.get("name") or "").casefold() in status_keys
    }


def _non_status_tag_ids(image: Dict[str, Any]) -> List[str]:
    status_keys = _status_marker_keys()
    return [
        str(tag["id"])
        for tag in (image.get("tags") or [])
        if str(tag.get("name") or "").casefold() not in status_keys
    ]


_REVIEW_CONFIDENCE_FRAGMENT_KEY = "booru-importer-review-confidence"


def _review_candidate_parts(url: str) -> Tuple[str, Optional[float]]:
    """Return the canonical candidate URL plus an optional persisted SauceNAO score."""
    text = str(url or "").strip()
    if not text:
        return "", None
    parts = urllib.parse.urlsplit(text)
    fragment_values = urllib.parse.parse_qs(parts.fragment, keep_blank_values=True)
    raw_score = (fragment_values.get(_REVIEW_CONFIDENCE_FRAGMENT_KEY) or [None])[0]
    if raw_score is None:
        return text, None
    try:
        score = max(0.0, min(100.0, float(raw_score)))
    except (TypeError, ValueError):
        score = None
    canonical = urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, parts.query, "")
    )
    return canonical, score


def _review_candidate_storage_url(url: str, confidence: Optional[float]) -> str:
    """Persist Review confidence in a URL fragment without changing the source request."""
    canonical, _ = _review_candidate_parts(url)
    if not canonical or confidence is None:
        return canonical
    score = max(0.0, min(100.0, float(confidence)))
    parts = urllib.parse.urlsplit(canonical)
    fragment = urllib.parse.urlencode(
        {_REVIEW_CONFIDENCE_FRAGMENT_KEY: f"{score:.1f}"}
    )
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, parts.query, fragment)
    )


def transition_image_status(
    stash: Stash,
    image: Dict[str, Any],
    marker_name: str,
    tag_cache: Dict[str, Dict[str, str]],
    normalized_tag_index: Dict[str, Dict[str, str]],
    similarity_buckets: Dict[Tuple[str, int], List[Tuple[str, Dict[str, str]]]],
    *,
    extra_url: Optional[str] = None,
    review_confidence: Optional[float] = None,
    dry_run: bool = False,
) -> bool:
    """Set exactly one Multi-Booru workflow marker while preserving user metadata.

    Status markers are mutually exclusive. ``extra_url`` is used for REVIEW so the
    candidate source can be opened directly from the Stash image. Review confidence is
    persisted only in an internal URL fragment that the UI hides from the visible link.
    """
    if marker_name not in STATUS_MARKER_TAGS:
        raise ValueError(f"Unknown workflow marker: {marker_name}")
    if dry_run:
        return False

    marker_id = ensure_marker_tag(
        stash, marker_name, tag_cache, normalized_tag_index, similarity_buckets
    )
    base_ids = _non_status_tag_ids(image)
    final_ids = list(dict.fromkeys([*base_ids, marker_id]))
    current_ids = [str(tag["id"]) for tag in (image.get("tags") or [])]

    current_urls = [str(url).strip() for url in (image.get("urls") or []) if str(url).strip()]
    target_urls: Optional[List[str]] = None
    if extra_url:
        url = str(extra_url).strip()
        if url and marker_name == REVIEW_MARKER_TAG:
            url = _review_candidate_storage_url(url, review_confidence)
        if url:
            # REVIEW uses URL ordering to identify the active proposed candidate in
            # the image-page UI. Preserve every other URL, de-duplicate the candidate,
            # and keep the active candidate last even if it was already present.
            reordered_urls = [
                existing_url
                for existing_url in current_urls
                if existing_url.casefold() != url.casefold()
            ]
            reordered_urls.append(url)
            if reordered_urls != current_urls:
                target_urls = reordered_urls

    changed = set(final_ids) != set(current_ids) or target_urls is not None
    if changed:
        stash.update_image_tags(
            str(image["id"]),
            final_ids,
            urls=target_urls,
        )

    clean_tags = [
        tag for tag in (image.get("tags") or [])
        if str(tag.get("name") or "").casefold() not in _status_marker_keys()
    ]
    marker_obj = tag_cache.get(marker_name.casefold()) or {"id": marker_id, "name": marker_name}
    image["tags"] = [*clean_tags, {"id": str(marker_obj["id"]), "name": marker_obj["name"]}]
    if target_urls is not None:
        image["urls"] = target_urls
    return changed

def build_imported_phash_index(images: List[Dict[str, Any]]) -> PHashIndex:
    pairs: List[Tuple[str, Dict[str, Any]]] = []
    for image in images:
        phash = image_phash(image)
        if phash:
            pairs.append((phash, image))
    return PHashIndex(pairs)


def find_local_phash_match(
    image: Dict[str, Any],
    phash_index: PHashIndex,
    max_distance: int,
    min_margin: int = 1,
) -> Optional[Tuple[int, Dict[str, Any]]]:
    target = image_phash(image)
    if not target:
        return None
    return phash_index.unique_nearest(
        target,
        max_distance,
        exclude_image_id=str(image.get("id") or ""),
        min_margin=min_margin,
    )
def source_post_url(source: str, post: Dict[str, Any]) -> Optional[str]:
    post_id = post.get("id")
    if not post_id:
        return None
    if source == "danbooru":
        return f"https://danbooru.donmai.us/posts/{post_id}"
    if source == "e621":
        return f"https://e621.net/posts/{post_id}"
    if source == "gelbooru":
        return f"https://gelbooru.com/index.php?page=post&s=view&id={post_id}"
    if source == "rule34":
        return f"https://rule34.xxx/index.php?page=post&s=view&id={post_id}"
    return None


def normalize_source_date(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("date") or value.get("timestamp") or value.get("s") or value.get("iso")
    text = str(value or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d{9,13}", text):
        try:
            stamp = int(text)
            if stamp > 10_000_000_000:
                stamp //= 1000
            return datetime.fromtimestamp(stamp, tz=timezone.utc).strftime("%Y-%m-%d")
        except Exception:
            pass
    candidate = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(candidate).strftime("%Y-%m-%d")
    except ValueError:
        pass
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", text)
    return m.group(1) if m else None


def source_post_date(source: str, post: Dict[str, Any]) -> Optional[str]:
    # Only use fields that clearly represent source creation/posting dates.
    # Do not use generic change/update timestamps, which may reflect later edits.
    for value in (
        post.get("created_at"),
        post.get("created"),
        post.get("date"),
        post.get("posted_at"),
    ):
        parsed = normalize_source_date(value)
        if parsed:
            return parsed
    return None


def _attempt_lookup(provider: str, stage: str, fn: Any) -> LookupOutcome:
    try:
        value = fn()
    except Exception as exc:
        status = LookupStatus.RETRYABLE_ERROR if is_retryable_exception(exc) else LookupStatus.UNAVAILABLE
        return LookupOutcome(provider, stage, status, None, str(exc))
    if value is None:
        return LookupOutcome(provider, stage, LookupStatus.MISS)
    return LookupOutcome(provider, stage, LookupStatus.MATCH, value)


def _outcome_summary(outcomes: List[LookupOutcome]) -> str:
    return ", ".join(
        f"{o.provider}/{o.stage}={o.status.value}" + (f" ({o.detail[:80]})" if o.detail else "")
        for o in outcomes
    )


def _metric(metrics: Optional[Dict[str, Any]], key: str, amount: int = 1) -> None:
    if metrics is None:
        return
    metrics[key] = int(metrics.get(key, 0) or 0) + int(amount)


def _decision_enabled(settings: Dict[str, Any], dry_run: bool, lookup_mode: str) -> bool:
    # Deep runs are intentionally verbose because they are the expensive/review pass.
    # The fast-pass verbosity preset is fixed in constants.py; legacy Stash values are ignored.
    return dry_run or lookup_mode == "deep" or VERBOSE_FAST_DECISION_LOGGING


def _saucenao_policy_score(score: float) -> float:
    """Normalize SauceNAO similarity to the same one-decimal value shown to users."""
    try:
        value = float(score)
    except (TypeError, ValueError):
        value = 0.0
    value = max(0.0, min(100.0, value))
    return float(f"{value:.1f}")


def _visual_confidence(score: float, auto_threshold: float, review_threshold: float) -> str:
    score = _saucenao_policy_score(score)
    if score >= auto_threshold:
        return "HIGH"
    if score >= review_threshold:
        return "REVIEW"
    return "LOW"


def _saucenao_thresholds(settings: Dict[str, Any]) -> Tuple[float, float, bool]:
    # Fixed policy helper. Legacy Stash tuning values are ignored.
    high = max(0.0, min(100.0, float(SAUCENAO_HIGH_CONFIDENCE)))
    review = max(0.0, min(high, float(SAUCENAO_REVIEW_MINIMUM)))
    return high, review, bool(SAUCENAO_ACCEPT_REVIEW_BAND)


def _format_stage_decisions(items: List[str]) -> str:
    return "; ".join(item for item in items if item) or "no applicable lookups"


def _refresh_image_metadata_for_index(
    image: Dict[str, Any],
    tag_ids: Iterable[str],
    tag_cache: Dict[str, Dict[str, str]],
    performer_ids: Iterable[str],
    performer_cache: Dict[str, Dict[str, Any]],
    studio: Optional[Dict[str, Any]],
    date_value: Optional[str],
    urls: Optional[List[str]],
) -> None:
    tags_by_id = {str(t.get("id")): t for t in tag_cache.values() if t.get("id") is not None}
    performers_by_id = {
        str(p.get("id")): p for p in unique_entities(performer_cache) if p.get("id") is not None
    }
    image["tags"] = [tags_by_id.get(str(tid), {"id": str(tid), "name": ""}) for tid in tag_ids]
    image["performers"] = [
        performers_by_id.get(str(pid), {"id": str(pid), "name": ""}) for pid in performer_ids
    ]
    if studio is not None:
        image["studio"] = studio
    if date_value is not None:
        image["date"] = date_value
    if urls is not None:
        image["urls"] = list(urls)


def _resolve_supported_booru_url(
    url: str,
    *,
    danbooru_login: str,
    danbooru_api_key: str,
    gelbooru_api_key: str,
    gelbooru_user_id: str,
    rule34_api_key: str,
    rule34_user_id: str,
    e621_username: str,
    e621_api_key: str,
) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Resolve a canonical booru post URL back to current provider metadata."""
    text = str(url or "").strip()
    if not text:
        return None
    match = re.search(r'danbooru\.donmai\.us/posts/(\d+)', text)
    if match:
        post = danbooru_post_by_id(match.group(1), danbooru_login, danbooru_api_key)
        return ("danbooru", post) if post else None
    match = re.search(r'gelbooru\.com/.*[?&]id=(\d+)', text)
    if match:
        post = gelbooru_style_post_by_id(
            GELBOORU_BASE, match.group(1), "Gelbooru", gelbooru_api_key, gelbooru_user_id
        )
        return ("gelbooru", post) if post else None
    match = re.search(r'rule34\.xxx/.*[?&]id=(\d+)', text)
    if match:
        if not rule34_api_key or not rule34_user_id:
            return None
        post = gelbooru_style_post_by_id(
            RULE34_BASE, match.group(1), "Rule34", rule34_api_key, rule34_user_id, True
        )
        return ("rule34", post) if post else None
    match = re.search(r'e621\.net/posts/(\d+)', text)
    if match:
        post = e621_post_by_id(match.group(1), e621_username, e621_api_key)
        return ("e621", post) if post else None
    return None


def _try_local_phash_reuse(
    image: Dict[str, Any],
    phash_index: PHashIndex,
    *,
    danbooru_login: str,
    danbooru_api_key: str,
    gelbooru_api_key: str,
    gelbooru_user_id: str,
    rule34_api_key: str,
    rule34_user_id: str,
    e621_username: str,
    e621_api_key: str,
    metrics: Optional[Dict[str, Any]] = None,
    decision_details: Optional[List[str]] = None,
) -> LookupOutcome:
    """Use pHash only to locate a trusted source post, never to copy local metadata.

    Imported Stash images may contain user-created tags, performers, studios, dates, or
    URLs that did not come from a booru. Copying those fields to another image would
    contaminate user metadata. A pHash hit therefore re-resolves one of the source
    image's canonical booru URLs and then feeds that fresh provider payload through the
    same normal metadata application path as an exact MD5 match.
    """
    if not ENABLE_LOCAL_PHASH_REUSE:
        if decision_details is not None:
            decision_details.append("Local pHash: disabled")
        return LookupOutcome("Local pHash", "source", LookupStatus.MISS)

    target_phash = image_phash(image)
    if not target_phash:
        if decision_details is not None:
            decision_details.append("Local pHash: unavailable (image has no pHash fingerprint)")
        return LookupOutcome("Local pHash", "source", LookupStatus.MISS)

    _metric(metrics, "phash_queries")
    max_distance = max(0, min(16, int(LOCAL_PHASH_MAX_DISTANCE)))
    min_margin = max(1, min(16, int(LOCAL_PHASH_MIN_MARGIN)))
    match = find_local_phash_match(image, phash_index, max_distance, min_margin)
    if not match:
        if decision_details is not None:
            decision_details.append(
                f"Local pHash: no unique candidate within distance {max_distance} "
                f"(winner margin {min_margin})"
            )
        return LookupOutcome("Local pHash", "source", LookupStatus.MISS)

    distance, source_image = match
    source_image_id = str(source_image.get("id") or "")
    source_urls = [str(url or "").strip() for url in (source_image.get("urls") or []) if str(url or "").strip()]
    supported_urls: List[str] = []
    seen_supported: set[str] = set()
    for source_url in source_urls:
        folded = source_url.casefold()
        if not any(
            domain in folded for domain in (
                "danbooru.donmai.us/posts/", "gelbooru.com/", "rule34.xxx/", "e621.net/posts/"
            )
        ):
            continue
        if folded in seen_supported:
            continue
        seen_supported.add(folded)
        supported_urls.append(source_url)

    if not supported_urls:
        if decision_details is not None:
            decision_details.append(
                f"Local pHash: Stash image {source_image_id} matched at distance {distance}, "
                "but has no trusted booru source URL"
            )
        return LookupOutcome("Local pHash", "source", LookupStatus.MISS)

    # Never guess which provider post is authoritative when the source image carries
    # multiple supported booru URLs. This can happen after a prior review candidate
    # was promoted to Imported. Falling back to the normal provider chain is slower
    # but prevents pHash from attaching metadata from the wrong post.
    if len(supported_urls) != 1:
        if decision_details is not None:
            decision_details.append(
                f"Local pHash: Stash image {source_image_id} matched at distance {distance}, "
                f"but has {len(supported_urls)} supported booru source URLs; refusing ambiguous reuse"
            )
        return LookupOutcome("Local pHash", "source", LookupStatus.MISS)

    source_url = supported_urls[0]
    last_error: Optional[BaseException] = None
    try:
        resolved = _resolve_supported_booru_url(
            source_url,
            danbooru_login=danbooru_login,
            danbooru_api_key=danbooru_api_key,
            gelbooru_api_key=gelbooru_api_key,
            gelbooru_user_id=gelbooru_user_id,
            rule34_api_key=rule34_api_key,
            rule34_user_id=rule34_user_id,
            e621_username=e621_username,
            e621_api_key=e621_api_key,
        )
    except BaseException as exc:  # classified below; do not copy local metadata on failure
        last_error = exc
        resolved = None

    if resolved:
        source, post = resolved
        post = dict(post)
        post["_local_phash_distance"] = distance
        post["_local_phash_source_image_id"] = source_image_id
        if decision_details is not None:
            decision_details.append(
                f"Local pHash: trusted Stash image {source_image_id} at distance {distance}; "
                f"refreshed {source} post #{post.get('id')}"
            )
        return LookupOutcome(
            "Local pHash", "source", LookupStatus.MATCH,
            (source, post), f"Stash image {source_image_id}, distance {distance}",
        )

    if last_error is not None:
        status = LookupStatus.RETRYABLE_ERROR if is_retryable_exception(last_error) else LookupStatus.UNAVAILABLE
        detail = str(last_error)
        if decision_details is not None:
            decision_details.append(
                f"Local pHash: source refresh {status.value}"
                + (f" ({detail})" if detail else "")
            )
        return LookupOutcome("Local pHash", "source", status, None, detail)

    if decision_details is not None:
        decision_details.append(
            f"Local pHash: Stash image {source_image_id} matched at distance {distance}, "
            "but its booru source post no longer resolves"
        )
    return LookupOutcome("Local pHash", "source", LookupStatus.MISS)


def process_image(
    stash: Stash,
    image: Dict[str, Any],
    settings: Dict[str, Any],
    tag_cache: Dict[str, Dict[str, str]],
    dry_run: bool,
    normalized_tag_index: Dict[str, Dict[str, str]],
    similarity_buckets: Dict[Tuple[str, int], List[Tuple[str, Dict[str, str]]]],
    studio_cache: Dict[str, Dict[str, Any]],
    performer_cache: Dict[str, Dict[str, Any]],
    phash_index: PHashIndex,
    force_recheck: bool = False,
    lookup_mode: str = "deep",
    metrics: Optional[Dict[str, Any]] = None,
    approved_source_url: str = "",
) -> str:
    iid = str(image["id"])
    lookup_mode = str(lookup_mode or "deep").strip().casefold()
    if lookup_mode not in {"fast", "deep"}:
        lookup_mode = "deep"
    decision_details: List[str] = []
    detailed_decisions = _decision_enabled(settings, dry_run, lookup_mode)
    image_status_names = _image_status_names(image)
    had_imported_status = IMPORT_MARKER_TAG.casefold() in image_status_names

    skip_fast_stages = (
        lookup_mode == "deep"
        and not force_recheck
        and UNRESOLVED_MARKER_TAG.casefold() in image_status_names
    )

    if not force_recheck:
        if had_imported_status:
            return "skipped_imported"
        if NO_MATCH_MARKER_TAG.casefold() in image_status_names:
            return "skipped_no_match"
        if REVIEW_MARKER_TAG.casefold() in image_status_names:
            return "skipped_review"
        if lookup_mode == "fast" and UNRESOLVED_MARKER_TAG.casefold() in image_status_names:
            return "skipped_unresolved"

    excluded_extensions = set(EXCLUDED_EXTENSIONS)
    if is_excluded_extension(image, excluded_extensions):
        return "skipped_extension"

    md5 = image_md5(image)
    danbooru_login = str(settings.get("danbooru_login") or "").strip()
    danbooru_api_key = str(settings.get("danbooru_api_key") or "").strip()
    gelbooru_api_key = str(settings.get("gelbooru_api_key") or "").strip()
    gelbooru_user_id = str(settings.get("gelbooru_user_id") or "").strip()
    rule34_api_key = str(settings.get("rule34_api_key") or "").strip()
    rule34_user_id = str(settings.get("rule34_user_id") or "").strip()
    e621_username = str(settings.get("e621_username") or "").strip()
    e621_api_key = str(settings.get("e621_api_key") or "").strip()
    saucenao_api_key = str(settings.get("saucenao_api_key") or "").strip()
    saucenao_requests_per_30_seconds = _saucenao_requested_rate(settings)

    include_meta = bool(INCLUDE_META_TAGS)
    merge_similar = bool(MERGE_SIMILAR_TAGS)
    similar_threshold = max(90.0, min(100.0, float(SIMILAR_TAG_THRESHOLD))) / 100.0
    tag_similarity_margin = max(0.0, min(20.0, float(SIMILAR_TAG_MARGIN))) / 100.0

    merge_normalized_studios = bool(MERGE_NORMALIZED_STUDIOS)
    merge_similar_studios = bool(MERGE_SIMILAR_STUDIOS)
    studio_similarity_threshold = max(90.0, min(100.0, float(STUDIO_SIMILARITY_THRESHOLD))) / 100.0
    merge_normalized_performers = bool(MERGE_NORMALIZED_PERFORMERS)
    merge_similar_performers = bool(MERGE_SIMILAR_PERFORMERS)
    performer_similarity_threshold = max(90.0, min(100.0, float(PERFORMER_SIMILARITY_THRESHOLD))) / 100.0
    entity_similarity_margin = max(0.0, min(20.0, float(ENTITY_SIMILARITY_MARGIN))) / 100.0

    artist_mapping = ARTIST_MAPPING
    character_mapping = CHARACTER_MAPPING
    create_secondary_artist_studios = bool(CREATE_SECONDARY_ARTIST_STUDIOS)

    post: Optional[Dict[str, Any]] = None
    source = ""
    match_method = ""
    outcomes: List[LookupOutcome] = []

    approved_source_url = str(approved_source_url or "").strip()
    if approved_source_url:
        resolved = _resolve_supported_booru_url(
            approved_source_url,
            danbooru_login=danbooru_login,
            danbooru_api_key=danbooru_api_key,
            gelbooru_api_key=gelbooru_api_key,
            gelbooru_user_id=gelbooru_user_id,
            rule34_api_key=rule34_api_key,
            rule34_user_id=rule34_user_id,
            e621_username=e621_username,
            e621_api_key=e621_api_key,
        )
        if not resolved:
            raise RuntimeError("The approved Review candidate could not be resolved from its source site")
        source, post = resolved
        match_method = "manual_review"
        decision_details.append(f"Review candidate: manually approved {approved_source_url}")

    if post is None and skip_fast_stages:
        decision_details.append("Fast MD5/pHash stages: skipped (already Unresolved)")
        _metric(metrics, "fast_stages_reused")
    elif post is None:
        # Authoritative exact hashes always win over perceptual reuse.
        exact_specs = []
        if md5 and ENABLE_DANBOORU:
            exact_specs.append(("Danbooru", "danbooru", lambda: danbooru_post(md5, danbooru_login, danbooru_api_key)))
        if md5 and ENABLE_GELBOORU:
            exact_specs.append(("Gelbooru", "gelbooru", lambda: gelbooru_post(md5, gelbooru_api_key, gelbooru_user_id)))
        if md5 and ENABLE_RULE34:
            if rule34_api_key and rule34_user_id:
                exact_specs.append(("Rule34", "rule34", lambda: rule34_post(md5, rule34_api_key, rule34_user_id)))
            else:
                decision_details.append("Rule34 MD5: skipped (credentials not configured)")
        if md5 and ENABLE_E621:
            exact_specs.append(("e621", "e621", lambda: e621_post(md5, e621_username, e621_api_key)))

        for provider_label, source_key, lookup_fn in exact_specs:
            if provider_label == "Rule34":
                _maybe_log_rule34_resumed()

            if source_disabled(provider_label):
                outcome = LookupOutcome(
                    provider_label, "md5", LookupStatus.UNAVAILABLE, None,
                    _SOURCE_DISABLED.get(provider_label, "disabled for this run"),
                )
            elif provider_label == "Rule34" and HTTP.cooldown_remaining(_rule34_host()) > 0:
                remaining = HTTP.cooldown_remaining(_rule34_host())
                outcome = LookupOutcome(
                    provider_label, "md5", LookupStatus.RETRYABLE_ERROR, None,
                    f"rate-limit cooldown active ({remaining:.0f}s remaining)",
                )
            else:
                _metric(metrics, "md5_queries")
                outcome = _attempt_lookup(provider_label, "md5", lookup_fn)
            outcomes.append(outcome)
            decision_details.append(
                f"{provider_label} MD5: {outcome.status.value}"
                + (f" ({outcome.detail})" if outcome.detail else "")
            )
            if outcome.matched:
                source = source_key
                post = outcome.value
                match_method = "md5"
                break
            if outcome.status in {LookupStatus.RETRYABLE_ERROR, LookupStatus.UNAVAILABLE}:
                if not _observe_rule34_problem(outcome, metrics):
                    _log_lookup_problem_once(provider_label, str(outcome.detail or outcome.status.value))

        if not md5:
            decision_details.append("MD5: unavailable (image has no MD5 fingerprint)")

        if post is None:
            local_outcome = _try_local_phash_reuse(
                image, phash_index,
                danbooru_login=danbooru_login, danbooru_api_key=danbooru_api_key,
                gelbooru_api_key=gelbooru_api_key, gelbooru_user_id=gelbooru_user_id,
                rule34_api_key=rule34_api_key, rule34_user_id=rule34_user_id,
                e621_username=e621_username, e621_api_key=e621_api_key,
                metrics=metrics, decision_details=decision_details,
            )
            outcomes.append(local_outcome)
            if local_outcome.matched:
                source, post = local_outcome.value
                match_method = "local_phash"
            elif local_outcome.status in {LookupStatus.RETRYABLE_ERROR, LookupStatus.UNAVAILABLE}:
                _log_lookup_problem_once("Local pHash", str(local_outcome.detail or local_outcome.status.value))

    # Fast pass deliberately stops before any image upload / reverse-image-search API.
    if post is None and lookup_mode == "fast":
        if any(
            o.status in {LookupStatus.RETRYABLE_ERROR, LookupStatus.UNAVAILABLE}
            for o in outcomes
        ):
            if any(
                o.provider == "Rule34"
                and o.status in {LookupStatus.RETRYABLE_ERROR, LookupStatus.UNAVAILABLE}
                for o in outcomes
            ):
                _metric(metrics, "pending_rule34_confirmation")
            if detailed_decisions:
                log(
                    "WARNING",
                    f"Image {iid}: RETRY LATER (fast pass); "
                    f"{_format_stage_decisions(decision_details)}",
                )
            return "retry_later"
        if detailed_decisions:
            log(
                "INFO",
                f"Image {iid}: UNRESOLVED (fast pass); "
                f"{_format_stage_decisions(decision_details)}; "
                + (
                    f"'{UNRESOLVED_MARKER_TAG}' would be written"
                    if dry_run
                    else f"writing '{UNRESOLVED_MARKER_TAG}' for Deep Match"
                ),
            )
        if not dry_run:
            transition_image_status(
                stash, image, UNRESOLVED_MARKER_TAG, tag_cache,
                normalized_tag_index, similarity_buckets,
            )
        return "unresolved"

    image_payload: Optional[bytes] = None

    def get_image_payload() -> bytes:
        nonlocal image_payload
        if image_payload is None:
            image_payload = stash.image_bytes(iid)
        return image_payload

    if (
        post is None
        and ENABLE_DANBOORU
        and ENABLE_DANBOORU_IQDB
        and danbooru_login
        and danbooru_api_key
    ):
        iqdb_diag: Dict[str, Any] = {}
        minimum_score = max(0.0, min(100.0, float(DANBOORU_IQDB_MIN_SCORE)))
        _metric(metrics, "iqdb_queries")
        outcome = _attempt_lookup(
            "Danbooru", "iqdb",
            lambda: danbooru_iqdb(
                get_image_payload(), danbooru_login, danbooru_api_key,
                minimum_score, diagnostics=iqdb_diag,
            ),
        )
        outcomes.append(outcome)
        best_score = float(iqdb_diag.get("best_score", 0.0) or 0.0)
        if outcome.matched:
            source, post, match_method = "danbooru", outcome.value, "iqdb"
            decision_details.append(
                f"Danbooru IQDB: match {post.get('_iqdb_score', best_score):.1f}%"
            )
        elif outcome.status == LookupStatus.MISS:
            decision_details.append(
                f"Danbooru IQDB: no qualifying result"
                + (f" (best {best_score:.1f}%)" if best_score else "")
            )
        else:
            decision_details.append(
                f"Danbooru IQDB: {outcome.status.value}"
                + (f" ({outcome.detail})" if outcome.detail else "")
            )
            log("WARNING", f"Danbooru IQDB not authoritative: {outcome.detail}")
    elif post is None and ENABLE_DANBOORU and ENABLE_DANBOORU_IQDB:
        decision_details.append("Danbooru IQDB: skipped (credentials not configured)")
    elif post is None:
        decision_details.append("Danbooru IQDB: disabled")

    review_candidate_score = 0.0
    review_candidate_url = ""
    (
        saucenao_auto_accept,
        saucenao_review_min,
        saucenao_accept_review,
    ) = _saucenao_thresholds(settings)

    if post is None:
        e621_iqdb_diag: Dict[str, Any] = {}
        saucenao_diag: Dict[str, Any] = {}
        visual_outcomes: Dict[str, LookupOutcome] = {}
        visual_jobs: Dict[str, Tuple[str, str, Any]] = {}

        run_e621_iqdb = bool(ENABLE_E621 and ENABLE_E621_IQDB)
        if not run_e621_iqdb:
            decision_details.append("e621 IQDB: disabled")

        run_saucenao = bool(ENABLE_SAUCENAO and saucenao_api_key)
        if ENABLE_SAUCENAO and not saucenao_api_key:
            decision_details.append("SauceNAO: skipped (API key not configured)")
        elif not ENABLE_SAUCENAO:
            decision_details.append("SauceNAO: disabled")
        elif run_saucenao and _saucenao_is_disabled():
            visual_outcomes["saucenao"] = LookupOutcome(
                "SauceNAO", "visual", LookupStatus.UNAVAILABLE, None,
                _SAUCENAO_DISABLED_REASON or "disabled for this run",
            )
            run_saucenao = False

        if run_e621_iqdb or run_saucenao:
            payload = get_image_payload()
            _metric(metrics, "visual_upload_bytes", len(payload))

            if run_e621_iqdb:
                minimum_e621_score = max(
                    0.0, min(100.0, float(E621_IQDB_MIN_SCORE))
                )
                _metric(metrics, "e621_iqdb_queries")
                visual_jobs["e621"] = (
                    "e621",
                    "iqdb",
                    lambda: e621_iqdb(
                        payload,
                        e621_username,
                        e621_api_key,
                        minimum_e621_score,
                        diagnostics=e621_iqdb_diag,
                    ),
                )

            if run_saucenao:
                minimum_similarity = (
                    saucenao_review_min
                    if saucenao_accept_review
                    else saucenao_auto_accept
                )
                _metric(metrics, "saucenao_queries")
                visual_jobs["saucenao"] = (
                    "SauceNAO",
                    "visual",
                    lambda: saucenao_resolve(
                        payload,
                        saucenao_api_key,
                        minimum_similarity,
                        danbooru_login,
                        danbooru_api_key,
                        gelbooru_api_key,
                        gelbooru_user_id,
                        rule34_api_key,
                        rule34_user_id,
                        e621_username,
                        e621_api_key,
                        saucenao_requests_per_30_seconds,
                        diagnostics=saucenao_diag,
                    ),
                )

            visual_started = time.monotonic()
            workers = max(
                1, min(int(DEEP_VISUAL_MAX_WORKERS), len(visual_jobs))
            )
            with ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="booru-deep"
            ) as pool:
                futures = {
                    key: pool.submit(_attempt_lookup, provider, stage, fn)
                    for key, (provider, stage, fn) in visual_jobs.items()
                }
                for key, future in futures.items():
                    visual_outcomes[key] = future.result()
            visual_elapsed = max(0.0, time.monotonic() - visual_started)
            if metrics is not None:
                metrics["visual_search_seconds"] = round(
                    float(metrics.get("visual_search_seconds", 0.0) or 0.0)
                    + visual_elapsed,
                    3,
                )
                metrics["visual_search_images"] = (
                    int(metrics.get("visual_search_images", 0) or 0) + 1
                )

        e621_outcome = visual_outcomes.get("e621")
        if e621_outcome is not None:
            outcomes.append(e621_outcome)
            best_score = float(e621_iqdb_diag.get("best_score", 0.0) or 0.0)
            if e621_outcome.matched:
                source, post, match_method = (
                    "e621", e621_outcome.value, "e621_iqdb"
                )
                decision_details.append(
                    f"e621 IQDB: match "
                    f"{post.get('_e621_iqdb_score', best_score):.1f}%"
                )
            elif e621_outcome.status == LookupStatus.MISS:
                decision_details.append(
                    "e621 IQDB: no qualifying result"
                    + (f" (best {best_score:.1f}%)" if best_score else "")
                )
            else:
                decision_details.append(
                    f"e621 IQDB: {e621_outcome.status.value}"
                    + (f" ({e621_outcome.detail})" if e621_outcome.detail else "")
                )
                _log_lookup_problem_once(
                    "e621 IQDB",
                    str(e621_outcome.detail or e621_outcome.status.value),
                )

        saucenao_outcome = visual_outcomes.get("saucenao")
        if saucenao_outcome is not None:
            outcomes.append(saucenao_outcome)
            best_similarity = float(
                saucenao_diag.get("best_similarity", 0.0) or 0.0
            )
            best_supported_similarity = float(
                saucenao_diag.get("best_supported_similarity", best_similarity)
                or 0.0
            )
            best_supported_url = str(
                saucenao_diag.get("best_supported_url") or ""
            ).strip()
            if saucenao_outcome.matched:
                sauce_source, sauce_post = saucenao_outcome.value
                score = float(
                    sauce_post.get("_saucenao_score", best_similarity) or 0.0
                )
                confidence = _visual_confidence(
                    score, saucenao_auto_accept, saucenao_review_min
                )
                if confidence == "REVIEW":
                    _metric(metrics, "saucenao_review_band_matches")
                if post is None:
                    source, post, match_method = (
                        sauce_source, sauce_post, "saucenao"
                    )
                    decision_details.append(
                        f"SauceNAO: match {score:.1f}% ({confidence})"
                    )
                else:
                    decision_details.append(
                        f"SauceNAO: match {score:.1f}% ({confidence}; "
                        "e621 IQDB retained by priority)"
                    )
            elif saucenao_outcome.status == LookupStatus.MISS:
                if 0 < best_supported_similarity < saucenao_review_min:
                    _metric(metrics, "saucenao_below_threshold")
                elif best_supported_similarity <= 0 < best_similarity:
                    _metric(metrics, "saucenao_unsupported_results")
                elif best_similarity <= 0:
                    _metric(metrics, "saucenao_no_results")
                if (
                    post is None
                    and not saucenao_accept_review
                    and saucenao_review_min
                    <= best_supported_similarity
                    < saucenao_auto_accept
                ):
                    review_candidate_score = best_supported_similarity
                    review_candidate_url = best_supported_url
                    _metric(metrics, "saucenao_review_candidates")
                    decision_details.append(
                        f"SauceNAO: supported review candidate "
                        f"{best_supported_similarity:.1f}% not auto-accepted "
                        f"(HIGH starts at {saucenao_auto_accept:.1f}%)"
                    )
                else:
                    decision_details.append(
                        "SauceNAO: no qualifying supported result"
                        + (
                            f" (best supported {best_supported_similarity:.1f}%)"
                            if best_supported_similarity
                            else (
                                f" (best overall {best_similarity:.1f}%, unsupported)"
                                if best_similarity else ""
                            )
                        )
                    )
            else:
                decision_details.append(
                    f"SauceNAO: {saucenao_outcome.status.value}"
                    + (
                        f" ({saucenao_outcome.detail})"
                        if saucenao_outcome.detail else ""
                    )
                )
                sauce_detail = str(
                    saucenao_outcome.detail or saucenao_outcome.status.value
                )
                # 52x outages are already announced once by _saucenao_note_outage.
                # Keep the per-image Retry Later result at INFO without another warning.
                if not re.search(r"HTTP (?:500|52[0-4])", sauce_detail):
                    _log_lookup_problem_once("SauceNAO", sauce_detail)

    if post is None:
        if review_candidate_score > 0:
            if force_recheck and had_imported_status:
                log(
                    "INFO",
                    f"Image {iid}: REVIEW CANDIDATE on forced recheck; preserving "
                    f"'{IMPORT_MARKER_TAG}' rather than downgrading an accepted import. "
                    f"{_format_stage_decisions(decision_details)}",
                )
                if image_status_names != {IMPORT_MARKER_TAG.casefold()}:
                    transition_image_status(
                        stash, image, IMPORT_MARKER_TAG, tag_cache,
                        normalized_tag_index, similarity_buckets,
                    )
                return "preserved_imported"
            log(
                "INFO",
                f"Image {iid}: REVIEW CANDIDATE; {_format_stage_decisions(decision_details)}; "
                + (
                    f"'{REVIEW_MARKER_TAG}' would be written"
                    + (f" and candidate URL {review_candidate_url} would be appended" if review_candidate_url else "")
                    if dry_run
                    else f"writing '{REVIEW_MARKER_TAG}'"
                    + (f" and appending candidate URL {review_candidate_url}" if review_candidate_url else "")
                ),
            )
            if not dry_run:
                transition_image_status(
                    stash, image, REVIEW_MARKER_TAG, tag_cache,
                    normalized_tag_index, similarity_buckets,
                    extra_url=review_candidate_url or None,
                    review_confidence=review_candidate_score,
                )
            return "review_candidate"

        if can_mark_no_match(outcomes):
            if force_recheck and had_imported_status:
                log(
                    "INFO",
                    f"Image {iid}: forced recheck found no authoritative replacement; "
                    f"preserving '{IMPORT_MARKER_TAG}'. {_format_stage_decisions(decision_details)}",
                )
                if image_status_names != {IMPORT_MARKER_TAG.casefold()}:
                    transition_image_status(
                        stash, image, IMPORT_MARKER_TAG, tag_cache,
                        normalized_tag_index, similarity_buckets,
                    )
                return "preserved_imported"
            if detailed_decisions:
                log(
                    "INFO",
                    f"Image {iid}: NO MATCH (deep pass); "
                    f"{_format_stage_decisions(decision_details)}; "
                    + (
                        f"'{NO_MATCH_MARKER_TAG}' would be written"
                        if dry_run
                        else f"writing '{NO_MATCH_MARKER_TAG}'"
                    ),
                )
            if not dry_run:
                transition_image_status(
                    stash, image, NO_MATCH_MARKER_TAG, tag_cache,
                    normalized_tag_index, similarity_buckets,
                )
            return "no_match"

        if not outcomes:
            if detailed_decisions:
                log(
                    "INFO",
                    f"Image {iid}: UNRESOLVED; {_format_stage_decisions(decision_details)}",
                )
            return "no_md5" if not md5 else "unresolved"
        # Provider-level warnings are emitted once at the point of failure.
        # Per-image Retry Later lines are operational state, not new warnings.
        log(
            "INFO",
            f"Image {iid}: RETRY LATER; no persistent No Match marker was written. "
            f"{_format_stage_decisions(decision_details)}",
        )
        return "retry_later"

    names = tag_names(post, source, include_meta, False)
    artists = artist_names(
        post, source,
        gelbooru_api_key, gelbooru_user_id,
        rule34_api_key, rule34_user_id,
    )
    characters = performer_names(
        post, source,
        gelbooru_api_key, gelbooru_user_id,
        rule34_api_key, rule34_user_id,
    )

    ignored_artist_keys = {
        str(name).strip().casefold() for name in IGNORED_ARTIST_TAGS
    }
    usable_artists = [
        artist for artist in artists
        if str(artist).strip().casefold() not in ignored_artist_keys
    ]
    studio_artists = (
        usable_artists if artist_mapping in {"studios", "both"} else []
    )

    # Gelbooru/Rule34 start with one flat source tag list. If a value is known to
    # be an ignored artist marker, remove it before any mapping policy can turn it
    # back into an ordinary Stash tag.
    if source in {"gelbooru", "rule34"} and artists:
        ignored_source_artist_keys = {
            str(artist).strip().casefold() for artist in artists
            if str(artist).strip().casefold() in ignored_artist_keys
        }
        if ignored_source_artist_keys:
            names = [
                name for name in names
                if str(name).strip().casefold() not in ignored_source_artist_keys
            ]

    # Map structured categories according to user policy.
    if artist_mapping in {"tags", "both"}:
        existing_name_keys = {n.casefold() for n in names}
        for artist in usable_artists:
            if artist.casefold() not in existing_name_keys:
                names.append(artist)
                existing_name_keys.add(artist.casefold())
    elif artist_mapping == "studios":
        if source in {"gelbooru", "rule34"} and artists:
            # Artist-category entries are removed from the flat source tag list.
            # The first usable artist becomes the Studio; later usable artists
            # are re-added below as ordinary image tags.
            artist_keys = {a.casefold() for a in artists}
            names = [n for n in names if n.casefold() not in artist_keys]

        # A Stash image has one Studio slot. Preserve every additional usable
        # artist as an image tag instead of silently dropping that metadata.
        existing_name_keys = {n.casefold() for n in names}
        for artist in usable_artists[1:]:
            if artist.casefold() not in existing_name_keys:
                names.append(artist)
                existing_name_keys.add(artist.casefold())

    if character_mapping in {"tags", "both"}:
        existing_name_keys = {n.casefold() for n in names}
        for character in characters:
            if character.casefold() not in existing_name_keys:
                names.append(character)
                existing_name_keys.add(character.casefold())
    elif source in {"gelbooru", "rule34"} and characters:
        character_keys = {p.casefold() for p in characters}
        names = [n for n in names if n.casefold() not in character_keys]

    performers = characters if character_mapping in {"performers", "both"} else []
    studio_targets = (
        studio_artists if create_secondary_artist_studios else studio_artists[:1]
    )
    desired_studio_name = studio_targets[0] if studio_targets else None

    current_studio = image.get("studio")
    current_date = str(image.get("date") or "").strip()
    original_current_urls = [
        str(u).strip() for u in (image.get("urls") or []) if str(u).strip()
    ]
    current_urls = list(original_current_urls)
    if match_method == "manual_review" and approved_source_url:
        approved_canonical_url, _approved_review_score = _review_candidate_parts(
            approved_source_url
        )
        if approved_canonical_url:
            normalized_urls: List[str] = []
            seen_urls: set[str] = set()
            approved_storage_key = approved_source_url.casefold()
            approved_canonical_key = approved_canonical_url.casefold()
            for existing_url in current_urls:
                candidate = (
                    approved_canonical_url
                    if existing_url.casefold() == approved_storage_key
                    else existing_url
                )
                key = candidate.casefold()
                if key in seen_urls:
                    continue
                seen_urls.add(key)
                normalized_urls.append(candidate)
            if approved_canonical_key not in seen_urls:
                normalized_urls.append(approved_canonical_url)
            current_urls = normalized_urls
    current_url_keys = {u.casefold() for u in current_urls}
    desired_source_url = source_post_url(source, post)
    desired_source_date = source_post_date(source, post)
    existing_performer_ids = {str(p["id"]) for p in image.get("performers") or []}
    existing_ids = {str(tag["id"]) for tag in image.get("tags") or []}

    source_label = {
        "danbooru": "Danbooru",
        "gelbooru": "Gelbooru",
        "rule34": "Rule34",
        "e621": "e621",
    }.get(source, source)

    if dry_run:
        new_names = [
            n for n in names
            if n.casefold() not in tag_cache
            and not (
                merge_similar
                and find_similar_existing_tag(
                    n, tag_cache, normalized_tag_index, similar_threshold,
                    similarity_buckets, tag_similarity_margin,
                )
            )
        ]
        already_names = [
            n for n in names
            if n.casefold() in tag_cache
            and str(tag_cache[n.casefold()]["id"]) in existing_ids
        ]
        if match_method == "iqdb":
            detail = f"IQDB {post.get('_iqdb_score', 0):.1f}%"
        elif match_method == "e621_iqdb":
            detail = f"e621 IQDB {post.get('_e621_iqdb_score', 0):.1f}%"
        elif match_method == "saucenao":
            sauce_score = float(post.get("_saucenao_score", 0) or 0.0)
            sauce_confidence = _visual_confidence(
                sauce_score, saucenao_auto_accept, saucenao_review_min
            )
            detail = f"SauceNAO {sauce_score:.1f}% [{sauce_confidence}]"
        elif match_method == "local_phash":
            detail = (
                f"trusted local pHash distance {int(post.get('_local_phash_distance', 0) or 0)} "
                f"via Stash image {post.get('_local_phash_source_image_id')}"
            )
        elif match_method == "manual_review":
            detail = "manually approved Review candidate"
        else:
            detail = f"exact MD5 {md5}"

        studio_note = ""
        if studio_targets:
            for artist in studio_targets:
                ensure_studio(
                    stash, studio_cache, artist, dry_run=True,
                    merge_normalized=merge_normalized_studios,
                    merge_similar=merge_similar_studios,
                    similarity_threshold=studio_similarity_threshold,
                    similarity_margin=entity_similarity_margin,
                )
            if current_studio:
                studio_note = f"; existing studio '{current_studio.get('name')}' preserved"
            else:
                studio_note = f"; Studio '{studio_targets[0]}' would be assigned"
            if len(studio_targets) > 1:
                studio_note += f"; {len(studio_targets) - 1} secondary Studio record(s) would be ensured"

        performer_note = ""
        if performers:
            for performer_name in performers:
                ensure_performer(
                    stash, performer_cache, performer_name, dry_run=True,
                    merge_normalized=merge_normalized_performers,
                    merge_similar=merge_similar_performers,
                    similarity_threshold=performer_similarity_threshold,
                    similarity_margin=entity_similarity_margin,
                )
            performer_note = f"; {len(performers)} performer(s) would be attached"

        source_meta_note = ""
        if desired_source_date and not current_date:
            source_meta_note += f"; date {desired_source_date} would be set"
        if desired_source_url and desired_source_url.casefold() not in current_url_keys:
            source_meta_note += "; source URL would be appended"

        log(
            "INFO",
            f"DRY RUN image {iid}: {source_label} #{post.get('id')} matched by {detail}; "
            f"{len(names)} tags, {len(new_names)} would be created, "
            f"{len(already_names)} already attached{studio_note}{performer_note}{source_meta_note}",
        )
        if detailed_decisions:
            log(
                "INFO",
                f"Image {iid} decision path: {_format_stage_decisions(decision_details)}",
            )
        if match_method == "saucenao":
            return "matched_saucenao"
        if match_method == "e621_iqdb":
            return "matched_e621_iqdb"
        if match_method == "local_phash":
            return "matched_local_phash"
        return f"matched_{source}"

    performer_ids_to_attach = set(existing_performer_ids)
    for performer_name in performers:
        performer_obj = ensure_performer(
            stash, performer_cache, performer_name, dry_run=False,
            merge_normalized=merge_normalized_performers,
            merge_similar=merge_similar_performers,
            similarity_threshold=performer_similarity_threshold,
            similarity_margin=entity_similarity_margin,
        )
        if performer_obj:
            canonical_id = str(performer_obj["id"])
            duplicate_ids = _equivalent_attached_performer_ids(
                image, performer_cache, performer_name, performer_obj
            )
            if duplicate_ids:
                performer_ids_to_attach.difference_update(duplicate_ids)
                log(
                    "INFO",
                    f"Image {iid}: replaced duplicate performer attachment(s) "
                    f"{', '.join(sorted(duplicate_ids))} with canonical performer "
                    f"'{performer_obj.get('name')}' ({canonical_id})",
                )
            performer_ids_to_attach.add(canonical_id)

    ensured_studios: List[Dict[str, Any]] = []
    for artist in studio_targets:
        studio_obj = ensure_studio(
            stash, studio_cache, artist, dry_run=False,
            merge_normalized=merge_normalized_studios,
            merge_similar=merge_similar_studios,
            similarity_threshold=studio_similarity_threshold,
            similarity_margin=entity_similarity_margin,
        )
        if studio_obj:
            ensured_studios.append(studio_obj)

    studio_id_to_assign: Optional[str] = None
    assigned_studio: Optional[Dict[str, Any]] = None
    if desired_studio_name and not current_studio and ensured_studios:
        assigned_studio = ensured_studios[0]
        studio_id_to_assign = str(assigned_studio["id"])

    imported_ids = ensure_tags(
        stash, tag_cache, names, dry_run=False,
        merge_similar=merge_similar,
        similarity_threshold=similar_threshold,
        similarity_margin=tag_similarity_margin,
        normalized_index=normalized_tag_index,
        similarity_buckets=similarity_buckets,
    )
    clean_existing_ids = _non_status_tag_ids(image)
    marker_id = ensure_import_marker_tag(stash, tag_cache, normalized_tag_index, similarity_buckets)
    merged = list(dict.fromkeys([*clean_existing_ids, *imported_ids, marker_id]))
    target_date = desired_source_date if (desired_source_date and not current_date) else None
    target_urls = current_urls if current_urls != original_current_urls else None
    if desired_source_url and desired_source_url.casefold() not in current_url_keys:
        target_urls = [*current_urls, desired_source_url]

    tags_changed = set(merged) != existing_ids
    studio_changed = studio_id_to_assign is not None
    performers_changed = performer_ids_to_attach != existing_performer_ids
    date_changed = target_date is not None
    urls_changed = target_urls is not None
    result_prefix = "unchanged"

    if tags_changed or studio_changed or performers_changed or date_changed or urls_changed:
        stash.update_image_tags(
            iid, merged,
            studio_id=studio_id_to_assign,
            performer_ids=sorted(performer_ids_to_attach),
            date=target_date,
            urls=target_urls,
        )
        final_studio = assigned_studio or current_studio
        _refresh_image_metadata_for_index(
            image, merged, tag_cache, sorted(performer_ids_to_attach), performer_cache,
            final_studio, target_date or (current_date or None), target_urls or current_urls,
        )
        result_prefix = "updated"
        detail = (
            f"IQDB {post.get('_iqdb_score', 0):.1f}%" if match_method == "iqdb"
            else f"e621 IQDB {post.get('_e621_iqdb_score', 0):.1f}%" if match_method == "e621_iqdb"
            else (
                f"SauceNAO {float(post.get('_saucenao_score', 0) or 0.0):.1f}% "
                f"[{_visual_confidence(float(post.get('_saucenao_score', 0) or 0.0), saucenao_auto_accept, saucenao_review_min)}]"
            ) if match_method == "saucenao"
            else (
                f"trusted local pHash distance {int(post.get('_local_phash_distance', 0) or 0)} "
                f"via Stash image {post.get('_local_phash_source_image_id')}"
            ) if match_method == "local_phash"
            else "manually approved Review candidate" if match_method == "manual_review"
            else "exact MD5"
        )
        log(
            "INFO",
            f"Updated image {iid} from {source_label} #{post.get('id')} ({detail}): "
            f"attached {len(imported_ids)} matched tags"
            + (f"; assigned studio '{assigned_studio.get('name')}'" if assigned_studio else "")
            + (f"; attached {len(performers)} performer(s)" if performers else "")
            + (f"; set date {target_date}" if target_date else "")
            + ("; appended source URL" if target_urls is not None else ""),
        )
    else:
        log("INFO", f"Image {iid} already has all {source_label} #{post.get('id')} metadata")

    if detailed_decisions:
        log(
            "INFO",
            f"Image {iid} decision path: {_format_stage_decisions(decision_details)}",
        )

    phash = image_phash(image)
    if phash:
        phash_index.add(phash, image)

    if match_method == "saucenao":
        return f"{result_prefix}_saucenao"
    if match_method == "e621_iqdb":
        return f"{result_prefix}_e621_iqdb"
    if match_method == "local_phash":
        return f"{result_prefix}_local_phash"
    return f"{result_prefix}_{source}"


def _is_supported_booru_url(url: str) -> bool:
    text = str(url or "").strip()
    return bool(
        re.search(r"danbooru\.donmai\.us/posts/\d+", text)
        or re.search(r"gelbooru\.com/.*[?&]id=\d+", text)
        or re.search(r"rule34\.xxx/.*[?&]id=\d+", text)
        or re.search(r"e621\.net/posts/\d+", text)
    )


def review_candidate_action(
    stash: Stash,
    settings: Dict[str, Any],
    args: Dict[str, Any],
) -> Dict[str, Any]:
    """Accept or reject one Review candidate from the individual Stash image page."""
    image_id = str(args.get("image_id") or "").strip()
    candidate_url = str(args.get("candidate_url") or "").strip()
    candidate_source_url, review_confidence = _review_candidate_parts(candidate_url)
    action = str(args.get("action") or "").strip().casefold()

    if not image_id:
        raise RuntimeError("Review decision is missing image_id")
    if action not in {"yes", "no"}:
        raise RuntimeError("Review decision action must be 'yes' or 'no'")
    if not candidate_url or not _is_supported_booru_url(candidate_source_url):
        raise RuntimeError("Review decision is missing a supported booru candidate URL")

    image = stash.find_image(image_id)
    if not image:
        raise RuntimeError(f"Image {image_id} was not found")
    if REVIEW_MARKER_TAG.casefold() not in _image_status_names(image):
        raise RuntimeError(f"Image {image_id} is not currently marked '{REVIEW_MARKER_TAG}'")

    current_urls = [
        str(url).strip()
        for url in (image.get("urls") or [])
        if str(url).strip()
    ]
    supported_review_urls = [
        url for url in current_urls if _is_supported_booru_url(url)
    ]
    if not supported_review_urls:
        raise RuntimeError("This Review image no longer has a supported booru candidate URL")
    expected_candidate_url = supported_review_urls[-1]
    if candidate_url.casefold() != expected_candidate_url.casefold():
        raise RuntimeError("The proposed Review candidate URL is no longer the active candidate for this image")

    configure_network(settings, reset=True)
    tag_cache = stash.all_tags()
    normalized_index = build_normalized_tag_index(tag_cache)
    similarity_buckets = build_similarity_buckets(normalized_index)

    if action == "no":
        marker_id = ensure_no_match_marker_tag(
            stash, tag_cache, normalized_index, similarity_buckets
        )
        final_tag_ids = list(dict.fromkeys([*_non_status_tag_ids(image), marker_id]))
        remaining_urls = [
            url for url in current_urls
            if url.casefold() != candidate_url.casefold()
        ]
        stash.update_image_tags(
            image_id,
            final_tag_ids,
            urls=remaining_urls,
        )
        log(
            "INFO",
            f"Image {image_id}: Review candidate rejected; "
            f"removed candidate URL and wrote '{NO_MATCH_MARKER_TAG}'.",
        )
        return {
            "status": "rejected",
            "image_id": image_id,
            "candidate_url": candidate_source_url,
            "review_confidence": review_confidence,
            "marker": NO_MATCH_MARKER_TAG,
        }

    studio_cache = stash.all_studios()
    performer_cache = stash.all_performers()
    result = process_image(
        stash,
        image,
        settings,
        tag_cache,
        dry_run=False,
        normalized_tag_index=normalized_index,
        similarity_buckets=similarity_buckets,
        studio_cache=studio_cache,
        performer_cache=performer_cache,
        phash_index=PHashIndex(),
        force_recheck=True,
        lookup_mode="deep",
        approved_source_url=candidate_url,
    )
    if not (
        result.startswith("updated_")
        or result.startswith("unchanged_")
    ):
        raise RuntimeError(
            f"Approved Review candidate did not complete a normal metadata import: {result}"
        )
    log(
        "INFO",
        f"Image {image_id}: Review candidate approved and imported from {candidate_source_url}.",
    )
    return {
        "status": "imported",
        "image_id": image_id,
        "candidate_url": candidate_source_url,
        "review_confidence": review_confidence,
        "result": result,
        "marker": IMPORT_MARKER_TAG,
    }


def configure_network(settings: Dict[str, Any], reset: bool = True) -> None:
    """Apply per-host HTTP pacing/retry/circuit-breaker settings."""
    if reset:
        HTTP.reset()
    HTTP.configure(
        default_min_interval=max(0.0, float(PROVIDER_REQUEST_INTERVAL_MS)) / 1000.0,
        max_retries=max(0, min(5, int(HTTP_MAX_RETRIES))),
        backoff_base=max(0.1, float(HTTP_RETRY_BACKOFF_SECONDS)),
        circuit_threshold=max(2, int(HTTP_CIRCUIT_FAILURE_THRESHOLD)),
        circuit_cooldown=max(10.0, float(HTTP_CIRCUIT_COOLDOWN_SECONDS)),
    )
    # Danbooru asks long-running API clients to stay around one read request/second.
    HTTP.set_host_interval(urllib.parse.urlparse(DANBOORU_BASE).hostname or "danbooru.donmai.us", 1.0)
    HTTP.set_host_rate_limit_cooldown(
        _rule34_host(), max(60.0, float(RULE34_RATE_LIMIT_COOLDOWN_SECONDS))
    )

def _finalize_stats(stats: Dict[str, Any]) -> Dict[str, Any]:
    stats["source_errors_danbooru"] = 1 if "Danbooru" in _SOURCE_DISABLED else 0
    stats["source_errors_gelbooru"] = 1 if "Gelbooru" in _SOURCE_DISABLED else 0
    stats["source_errors_rule34"] = 1 if ("Rule34" in _SOURCE_DISABLED or int(stats.get("rule34_rate_limit_hits", 0) or 0) or int(stats.get("rule34_cooldown_skips", 0) or 0)) else 0
    stats["saucenao_rate_limit_hits"] = _SAUCENAO_RATE_LIMIT_HITS
    stats["saucenao_outage_hits"] = _SAUCENAO_OUTAGE_HITS
    stats["saucenao_quota_pauses"] = _SAUCENAO_QUOTA_PAUSES
    stats["saucenao_daily_quota_exhausted"] = 1 if _SAUCENAO_DAILY_EXHAUSTED else 0
    stats["saucenao_account_type"] = _SAUCENAO_ACCOUNT_TYPE
    stats["saucenao_short_limit"] = _SAUCENAO_SHORT_LIMIT
    stats["saucenao_short_remaining"] = _SAUCENAO_SHORT_REMAINING
    stats["saucenao_long_limit"] = _SAUCENAO_LONG_LIMIT
    stats["saucenao_long_remaining"] = _SAUCENAO_LONG_REMAINING
    requested_sauce_rate = float(stats.get("saucenao_requested_requests_per_30_seconds", 0.0) or 0.0)
    stats["saucenao_effective_requests_per_30_seconds"] = round(_saucenao_effective_rate(requested_sauce_rate), 3)
    stats["e621_iqdb_rate_limit_hits"] = _E621_IQDB_RATE_LIMIT_HITS
    started = float(stats.pop("_started_at_monotonic", time.monotonic()))
    elapsed = max(0.0, time.monotonic() - started)
    stats["elapsed_seconds"] = round(elapsed, 2)
    seen = int(stats.get("seen", 0) or 0)
    stats["average_seconds_per_image"] = round(elapsed / seen, 2) if seen else 0.0
    stats["images_per_minute"] = round((seen * 60.0) / elapsed, 2) if elapsed > 0 and seen else 0.0
    visual_images = int(stats.get("visual_search_images", 0) or 0)
    visual_seconds = float(stats.get("visual_search_seconds", 0.0) or 0.0)
    upload_bytes = int(stats.get("visual_upload_bytes", 0) or 0)
    stats["average_visual_seconds_per_image"] = (
        round(visual_seconds / visual_images, 2) if visual_images else 0.0
    )
    stats["average_visual_upload_kb"] = (
        round((upload_bytes / 1024.0) / visual_images, 1)
        if visual_images else 0.0
    )
    progress(1.0)
    return stats


def _record_result(stats: Dict[str, Any], result: str) -> None:
    direct_keys = {
        "skipped_imported", "skipped_unresolved", "skipped_review",
        "skipped_no_match", "skipped_extension", "preserved_imported",
        "no_match", "no_md5", "retry_later", "unresolved", "review_candidate",
    }
    if result in direct_keys:
        stats[result] += 1
        return
    if result.startswith("matched_"):
        source = result.split("_", 1)[1]
        stats["matched"] += 1
        key = f"matched_{source}"
        if key in stats:
            stats[key] += 1
        if source == "e621_iqdb":
            stats["matched_e621"] += 1
        return
    if result.startswith("updated_"):
        source = result.split("_", 1)[1]
        stats["matched"] += 1
        stats["updated"] += 1
        key = f"matched_{source}"
        if key in stats:
            stats[key] += 1
        if source == "e621_iqdb":
            stats["matched_e621"] += 1
        return
    if result.startswith("unchanged_"):
        source = result.split("_", 1)[1]
        stats["matched"] += 1
        stats["unchanged"] += 1
        key = f"matched_{source}"
        if key in stats:
            stats[key] += 1
        if source == "e621_iqdb":
            stats["matched_e621"] += 1


def import_all(stash: Stash, settings: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
    global _E621_IQDB_RATE_LIMIT_HITS
    global _SAUCENAO_RATE_LIMIT_HITS, _SAUCENAO_DAILY_EXHAUSTED, _SAUCENAO_DISABLED_REASON
    global _SAUCENAO_ACCOUNT_TYPE, _SAUCENAO_SHORT_LIMIT, _SAUCENAO_SHORT_REMAINING
    global _SAUCENAO_LONG_LIMIT, _SAUCENAO_LONG_REMAINING, _SAUCENAO_LAST_REQUEST_AT
    global _SAUCENAO_PAUSE_UNTIL, _SAUCENAO_QUOTA_PAUSES, _SAUCENAO_QUOTA_ANNOUNCED
    global _SAUCENAO_OUTAGE_HITS, _SAUCENAO_OUTAGE_STREAK

    _E621_IQDB_RATE_LIMIT_HITS = 0
    _SAUCENAO_RATE_LIMIT_HITS = 0
    _SAUCENAO_DAILY_EXHAUSTED = False
    _SAUCENAO_DISABLED_REASON = ""
    _SAUCENAO_ACCOUNT_TYPE = ""
    _SAUCENAO_SHORT_LIMIT = 0
    _SAUCENAO_SHORT_REMAINING = -1
    _SAUCENAO_LONG_LIMIT = 0
    _SAUCENAO_LONG_REMAINING = -1
    _SAUCENAO_LAST_REQUEST_AT = 0.0
    _SAUCENAO_PAUSE_UNTIL = 0.0
    _SAUCENAO_QUOTA_PAUSES = 0
    _SAUCENAO_QUOTA_ANNOUNCED = False
    _SAUCENAO_OUTAGE_HITS = 0
    _SAUCENAO_OUTAGE_STREAK = 0
    _RULE34_COOLDOWN_ANNOUNCED = False
    _PROVIDER_WARNING_ONCE.clear()
    _SOURCE_DISABLED.clear()
    _TYPED_TAG_CACHE.clear()
    configure_network(settings, reset=True)

    dry_run = as_bool(args.get("dry_run"), False)
    force_recheck = as_bool(args.get("force_recheck"), False)
    only_no_match = as_bool(args.get("only_no_match"), False)
    only_review = as_bool(args.get("only_review"), False)
    lookup_mode = str(args.get("lookup_mode") or "fast").strip().casefold()
    if lookup_mode not in {"fast", "deep"}:
        lookup_mode = "fast"
    if only_no_match or only_review:
        lookup_mode = "deep"
    requested_limit = as_int(args.get("limit"), -1)
    configured_limit = max(0, int(MAX_IMAGES_PER_RUN))
    limits = [x for x in (requested_limit, configured_limit) if x > 0]
    limit = min(limits) if limits else 0
    delay = max(0, int(REQUEST_DELAY_MS)) / 1000.0

    tag_cache = stash.all_tags()
    studio_cache = stash.all_studios()
    performer_cache = stash.all_performers()

    phash_index = PHashIndex()
    if ENABLE_LOCAL_PHASH_REUSE:
        marker = tag_cache.get(IMPORT_MARKER_TAG.casefold())
        if marker:
            imported_images = stash.imported_images_for_phash_reuse(str(marker["id"]))
            phash_index = build_imported_phash_index(imported_images)
            log("INFO", f"Loaded {len(phash_index)} imported image pHash entries")

    normalized_tag_index = build_normalized_tag_index(tag_cache)
    similarity_buckets = build_similarity_buckets(normalized_tag_index)
    stats: Dict[str, Any] = {
        "_started_at_monotonic": time.monotonic(),
        "lookup_mode": lookup_mode,
        "scanned": 0,
        "seen": 0,
        "matched": 0,
        "matched_danbooru": 0,
        "matched_gelbooru": 0,
        "matched_rule34": 0,
        "matched_e621": 0,
        "matched_e621_iqdb": 0,
        "e621_iqdb_rate_limit_hits": 0,
        "matched_saucenao": 0,
        "matched_local_phash": 0,
        "updated": 0,
        "unchanged": 0,
        "no_match": 0,
        "unresolved": 0,
        "review_candidate": 0,
        "retry_later": 0,
        "no_md5": 0,
        "skipped_imported": 0,
        "skipped_unresolved": 0,
        "skipped_review": 0,
        "skipped_no_match": 0,
        "preserved_imported": 0,
        "skipped_extension": 0,
        "errors": 0,
        "source_errors_danbooru": 0,
        "source_errors_gelbooru": 0,
        "source_errors_rule34": 0,
        "rule34_rate_limit_hits": 0,
        "rule34_cooldown_activations": 0,
        "rule34_cooldown_skips": 0,
        "pending_rule34_confirmation": 0,
        "saucenao_rate_limit_hits": 0,
        "saucenao_outage_hits": 0,
        "saucenao_quota_pauses": 0,
        "saucenao_daily_quota_exhausted": 0,
        "saucenao_account_type": "",
        "saucenao_short_limit": 0,
        "saucenao_short_remaining": -1,
        "saucenao_long_limit": 0,
        "saucenao_long_remaining": -1,
        "saucenao_requested_requests_per_30_seconds": _saucenao_requested_rate(settings),
        "saucenao_effective_requests_per_30_seconds": 0.0,
        "md5_queries": 0,
        "phash_queries": 0,
        "iqdb_queries": 0,
        "e621_iqdb_queries": 0,
        "saucenao_queries": 0,
        "saucenao_below_threshold": 0,
        "saucenao_unsupported_results": 0,
        "saucenao_no_results": 0,
        "saucenao_review_candidates": 0,
        "saucenao_review_band_matches": 0,
        "fast_stages_reused": 0,
        "visual_search_images": 0,
        "visual_search_seconds": 0.0,
        "visual_upload_bytes": 0,
    }

    target_marker_name: Optional[str] = None
    target_label = ""
    if only_no_match:
        target_marker_name = NO_MATCH_MARKER_TAG
        target_label = "no-match recheck"
    elif only_review:
        target_marker_name = REVIEW_MARKER_TAG
        target_label = "review recheck"
    elif lookup_mode == "deep" and not force_recheck:
        target_marker_name = UNRESOLVED_MARKER_TAG
        target_label = "unresolved Deep Match"

    if target_marker_name:
        marker = tag_cache.get(target_marker_name.casefold())
        if not marker:
            log(
                "INFO",
                f"No '{target_marker_name}' tag exists; there are no images for {target_label}",
            )
            return _finalize_stats(stats)
        images = stash.all_images_with_tag(str(marker["id"]), limit=limit)
        total = len(images)
        log(
            "INFO",
            f"Starting {'dry run ' if dry_run else ''}{target_label} for {total} image(s) "
            f"tagged '{target_marker_name}'",
        )
        batches = [images]
    else:
        batches = None
        total = None

    page = 1
    while True:
        if batches is not None:
            images = batches[0] if page == 1 else []
            count = len(images)
        else:
            per_page = min(100, limit) if limit else 100
            count, images = stash.find_images(page, per_page)
            if total is None:
                total = count
                target = min(count, limit) if limit else count
                log(
                    "INFO",
                    f"Starting {'dry run' if dry_run else 'import'} [{lookup_mode.upper()}]"
                    f"{' (force recheck)' if force_recheck else ''} for up to {target} eligible Stash images"
                    f" (scanning a library of {count})",
                )

        if not images:
            break

        for image in images:
            if limit and stats["seen"] >= limit:
                return _finalize_stats(stats)

            stats["scanned"] += 1
            if batches is None and not force_recheck:
                marker_names = _image_status_names(image)
                skip_key = None
                if IMPORT_MARKER_TAG.casefold() in marker_names:
                    skip_key = "skipped_imported"
                elif NO_MATCH_MARKER_TAG.casefold() in marker_names:
                    skip_key = "skipped_no_match"
                elif REVIEW_MARKER_TAG.casefold() in marker_names:
                    skip_key = "skipped_review"
                elif UNRESOLVED_MARKER_TAG.casefold() in marker_names:
                    skip_key = "skipped_unresolved"
                if skip_key:
                    stats[skip_key] += 1
                    if total:
                        progress(stats["scanned"] / total)
                    continue

            stats["seen"] += 1
            try:
                result = process_image(
                    stash, image, settings, tag_cache, dry_run,
                    normalized_tag_index, similarity_buckets,
                    studio_cache, performer_cache, phash_index,
                    force_recheck=force_recheck or only_no_match or only_review,
                    lookup_mode=lookup_mode,
                    metrics=stats,
                )
                _record_result(stats, result)
            except Exception as exc:
                stats["errors"] += 1
                log("ERROR", f"Image {image.get('id')}: {exc}")

            if total:
                progress(stats["scanned"] / total)
            if delay:
                time.sleep(delay)

        if batches is not None:
            break
        if page * per_page >= count:
            break
        page += 1

    return _finalize_stats(stats)


def extract_hook_image_id(args: Any) -> Optional[str]:
    """Best-effort extraction across Stash hook payload variants."""
    wanted_keys = {"image_id", "imageid"}
    def walk(obj: Any) -> Optional[str]:
        if isinstance(obj, dict):
            for key, value in obj.items():
                norm = str(key).replace("-", "_").casefold()
                if norm in wanted_keys and value is not None:
                    return str(value)
            # A hook context entity object may be directly identified as Image.
            typ = str(obj.get("type") or obj.get("Type") or "").casefold()
            if typ == "image" and obj.get("id") is not None:
                return str(obj["id"])
            for value in obj.values():
                found = walk(value)
                if found:
                    return found
        elif isinstance(obj, list):
            for value in obj:
                found = walk(value)
                if found:
                    return found
        return None
    return walk(args)


def run_hook(stash: Stash, settings: Dict[str, Any], args: Dict[str, Any]) -> None:
    if not AUTO_IMPORT_NEW_IMAGES:
        return
    configure_network(settings, reset=True)
    image_id = extract_hook_image_id(args)
    if not image_id:
        log("WARNING", "Image.Create.Post fired but no image id was found in the hook payload; use the bulk task for this image")
        return
    image = stash.find_image(image_id)
    if not image:
        log("WARNING", f"New image {image_id} could not be queried")
        return
    cache = stash.all_tags()
    studio_cache = stash.all_studios()
    performer_cache = stash.all_performers()

    phash_index = PHashIndex()
    if ENABLE_LOCAL_PHASH_REUSE:
        marker = cache.get(IMPORT_MARKER_TAG.casefold())
        if marker:
            imported_images = stash.imported_images_for_phash_reuse(str(marker["id"]))
            phash_index = build_imported_phash_index(imported_images)

    normalized_index = build_normalized_tag_index(cache)
    similarity_buckets = build_similarity_buckets(normalized_index)
    try:
        result = process_image(
            stash, image, settings, cache, dry_run=False,
            normalized_tag_index=normalized_index,
            similarity_buckets=similarity_buckets,
            studio_cache=studio_cache,
            performer_cache=performer_cache,
            phash_index=phash_index,
            force_recheck=False,
            lookup_mode="fast",
        )
        log("INFO", f"Auto-import image {image_id}: {result}")
    except Exception as exc:
        log("ERROR", f"Auto-import image {image_id} failed: {exc}")


def main() -> None:
    payload = read_input()
    conn = payload.get("server_connection") or {}
    args = payload.get("args") or {}
    stash = Stash(conn)
    settings = stash.settings()

    # Never log credentials or the settings object because it may contain the Danbooru API key.
    mode = str(args.get("mode") or "").strip()
    if mode == "import_all":
        stats = import_all(stash, settings, args)
        log("INFO", f"Finished: {stats}")
        print(json.dumps({"output": "ok", "stats": stats}))
        return
    if mode == "review_decision":
        result = review_candidate_action(stash, settings, args)
        print(json.dumps({"output": "ok", **result}))
        return
    # Hook context may be outside args in Stash v0.31.x.
    run_hook(stash, settings, payload)
    print(json.dumps({"output": "ok"}))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("ERROR", str(exc))
        print(json.dumps({"output": "error", "error": str(exc)}))
        sys.exit(1)
