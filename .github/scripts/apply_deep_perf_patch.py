from pathlib import Path

ROOT = Path("plugins/DanbooruTagImporter")
PLUGIN = ROOT / "DanbooruTagImporter.py"
CONSTANTS = ROOT / "constants.py"
NETWORK = ROOT / "network.py"
STASH = ROOT / "stash_client.py"
MANIFEST = ROOT / "DanbooruTagImporter.yml"
README = ROOT / "README.md"
TASK_TEST = ROOT / "tests/test_task_menu.py"
PERF_TEST = ROOT / "tests/test_deep_performance.py"


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if new in text:
        return text
    if old not in text:
        raise SystemExit(f"{label}: expected source anchor not found")
    return text.replace(old, new, 1)


# --- Main plugin ---
text = PLUGIN.read_text(encoding="utf-8")

text = replace_once(
    text,
    "from datetime import datetime, timezone\n",
    "from concurrent.futures import ThreadPoolExecutor\nfrom datetime import datetime, timezone\n",
    "ThreadPoolExecutor import",
)

text = replace_once(
    text,
    "    CREATE_SECONDARY_ARTIST_STUDIOS, DANBOORU_BASE, DANBOORU_IQDB_MIN_SCORE,\n",
    "    CREATE_SECONDARY_ARTIST_STUDIOS, DANBOORU_BASE, DANBOORU_IQDB_MIN_SCORE,\n"
    "    DEEP_VISUAL_HTTP_RETRIES, DEEP_VISUAL_MAX_WORKERS, DEEP_VISUAL_TIMEOUT_SECONDS,\n",
    "Deep visual constants import",
)

text = replace_once(
    text,
    "    SAUCENAO_QUOTA_WINDOW_SECONDS, SAUCENAO_RATE_LIMIT_FALLBACK_SECONDS,\n",
    "    SAUCENAO_OUTAGE_COOLDOWN_SECONDS, SAUCENAO_QUOTA_WINDOW_SECONDS,\n"
    "    SAUCENAO_RATE_LIMIT_FALLBACK_SECONDS,\n",
    "SauceNAO outage constant import",
)

text = replace_once(
    text,
    "_SAUCENAO_QUOTA_ANNOUNCED = False\n",
    "_SAUCENAO_QUOTA_ANNOUNCED = False\n_SAUCENAO_OUTAGE_UNTIL = 0.0\n",
    "SauceNAO outage global",
)

old_disabled = '''def _saucenao_is_disabled() -> bool:
    return _SAUCENAO_DAILY_EXHAUSTED
'''
new_disabled = '''def _saucenao_is_disabled() -> bool:
    global _SAUCENAO_OUTAGE_UNTIL, _SAUCENAO_DISABLED_REASON
    if _SAUCENAO_DAILY_EXHAUSTED:
        return True
    now = time.monotonic()
    if _SAUCENAO_OUTAGE_UNTIL > now:
        return True
    if _SAUCENAO_OUTAGE_UNTIL:
        _SAUCENAO_OUTAGE_UNTIL = 0.0
        if _SAUCENAO_DISABLED_REASON.startswith("temporary SauceNAO outage"):
            _SAUCENAO_DISABLED_REASON = ""
    return False


def _saucenao_note_outage(status_code: int) -> None:
    global _SAUCENAO_OUTAGE_UNTIL, _SAUCENAO_DISABLED_REASON
    cooldown = max(30.0, float(SAUCENAO_OUTAGE_COOLDOWN_SECONDS))
    _SAUCENAO_OUTAGE_UNTIL = max(
        _SAUCENAO_OUTAGE_UNTIL, time.monotonic() + cooldown
    )
    _SAUCENAO_DISABLED_REASON = (
        f"temporary SauceNAO outage cooldown after HTTP {status_code} "
        f"({cooldown:.0f}s)"
    )
    log(
        "WARNING",
        f"SauceNAO HTTP {status_code}; pausing SauceNAO lookups for {cooldown:.0f}s "
        "while Deep Match continues with the other providers.",
    )
'''
text = replace_once(text, old_disabled, new_disabled, "SauceNAO disabled helper")

