from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "DanbooruTagImporter"


def read(path):
    return path.read_text(encoding="utf-8")


def write(path, text):
    path.write_text(text, encoding="utf-8")


def replace_once(text, old, new, label):
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one match, found {count}")
    return text.replace(old, new, 1)


# constants.py
path = PLUGIN / "constants.py"
text = read(path)
text = replace_once(text, 'VERSION = "3.26.6"', 'VERSION = "3.26.7"', "constants version")
text = replace_once(
    text,
    'E621_IQDB_AUTH_MIN_INTERVAL_SECONDS = 2.0\nE621_IQDB_ANON_MIN_INTERVAL_SECONDS = 65.0\nE621_IQDB_RATE_LIMIT_BACKOFF_SECONDS = 5.0\nE621_IQDB_RATE_LIMIT_MAX_BACKOFF_SECONDS = 60.0',
    'E621_IQDB_AUTH_MIN_INTERVAL_SECONDS = 3.0\nE621_IQDB_ANON_MIN_INTERVAL_SECONDS = 65.0\nE621_IQDB_RATE_LIMIT_BACKOFF_SECONDS = 5.0\nE621_IQDB_RATE_LIMIT_MAX_BACKOFF_SECONDS = 60.0\nE621_IQDB_RECOVERY_DECAY_SECONDS = 1.0',
    "e621 pacing constants",
)
write(path, text)

