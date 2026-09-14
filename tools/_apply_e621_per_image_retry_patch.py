from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def replace_once(path, old, new, label):
    p = ROOT / path
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected 1 exact match, found {count}")
    p.write_text(text.replace(old, new, 1), encoding="utf-8")


def regex_once(path, pattern, repl, label, flags=0):
    p = ROOT / path
    text = p.read_text(encoding="utf-8")
    new, count = re.subn(pattern, repl, text, count=1, flags=flags)
    if count != 1:
        raise SystemExit(f"{label}: expected 1 regex match, found {count}")
    p.write_text(new, encoding="utf-8")


plugin_path = "plugins/DanbooruTagImporter/DanbooruTagImporter.py"
network_path = "plugins/DanbooruTagImporter/network.py"
constants_path = "plugins/DanbooruTagImporter/constants.py"
manifest_path = "plugins/DanbooruTagImporter/DanbooruTagImporter.yml"
task_test_path = "plugins/DanbooruTagImporter/tests/test_task_menu.py"
tech_readme_path = "plugins/DanbooruTagImporter/README.md"
root_readme_path = "README.md"
release_path = ".github/workflows/release.yml"

# e621 IQDB failures are image-local. No run-wide disabled state remains.
replace_once(
    plugin_path,
    "_E621_IQDB_DISABLED_FOR_RUN = False\n_E621_IQDB_RATE_LIMIT_HITS = 0\n",
    "_E621_IQDB_RATE_LIMIT_HITS = 0\n",
    "remove e621 run-disable state",
)

regex_once(
    plugin_path,
    r"\ndef _e621_iqdb_is_disabled\(\) -> bool:\n    return bool\(_E621_IQDB_DISABLED_FOR_RUN\)\n\n\ndef _disable_e621_iqdb_for_run\(reason: str\) -> None:\n    global _E621_IQDB_DISABLED_FOR_RUN\n    if not _E621_IQDB_DISABLED_FOR_RUN:\n        _E621_IQDB_DISABLED_FOR_RUN = True\n        log\(\"WARNING\", f\"Disabling e621 IQDB for remainder of this run: \{reason\}\"\)\n\n",
    "\n",
    "remove e621 run-disable helpers",
)

replace_once(
    plugin_path,
    """        if exc.code == 429 or cloudflare_challenge:\n            _E621_IQDB_RATE_LIMIT_HITS += 1\n            _disable_e621_iqdb_for_run(\n                f\"HTTP {exc.code}\" + (\" / Cloudflare challenge\" if cloudflare_challenge else \"\")\n            )\n            raise RuntimeError(\n                f\"e621 IQDB temporarily unavailable: HTTP {exc.code}\"\n            ) from exc\n""",
    """        if exc.code == 429 or cloudflare_challenge:\n            _E621_IQDB_RATE_LIMIT_HITS += 1\n            reason = f\"HTTP {exc.code}\" + (\n                \" / Cloudflare challenge\" if cloudflare_challenge else \"\"\n            )\n            raise RuntimeError(\n                f\"e621 IQDB temporarily unavailable for this image: {reason}\"\n            ) from exc\n""",
    "make e621 429 image-local",
)

replace_once(
    plugin_path,
    """        run_e621_iqdb = bool(ENABLE_E621 and ENABLE_E621_IQDB)\n        if run_e621_iqdb and _e621_iqdb_is_disabled():\n            visual_outcomes[\"e621\"] = LookupOutcome(\n                \"e621\", \"iqdb\", LookupStatus.UNAVAILABLE, None,\n                \"e621 IQDB disabled for this run after a rate-limit/challenge response\",\n            )\n            run_e621_iqdb = False\n        elif not run_e621_iqdb:\n            decision_details.append(\"e621 IQDB: disabled\")\n""",
    """        run_e621_iqdb = bool(ENABLE_E621 and ENABLE_E621_IQDB)\n        if not run_e621_iqdb:\n            decision_details.append(\"e621 IQDB: disabled\")\n""",
    "always reconsider e621 IQDB per image",
)