# Visual endpoints: one bounded attempt, no inline retries.
text = text.replace(
    "with HTTP.urlopen(req, timeout=60) as resp:",
    "with HTTP.urlopen(\n"
    "            req,\n"
    "            timeout=DEEP_VISUAL_TIMEOUT_SECONDS,\n"
    "            retries=DEEP_VISUAL_HTTP_RETRIES,\n"
    "        ) as resp:",
)
text = text.replace(
    "with HTTP.urlopen(req, timeout=60, min_interval=0.0) as resp:",
    "with HTTP.urlopen(\n"
    "            req,\n"
    "            timeout=DEEP_VISUAL_TIMEOUT_SECONDS,\n"
    "            min_interval=0.0,\n"
    "            retries=DEEP_VISUAL_HTTP_RETRIES,\n"
    "        ) as resp:",
)

old_sauce_http = '''        if exc.code == 429:
            retry_after_raw = exc.headers.get("Retry-After") if exc.headers else None
'''
new_sauce_http = '''        if exc.code in {520, 521, 522, 523, 524}:
            _saucenao_note_outage(exc.code)
            raise RuntimeError(
                f"SauceNAO temporarily unavailable: HTTP {exc.code}"
            ) from exc
        if exc.code == 429:
            retry_after_raw = exc.headers.get("Retry-After") if exc.headers else None
'''
text = replace_once(text, old_sauce_http, new_sauce_http, "SauceNAO 52x handling")

status_anchor = "    had_imported_status = IMPORT_MARKER_TAG.casefold() in image_status_names\n\n"
skip_code = '''    skip_fast_stages = (
        lookup_mode == "deep"
        and not force_recheck
        and UNRESOLVED_MARKER_TAG.casefold() in image_status_names
    )

'''
text = replace_once(text, status_anchor, status_anchor + skip_code, "Deep cached-fast policy")

fast_start_marker = "    # Authoritative exact hashes always win over perceptual reuse.\n"
fast_end_marker = "    # Fast pass deliberately stops before any image upload / reverse-image-search API.\n"
fast_start = text.find(fast_start_marker)
fast_end = text.find(fast_end_marker, fast_start)
if fast_start < 0 or fast_end < 0:
    raise SystemExit("Fast-stage block markers not found")
fast_block = text[fast_start:fast_end]
if "Fast MD5/pHash stages: skipped (already Unresolved)" not in fast_block:
    indented = "".join(
        ("    " + line if line.strip() else line)
        for line in fast_block.splitlines(keepends=True)
    )
    replacement = (
        '    if skip_fast_stages:\n'
        '        decision_details.append("Fast MD5/pHash stages: skipped (already Unresolved)")\n'
        '        _metric(metrics, "fast_stages_reused")\n'
        '    else:\n'
        + indented
    )
    text = text[:fast_start] + replacement + text[fast_end:]

# Keep Danbooru IQDB first. If it misses, run e621 IQDB and SauceNAO concurrently.
deep_start_marker = '''    if (
        post is None
        and ENABLE_E621
        and ENABLE_E621_IQDB
    ):
'''
deep_end_marker = '''    if post is None:
        if review_candidate_score > 0:
'''
deep_start = text.find(deep_start_marker)
deep_end = text.find(deep_end_marker, deep_start)
if deep_start < 0 or deep_end < 0:
    raise SystemExit("e621/SauceNAO visual block markers not found")

new_deep = '''    review_candidate_score = 0.0
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
        if run_e621_iqdb and _e621_iqdb_is_disabled():
            visual_outcomes["e621"] = LookupOutcome(
                "e621", "iqdb", LookupStatus.UNAVAILABLE, None,
                "e621 IQDB disabled for this run after a rate-limit/challenge response",
            )
            run_e621_iqdb = False
        elif not run_e621_iqdb:
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
                log(
                    "WARNING",
                    f"e621 IQDB not authoritative: {e621_outcome.detail}",
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
                log(
                    "WARNING",
                    f"SauceNAO not authoritative: {saucenao_outcome.detail}",
                )

'''
text = text[:deep_start] + new_deep + text[deep_end:]

# Add run-level metrics.
stats_anchor = '        "saucenao_review_band_matches": 0,\n'
stats_extra = (
    '        "fast_stages_reused": 0,\n'
    '        "visual_search_images": 0,\n'
    '        "visual_search_seconds": 0.0,\n'
    '        "visual_upload_bytes": 0,\n'
)
if stats_extra not in text:
    if stats_anchor not in text:
        raise SystemExit("stats anchor not found")
    text = text.replace(stats_anchor, stats_anchor + stats_extra, 1)