# Main plugin.
path = PLUGIN / "DanbooruTagImporter.py"
text = read(path)
text = replace_once(
    text,
    '    E621_IQDB_AUTH_MIN_INTERVAL_SECONDS, E621_IQDB_MIN_SCORE,\n    E621_IQDB_RATE_LIMIT_BACKOFF_SECONDS, E621_IQDB_RATE_LIMIT_MAX_BACKOFF_SECONDS,',
    '    E621_IQDB_AUTH_MIN_INTERVAL_SECONDS, E621_IQDB_MIN_SCORE,\n    E621_IQDB_RATE_LIMIT_BACKOFF_SECONDS, E621_IQDB_RATE_LIMIT_MAX_BACKOFF_SECONDS,\n    E621_IQDB_RECOVERY_DECAY_SECONDS,',
    "e621 constant import",
)
text = replace_once(
    text,
    '_E621_IQDB_BACKOFF_UNTIL = 0.0\n_E621_IQDB_CONSECUTIVE_RATE_LIMITS = 0',
    '_E621_IQDB_BACKOFF_UNTIL = 0.0\n_E621_IQDB_CONSECUTIVE_RATE_LIMITS = 0\n_E621_IQDB_RECOVERY_INTERVAL_SECONDS = 0.0',
    "e621 recovery state",
)
text = replace_once(
    text,
    '''def _e621_iqdb_min_interval(username: str, api_key: str) -> float:\n    return float(\n        E621_IQDB_AUTH_MIN_INTERVAL_SECONDS\n        if _e621_iqdb_is_authenticated(username, api_key)\n        else E621_IQDB_ANON_MIN_INTERVAL_SECONDS\n    )\n''',
    '''def _e621_iqdb_base_interval(username: str, api_key: str) -> float:\n    return float(\n        E621_IQDB_AUTH_MIN_INTERVAL_SECONDS\n        if _e621_iqdb_is_authenticated(username, api_key)\n        else E621_IQDB_ANON_MIN_INTERVAL_SECONDS\n    )\n\n\ndef _e621_iqdb_min_interval(username: str, api_key: str) -> float:\n    return max(\n        _e621_iqdb_base_interval(username, api_key),\n        float(_E621_IQDB_RECOVERY_INTERVAL_SECONDS or 0.0),\n    )\n''',
    "e621 base interval function",
)
text = replace_once(
    text,
    '''    global _E621_IQDB_BACKOFF_UNTIL, _E621_IQDB_CONSECUTIVE_RATE_LIMITS\n    _E621_IQDB_CONSECUTIVE_RATE_LIMITS += 1\n    minimum = _e621_iqdb_min_interval(username, api_key)\n''',
    '''    global _E621_IQDB_BACKOFF_UNTIL, _E621_IQDB_CONSECUTIVE_RATE_LIMITS\n    global _E621_IQDB_RECOVERY_INTERVAL_SECONDS\n    _E621_IQDB_CONSECUTIVE_RATE_LIMITS += 1\n    minimum = _e621_iqdb_base_interval(username, api_key)\n''',
    "e621 rate-limit globals",
)
text = replace_once(
    text,
    '''    _E621_IQDB_BACKOFF_UNTIL = max(\n        _E621_IQDB_BACKOFF_UNTIL, time.monotonic() + delay\n    )\n    return delay\n\n\ndef _e621_iqdb_note_success() -> None:\n    global _E621_IQDB_BACKOFF_UNTIL, _E621_IQDB_CONSECUTIVE_RATE_LIMITS\n    _E621_IQDB_BACKOFF_UNTIL = 0.0\n    _E621_IQDB_CONSECUTIVE_RATE_LIMITS = 0\n''',
    '''    _E621_IQDB_BACKOFF_UNTIL = max(\n        _E621_IQDB_BACKOFF_UNTIL, time.monotonic() + delay\n    )\n    _E621_IQDB_RECOVERY_INTERVAL_SECONDS = max(\n        float(_E621_IQDB_RECOVERY_INTERVAL_SECONDS or 0.0), delay\n    )\n    return delay\n\n\ndef _e621_iqdb_note_success(username: str, api_key: str) -> None:\n    """Recover gradually after throttling instead of snapping back to the base rate."""\n    global _E621_IQDB_BACKOFF_UNTIL, _E621_IQDB_CONSECUTIVE_RATE_LIMITS\n    global _E621_IQDB_RECOVERY_INTERVAL_SECONDS\n    _E621_IQDB_BACKOFF_UNTIL = 0.0\n    _E621_IQDB_CONSECUTIVE_RATE_LIMITS = max(\n        0, _E621_IQDB_CONSECUTIVE_RATE_LIMITS - 1\n    )\n    base = _e621_iqdb_base_interval(username, api_key)\n    recovery = float(_E621_IQDB_RECOVERY_INTERVAL_SECONDS or 0.0)\n    if recovery > base:\n        recovery = max(\n            base, recovery - max(0.1, float(E621_IQDB_RECOVERY_DECAY_SECONDS))\n        )\n        _E621_IQDB_RECOVERY_INTERVAL_SECONDS = (\n            0.0 if recovery <= base else recovery\n        )\n    else:\n        _E621_IQDB_RECOVERY_INTERVAL_SECONDS = 0.0\n''',
    "e621 gradual recovery",
)
text = replace_once(
    text,
    '        _e621_iqdb_note_success()\n',
    '        _e621_iqdb_note_success(username, api_key)\n',
    "e621 success call",
)
text = replace_once(
    text,
    '''                log(\n                    "WARNING",\n                    f"SauceNAO not authoritative: {saucenao_outcome.detail}",\n                )\n''',
    '''                sauce_detail = str(\n                    saucenao_outcome.detail or saucenao_outcome.status.value\n                )\n                # _saucenao_note_outage already announces the cooldown once.\n                # Do not emit the same provider warning for every image skipped\n                # during that cooldown; the per-image decision remains visible.\n                if not sauce_detail.startswith("temporary SauceNAO outage cooldown"):\n                    _log_lookup_problem_once("SauceNAO", sauce_detail)\n''',
    "SauceNAO warning dedupe",
)
text = replace_once(
    text,
    '''        only_e621_iqdb_retry = bool(retry_outcomes) and all(\n            outcome.provider == "e621" and outcome.stage == "iqdb"\n            for outcome in retry_outcomes\n        )\n        log(\n            "INFO" if only_e621_iqdb_retry else "WARNING",\n''',
    '''        quiet_provider_deferral = bool(retry_outcomes) and all(\n            (outcome.provider == "e621" and outcome.stage == "iqdb")\n            or (\n                outcome.provider == "SauceNAO"\n                and outcome.stage == "visual"\n                and str(outcome.detail or "").startswith(\n                    "temporary SauceNAO outage cooldown"\n                )\n            )\n            for outcome in retry_outcomes\n        )\n        log(\n            "INFO" if quiet_provider_deferral else "WARNING",\n''',
    "quiet retry logging",
)
write(path, text)

