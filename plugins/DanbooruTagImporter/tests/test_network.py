import io
import unittest
import urllib.error
import urllib.request
from unittest import mock

from network import HTTPClient, CircuitOpenError, is_retryable_exception


class DummyResponse:
    def __init__(self, body=b"{}"):
        self.body = body
    def read(self):
        return self.body
    def close(self):
        pass
    def __enter__(self):
        return self
    def __exit__(self, *args):
        self.close()


class NetworkTests(unittest.TestCase):
    def test_429_is_retried_then_succeeds(self):
        client = HTTPClient()
        client.configure(default_min_interval=0, max_retries=1, backoff_base=0.05)
        request = urllib.request.Request("https://example.test/value")
        error = urllib.error.HTTPError(
            request.full_url, 429, "rate", {"Retry-After": "0"}, io.BytesIO(b"rate")
        )
        with mock.patch("network.urllib.request.urlopen", side_effect=[error, DummyResponse()]):
            with mock.patch("network.time.sleep"):
                response = client.urlopen(request)
        self.assertIsInstance(response, DummyResponse)

    def test_retryable_exception_detects_wrapped_429(self):
        try:
            try:
                raise urllib.error.HTTPError("https://x", 429, "rate", {}, None)
            except urllib.error.HTTPError as exc:
                raise RuntimeError("wrapped") from exc
        except RuntimeError as wrapped:
            self.assertTrue(is_retryable_exception(wrapped))

    def test_nonretryable_403_is_not_retryable(self):
        exc = urllib.error.HTTPError("https://x", 403, "forbidden", {}, None)
        self.assertFalse(is_retryable_exception(exc))

    def test_circuit_opens_after_threshold(self):
        client = HTTPClient()
        client.configure(
            default_min_interval=0, max_retries=0,
            circuit_threshold=2, circuit_cooldown=60,
        )
        request = urllib.request.Request("https://example.test/value")
        failure = urllib.error.URLError("offline")
        with mock.patch("network.urllib.request.urlopen", side_effect=[failure, failure]):
            with self.assertRaises(urllib.error.URLError):
                client.urlopen(request)
            with self.assertRaises(urllib.error.URLError):
                client.urlopen(request)
        with self.assertRaises(CircuitOpenError):
            client.urlopen(request)


if __name__ == "__main__":
    unittest.main()

class Rule34CooldownNetworkTests(unittest.TestCase):
    def test_configured_429_opens_host_cooldown_without_sleeping_or_retrying(self):
        client = HTTPClient()
        client.configure(default_min_interval=0, max_retries=5, backoff_base=0.05)
        client.set_host_rate_limit_cooldown("api.rule34.xxx", 600)
        request = urllib.request.Request("https://api.rule34.xxx/index.php?page=dapi")
        error = urllib.error.HTTPError(
            request.full_url, 429, "rate", {"Retry-After": "60"}, io.BytesIO(b"rate")
        )
        with mock.patch("network.urllib.request.urlopen", side_effect=error) as upstream, \
             mock.patch("network.time.sleep") as sleeper:
            with self.assertRaises(urllib.error.HTTPError):
                client.urlopen(request)
            self.assertEqual(upstream.call_count, 1)
            sleeper.assert_not_called()
            self.assertGreater(client.cooldown_remaining("api.rule34.xxx"), 590)
            with self.assertRaises(CircuitOpenError):
                client.urlopen(request)
            self.assertEqual(upstream.call_count, 1)
