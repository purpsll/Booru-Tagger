from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "DanbooruTagImporter"
MAIN = PLUGIN / "DanbooruTagImporter.py"
CONSTANTS = PLUGIN / "constants.py"
MANIFEST = PLUGIN / "DanbooruTagImporter.yml"
ROOT_README = ROOT / "README.md"
PLUGIN_README = PLUGIN / "README.md"
TEST = PLUGIN / "tests" / "test_saucenao_resilience.py"


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one match, found {count}")
    return text.replace(old, new, 1)


def regex_once(text: str, pattern: str, repl: str, label: str) -> str:
    new, count = re.subn(pattern, repl, text, count=1, flags=re.S)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one regex match, found {count}")
    return new


# constants.py
text = CONSTANTS.read_text(encoding="utf-8")
text = replace_once(text, 'VERSION = "3.26.7"', 'VERSION = "3.26.8"', "constants version")
text = replace_once(
    text,
    'SAUCENAO_OUTAGE_COOLDOWN_SECONDS = 180.0',
    'SAUCENAO_OUTAGE_BACKOFF_SECONDS = 5.0\nSAUCENAO_OUTAGE_MAX_BACKOFF_SECONDS = 20.0',
    "SauceNAO outage constants",
)
CONSTANTS.write_text(text, encoding="utf-8")

# Main plugin.
text = MAIN.read_text(encoding="utf-8")
text = replace_once(
    text,
    '    SAUCENAO_OUTAGE_COOLDOWN_SECONDS, SAUCENAO_QUOTA_WINDOW_SECONDS,',
    '    SAUCENAO_OUTAGE_BACKOFF_SECONDS, SAUCENAO_OUTAGE_MAX_BACKOFF_SECONDS,\n    SAUCENAO_QUOTA_WINDOW_SECONDS,',
    "SauceNAO imports",
)
text = replace_once(
    text,
    '_SAUCENAO_OUTAGE_UNTIL = 0.0',
    '_SAUCENAO_OUTAGE_HITS = 0\n_SAUCENAO_OUTAGE_STREAK = 0',
    "SauceNAO outage state",
)

replacement = '''def _saucenao_is_disabled() -> bool:\n    # Only a genuinely exhausted long-term quota disables SauceNAO. Temporary\n    # 52x origin failures never skip SauceNAO for later images.\n    return bool(_SAUCENAO_DAILY_EXHAUSTED)\n\n\ndef _saucenao_note_success() -> None:\n    \"\"\"Recover gradually from an outage streak after a successful request.\"\"\"\n    global _SAUCENAO_OUTAGE_STREAK\n    _SAUCENAO_OUTAGE_STREAK = max(0, int(_SAUCENAO_OUTAGE_STREAK) - 1)\n\n\ndef _saucenao_note_outage(status_code: int) -> float:\n    \"\"\"Back off briefly after 52x, but keep SauceNAO eligible for every image.\"\"\"\n    global _SAUCENAO_OUTAGE_HITS, _SAUCENAO_OUTAGE_STREAK\n    global _SAUCENAO_PAUSE_UNTIL, _SAUCENAO_QUOTA_PAUSES\n\n    _SAUCENAO_OUTAGE_HITS += 1\n    _SAUCENAO_OUTAGE_STREAK += 1\n    base = max(1.0, float(SAUCENAO_OUTAGE_BACKOFF_SECONDS))\n    maximum = max(base, float(SAUCENAO_OUTAGE_MAX_BACKOFF_SECONDS))\n    delay = min(maximum, base * (2 ** min(6, _SAUCENAO_OUTAGE_STREAK - 1)))\n    _SAUCENAO_PAUSE_UNTIL = max(\n        _SAUCENAO_PAUSE_UNTIL, time.monotonic() + delay\n    )\n    _SAUCENAO_QUOTA_PAUSES += 1\n\n    key = (\"SauceNAO outage\", str(status_code))\n    if key not in _PROVIDER_WARNING_ONCE:\n        _PROVIDER_WARNING_ONCE.add(key)\n        log(\n            \"WARNING\",\n            f\"SauceNAO HTTP {status_code}; keeping SauceNAO enabled and waiting \"\n            f\"{delay:.0f}s before its next image search. Repeated identical \"\n            \"outage warnings are suppressed.\",\n        )\n    return delay\n\n\n'''
text = regex_once(
    text,
    r'def _saucenao_is_disabled\(\) -> bool:.*?(?=def _rule34_host\(\) -> str:)',
    replacement,
    "SauceNAO outage functions",
)