# e621 pacing tests.
path = PLUGIN / "tests" / "test_e621_per_image_retry.py"
text = read(path)
text = replace_once(
    text,
    '        plugin._E621_IQDB_CONSECUTIVE_RATE_LIMITS = 0\n',
    '        plugin._E621_IQDB_CONSECUTIVE_RATE_LIMITS = 0\n        plugin._E621_IQDB_RECOVERY_INTERVAL_SECONDS = 0.0\n',
    "test recovery reset",
)
text = replace_once(
    text,
    '''    def test_authenticated_iqdb_pacing_uses_two_second_floor(self):\n        plugin._E621_IQDB_LAST_REQUEST_AT = 100.0\n        with mock.patch.object(plugin.time, "monotonic", side_effect=[100.5, 102.0]), \\\n             mock.patch.object(plugin.time, "sleep") as sleeper:\n            interval = plugin._e621_iqdb_wait_for_slot("user", "key")\n\n        self.assertEqual(interval, plugin.E621_IQDB_AUTH_MIN_INTERVAL_SECONDS)\n        sleeper.assert_called_once_with(1.5)\n        self.assertEqual(plugin._E621_IQDB_LAST_REQUEST_AT, 102.0)\n''',
    '''    def test_authenticated_iqdb_pacing_uses_three_second_floor(self):\n        plugin._E621_IQDB_LAST_REQUEST_AT = 100.0\n        with mock.patch.object(plugin.time, "monotonic", side_effect=[100.5, 103.0]), \\\n             mock.patch.object(plugin.time, "sleep") as sleeper:\n            interval = plugin._e621_iqdb_wait_for_slot("user", "key")\n\n        self.assertEqual(interval, plugin.E621_IQDB_AUTH_MIN_INTERVAL_SECONDS)\n        self.assertEqual(plugin.E621_IQDB_AUTH_MIN_INTERVAL_SECONDS, 3.0)\n        sleeper.assert_called_once_with(2.5)\n        self.assertEqual(plugin._E621_IQDB_LAST_REQUEST_AT, 103.0)\n''',
    "authenticated pacing test",
)
text = replace_once(
    text,
    '''        self.assertEqual(plugin._E621_IQDB_CONSECUTIVE_RATE_LIMITS, 1)\n\n        plugin._e621_iqdb_note_success()\n        self.assertEqual(plugin._E621_IQDB_BACKOFF_UNTIL, 0.0)\n        self.assertEqual(plugin._E621_IQDB_CONSECUTIVE_RATE_LIMITS, 0)\n''',
    '''        self.assertEqual(plugin._E621_IQDB_CONSECUTIVE_RATE_LIMITS, 1)\n        self.assertEqual(plugin._E621_IQDB_RECOVERY_INTERVAL_SECONDS, 7.0)\n\n        plugin._e621_iqdb_note_success("user", "key")\n        self.assertEqual(plugin._E621_IQDB_BACKOFF_UNTIL, 0.0)\n        self.assertEqual(plugin._E621_IQDB_CONSECUTIVE_RATE_LIMITS, 0)\n        self.assertEqual(plugin._E621_IQDB_RECOVERY_INTERVAL_SECONDS, 6.0)\n\n    def test_successful_iqdb_requests_decay_recovery_floor_gradually(self):\n        plugin._E621_IQDB_RECOVERY_INTERVAL_SECONDS = 6.0\n\n        plugin._e621_iqdb_note_success("user", "key")\n        self.assertEqual(plugin._E621_IQDB_RECOVERY_INTERVAL_SECONDS, 5.0)\n        plugin._e621_iqdb_note_success("user", "key")\n        self.assertEqual(plugin._E621_IQDB_RECOVERY_INTERVAL_SECONDS, 4.0)\n        plugin._e621_iqdb_note_success("user", "key")\n        self.assertEqual(plugin._E621_IQDB_RECOVERY_INTERVAL_SECONDS, 0.0)\n''',
    "retry-after recovery test",
)
write(path, text)