finalize_anchor = (
    '    stats["images_per_minute"] = round((seen * 60.0) / elapsed, 2) '
    'if elapsed > 0 and seen else 0.0\n'
)
finalize_extra = '''    visual_images = int(stats.get("visual_search_images", 0) or 0)
    visual_seconds = float(stats.get("visual_search_seconds", 0.0) or 0.0)
    upload_bytes = int(stats.get("visual_upload_bytes", 0) or 0)
    stats["average_visual_seconds_per_image"] = (
        round(visual_seconds / visual_images, 2) if visual_images else 0.0
    )
    stats["average_visual_upload_kb"] = (
        round((upload_bytes / 1024.0) / visual_images, 1)
        if visual_images else 0.0
    )
'''
if finalize_extra not in text:
    if finalize_anchor not in text:
        raise SystemExit("finalize metrics anchor not found")
    text = text.replace(finalize_anchor, finalize_anchor + finalize_extra, 1)

# Reset SauceNAO outage state per bulk run.
global_line = (
    '    global _SAUCENAO_PAUSE_UNTIL, _SAUCENAO_QUOTA_PAUSES, '
    '_SAUCENAO_QUOTA_ANNOUNCED\n'
)
if '    global _SAUCENAO_OUTAGE_UNTIL\n' not in text:
    if global_line not in text:
        raise SystemExit("import_all SauceNAO global anchor not found")
    text = text.replace(
        global_line,
        global_line + '    global _SAUCENAO_OUTAGE_UNTIL\n',
        1,
    )

import_all_pos = text.find("def import_all(")
reset_anchor = "    _SAUCENAO_QUOTA_ANNOUNCED = False\n"
reset_pos = text.find(reset_anchor, import_all_pos)
if reset_pos < 0:
    raise SystemExit("import_all SauceNAO reset anchor not found")
if text.find("    _SAUCENAO_OUTAGE_UNTIL = 0.0\n", reset_pos, reset_pos + 200) < 0:
    insert_at = reset_pos + len(reset_anchor)
    text = (
        text[:insert_at]
        + "    _SAUCENAO_OUTAGE_UNTIL = 0.0\n"
        + text[insert_at:]
    )

PLUGIN.write_text(text, encoding="utf-8")

# --- Stash client: use cached/generated 640px thumbnail for reverse-search upload ---
stash_text = STASH.read_text(encoding="utf-8")
old_image_bytes = '''    def image_bytes(self, image_id: str) -> bytes:
        """Read the existing Stash image in memory. No file is written."""
        headers = {k: v for k, v in self.headers.items() if k.lower() != "content-type"}
        req = urllib.request.Request(
            f"{self.base_url}/image/{urllib.parse.quote(str(image_id))}/image",
            headers=headers,
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Stash image HTTP {exc.code}: {detail[:300]}") from exc
'''
new_image_bytes = '''    def image_bytes(self, image_id: str) -> bytes:
        """Read Stash's 640px thumbnail for reverse-image search.

        Stash generates/caches this representation locally. Sending the thumbnail
        instead of the original full-resolution file dramatically reduces upload
        time while preserving enough visual detail for IQDB/SauceNAO matching.
        No duplicate source image is written by this plugin.
        """
        headers = {k: v for k, v in self.headers.items() if k.lower() != "content-type"}
        req = urllib.request.Request(
            f"{self.base_url}/image/{urllib.parse.quote(str(image_id))}/thumbnail",
            headers=headers,
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Stash thumbnail HTTP {exc.code}: {detail[:300]}"
            ) from exc
'''
stash_text = replace_once(
    stash_text, old_image_bytes, new_image_bytes, "Stash thumbnail payload"
)
STASH.write_text(stash_text, encoding="utf-8")