replace_once(
    plugin_path,
    """                log(\n                    \"WARNING\",\n                    f\"e621 IQDB not authoritative: {e621_outcome.detail}\",\n                )\n""",
    """                _log_lookup_problem_once(\n                    \"e621 IQDB\",\n                    str(e621_outcome.detail or e621_outcome.status.value),\n                )\n""",
    "dedupe e621 provider warnings",
)

replace_once(
    plugin_path,
    """        log(\n            \"WARNING\",\n            f\"Image {iid}: RETRY LATER; no persistent No Match marker was written. \"\n            f\"{_format_stage_decisions(decision_details)}\",\n        )\n        return \"retry_later\"\n""",
    """        retry_outcomes = [\n            outcome for outcome in outcomes\n            if outcome.status in {LookupStatus.RETRYABLE_ERROR, LookupStatus.UNAVAILABLE}\n        ]\n        only_e621_iqdb_retry = bool(retry_outcomes) and all(\n            outcome.provider == \"e621\" and outcome.stage == \"iqdb\"\n            for outcome in retry_outcomes\n        )\n        log(\n            \"INFO\" if only_e621_iqdb_retry else \"WARNING\",\n            f\"Image {iid}: RETRY LATER; no persistent No Match marker was written. \"\n            f\"{_format_stage_decisions(decision_details)}\",\n        )\n        return \"retry_later\"\n""",
    "lower repeated e621-only retry lines to info",
)

replace_once(
    plugin_path,
    "stats[\"e621_iqdb_disabled_after_429\"] = 1 if _E621_IQDB_DISABLED_FOR_RUN else 0\n",
    "",
    "remove obsolete e621 disabled metric finalizer",
)
replace_once(
    plugin_path,
    "    global _E621_IQDB_DISABLED_FOR_RUN, _E621_IQDB_RATE_LIMIT_HITS\n",
    "    global _E621_IQDB_RATE_LIMIT_HITS\n",
    "remove obsolete e621 disabled global",
)
replace_once(
    plugin_path,
    "    _E621_IQDB_DISABLED_FOR_RUN = False\n    _E621_IQDB_RATE_LIMIT_HITS = 0\n",
    "    _E621_IQDB_RATE_LIMIT_HITS = 0\n",
    "remove obsolete e621 disabled reset",
)
replace_once(
    plugin_path,
    "        \"e621_iqdb_disabled_after_429\": 0,\n",
    "",
    "remove obsolete e621 disabled stat",
)

# Preserve pacing, but clear only failure/circuit state for a host.
replace_once(
    network_path,
    """    def reset(self) -> None:\n        with self._lock:\n            self._states.clear()\n\n    @staticmethod\n    def _host_for(request: urllib.request.Request) -> str:\n""",
    """    def reset(self) -> None:\n        with self._lock:\n            self._states.clear()\n\n    def clear_host_failures(self, host: str) -> None:\n        \"\"\"Clear failure/circuit state for one host while preserving request pacing.\"\"\"\n        host = str(host or \"\").casefold().strip()\n        if not host:\n            return\n        with self._lock:\n            state = self._states.get(host)\n            if state is None:\n                return\n            state.consecutive_failures = 0\n            state.circuit_open_until = 0.0\n\n    @staticmethod\n    def _host_for(request: urllib.request.Request) -> str:\n""",
    "add targeted host failure reset",
)

replace_once(
    plugin_path,
    """def e621_iqdb(\n    image_bytes: bytes,\n    username: str,\n    api_key: str,\n    minimum_score: float,\n    diagnostics: Optional[Dict[str, Any]] = None,\n) -> Optional[Dict[str, Any]]:\n    \"\"\"Reverse-search the existing Stash image against e621's IQDB endpoint.\n\n    The image is uploaded in memory only; no duplicate image is written to disk.\n    \"\"\"\n    boundary = \"----StashE621IQDBBoundary7MA4YWxkTrZu0gW\"\n""",
    """def e621_iqdb(\n    image_bytes: bytes,\n    username: str,\n    api_key: str,\n    minimum_score: float,\n    diagnostics: Optional[Dict[str, Any]] = None,\n) -> Optional[Dict[str, Any]]:\n    \"\"\"Reverse-search the existing Stash image against e621's IQDB endpoint.\n\n    The image is uploaded in memory only; no duplicate image is written to disk.\n    A failure on one image never disables IQDB for the next image in the queue.\n    \"\"\"\n    e621_host = (urllib.parse.urlparse(E621_BASE).hostname or \"e621.net\").casefold()\n    HTTP.clear_host_failures(e621_host)\n    boundary = \"----StashE621IQDBBoundary7MA4YWxkTrZu0gW\"\n""",
    "reset e621 IQDB circuit per image",
)

