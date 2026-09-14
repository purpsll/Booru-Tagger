#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "DanbooruTagImporter"


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected exactly one occurrence, found {count}: {old[:120]!r}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def replace_all_version(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    if "3.26.5" not in text:
        raise SystemExit(f"{path}: expected 3.26.5 before version bump")
    path.write_text(text.replace("3.26.5", "3.26.6"), encoding="utf-8")


# Fixed policy / version.
constants = PLUGIN / "constants.py"
replace_once(constants, 'VERSION = "3.26.5"', 'VERSION = "3.26.6"')
replace_once(
    constants,
    'E621_IQDB_MIN_SCORE = 90.0\nENABLE_SAUCENAO = True\n',
    'E621_IQDB_MIN_SCORE = 90.0\n'
    '# e621 publishes a 2 req/s hard API ceiling and recommends <=1 req/s sustained.\n'
    '# Reverse-image file uploads are more heavily throttled. Authenticated IQDB\n'
    '# uploads are paced below the current 6-per-10-second allowance; anonymous\n'
    '# uploads respect the much stricter 1-per-60-second allowance.\n'
    'E621_GENERAL_MIN_INTERVAL_SECONDS = 1.0\n'
    'E621_IQDB_AUTH_MIN_INTERVAL_SECONDS = 2.0\n'
    'E621_IQDB_ANON_MIN_INTERVAL_SECONDS = 65.0\n'
    'E621_IQDB_RATE_LIMIT_BACKOFF_SECONDS = 5.0\n'
    'E621_IQDB_RATE_LIMIT_MAX_BACKOFF_SECONDS = 60.0\n'
    'ENABLE_SAUCENAO = True\n',
)

main = PLUGIN / "DanbooruTagImporter.py"
replace_once(main, 'import difflib\n', 'import difflib\nimport email.utils\n')
replace_once(
    main,
    '    E621_BASE, E621_IQDB_MIN_SCORE, ENABLE_DANBOORU, ENABLE_DANBOORU_IQDB,\n',
    '    E621_BASE, E621_GENERAL_MIN_INTERVAL_SECONDS, E621_IQDB_ANON_MIN_INTERVAL_SECONDS,\n'
    '    E621_IQDB_AUTH_MIN_INTERVAL_SECONDS, E621_IQDB_MIN_SCORE,\n'
    '    E621_IQDB_RATE_LIMIT_BACKOFF_SECONDS, E621_IQDB_RATE_LIMIT_MAX_BACKOFF_SECONDS,\n'
    '    ENABLE_DANBOORU, ENABLE_DANBOORU_IQDB,\n',
)
replace_once(
    main,
    '_E621_IQDB_RATE_LIMIT_HITS = 0\n_RULE34_COOLDOWN_ANNOUNCED = False\n',
    '_E621_IQDB_RATE_LIMIT_HITS = 0\n'
    '_E621_IQDB_LAST_REQUEST_AT = 0.0\n'
    '_E621_IQDB_BACKOFF_UNTIL = 0.0\n'
    '_E621_IQDB_CONSECUTIVE_RATE_LIMITS = 0\n'
    '_RULE34_COOLDOWN_ANNOUNCED = False\n',
)
replace_once(
    main,
    '_PROVIDER_WARNING_ONCE: set[tuple[str, str]] = set()\n\n\n_STASH_LOG_LEVEL_CHARS = {\n',
    '_PROVIDER_WARNING_ONCE: set[tuple[str, str]] = set()\n\n'
    '# Keep ordinary e621 API traffic below the documented hard ceiling. IQDB file\n'
    '# uploads use their own stricter pacing below.\n'
    'HTTP.set_host_interval(\n'
    '    (urllib.parse.urlparse(E621_BASE).hostname or "e621.net").casefold(),\n'
    '    E621_GENERAL_MIN_INTERVAL_SECONDS,\n'
    ')\n\n\n_STASH_LOG_LEVEL_CHARS = {\n',
)

helpers = '''def _e621_iqdb_is_authenticated(username: str, api_key: str) -> bool:\n    return bool(str(username or "").strip() and str(api_key or "").strip())\n\n\ndef _e621_iqdb_min_interval(username: str, api_key: str) -> float:\n    return float(\n        E621_IQDB_AUTH_MIN_INTERVAL_SECONDS\n        if _e621_iqdb_is_authenticated(username, api_key)\n        else E621_IQDB_ANON_MIN_INTERVAL_SECONDS\n    )\n\n\ndef _e621_retry_after_seconds(exc: urllib.error.HTTPError) -> float:\n    raw = exc.headers.get("Retry-After") if exc.headers else None\n    if not raw:\n        return 0.0\n    raw = str(raw).strip()\n    try:\n        return max(0.0, float(raw))\n    except ValueError:\n        pass\n    try:\n        dt = email.utils.parsedate_to_datetime(raw)\n        if dt.tzinfo is None:\n            dt = dt.replace(tzinfo=timezone.utc)\n        return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())\n    except (TypeError, ValueError, OverflowError):\n        return 0.0\n\n\ndef _e621_iqdb_wait_for_slot(username: str, api_key: str) -> float:\n    \"\"\"Pace heavy e621 IQDB file uploads without disabling later images.\"\"\"\n    global _E621_IQDB_LAST_REQUEST_AT\n    interval = _e621_iqdb_min_interval(username, api_key)\n    now = time.monotonic()\n    wait_for = max(0.0, _E621_IQDB_BACKOFF_UNTIL - now)\n    if _E621_IQDB_LAST_REQUEST_AT > 0:\n        wait_for = max(wait_for, (_E621_IQDB_LAST_REQUEST_AT + interval) - now)\n    if wait_for > 0:\n        time.sleep(wait_for)\n    _E621_IQDB_LAST_REQUEST_AT = time.monotonic()\n    return interval\n\n\ndef _e621_iqdb_note_rate_limit(\n    exc: urllib.error.HTTPError, username: str, api_key: str\n) -> float:\n    \"\"\"Delay only the next e621 IQDB attempt after a 429/challenge.\"\"\"\n    global _E621_IQDB_BACKOFF_UNTIL, _E621_IQDB_CONSECUTIVE_RATE_LIMITS\n    _E621_IQDB_CONSECUTIVE_RATE_LIMITS += 1\n    minimum = _e621_iqdb_min_interval(username, api_key)\n    adaptive = min(\n        float(E621_IQDB_RATE_LIMIT_MAX_BACKOFF_SECONDS),\n        float(E621_IQDB_RATE_LIMIT_BACKOFF_SECONDS)\n        * (2 ** max(0, _E621_IQDB_CONSECUTIVE_RATE_LIMITS - 1)),\n    )\n    delay = max(minimum, adaptive, _e621_retry_after_seconds(exc))\n    _E621_IQDB_BACKOFF_UNTIL = max(\n        _E621_IQDB_BACKOFF_UNTIL, time.monotonic() + delay\n    )\n    return delay\n\n\ndef _e621_iqdb_note_success() -> None:\n    global _E621_IQDB_BACKOFF_UNTIL, _E621_IQDB_CONSECUTIVE_RATE_LIMITS\n    _E621_IQDB_BACKOFF_UNTIL = 0.0\n    _E621_IQDB_CONSECUTIVE_RATE_LIMITS = 0\n\n\n'''
replace_once(main, '\ndef e621_iqdb(\n', '\n' + helpers + 'def e621_iqdb(\n')
replace_once(
    main,
    '    HTTP.clear_host_failures(e621_host)\n    boundary = "----StashE621IQDBBoundary7MA4YWxkTrZu0gW"\n',
    '    HTTP.clear_host_failures(e621_host)\n'
    '    _e621_iqdb_wait_for_slot(username, api_key)\n'
    '    boundary = "----StashE621IQDBBoundary7MA4YWxkTrZu0gW"\n',
)
replace_once(
    main,
    '            timeout=DEEP_VISUAL_TIMEOUT_SECONDS,\n            retries=DEEP_VISUAL_HTTP_RETRIES,\n',
    '            timeout=DEEP_VISUAL_TIMEOUT_SECONDS,\n'
    '            min_interval=0.0,\n'
    '            retries=DEEP_VISUAL_HTTP_RETRIES,\n',
)
replace_once(
    main,
    '        ) as resp:\n            raw = resp.read().decode("utf-8", errors="replace")\n    except urllib.error.HTTPError as exc:\n',
    '        ) as resp:\n'
    '            raw = resp.read().decode("utf-8", errors="replace")\n'
    '        _e621_iqdb_note_success()\n'
    '    except urllib.error.HTTPError as exc:\n',
)
replace_once(
    main,
    '        if exc.code == 429 or cloudflare_challenge:\n            _E621_IQDB_RATE_LIMIT_HITS += 1\n            reason = f"HTTP {exc.code}" + (\n                " / Cloudflare challenge" if cloudflare_challenge else ""\n            )\n            raise RuntimeError(\n                f"e621 IQDB temporarily unavailable for this image: {reason}"\n            ) from exc\n',
    '        if exc.code == 429 or cloudflare_challenge:\n'
    '            _E621_IQDB_RATE_LIMIT_HITS += 1\n'
    '            _e621_iqdb_note_rate_limit(exc, username, api_key)\n'
    '            reason = f"HTTP {exc.code}" + (\n'
    '                " / Cloudflare challenge" if cloudflare_challenge else ""\n'
    '            )\n'
    '            raise RuntimeError(\n'
    '                f"e621 IQDB temporarily unavailable for this image: {reason}"\n'
    '            ) from exc\n',
)

# Tests: keep existing behavior and add pacing/backoff coverage.
test = PLUGIN / "tests" / "test_e621_per_image_retry.py"
replace_once(
    test,
    '        plugin._PROVIDER_WARNING_ONCE.clear()\n',
    '        plugin._PROVIDER_WARNING_ONCE.clear()\n'
    '        plugin._E621_IQDB_LAST_REQUEST_AT = 0.0\n'
    '        plugin._E621_IQDB_BACKOFF_UNTIL = 0.0\n'
    '        plugin._E621_IQDB_CONSECUTIVE_RATE_LIMITS = 0\n',
)
extra_tests = '''\n    def test_authenticated_iqdb_pacing_uses_two_second_floor(self):\n        plugin._E621_IQDB_LAST_REQUEST_AT = 100.0\n        with mock.patch.object(plugin.time, "monotonic", side_effect=[100.5, 102.0]), \\\n             mock.patch.object(plugin.time, "sleep") as sleeper:\n            interval = plugin._e621_iqdb_wait_for_slot("user", "key")\n\n        self.assertEqual(interval, plugin.E621_IQDB_AUTH_MIN_INTERVAL_SECONDS)\n        sleeper.assert_called_once_with(1.5)\n        self.assertEqual(plugin._E621_IQDB_LAST_REQUEST_AT, 102.0)\n\n    def test_anonymous_iqdb_pacing_uses_sixty_five_second_floor(self):\n        self.assertEqual(\n            plugin._e621_iqdb_min_interval("", ""),\n            plugin.E621_IQDB_ANON_MIN_INTERVAL_SECONDS,\n        )\n        self.assertGreaterEqual(plugin.E621_IQDB_ANON_MIN_INTERVAL_SECONDS, 60.0)\n\n    def test_e621_429_respects_retry_after_without_disabling_next_image(self):\n        error = __import__("urllib.error").error.HTTPError(\n            "https://e621.net/iqdb_queries.json", 429, "rate", {"Retry-After": "7"}, None\n        )\n        with mock.patch.object(plugin.time, "monotonic", return_value=100.0):\n            delay = plugin._e621_iqdb_note_rate_limit(error, "user", "key")\n\n        self.assertEqual(delay, 7.0)\n        self.assertEqual(plugin._E621_IQDB_BACKOFF_UNTIL, 107.0)\n        self.assertEqual(plugin._E621_IQDB_CONSECUTIVE_RATE_LIMITS, 1)\n\n        plugin._e621_iqdb_note_success()\n        self.assertEqual(plugin._E621_IQDB_BACKOFF_UNTIL, 0.0)\n        self.assertEqual(plugin._E621_IQDB_CONSECUTIVE_RATE_LIMITS, 0)\n'''
replace_once(test, '\n\nif __name__ == "__main__":\n', extra_tests + '\n\nif __name__ == "__main__":\n')

# Version-aligned public metadata/docs/tests.
manifest = PLUGIN / "DanbooruTagImporter.yml"
replace_all_version(manifest)
replace_once(
    manifest,
    '# e621 IQDB policy: transient failures are image-local; every new image is eligible to try e621 IQDB again while normal request pacing is preserved.\n',
    '# e621 IQDB policy: transient failures are image-local; every new image remains eligible. Authenticated file uploads are paced at >=2s, anonymous uploads at >=65s, with adaptive post-429 backoff.\n',
)

for path in [ROOT / "README.md", PLUGIN / "README.md", PLUGIN / "tests" / "test_task_menu.py", ROOT / ".github" / "workflows" / "release.yml"]:
    replace_all_version(path)

root_readme = ROOT / "README.md"
replace_once(
    root_readme,
    'For e621 IQDB specifically, a 429 or Cloudflare challenge leaves that image eligible for retry but does not disable e621 IQDB for later images in the same queue. Repeated identical e621 provider warnings are de-duplicated, while the per-image result remains visible in the normal log.\n',
    'For e621 IQDB specifically, a 429 or Cloudflare challenge leaves that image eligible for retry but does not disable e621 IQDB for later images in the same queue. Authenticated IQDB file uploads are paced at least 2 seconds apart (below e621\'s current heavy-search allowance), anonymous uploads at least 65 seconds apart, and 429/Cloudflare responses add adaptive backoff for the next e621 attempt while still allowing the next image to retry. Repeated identical e621 provider warnings are de-duplicated, while the per-image result remains visible in the normal log.\n',
)

plugin_readme = PLUGIN / "README.md"
text = plugin_readme.read_text(encoding="utf-8")
needle = 'A failure on one image never disables e621 IQDB for the next image in the queue.'
if needle in text:
    text = text.replace(
        needle,
        needle + ' Authenticated IQDB file uploads are paced at least 2 seconds apart; anonymous uploads are paced at least 65 seconds apart. A 429/Cloudflare response adds adaptive backoff to the next e621 attempt without disabling it.',
        1,
    )
elif '2 seconds' not in text:
    text += '\n\n### e621 IQDB pacing\n\nAuthenticated file uploads are paced at least 2 seconds apart; anonymous uploads are paced at least 65 seconds apart. HTTP 429 / Cloudflare responses add adaptive backoff to the next e621 attempt without disabling later images.\n'
plugin_readme.write_text(text, encoding="utf-8")

release = ROOT / ".github" / "workflows" / "release.yml"
replace_once(
    release,
    'This patch release changes e621 IQDB rate-limit handling so one failed image never disables e621 IQDB for the rest of a Deep Match queue.',
    'This patch release adds provider-aware e621 pacing so Deep Match stays below e621\'s published request limits while preserving per-image retries.',
)
replace_once(
    release,
    '          - **No run-wide e621 IQDB disable.** The next image retries e621 IQDB normally.\n',
    '          - **No run-wide e621 IQDB disable.** The next image retries e621 IQDB normally.\n'
    '          - **Provider-aware pacing.** Ordinary e621 traffic is limited to at most 1 request/second; authenticated IQDB file uploads are spaced at least 2 seconds apart and anonymous uploads at least 65 seconds apart.\n'
    '          - **Adaptive 429 backoff.** Retry-After is honored when present; otherwise repeated 429/Cloudflare responses back off progressively without disabling the next image.\n',
)
replace_once(release, '**81 tests**', '**84 tests**')

print("Applied e621 IQDB pacing patch for 3.26.6")