# --- Constants/version ---
constants = CONSTANTS.read_text(encoding="utf-8")
constants = replace_once(
    constants, 'VERSION = "3.26.3"', 'VERSION = "3.26.4"', "Version bump"
)
perf_policy = '''# Deep visual-search requests upload a Stash-generated 640px thumbnail.
# Do not retry an expensive image upload inline; transient failures stay Retry Later.
DEEP_VISUAL_TIMEOUT_SECONDS = 30.0
DEEP_VISUAL_HTTP_RETRIES = 0
DEEP_VISUAL_MAX_WORKERS = 2
'''
if "DEEP_VISUAL_TIMEOUT_SECONDS" not in constants:
    constants = replace_once(
        constants,
        "ENABLE_SAUCENAO = True\n",
        "ENABLE_SAUCENAO = True\n" + perf_policy,
        "Deep visual policy constants",
    )
if "SAUCENAO_OUTAGE_COOLDOWN_SECONDS" not in constants:
    constants = replace_once(
        constants,
        "SAUCENAO_RATE_LIMIT_FALLBACK_SECONDS = 30.0\n",
        "SAUCENAO_RATE_LIMIT_FALLBACK_SECONDS = 30.0\n"
        "SAUCENAO_OUTAGE_COOLDOWN_SECONDS = 180.0\n",
        "SauceNAO outage cooldown constant",
    )
CONSTANTS.write_text(constants, encoding="utf-8")

# --- Retry classification ---
network = NETWORK.read_text(encoding="utf-8")
network = replace_once(
    network,
    "RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}",
    "RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}",
    "Cloudflare retryable status set",
)
NETWORK.write_text(network, encoding="utf-8")

# --- Manifest / docs ---
manifest = MANIFEST.read_text(encoding="utf-8")
manifest = replace_once(
    manifest, "version: 3.26.3", "version: 3.26.4", "Manifest version"
)
old_desc = (
    "description: Two-step image metadata importer for Danbooru, Gelbooru, Rule34, "
    "and e621. The first usable artist becomes the Studio and additional artists "
    "are retained as tags. Run Fast Scan first, then Deep Match unresolved images."
)
new_desc = (
    "description: Two-step image metadata importer for Danbooru, Gelbooru, Rule34, "
    "and e621. Fast Scan handles exact matches; Deep Match reuses that result and "
    "uses bandwidth-efficient visual search for unresolved images."
)
manifest = replace_once(manifest, old_desc, new_desc, "Manifest description")
MANIFEST.write_text(manifest, encoding="utf-8")

readme = README.read_text(encoding="utf-8")
readme = replace_once(
    readme, "# Booru Importer v3.26.3", "# Booru Importer v3.26.4", "README version"
)
goal_start = "v3.26.3 expands the artist-exclusion policy:"
if goal_start in readme:
    paragraph_start = readme.index(goal_start)
    paragraph_end = readme.find("\n\n", paragraph_start)
    if paragraph_end < 0:
        paragraph_end = len(readme)
    new_para = (
        "v3.26.4 substantially improves Deep Match throughput. Images already marked "
        "Unresolved reuse their completed Fast Scan result instead of repeating MD5 "
        "and local pHash lookups. Reverse-image searches upload Stash's generated "
        "640px thumbnail instead of the original full-resolution image. After Danbooru "
        "IQDB misses, e621 IQDB and SauceNAO run concurrently, with a 30-second "
        "single-attempt timeout so temporary upload/network failures remain Retry Later "
        "instead of blocking one image for minutes. SauceNAO Cloudflare/origin 52x "
        "errors enter a short provider-only cooldown. Match thresholds and artist "
        "mapping behavior are unchanged."
    )
    readme = readme[:paragraph_start] + new_para + readme[paragraph_end:]
README.write_text(readme, encoding="utf-8")

task_test = TASK_TEST.read_text(encoding="utf-8")
task_test = replace_once(
    task_test,
    'self.assertEqual(yaml_version, "3.26.3")',
    'self.assertEqual(yaml_version, "3.26.4")',
    "Task-menu version assertion",
)
TASK_TEST.write_text(task_test, encoding="utf-8")