text = replace_once(
    text,
    '    log("WARNING", f"SauceNAO rate limit hit; pausing SauceNAO for {delay:.0f}s and continuing afterward ({reason})")',
    '    key = ("SauceNAO rate limit", "429")\n    if key not in _PROVIDER_WARNING_ONCE:\n        _PROVIDER_WARNING_ONCE.add(key)\n        log("WARNING", f"SauceNAO rate limit hit; pausing SauceNAO for {delay:.0f}s and continuing afterward ({reason}). Repeated rate-limit warnings are suppressed.")',
    "SauceNAO 429 warning dedupe",
)

text = replace_once(
    text,
    '            _saucenao_note_rate_limit("API rate status")\n            raise RuntimeError("SauceNAO temporarily unavailable: API rate status")\n    results=payload.get("results") if isinstance(payload,dict) else None',
    '            _saucenao_note_rate_limit("API rate status")\n            raise RuntimeError("SauceNAO temporarily unavailable: API rate status")\n    _saucenao_note_success()\n    results=payload.get("results") if isinstance(payload,dict) else None',
    "SauceNAO success recovery",
)

old = '''                # _saucenao_note_outage already announces the cooldown once.\n                # Do not emit the same provider warning for every image skipped\n                # during that cooldown; the per-image decision remains visible.\n                if not sauce_detail.startswith("temporary SauceNAO outage cooldown"):\n                    _log_lookup_problem_once("SauceNAO", sauce_detail)'''
new = '''                # 52x outages are already announced once by _saucenao_note_outage.\n                # Keep the per-image Retry Later result at INFO without another warning.\n                if not re.search(r"HTTP 52[0-4]", sauce_detail):\n                    _log_lookup_problem_once("SauceNAO", sauce_detail)'''
text = replace_once(text, old, new, "SauceNAO per-image warning suppression")

text = regex_once(
    text,
    r'        retry_outcomes = \[.*?        log\(\n            "INFO" if quiet_provider_deferral else "WARNING",\n            f"Image \{iid\}: RETRY LATER; no persistent No Match marker was written\. "\n            f"\{_format_stage_decisions\(decision_details\)\}",\n        \)\n        return "retry_later"',
    '''        # Provider-level warnings are emitted once at the point of failure.\n        # Per-image Retry Later lines are operational state, not new warnings.\n        log(\n            "INFO",\n            f"Image {iid}: RETRY LATER; no persistent No Match marker was written. "\n            f"{_format_stage_decisions(decision_details)}",\n        )\n        return "retry_later"''',
    "Retry Later severity",
)

text = replace_once(
    text,
    '    global _SAUCENAO_OUTAGE_UNTIL',
    '    global _SAUCENAO_OUTAGE_HITS, _SAUCENAO_OUTAGE_STREAK',
    "import_all SauceNAO outage globals",
)
text = replace_once(
    text,
    '    _SAUCENAO_OUTAGE_UNTIL = 0.0',
    '    _SAUCENAO_OUTAGE_HITS = 0\n    _SAUCENAO_OUTAGE_STREAK = 0',
    "import_all SauceNAO outage reset",
)
text = replace_once(
    text,
    '        "saucenao_rate_limit_hits": 0,',
    '        "saucenao_rate_limit_hits": 0,\n        "saucenao_outage_hits": 0,',
    "SauceNAO outage metric",
)
text = replace_once(
    text,
    '    stats["saucenao_rate_limit_hits"] = _SAUCENAO_RATE_LIMIT_HITS',
    '    stats["saucenao_rate_limit_hits"] = _SAUCENAO_RATE_LIMIT_HITS\n    stats["saucenao_outage_hits"] = _SAUCENAO_OUTAGE_HITS',
    "SauceNAO outage final metric",
)
MAIN.write_text(text, encoding="utf-8")

# Manifest and docs.
text = MANIFEST.read_text(encoding="utf-8")
text = replace_once(text, 'version: 3.26.7', 'version: 3.26.8', "manifest version")
MANIFEST.write_text(text, encoding="utf-8")

