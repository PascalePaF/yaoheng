from __future__ import annotations

import json
import unittest
from collections import deque
from pathlib import Path

import requests

from c2c.base import (
    CircuitBreaker,
    CircuitOpenError,
    HttpResponse,
    ProviderProtocolError,
    ProviderRateLimitError,
    ProviderUnavailableError,
    ResilientJsonClient,
    ResponseTooLargeError,
    TokenBucket,
)
from c2c.binance import (
    BINANCE_AD_LIST_PATH,
    BINANCE_QUOTE_PATH,
    BINANCE_TRADE_METHODS_PATH,
    BinanceP2PAdapter,
    parse_binance_ads,
    parse_binance_quote_price,
    parse_binance_trade_methods,
)
from c2c.models import Direction, QuoteRequest, QuoteStatus
from c2c.okx import (
    OkxApprovedRequest,
    OkxP2PAdapter,
    OkxP2PConfig,
    OkxResponseSchema,
    parse_okx_ads,
)


FIXTURES = Path(__file__).parent / "fixtures" / "c2c"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fixture_json(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeTransport:
    def __init__(self, *responses) -> None:
        self.responses = deque(responses)
        self.calls: list[dict[str, object]] = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            raise AssertionError("unexpected transport request")
        item = self.responses.popleft()
        if isinstance(item, BaseException):
            raise item
        return item


def response(body: bytes = b'{"code":"0","data":[]}', status: int = 200, **headers):
    return HttpResponse(status, headers, body)


def resilient_client(transport: FakeTransport, clock: FakeClock | None = None):
    clock = clock or FakeClock()
    return ResilientJsonClient(
        transport=transport,
        clock=clock,
        sleeper=clock.sleep,
        jitter=lambda: 0.150,
        limiter=TokenBucket(clock=clock, sleeper=clock.sleep),
        breaker=CircuitBreaker(clock=clock),
    )


class BinanceAdapterTests(unittest.TestCase):
    def test_official_fixture_parsers_normalize_without_merchant_identity(self):
        request = QuoteRequest("CNY", "USDT", "BUY", "1000", asset_step="0.01")

        ads = parse_binance_ads(fixture_json("binance_ads.json"), request)
        methods = parse_binance_trade_methods(fixture_json("binance_trade_methods.json"))
        quote = parse_binance_quote_price(fixture_json("binance_quote.json"))

        self.assertEqual([ad.ad_id for ad in ads], ["fixture-binance-a", "fixture-binance-b"])
        self.assertEqual(ads[0].completion_rate, "99")
        self.assertEqual(ads[0].asset_step, "0.01")
        self.assertEqual([item.identifier for item in methods], [
            "FIXTURE_BANK", "FIXTURE_WALLET"
        ])
        self.assertEqual(quote, "7.18")
        self.assertNotIn("advertiser", json.dumps(ads[0].to_dict()))

    def test_payment_identifiers_are_resolved_then_sent_as_plain_query_values(self):
        transport = FakeTransport(
            response(fixture_bytes("binance_trade_methods.json")),
            response(fixture_bytes("binance_ads.json")),
        )
        adapter = BinanceP2PAdapter(client=resilient_client(transport))
        request = QuoteRequest(
            "CNY",
            "USDT",
            "BUY",
            "1000",
            payment_methods=("FIXTURE_BANK",),
            asset_step="0.01",
        )

        result = adapter.quote(request)

        self.assertEqual(result.status, QuoteStatus.OK)
        self.assertEqual(result.market_best_price, "7.1")
        self.assertEqual(result.match.ad.ad_id, "fixture-binance-b")  # type: ignore[union-attr]
        self.assertTrue(str(transport.calls[0]["url"]).endswith(BINANCE_TRADE_METHODS_PATH))
        self.assertTrue(str(transport.calls[1]["url"]).endswith(BINANCE_AD_LIST_PATH))
        self.assertIn(("tradeMethodIdentifiers", "FIXTURE_BANK"), transport.calls[1]["params"])
        self.assertNotIn('["FIXTURE_BANK"]', repr(transport.calls[1]["params"]))

    def test_unknown_payment_identifier_returns_no_match_without_ad_request(self):
        transport = FakeTransport(response(fixture_bytes("binance_trade_methods.json")))
        adapter = BinanceP2PAdapter(client=resilient_client(transport))
        request = QuoteRequest(
            "CNY", "USDT", "BUY", "1000", payment_methods=("NOT_RETURNED",)
        )

        result = adapter.quote(request)

        self.assertEqual(result.status, QuoteStatus.NO_MATCH)
        self.assertEqual(len(transport.calls), 1)

    def test_trade_methods_use_24_hour_memory_cache_and_quote_endpoint_is_read_only(self):
        transport = FakeTransport(
            response(fixture_bytes("binance_trade_methods.json")),
            response(fixture_bytes("binance_quote.json")),
        )
        clock = FakeClock()
        adapter = BinanceP2PAdapter(client=resilient_client(transport, clock), clock=clock)

        first = adapter.list_trade_methods("CNY")
        second = adapter.list_trade_methods("CNY")
        price = adapter.fetch_quote_price("CNY", "USDT", Direction.BUY)

        self.assertIs(first, second)
        self.assertEqual(price, "7.18")
        self.assertEqual(len(transport.calls), 2)
        self.assertTrue(str(transport.calls[1]["url"]).endswith(BINANCE_QUOTE_PATH))
        self.assertEqual({call["method"] for call in transport.calls}, {"GET"})

    def test_all_malformed_or_negative_ads_fail_closed(self):
        payload = {
            "code": "000000",
            "success": True,
            "data": [{
                "adv": {
                    "advNo": "bad",
                    "price": "NaN",
                    "surplusAmount": "-1",
                    "minSingleTransAmount": "0",
                }
            }],
        }
        with self.assertRaises(ProviderProtocolError):
            parse_binance_ads(payload, QuoteRequest("CNY", "USDT", "BUY", "100"))

    def test_current_public_ad_list_schema_is_accepted(self):
        """The live public Skill API uses *TransAmount/tradableAmount names."""

        payload = {
            "code": "000000",
            "success": True,
            "data": [{
                "adNo": "current-schema-a",
                "price": "6.68",
                "fiat": "CNY",
                "fiatScale": 2,
                "asset": "USDT",
                "assetScale": 2,
                "minTransAmount": "100",
                "maxTransAmount": "50000",
                "tradableAmount": "1000.00",
                "tradeMethods": [{"identifier": "BANK", "tradeMethodName": "银行卡"}],
                "advertiser": {
                    "monthFinishRate": "0.995",
                    "monthOrderCount": 123,
                },
            }],
        }

        ads = parse_binance_ads(payload, QuoteRequest("CNY", "USDT", "BUY", "1000"))

        self.assertEqual(len(ads), 1)
        self.assertEqual(ads[0].ad_id, "current-schema-a")
        self.assertEqual(ads[0].min_fiat, "100")
        self.assertEqual(ads[0].max_fiat, "50000")
        self.assertEqual(ads[0].available_asset, "1000")
        self.assertEqual(ads[0].effective_max_fiat, "6680")
        self.assertEqual(ads[0].completion_rate, "99.5")


class OkxAdapterTests(unittest.TestCase):
    @staticmethod
    def approved_contract() -> OkxApprovedRequest:
        return OkxApprovedRequest(
            path="/approved-fixture/p2p/ads",
            method="POST",
            asset_parameter="assetCode",
            fiat_parameter="fiatCode",
            direction_parameter="sideCode",
            buy_value="B",
            sell_value="S",
            payment_parameter="paymentCode",
            payment_mode="single",
            limit_parameter="pageSize",
            limit_value="20",
        )

    @staticmethod
    def schema() -> OkxResponseSchema:
        return OkxResponseSchema(list_path=("data", "ads"))

    def test_default_and_incomplete_okx_are_stably_unconfigured_or_denied(self):
        request = QuoteRequest("CNY", "USDT", "BUY", "1000", provider="okx")
        self.assertEqual(OkxP2PAdapter().quote(request).status, QuoteStatus.UNCONFIGURED)

        config = OkxP2PConfig(
            enabled=True,
            whitelisted=False,
            approved_request=self.approved_contract(),
            response_schema=self.schema(),
        )
        denied = OkxP2PAdapter(
            config,
            credentials_provider=lambda: {
                "api_key": "fixture-key",
                "secret_key": "fixture-signing-value",
                "passphrase": "fixture-passphrase",
            },
        ).quote(request)
        self.assertEqual(denied.status, QuoteStatus.PERMISSION_DENIED)
        self.assertIn("白名单", denied.warnings[0])

    def test_whitelisted_fixture_is_signed_and_parsed_through_configured_boundary(self):
        transport = FakeTransport(response(fixture_bytes("okx_ads.json")))
        config = OkxP2PConfig(
            enabled=True,
            whitelisted=True,
            approved_request=self.approved_contract(),
            response_schema=self.schema(),
        )
        adapter = OkxP2PAdapter(
            config,
            credentials_provider=lambda: {
                "api_key": "fixture-key",
                "secret_key": "fixture-signing-value",
                "passphrase": "fixture-passphrase",
            },
            client=resilient_client(transport),
            timestamp_provider=lambda: "2026-01-01T00:00:00.000Z",
        )
        request = QuoteRequest(
            "CNY", "USDT", "BUY", "1000", provider="okx",
            payment_methods=("FIXTURE_BANK",), asset_step="0.01"
        )

        result = adapter.quote(request)

        self.assertEqual(result.status, QuoteStatus.OK)
        self.assertEqual(result.provider, "okx")
        self.assertEqual(result.match.ad.ad_id, "fixture-okx-a")  # type: ignore[union-attr]
        call = transport.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], "https://openapi.okx.com/approved-fixture/p2p/ads")
        self.assertIn(b'"paymentCode":"FIXTURE_BANK"', call["body"])
        self.assertEqual(call["headers"]["OK-ACCESS-TIMESTAMP"], "2026-01-01T00:00:00.000Z")
        public_config = json.dumps(config.to_dict(), ensure_ascii=False)
        self.assertNotIn("fixture-key", public_config)
        self.assertNotIn("fixture-signing-value", public_config)

    def test_okx_parser_rejects_unknown_direction_and_all_bad_rows(self):
        payload = fixture_json("okx_ads.json")
        payload["data"]["ads"][0]["side"] = "UNKNOWN"
        with self.assertRaises(ProviderProtocolError):
            parse_okx_ads(
                payload,
                QuoteRequest("CNY", "USDT", "BUY", "1000"),
                schema=self.schema(),
                contract=self.approved_contract(),
            )

    def test_okx_rejects_non_official_credential_destination(self):
        with self.assertRaises(ValueError):
            OkxP2PConfig(
                enabled=True,
                whitelisted=True,
                base_url="https://example.invalid",
                approved_request=self.approved_contract(),
            )
        with self.assertRaises(ValueError):
            OkxP2PConfig(enabled="false")  # type: ignore[arg-type]