# Version/public metadata.
replace_once(constants_path, 'VERSION = "3.26.4"', 'VERSION = "3.26.5"', "constants version")
replace_once(manifest_path, "version: 3.26.4", "version: 3.26.5", "manifest version")
replace_once(task_test_path, 'self.assertEqual(yaml_version, "3.26.4")', 'self.assertEqual(yaml_version, "3.26.5")', "task test version")

replace_once(
    manifest_path,
    "description: Two-step image metadata importer for Danbooru, Gelbooru, Rule34, and e621. Fast Scan handles exact matches; Deep Match reuses that result and uses bandwidth-efficient visual search for unresolved images.",
    "description: Two-step image metadata importer for Danbooru, Gelbooru, Rule34, and e621. Fast Scan handles exact matches; Deep Match uses bandwidth-efficient visual search and retries e621 IQDB independently for each image.",
    "manifest description",
)

replace_once(
    root_readme_path,
    "In **v3.26.4**, images already marked **Unresolved** do not repeat the Fast Scan work they already completed. Deep Match goes directly to visual search, uploads a Stash-generated 640px thumbnail instead of the original full-resolution image, and can run e621 IQDB and SauceNAO in parallel after Danbooru IQDB misses. This speeds up large unresolved queues without lowering the existing confidence thresholds.",
    "In **v3.26.5**, images already marked **Unresolved** do not repeat the Fast Scan work they already completed. Deep Match goes directly to visual search, uploads a Stash-generated 640px thumbnail instead of the original full-resolution image, and can run e621 IQDB and SauceNAO in parallel after Danbooru IQDB misses. An e621 IQDB rate-limit or Cloudflare failure applies only to the current image; the next image tries e621 IQDB again. This speeds up large unresolved queues without lowering the existing confidence thresholds.",
    "root README deep-match version paragraph",
)
replace_once(
    root_readme_path,
    "Temporary network/provider failures are designed not to become permanent \"No Match\" decisions.",
    "Temporary network/provider failures are designed not to become permanent \"No Match\" decisions. For e621 IQDB specifically, a 429 or Cloudflare challenge leaves that image eligible for retry but does not disable e621 IQDB for later images in the same queue. Repeated identical e621 provider warnings are de-duplicated, while the per-image result remains visible in the normal log.",
    "root README e621 retry behavior",
)