for path in (ROOT_README, PLUGIN_README):
    text = path.read_text(encoding="utf-8").replace("v3.26.7", "v3.26.8")
    text = text.replace("3.26.7", "3.26.8")
    text = text.replace(
        "SauceNAO outage cooldowns are announced once rather than repeated as provider warnings for every skipped image.",
        "SauceNAO HTTP 520–524 failures never disable the provider for later images: the next image still searches SauceNAO after a short adaptive 5–20 second wait, and repeated identical outage warnings are suppressed.",
    )
    text = text.replace(
        "- SauceNAO outage cooldowns are announced once; subsequent cooldown-only Retry Later image lines are informational",
        "- SauceNAO HTTP 520–524 never disables later image searches; each new image retries after a short adaptive 5–20 second wait, with repeated outage warnings suppressed",
    )
    text = text.replace(
        "- SauceNAO HTTP 520–524: 180-second SauceNAO-only outage cooldown while other providers continue",
        "- SauceNAO HTTP 520–524: 5–20 second adaptive wait before the next SauceNAO image search; the provider remains enabled",
    )
    text = text.replace(
        "SauceNAO outage cooldowns are announced once; subsequent cooldown-only Retry Later image lines are informational",
        "SauceNAO 52x outages are announced once; every later image remains eligible after a short adaptive wait",
    )
    path.write_text(text, encoding="utf-8")

TEST.write_text('''import importlib.util\nimport io\nimport pathlib\nimport sys\nimport unittest\nimport urllib.error\nfrom unittest import mock\n\nPLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]\nif str(PLUGIN_DIR) not in sys.path:\n    sys.path.insert(0, str(PLUGIN_DIR))\n\nspec = importlib.util.spec_from_file_location("plugin_sauce_resilience", PLUGIN_DIR / "DanbooruTagImporter.py")\nplugin = importlib.util.module_from_spec(spec)\nassert spec.loader is not None\nspec.loader.exec_module(plugin)\n\n\nclass FakeResponse:\n    def __init__(self, body: bytes):\n        self.body = body\n    def __enter__(self):\n        return self\n    def __exit__(self, exc_type, exc, tb):\n        return False\n    def read(self):\n        return self.body\n\n\nclass SauceNaoResilienceTests(unittest.TestCase):\n    def setUp(self):\n        plugin._SAUCENAO_DAILY_EXHAUSTED = False\n        plugin._SAUCENAO_DISABLED_REASON = ""\n        plugin._SAUCENAO_PAUSE_UNTIL = 0.0\n        plugin._SAUCENAO_OUTAGE_HITS = 0\n        plugin._SAUCENAO_OUTAGE_STREAK = 0\n        plugin._PROVIDER_WARNING_ONCE.clear()\n\n    def test_521_does_not_disable_saucenao_for_later_images(self):\n        with mock.patch.object(plugin, "log"):\n            delay = plugin._saucenao_note_outage(521)\n        self.assertEqual(delay, 5.0)\n        self.assertFalse(plugin._saucenao_is_disabled())\n        self.assertEqual(plugin._SAUCENAO_OUTAGE_HITS, 1)\n\n    def test_repeated_521_warns_once_and_uses_short_adaptive_backoff(self):\n        logs = []\n        with mock.patch.object(plugin, "log", side_effect=lambda level, message: logs.append((level, message))):\n            first = plugin._saucenao_note_outage(521)\n            second = plugin._saucenao_note_outage(521)\n            third = plugin._saucenao_note_outage(521)\n        self.assertEqual((first, second, third), (5.0, 10.0, 20.0))\n        warnings = [message for level, message in logs if level == "WARNING"]\n        self.assertEqual(len(warnings), 1)\n        self.assertIn("keeping SauceNAO enabled", warnings[0])\n\n    def test_next_image_retries_saucenao_after_521(self):\n        first_error = urllib.error.HTTPError(\n            plugin.SAUCENAO_BASE, 521, "Web server is down", {}, io.BytesIO(b"down")\n        )\n        payload = (\n            b'{"header":{"status":0,"short_limit":10,"short_remaining":9,'\n            b'"long_limit":200,"long_remaining":199},"results":[]}'\n        )\n        with mock.patch.object(plugin, "_saucenao_wait_for_slot", return_value=0.0), \\\n             mock.patch.object(plugin, "log"), \\\n             mock.patch.object(plugin.HTTP, "urlopen", side_effect=[first_error, FakeResponse(payload)]) as urlopen:\n            with self.assertRaises(RuntimeError):\n                plugin.saucenao_resolve(b"image", "key", 95.0, "", "", "", "", "", "")\n            self.assertFalse(plugin._saucenao_is_disabled())\n            result = plugin.saucenao_resolve(b"image2", "key", 95.0, "", "", "", "", "", "")\n        self.assertIsNone(result)\n        self.assertEqual(urlopen.call_count, 2)\n        self.assertEqual(plugin._SAUCENAO_OUTAGE_HITS, 1)\n        self.assertEqual(plugin._SAUCENAO_OUTAGE_STREAK, 0)\n\n\nif __name__ == "__main__":\n    unittest.main()\n''', encoding="utf-8")

print("Applied SauceNAO resilient fallback 3.26.8 patch")