PERF_TEST.write_text(
    '''import importlib.util
import pathlib
import sys
import threading
import unittest
import urllib.error
from unittest import mock

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

spec = importlib.util.spec_from_file_location(
    "plugin_perf", PLUGIN_DIR / "DanbooruTagImporter.py"
)
plugin = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(plugin)

from stash_client import Stash


class FakeResponse:
    def __init__(self, body=b"thumb"):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self.body


class FakeStash:
    def image_bytes(self, image_id):
        return b"small-thumbnail"

    def update_image_tags(self, *args, **kwargs):
        pass

    def create_tag(self, name):
        return {"id": "marker", "name": name}


def unresolved_image():
    return {
        "id": "11",
        "tags": [{"id": "u", "name": plugin.UNRESOLVED_MARKER_TAG}],
        "performers": [],
        "studio": None,
        "date": "",
        "urls": [],
        "files": [
            {
                "fingerprints": [
                    {"type": "md5", "value": "a" * 32},
                    {"type": "phash", "value": "0123456789abcdef"},
                ]
            }
        ],
    }


class DeepPerformanceTests(unittest.TestCase):
    def test_unresolved_deep_skips_completed_fast_stages(self):
        stash = FakeStash()
        with mock.patch.object(plugin, "ENABLE_DANBOORU_IQDB", False), \
             mock.patch.object(plugin, "ENABLE_E621_IQDB", False), \
             mock.patch.object(plugin, "ENABLE_SAUCENAO", False), \
             mock.patch.object(plugin, "danbooru_post") as danbooru, \
             mock.patch.object(plugin, "gelbooru_post") as gelbooru, \
             mock.patch.object(plugin, "rule34_post") as rule34, \
             mock.patch.object(plugin, "e621_post") as e621, \
             mock.patch.object(plugin, "_try_local_phash_reuse") as phash:
            result = plugin.process_image(
                stash,
                unresolved_image(),
                {},
                {},
                True,
                {},
                {},
                {},
                {},
                plugin.PHashIndex(),
                lookup_mode="deep",
            )
        self.assertIn(result, {"unresolved", "no_md5"})
        danbooru.assert_not_called()
        gelbooru.assert_not_called()
        rule34.assert_not_called()
        e621.assert_not_called()
        phash.assert_not_called()

    def test_e621_and_saucenao_run_concurrently_after_danbooru_miss(self):
        stash = FakeStash()
        barrier = threading.Barrier(2, timeout=2.0)

        def miss_together(*args, **kwargs):
            barrier.wait()
            return None

        settings = {"saucenao_api_key": "sauce"}
        with mock.patch.object(plugin, "ENABLE_DANBOORU_IQDB", False), \
             mock.patch.object(plugin, "e621_iqdb", side_effect=miss_together), \
             mock.patch.object(plugin, "saucenao_resolve", side_effect=miss_together):
            result = plugin.process_image(
                stash,
                unresolved_image(),
                settings,
                {},
                True,
                {},
                {},
                {},
                {},
                plugin.PHashIndex(),
                lookup_mode="deep",
            )
        self.assertEqual(result, "no_match")

    def test_visual_timeout_is_retryable_and_not_no_match(self):
        stash = FakeStash()
        settings = {"saucenao_api_key": "sauce"}

        def timeout(*args, **kwargs):
            raise urllib.error.URLError(TimeoutError("The write operation timed out"))

        with mock.patch.object(plugin, "ENABLE_DANBOORU_IQDB", False), \
             mock.patch.object(plugin, "e621_iqdb", side_effect=timeout), \
             mock.patch.object(plugin, "saucenao_resolve", side_effect=timeout):
            result = plugin.process_image(
                stash,
                unresolved_image(),
                settings,
                {},
                True,
                {},
                {},
                {},
                {},
                plugin.PHashIndex(),
                lookup_mode="deep",
            )
        self.assertEqual(result, "retry_later")

    def test_stash_image_bytes_uses_thumbnail_endpoint(self):
        stash = Stash({"Scheme": "http", "Host": "127.0.0.1", "Port": 9999})
        with mock.patch(
            "stash_client.urllib.request.urlopen",
            return_value=FakeResponse(b"tiny"),
        ) as opener:
            self.assertEqual(stash.image_bytes("42"), b"tiny")
        request = opener.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/image/42/thumbnail"))

    def test_cloudflare_521_is_retryable(self):
        exc = urllib.error.HTTPError(
            "https://saucenao.com", 521, "down", {}, None
        )
        self.assertTrue(plugin.is_retryable_exception(exc))


if __name__ == "__main__":
    unittest.main()
''',
    encoding="utf-8",
)

print("Deep Match performance patch prepared.")