replace_once(tech_readme_path, "# Booru Importer v3.26.4", "# Booru Importer v3.26.5", "technical README title")
replace_once(
    tech_readme_path,
    "v3.26.4 substantially improves Deep Match throughput. Images already marked Unresolved reuse their completed Fast Scan result instead of repeating MD5 and local pHash lookups. Reverse-image searches upload Stash's generated 640px thumbnail instead of the original full-resolution image. After Danbooru IQDB misses, e621 IQDB and SauceNAO run concurrently, with a 30-second single-attempt timeout so temporary upload/network failures remain Retry Later instead of blocking one image for minutes. SauceNAO Cloudflare/origin 52x errors enter a short provider-only cooldown. Match thresholds and artist mapping behavior are unchanged.",
    "v3.26.5 keeps the Deep Match throughput improvements from v3.26.4 and changes e621 IQDB failure scope. A 429, Cloudflare challenge, timeout, or other transient e621 IQDB failure affects only the current image; the next image starts with e621 IQDB eligible again. The e621 host circuit is cleared per IQDB image while request pacing is preserved, so one bad request cannot disqualify the rest of a long queue. Repeated identical provider warnings are de-duplicated. Match thresholds and artist mapping behavior are unchanged.",
    "technical README release goals",
)
replace_once(tech_readme_path, "the v3.26.4 release workflow", "the v3.26.5 release workflow", "technical README requirements version")
replace_once(tech_readme_path, "For images already marked `Multi-Booru Unresolved` by the normal Fast Scan, v3.26.4 reuses", "For images already marked `Multi-Booru Unresolved` by the normal Fast Scan, v3.26.5 reuses", "technical README deep version")
replace_once(
    tech_readme_path,
    "- After Danbooru IQDB misses, e621 IQDB and SauceNAO may run concurrently with at most 2 visual-search workers\n",
    "- After Danbooru IQDB misses, e621 IQDB and SauceNAO may run concurrently with at most 2 visual-search workers\n- e621 IQDB transient failures are image-local: every new image retries e621 IQDB; no run-wide e621 IQDB disable is retained\n- Repeated identical e621 IQDB provider warnings are logged once per run; e621-only Retry Later image lines are informational rather than warning-level noise\n",
    "technical README fixed policy e621",
)
replace_once(
    tech_readme_path,
    "Transient failures never become persistent negative matches. HTTP 429, temporary 5xx responses, network errors, malformed responses, and circuit-open states keep the current workflow state eligible for retry.",
    "Transient failures never become persistent negative matches. HTTP 429, temporary 5xx responses, network errors, malformed responses, and circuit-open states keep the current workflow state eligible for retry. e621 IQDB is deliberately reconsidered for every image: its failure/circuit state is cleared before each IQDB image request while host pacing remains in force, so a 429 or Cloudflare challenge on one image cannot disable e621 IQDB for the remainder of the queue.",
    "technical README provider reliability",
)

# Add regression coverage as a new test module.
test_path = ROOT / "plugins/DanbooruTagImporter/tests/test_e621_per_image_retry.py"
test_path.write_text(r'''import importlib.util
import pathlib
import sys
import time
import unittest
from unittest import mock

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

spec = importlib.util.spec_from_file_location(
    "plugin_e621_retry", PLUGIN_DIR / "DanbooruTagImporter.py"
)
plugin = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(plugin)

from network import HTTPClient


class FakeStash:
    def image_bytes(self, image_id):
        return b"small-thumbnail"

    def update_image_tags(self, *args, **kwargs):
        pass

    def create_tag(self, name):
        return {"id": "marker", "name": name}


def unresolved_image(image_id):
    return {
        "id": str(image_id),
        "tags": [{"id": "u", "name": plugin.UNRESOLVED_MARKER_TAG}],
        "performers": [],
        "studio": None,
        "date": "",
        "urls": [],
        "files": [{"fingerprints": [
            {"type": "md5", "value": "a" * 32},
            {"type": "phash", "value": "0123456789abcdef"},
        ]}],
    }


class E621PerImageRetryTests(unittest.TestCase):
    def setUp(self):
        plugin._PROVIDER_WARNING_ONCE.clear()

    def _process(self, image_id):
        return plugin.process_image(
            FakeStash(), unresolved_image(image_id), {}, {}, True, {}, {}, {}, {},
            plugin.PHashIndex(), lookup_mode="deep",
        )

    def test_e621_failure_on_one_image_does_not_disable_next_image(self):
        calls = []

        def e621_lookup(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError(
                    "e621 IQDB temporarily unavailable for this image: HTTP 429"
                )
            return None

        with mock.patch.object(plugin, "ENABLE_DANBOORU_IQDB", False), \
             mock.patch.object(plugin, "ENABLE_SAUCENAO", False), \
             mock.patch.object(plugin, "e621_iqdb", side_effect=e621_lookup):
            first = self._process(21)
            second = self._process(22)

        self.assertEqual(first, "retry_later")
        self.assertEqual(second, "no_match")
        self.assertEqual(len(calls), 2)

    def test_repeated_e621_provider_warning_is_deduplicated(self):
        def e621_lookup(*args, **kwargs):
            raise RuntimeError(
                "e621 IQDB temporarily unavailable for this image: HTTP 429"
            )

        with mock.patch.object(plugin, "ENABLE_DANBOORU_IQDB", False), \
             mock.patch.object(plugin, "ENABLE_SAUCENAO", False), \
             mock.patch.object(plugin, "e621_iqdb", side_effect=e621_lookup), \
             mock.patch.object(plugin, "log") as logger:
            self.assertEqual(self._process(23), "retry_later")
            self.assertEqual(self._process(24), "retry_later")

        provider_warnings = [
            call for call in logger.call_args_list
            if "e621 IQDB lookup did not complete authoritatively" in str(call)
        ]
        self.assertEqual(len(provider_warnings), 1)
        image_retry_logs = [
            call for call in logger.call_args_list
            if "RETRY LATER" in str(call)
        ]
        self.assertEqual(len(image_retry_logs), 2)
        self.assertTrue(all(call.args[0] == "INFO" for call in image_retry_logs))

    def test_clear_host_failures_preserves_pacing(self):
        client = HTTPClient()
        host = "e621.net"
        state = client._state(host)
        state.next_allowed_at = time.monotonic() + 5.0
        state.consecutive_failures = 4
        state.circuit_open_until = time.monotonic() + 120.0
        next_allowed = state.next_allowed_at

        client.clear_host_failures(host)

        self.assertEqual(state.consecutive_failures, 0)
        self.assertEqual(state.circuit_open_until, 0.0)
        self.assertEqual(state.next_allowed_at, next_allowed)


if __name__ == "__main__":
    unittest.main()
''', encoding="utf-8")