class NetworkProtectionTests(unittest.TestCase):
    def test_connection_failure_and_502_retry_once_for_idempotent_request(self):
        for first in (requests.ConnectionError("offline"), response(status=502)):
            with self.subTest(first=type(first).__name__):
                transport = FakeTransport(first, response())
                payload = resilient_client(transport).request_json("GET", "https://example.test")
                self.assertEqual(str(payload["code"]), "0")
                self.assertEqual(len(transport.calls), 2)

    def test_non_idempotent_request_does_not_retry(self):
        transport = FakeTransport(response(status=503), response())
        with self.assertRaises(ProviderUnavailableError):
            resilient_client(transport).request_json("POST", "https://example.test")
        self.assertEqual(len(transport.calls), 1)

    def test_timeout_retries_once_then_reports_unavailable(self):
        transport = FakeTransport(requests.Timeout("slow"), requests.Timeout("slow"))
        with self.assertRaises(ProviderUnavailableError):
            resilient_client(transport).request_json("GET", "https://example.test")
        self.assertEqual(len(transport.calls), 2)

    def test_429_honors_retry_after_opens_circuit_and_allows_half_open_probe(self):
        clock = FakeClock()
        transport = FakeTransport(response(status=429, **{"Retry-After": "12"}), response())
        client = resilient_client(transport, clock)

        with self.assertRaises(ProviderRateLimitError) as limited:
            client.request_json("GET", "https://example.test")
        self.assertEqual(limited.exception.retry_after_seconds, 12)
        with self.assertRaises(CircuitOpenError):
            client.request_json("GET", "https://example.test")
        self.assertEqual(len(transport.calls), 1)

        clock.advance(12)
        self.assertEqual(str(client.request_json("GET", "https://example.test")["code"]), "0")
        self.assertEqual(client.breaker.state, "closed")

    def test_429_without_valid_retry_after_uses_sixty_seconds(self):
        transport = FakeTransport(response(status=429))
        with self.assertRaises(ProviderRateLimitError) as limited:
            resilient_client(transport).request_json("GET", "https://example.test")
        self.assertEqual(limited.exception.retry_after_seconds, 60)

    def test_repeated_gateway_failures_open_then_half_open_the_circuit(self):
        clock = FakeClock()
        transport = FakeTransport(
            response(status=503), response(status=503), response(status=503), response()
        )
        client = resilient_client(transport, clock)
        with self.assertRaises(ProviderUnavailableError):
            client.request_json("GET", "https://example.test")
        with self.assertRaises(CircuitOpenError):
            client.request_json("GET", "https://example.test")
        self.assertEqual(client.breaker.state, "open")
        self.assertEqual(len(transport.calls), 3)

        clock.advance(60)
        self.assertEqual(str(client.request_json("GET", "https://example.test")["code"]), "0")
        self.assertEqual(client.breaker.state, "closed")

    def test_local_rate_protection_is_one_request_per_second_after_burst_two(self):
        clock = FakeClock()
        transport = FakeTransport(response(), response(), response())
        client = resilient_client(transport, clock)
        client.request_json("GET", "https://example.test/one")
        client.request_json("GET", "https://example.test/two")
        self.assertEqual(clock.now, 0)
        client.request_json("GET", "https://example.test/three")
        self.assertGreaterEqual(clock.now, 1.0)

    def test_response_size_nonfinite_json_and_invalid_content_length_fail_closed(self):
        cases = (
            (response(b"x" * (1024 * 1024 + 1)), ResponseTooLargeError),
            (response(b'{"value":NaN}'), ProviderProtocolError),
            (response(b"{}", **{"Content-Length": "not-a-number"}), ProviderProtocolError),
        )
        for raw_response, expected in cases:
            with self.subTest(expected=expected.__name__), self.assertRaises(expected):
                resilient_client(FakeTransport(raw_response)).request_json(
                    "GET", "https://example.test"
                )

    def test_network_timeouts_are_bounded_to_required_connect_read_and_total_values(self):
        transport = FakeTransport(response())
        resilient_client(transport).request_json("GET", "https://example.test")
        call = transport.calls[0]
        self.assertGreaterEqual(call["connect_timeout"], 2.0)
        self.assertLessEqual(call["connect_timeout"], 3.0)
        self.assertLessEqual(call["read_timeout"], 5.0)
        self.assertLessEqual(call["total_timeout"], 6.0)


if __name__ == "__main__":
    unittest.main()
