import http.client
import json
import secrets
import socket
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

from command_service import CommandService
from conversion_core import canonical_amount_string
from local_api import (
    MAX_BODY_BYTES,
    LocalAPIConfigurationError,
    LocalAPIPortInUseError,
    LocalAPIServer,
    create_test_server,
)
from secret_store import SecretStore


class APIExactConverter:
    def convert_exact(self, amount, source, target):
        del source, target
        return canonical_amount_string(Decimal(str(amount)) * Decimal("2.5"))


class APIQuoteService:
    def __init__(self):
        self.requests = []
        self.lock = threading.Lock()

    def quote(self, request, *, cancel=None):
        with self.lock:
            self.requests.append((request, cancel))
        return {
            "provider": None if request.provider == "auto" else request.provider,
            "status": "ok",
            "data_state": "live",
            "fiat": request.fiat,
            "asset": request.asset,
            "direction": request.direction.value,
            "input_amount": request.amount,
            "market_best_price": "7.12",
            "warnings": ["资格与广告状态需再次确认。"],
        }

    def capabilities(self):
        return (
            {
                "provider": "binance",
                "enabled": True,
                "configured": True,
                "read_only": True,
            },
        )


class BlockingQuoteService(APIQuoteService):
    def __init__(self):
        super().__init__()
        self.release = threading.Event()
        self.two_active = threading.Event()
        self.active = 0
        self.max_active = 0

    def quote(self, request, *, cancel=None):
        with self.lock:
            self.requests.append((request, cancel))
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            if self.active >= 2:
                self.two_active.set()
        try:
            deadline = time.monotonic() + 2
            while not self.release.wait(0.01):
                if cancel is not None and cancel.is_set():
                    break
                if time.monotonic() >= deadline:
                    break
            return super().quote(request, cancel=cancel)
        finally:
            with self.lock:
                self.active -= 1


class CancellationQuoteService(APIQuoteService):
    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.cancel_observed = threading.Event()

    def quote(self, request, *, cancel=None):
        self.started.set()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if cancel is not None and cancel.is_set():
                self.cancel_observed.set()
                break
            time.sleep(0.005)
        return super().quote(request, cancel=cancel)