# Release notes: replace only the current release heredoc.
release = (ROOT / release_path).read_text(encoding="utf-8")
pattern = r"cat > release-current\.md <<'EOF'\n.*?\n          EOF\n\n      - name: Update initial public release notes"
replacement = '''cat > release-current.md <<'EOF'
          ## Booru Importer v3.26.5

          This patch release changes e621 IQDB rate-limit handling so one failed image never disables e621 IQDB for the rest of a Deep Match queue.

          ### e621 IQDB reliability

          - **Every image gets its own e621 IQDB attempt.** HTTP 429, Cloudflare challenge, timeout, or other transient failure affects only the current image.
          - **No run-wide e621 IQDB disable.** The next image retries e621 IQDB normally.
          - **Circuit isolation.** The e621 host's failure/circuit state is cleared before each IQDB image request while normal host pacing is preserved.
          - **No inline upload retries.** A failing e621 IQDB request still uses the bounded single-attempt visual-search policy, so one image cannot stall the queue for minutes.
          - **Cleaner logs.** Repeated identical e621 provider warnings are de-duplicated. Images left Retry Later only because of e621 IQDB are logged at INFO while the first provider warning remains visible.
          - **Conservative state handling remains.** A transient e621 IQDB failure never becomes a false permanent No Match.

          ### Behavior retained

          - Deep Match still reuses completed Fast Scan work for normal Unresolved images.
          - Reverse-search uploads still use Stash's generated 640px thumbnail.
          - Danbooru IQDB remains first; e621 IQDB and SauceNAO may run concurrently after a Danbooru miss.
          - Existing automatic-match and Review thresholds are unchanged.
          - `conditional_dnp` and `third-party_edit` remain ignored as artist metadata.
          - Source-installed users retain the stable internal package ID `DanbooruTagImporter` for Stash's Installed Plugins → Update flow.

          ### Validation

          - Full regression suite includes dedicated e621 per-image retry, warning de-duplication, and host-circuit reset coverage.
          - The release workflow runs the complete suite before packaging.

          ### Recommended installation / update

          Add or refresh this source in **Stash → Settings → Plugins**:

          `https://purpsll.github.io/Booru-Tagger/main/index.yml`

          Existing source-based users should see **Booru Importer 3.26.5** as an update after refreshing plugin sources if needed.

          ### Manual installation from GitHub Releases

          Download **`Booru-Importer-v3.26.5.zip`** from the release Assets, extract the top-level `DanbooruTagImporter` folder, and replace the files in your existing manual installation.
          EOF

      - name: Update initial public release notes'''
new_release, count = re.subn(pattern, replacement, release, count=1, flags=re.S)
if count != 1:
    raise SystemExit(f"release-current block: expected 1 match, found {count}")
(ROOT / release_path).write_text(new_release, encoding="utf-8")

print("Applied e621 per-image retry patch for v3.26.5")