# SauceNAO cooldown logging test.
path = PLUGIN / "tests" / "test_deep_performance.py"
text = read(path)
text = replace_once(text, 'import threading\nimport unittest', 'import threading\nimport time\nimport unittest', "deep test time import")
insert = '''\n    def test_saucenao_cooldown_is_quiet_after_initial_outage_warning(self):\n        stash = FakeStash()\n        settings = {"saucenao_api_key": "sauce"}\n        old_until = plugin._SAUCENAO_OUTAGE_UNTIL\n        old_reason = plugin._SAUCENAO_DISABLED_REASON\n        plugin._SAUCENAO_OUTAGE_UNTIL = time.monotonic() + 180.0\n        plugin._SAUCENAO_DISABLED_REASON = (\n            "temporary SauceNAO outage cooldown after HTTP 521 (180s)"\n        )\n        try:\n            with mock.patch.object(plugin, "ENABLE_DANBOORU_IQDB", False), \\\n                 mock.patch.object(plugin, "e621_iqdb", return_value=None), \\\n                 mock.patch.object(plugin, "log") as logger:\n                first = plugin.process_image(\n                    stash, unresolved_image(), settings, {}, True, {}, {}, {}, {},\n                    plugin.PHashIndex(), lookup_mode="deep",\n                )\n                second = plugin.process_image(\n                    stash, unresolved_image(), settings, {}, True, {}, {}, {}, {},\n                    plugin.PHashIndex(), lookup_mode="deep",\n                )\n\n            self.assertEqual(first, "retry_later")\n            self.assertEqual(second, "retry_later")\n            repeated_provider_warnings = [\n                call for call in logger.call_args_list\n                if "SauceNAO not authoritative" in str(call)\n            ]\n            self.assertEqual(repeated_provider_warnings, [])\n            retry_logs = [\n                call for call in logger.call_args_list if "RETRY LATER" in str(call)\n            ]\n            self.assertEqual(len(retry_logs), 2)\n            self.assertTrue(all(call.args[0] == "INFO" for call in retry_logs))\n        finally:\n            plugin._SAUCENAO_OUTAGE_UNTIL = old_until\n            plugin._SAUCENAO_DISABLED_REASON = old_reason\n'''
text = replace_once(text, '\n\nif __name__ == "__main__":\n', insert + '\n\nif __name__ == "__main__":\n', "SauceNAO cooldown test insertion")
write(path, text)

# Version consistency test.
path = PLUGIN / "tests" / "test_task_menu.py"
text = read(path)
text = replace_once(text, 'self.assertEqual(yaml_version, "3.26.6")', 'self.assertEqual(yaml_version, "3.26.7")', "task version")
write(path, text)

# Manifest version and policy comments/settings help.
path = PLUGIN / "DanbooruTagImporter.yml"
text = read(path)
text = replace_once(text, 'version: 3.26.6', 'version: 3.26.7', "manifest version")
text = text.replace('Authenticated file uploads are paced at >=2s', 'Authenticated file uploads are paced at >=3s')
text = text.replace(
    'Optional but recommended. Used in the identifying e621 User-Agent and with the API key for authenticated requests.',
    'Strongly recommended for Deep Match. Used in the identifying e621 User-Agent and with the API key for authenticated requests; authenticated e621 IQDB is paced far more practically than anonymous IQDB.'
)
text = text.replace(
    'Optional e621 API key for authenticated requests. Never written to plugin logs.',
    'Strongly recommended with the e621 username for authenticated requests and practical IQDB pacing. Never written to plugin logs.'
)
write(path, text)

# Root README.
path = ROOT / "README.md"
text = read(path)
text = text.replace('3.26.6', '3.26.7')
text = text.replace(
    'Authenticated IQDB file uploads are paced at least 2 seconds apart (below e621\'s current heavy-search allowance), anonymous uploads at least 65 seconds apart, and 429/Cloudflare responses add adaptive backoff for the next e621 attempt while still allowing the next image to retry.',
    'Authenticated IQDB file uploads are paced at least 3 seconds apart, anonymous uploads at least 65 seconds apart, and 429/Cloudflare responses add adaptive backoff for the next e621 attempt while still allowing the next image to retry. After a successful authenticated request, any extra e621 recovery delay decays gradually instead of snapping immediately back to the 3-second floor. SauceNAO outage cooldowns are announced once rather than repeated as provider warnings for every skipped image.'
)
write(path, text)

# Technical README.
path = PLUGIN / "README.md"
text = read(path)
text = text.replace('3.26.6', '3.26.7')
text = text.replace('at least 2 seconds apart', 'at least 3 seconds apart')
text = text.replace(
    '- General external-provider minimum spacing: 250 ms per host',
    '- General external-provider minimum spacing: 250 ms per host; e621 overrides this with stricter provider-specific pacing'
)
needle = '- e621 IQDB transient failures are image-local: every new image retries e621 IQDB; no run-wide e621 IQDB disable is retained\n'
addition = needle + '- e621 authenticated IQDB base spacing: 3 seconds; 429/Cloudflare backoff decays by 1 second per successful IQDB request until the base floor is restored\n- SauceNAO outage cooldowns are announced once; subsequent cooldown-only Retry Later image lines are informational\n'
text = replace_once(text, needle, addition, "technical pacing bullets")
text = text.replace(
    'HTTP 429 / Cloudflare responses add adaptive backoff to the next e621 attempt without disabling later images.',
    'HTTP 429 / Cloudflare responses add adaptive backoff to the next e621 attempt without disabling later images. Successful authenticated IQDB requests reduce any extra recovery interval gradually rather than clearing it immediately.'
)
write(path, text)

print("Applied Booru Importer 3.26.7 hardening patch")
