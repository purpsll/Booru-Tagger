import importlib.util
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
        plugin._E621_IQDB_LAST_REQUEST_AT = 0.0
        plugin._E621_IQDB_BACKOFF_UNTIL = 0.0
        plugin._E621_IQDB_CONSECUTIVE_RATE_LIMITS = 0
        plugin._E621_IQDB_RECOVERY_INTERVAL_SECONDS = 0.0

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

    def test_authenticated_iqdb_pacing_uses_three_second_floor(self):
        plugin._E621_IQDB_LAST_REQUEST_AT = 100.0
        with mock.patch.object(plugin.time, "monotonic", side_effect=[100.5, 103.0]), \
             mock.patch.object(plugin.time, "sleep") as sleeper:
            interval = plugin._e621_iqdb_wait_for_slot("user", "key")

        self.assertEqual(interval, plugin.E621_IQDB_AUTH_MIN_INTERVAL_SECONDS)
        self.assertEqual(plugin.E621_IQDB_AUTH_MIN_INTERVAL_SECONDS, 3.0)
        sleeper.assert_called_once_with(2.5)
        self.assertEqual(plugin._E621_IQDB_LAST_REQUEST_AT, 103.0)

    def test_anonymous_iqdb_pacing_uses_sixty_five_second_floor(self):
        self.assertEqual(
            plugin._e621_iqdb_min_interval("", ""),
            plugin.E621_IQDB_ANON_MIN_INTERVAL_SECONDS,
        )
        self.assertGreaterEqual(plugin.E621_IQDB_ANON_MIN_INTERVAL_SECONDS, 60.0)

    def test_e621_429_respects_retry_after_without_disabling_next_image(self):
        error = __import__("urllib.error").error.HTTPError(
            "https://e621.net/iqdb_queries.json", 429, "rate", {"Retry-After": "7"}, None
        )
        with mock.patch.object(plugin.time, "monotonic", return_value=100.0):
            delay = plugin._e621_iqdb_note_rate_limit(error, "user", "key")

        self.assertEqual(delay, 7.0)
        self.assertEqual(plugin._E621_IQDB_BACKOFF_UNTIL, 107.0)
        self.assertEqual(plugin._E621_IQDB_CONSECUTIVE_RATE_LIMITS, 1)
        self.assertEqual(plugin._E621_IQDB_RECOVERY_INTERVAL_SECONDS, 7.0)

        plugin._e621_iqdb_note_success("user", "key")
        self.assertEqual(plugin._E621_IQDB_BACKOFF_UNTIL, 0.0)
        self.assertEqual(plugin._E621_IQDB_CONSECUTIVE_RATE_LIMITS, 0)
        self.assertEqual(plugin._E621_IQDB_RECOVERY_INTERVAL_SECONDS, 6.0)

    def test_successful_iqdb_requests_decay_recovery_floor_gradually(self):
        plugin._E621_IQDB_RECOVERY_INTERVAL_SECONDS = 6.0

        plugin._e621_iqdb_note_success("user", "key")
        self.assertEqual(plugin._E621_IQDB_RECOVERY_INTERVAL_SECONDS, 5.0)
        plugin._e621_iqdb_note_success("user", "key")
        self.assertEqual(plugin._E621_IQDB_RECOVERY_INTERVAL_SECONDS, 4.0)
        plugin._e621_iqdb_note_success("user", "key")
        self.assertEqual(plugin._E621_IQDB_RECOVERY_INTERVAL_SECONDS, 0.0)


if __name__ == "__main__":
    unittest.main()
