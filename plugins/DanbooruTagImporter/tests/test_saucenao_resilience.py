import importlib.util
import io
import pathlib
import sys
import unittest
import urllib.error
from unittest import mock

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

spec = importlib.util.spec_from_file_location("plugin_sauce_resilience", PLUGIN_DIR / "DanbooruTagImporter.py")
plugin = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(plugin)


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc, tb):
        return False
    def read(self):
        return self.body


class SauceNaoResilienceTests(unittest.TestCase):
    def setUp(self):
        plugin._SAUCENAO_DAILY_EXHAUSTED = False
        plugin._SAUCENAO_DISABLED_REASON = ""
        plugin._SAUCENAO_PAUSE_UNTIL = 0.0
        plugin._SAUCENAO_OUTAGE_HITS = 0
        plugin._SAUCENAO_OUTAGE_STREAK = 0
        plugin._PROVIDER_WARNING_ONCE.clear()

    def test_521_does_not_disable_saucenao_for_later_images(self):
        with mock.patch.object(plugin, "log"):
            delay = plugin._saucenao_note_outage(521)
        self.assertEqual(delay, 5.0)
        self.assertFalse(plugin._saucenao_is_disabled())
        self.assertEqual(plugin._SAUCENAO_OUTAGE_HITS, 1)

    def test_repeated_521_warns_once_and_uses_short_adaptive_backoff(self):
        logs = []
        with mock.patch.object(plugin, "log", side_effect=lambda level, message: logs.append((level, message))):
            first = plugin._saucenao_note_outage(521)
            second = plugin._saucenao_note_outage(521)
            third = plugin._saucenao_note_outage(521)
        self.assertEqual((first, second, third), (5.0, 10.0, 20.0))
        warnings = [message for level, message in logs if level == "WARNING"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("keeping SauceNAO enabled", warnings[0])

    def test_next_image_retries_saucenao_after_521(self):
        first_error = urllib.error.HTTPError(
            plugin.SAUCENAO_BASE, 521, "Web server is down", {}, io.BytesIO(b"down")
        )
        payload = (
            b'{"header":{"status":0,"short_limit":10,"short_remaining":9,'
            b'"long_limit":200,"long_remaining":199},"results":[]}'
        )
        with mock.patch.object(plugin, "_saucenao_wait_for_slot", return_value=0.0), \
             mock.patch.object(plugin, "log"), \
             mock.patch.object(plugin.HTTP, "urlopen", side_effect=[first_error, FakeResponse(payload)]) as urlopen:
            with self.assertRaises(RuntimeError):
                plugin.saucenao_resolve(b"image", "key", 95.0, "", "", "", "", "", "")
            self.assertFalse(plugin._saucenao_is_disabled())
            result = plugin.saucenao_resolve(b"image2", "key", 95.0, "", "", "", "", "", "")
        self.assertIsNone(result)
        self.assertEqual(urlopen.call_count, 2)
        self.assertEqual(plugin._SAUCENAO_OUTAGE_HITS, 1)
        self.assertEqual(plugin._SAUCENAO_OUTAGE_STREAK, 0)


    def test_http_500_html_body_is_clean_and_next_image_retries(self):
        html = (
            b'<!DOCTYPE html><html><head><meta http-equiv="Content-Type" '
            b'content="text/html; charset=utf-8"></head><body>server error</body></html>'
        )
        first_error = urllib.error.HTTPError(
            plugin.SAUCENAO_BASE,
            500,
            "Internal Server Error",
            {},
            io.BytesIO(html),
        )
        payload = (
            b'{"header":{"status":0,"short_limit":10,"short_remaining":9,'
            b'"long_limit":200,"long_remaining":199},"results":[]}'
        )
        logs = []
        with mock.patch.object(plugin, "_saucenao_wait_for_slot", return_value=0.0), \
             mock.patch.object(
                 plugin,
                 "log",
                 side_effect=lambda level, message: logs.append((level, message)),
             ), \
             mock.patch.object(
                 plugin.HTTP,
                 "urlopen",
                 side_effect=[first_error, FakeResponse(payload)],
             ) as urlopen:
            with self.assertRaises(RuntimeError) as raised:
                plugin.saucenao_resolve(
                    b"image", "key", 95.0, "", "", "", "", "", ""
                )
            self.assertIn("HTTP 500", str(raised.exception))
            self.assertNotIn("<html", str(raised.exception).casefold())
            self.assertFalse(plugin._saucenao_is_disabled())

            result = plugin.saucenao_resolve(
                b"image2", "key", 95.0, "", "", "", "", "", ""
            )

        self.assertIsNone(result)
        self.assertEqual(urlopen.call_count, 2)
        self.assertEqual(plugin._SAUCENAO_OUTAGE_HITS, 1)
        rendered_logs = "\n".join(message for _level, message in logs)
        self.assertNotIn("<html", rendered_logs.casefold())
        self.assertIn("SauceNAO HTTP 500", rendered_logs)


if __name__ == "__main__":
    unittest.main()