class LocalAPITestCase(unittest.TestCase):
    def setUp(self):
        self.token = secrets.token_urlsafe(32)
        self.quote_service = APIQuoteService()
        self.command_service = CommandService(APIExactConverter(), self.quote_service)

        def verify(candidate):
            return secrets.compare_digest(candidate, self.token)

        self.verifier = verify
        self.server = create_test_server(
            self.command_service,
            self.verifier,
            rate_limit=1000,
            request_timeout=2,
        )
        self.server.start()
        self.addCleanup(self.server.stop)

    def request(
        self,
        method,
        path,
        *,
        body=None,
        headers=None,
        authenticated=True,
        parse_json=True,
    ):
        request_headers = {
            "Host": f"127.0.0.1:{self.server.port}",
        }
        if authenticated:
            request_headers["Authorization"] = f"Bearer {self.token}"
        if isinstance(body, dict):
            body = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        if headers:
            request_headers.update(headers)
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=4)
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        raw = response.read().decode("utf-8", "replace")
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        connection.close()
        self.assertFalse(self.token in raw, "响应不得包含认证秘密")
        payload = json.loads(raw) if parse_json and raw else None
        return response.status, response_headers, payload, raw

    def test_health_is_unauthenticated_minimal_and_uses_rfc3339_utc(self):
        status, headers, payload, _raw = self.request(
            "GET", "/health", authenticated=False
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["api_version"], "v1")
        self.assertEqual(
            set(payload["data"]),
            {"version", "service_status", "utc_time"},
        )
        self.assertRegex(payload["data"]["utc_time"], r"^\d{4}-\d\d-\d\dT.*Z$")
        self.assertNotIn("capabilities", payload["data"])
        self.assertEqual(headers["cache-control"], "no-store, max-age=0")
        self.assertEqual(headers["x-content-type-options"], "nosniff")

    def test_missing_malformed_and_wrong_auth_are_uniform_401(self):
        missing = self.request(
            "GET", "/v1/capabilities", authenticated=False
        )[2]
        malformed = self.request(
            "GET",
            "/v1/capabilities",
            authenticated=False,
            headers={"Authorization": "Basic invalid"},
        )[2]
        wrong = secrets.token_urlsafe(32)
        wrong_status, wrong_headers, wrong_payload, _raw = self.request(
            "GET",
            "/v1/capabilities",
            authenticated=False,
            headers={"Authorization": f"Bearer {wrong}"},
        )
        self.assertEqual(wrong_status, 401)
        self.assertEqual(missing["error"], malformed["error"])
        self.assertEqual(missing["error"], wrong_payload["error"])
        self.assertEqual(missing["error"]["code"], "unauthorized")
        self.assertIn("Bearer", wrong_headers["www-authenticate"])

    def test_capabilities_truthfully_report_offline_okx(self):
        status, _headers, payload, _raw = self.request("GET", "/v1/capabilities")
        providers = payload["data"]["capabilities"]["c2c"]["providers"]
        self.assertEqual(status, 200)
        self.assertTrue(providers["binance"]["configured"])
        self.assertFalse(providers["okx"]["configured"])

        offline = CommandService().capabilities()
        self.assertFalse(offline["conversion"]["available"])
        self.assertFalse(offline["c2c"]["available"])

    def test_calculate_matches_direct_service_result(self):
        direct = self.command_service.calculate("(12.5+7)*3")
        status, _headers, payload, _raw = self.request(
            "POST", "/v1/calculate", body={"expression": "(12.5+7)*3"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["source"], direct.source)
        self.assertEqual(payload["data"], direct.data)
        self.assertEqual(payload["data"]["result"], "58.5")

    def test_convert_matches_exact_direct_service_and_uses_strings(self):
        direct = self.command_service.convert("1.20", "CNY", "USD")
        status, _headers, payload, _raw = self.request(
            "POST",
            "/v1/convert",
            body={"amount": "1.20", "source": "CNY", "target": "USD"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"], direct.data)
        self.assertEqual(payload["data"]["amount"], "1.2")
        self.assertEqual(payload["data"]["value"], "3")
        self.assertIsInstance(payload["data"]["value"], str)

    def test_command_matches_direct_service_and_c2c_has_non_guarantee_warning(self):
        text = "兑换 0.02 BTC CNY --mode c2c --provider auto"
        direct = self.command_service.execute(text, request_id="direct")
        status, _headers, payload, _raw = self.request(
            "POST", "/v1/command", body={"command": text}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["source"], direct.source)
        self.assertIn("不保证", " ".join(payload["warnings"]))
        self.assertIsInstance(payload["data"]["quote"]["input_amount"], str)

    def test_host_must_be_loopback_with_actual_port(self):
        status, _headers, payload, _raw = self.request(
            "GET",
            "/health",
            authenticated=False,
            headers={"Host": "example.invalid"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_host")

    def test_any_origin_is_rejected_and_no_cors_headers_are_returned(self):
        status, headers, payload, _raw = self.request(
            "GET",
            "/health",
            authenticated=False,
            headers={"Origin": f"http://127.0.0.1:{self.server.port}"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "origin_forbidden")
        self.assertFalse(any(key.startswith("access-control-allow") for key in headers))

    def test_content_type_is_exact_and_amount_must_be_string(self):
        body = json.dumps({"expression": "1+1"}).encode("utf-8")
        status, _headers, payload, _raw = self.request(
            "POST", "/v1/calculate", body=body
        )
        self.assertEqual(status, 415)
        self.assertEqual(payload["error"]["code"], "unsupported_media_type")

        status, _headers, payload, _raw = self.request(
            "POST",
            "/v1/calculate",
            body=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        self.assertEqual(status, 415)

        status, _headers, payload, _raw = self.request(
            "POST",
            "/v1/convert",
            body={"amount": 1, "source": "CNY", "target": "USD"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_field_type")

    def test_body_limit_is_enforced_before_json_parsing(self):
        oversized = b"{" + b" " * MAX_BODY_BYTES + b"}"
        status, _headers, payload, _raw = self.request(
            "POST",
            "/v1/calculate",
            body=oversized,
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 413)
        self.assertEqual(payload["error"]["code"], "payload_too_large")

    def test_duplicate_keys_nonfinite_numbers_and_non_object_json_are_rejected(self):
        cases = (
            ('{"expression":"1","expression":"2"}', "duplicate_json_key"),
            ('{"expression":NaN}', "invalid_json"),
            ('["expression","1"]', "json_object_required"),
        )
        for raw, code in cases:
            with self.subTest(code=code):
                status, _headers, payload, _body = self.request(
                    "POST",
                    "/v1/calculate",
                    body=raw,
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["code"], code)

    def test_unknown_fields_are_rejected(self):
        status, _headers, payload, _raw = self.request(
            "POST",
            "/v1/calculate",
            body={"expression": "1+1", "extra": "rejected"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "unknown_field")

    def test_unknown_path_traversal_and_methods_have_stable_statuses(self):
        status, _headers, payload, _raw = self.request("GET", "/missing")
        self.assertEqual((status, payload["error"]["code"]), (404, "not_found"))

        status, _headers, payload, _raw = self.request("GET", "/v1/%2e%2e/health")
        self.assertEqual((status, payload["error"]["code"]), (400, "invalid_path"))

        status, headers, payload, _raw = self.request("GET", "/v1/calculate")
        self.assertEqual((status, payload["error"]["code"]), (405, "method_not_allowed"))
        self.assertEqual(headers["allow"], "POST")

        status, _headers, payload, _raw = self.request("PROPFIND", "/health")
        self.assertEqual((status, payload["error"]["code"]), (405, "method_not_allowed"))

    def test_rate_limit_defaults_to_local_window_and_returns_retry_after(self):
        limited = create_test_server(
            self.command_service,
            self.verifier,
            rate_limit=2,
            rate_window_seconds=60,
        )
        limited.start()
        self.addCleanup(limited.stop)
        original = self.server
        self.server = limited
        try:
            self.assertEqual(self.request("GET", "/health", authenticated=False)[0], 200)
            self.assertEqual(self.request("GET", "/health", authenticated=False)[0], 200)
            status, headers, payload, _raw = self.request(
                "GET", "/health", authenticated=False
            )
            self.assertEqual(status, 429)
            self.assertEqual(payload["error"]["code"], "rate_limited")
            self.assertGreaterEqual(int(headers["retry-after"]), 1)
        finally:
            self.server = original

    def test_access_log_contains_only_safe_metadata(self):
        records = []
        logged_event = threading.Event()

        def capture(record):
            records.append(record)
            logged_event.set()

        logged = create_test_server(
            self.command_service,
            self.verifier,
            rate_limit=100,
            access_logger=capture,
        )
        logged.start()
        self.addCleanup(logged.stop)
        original = self.server
        self.server = logged
        try:
            command = "/fx 987654.321 CNY USD"
            self.request("POST", "/v1/command", body={"command": command})
        finally:
            self.server = original
        self.assertTrue(logged_event.wait(1))
        self.assertEqual(len(records), 1)
        rendered = repr(records[0])
        self.assertEqual(records[0].path, "/v1/command")
        self.assertFalse(self.token in rendered)
        self.assertNotIn("987654", rendered)
        self.assertNotIn("Authorization", rendered)

    def test_lifecycle_is_idempotent_daemonized_and_restartable(self):
        first_port = self.server.port
        self.assertEqual(self.server.start(), first_port)
        self.assertTrue(self.server.is_running)
        self.assertTrue(self.server._thread.daemon)
        self.assertTrue(self.server.stop())
        self.assertFalse(self.server.stop())
        self.assertFalse(self.server.is_running)
        second_port = self.server.start()
        self.assertGreater(second_port, 0)
        self.assertEqual(self.request("GET", "/health", authenticated=False)[0], 200)

    def test_secret_store_verifier_integrates_without_response_disclosure(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SecretStore(Path(directory) / "auth.json")
            issued = store.generate()
            integrated = create_test_server(CommandService(), store, rate_limit=100)
            integrated.start()
            self.addCleanup(integrated.stop)
            connection = http.client.HTTPConnection("127.0.0.1", integrated.port, timeout=3)
            connection.request(
                "GET",
                "/v1/capabilities",
                headers={
                    "Host": f"127.0.0.1:{integrated.port}",
                    "Authorization": f"Bearer {issued}",
                },
            )
            response = connection.getresponse()
            raw = response.read().decode("utf-8")
            connection.close()
            self.assertEqual(response.status, 200)
            self.assertFalse(issued in raw, "响应不得包含认证秘密")
            integrated.stop()


class LocalAPIConcurrencyTests(unittest.TestCase):
    @staticmethod
    def request(server, token, command):
        body = json.dumps({"command": command}).encode("utf-8")
        connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        connection.request(
            "POST",
            "/v1/command",
            body=body,
            headers={
                "Host": f"127.0.0.1:{server.port}",
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, raw

    def test_c2c_never_exceeds_two_concurrent_operations(self):
        token = secrets.token_urlsafe(32)
        quotes = BlockingQuoteService()
        service = CommandService(APIExactConverter(), quotes)
        server = create_test_server(
            service,
            lambda candidate: secrets.compare_digest(candidate, token),
            rate_limit=100,
            request_timeout=3,
        )
        server.start()
        self.addCleanup(server.stop)
        commands = [f"/c2c {index} CNY USDT" for index in (100, 200, 300)]
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(self.request, server, token, command) for command in commands]
            self.assertTrue(quotes.two_active.wait(1.5))
            time.sleep(0.05)
            self.assertEqual(quotes.max_active, 2)
            quotes.release.set()
            results = [future.result(timeout=4) for future in futures]
        self.assertTrue(all(status == 200 for status, _raw in results))
        self.assertLessEqual(quotes.max_active, 2)
        self.assertTrue(all(token.encode("ascii") not in raw for _status, raw in results))

    def test_timeout_sets_cancellation_token_for_c2c_service(self):
        token = secrets.token_urlsafe(32)
        quotes = CancellationQuoteService()
        server = create_test_server(
            CommandService(APIExactConverter(), quotes),
            lambda candidate: secrets.compare_digest(candidate, token),
            rate_limit=100,
            request_timeout=0.1,
        )
        server.start()
        self.addCleanup(server.stop)
        status, raw = self.request(server, token, "/c2c 100 CNY USDT")
        payload = json.loads(raw)
        self.assertEqual(status, 504)
        self.assertEqual(payload["error"]["code"], "operation_timeout")
        self.assertTrue(quotes.cancel_observed.wait(1))
        self.assertFalse(token.encode("ascii") in raw)


class LocalAPIConfigurationTests(unittest.TestCase):
    def test_production_rejects_wildcard_and_random_port(self):
        with self.assertRaises(LocalAPIConfigurationError):
            LocalAPIServer(host="0.0.0.0")
        with self.assertRaises(LocalAPIConfigurationError):
            LocalAPIServer(port=0)

    def test_port_conflict_fails_clearly_without_random_fallback(self):
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        server = LocalAPIServer(token_verifier=lambda _candidate: False, port=port)
        try:
            with self.assertRaises(LocalAPIPortInUseError) as caught:
                server.start()
            self.assertIn(str(port), str(caught.exception))
            self.assertEqual(server.port, port)
            self.assertFalse(server.is_running)
        finally:
            blocker.close()
            server.stop()


if __name__ == "__main__":
    unittest.main()
