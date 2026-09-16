import base64
import json
import time
import unittest
import uuid
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
import httpx
from pydantic import ValidationError

import server
from x402_payment import encode_header

REAL_ASYNC_CLIENT = httpx.AsyncClient


def signed_headers(client, path, method="GET"):
    """Dummy authorization used only with mocked facilitator methods."""
    challenge = json.loads(base64.b64decode(client.request(method, path).headers["PAYMENT-REQUIRED"]))
    payload = {"x402Version": 2, "resource": challenge["resource"], "accepted": challenge["accepts"][0],
               "payload": {"signature": "0x" + "11" * 65, "authorization": {
                   "from": "0x" + "1" * 40, "to": server.PAYEE_ADDRESS, "value": server.PRICE_ATOMIC,
                   "validAfter": str(int(time.time()) - 10), "validBefore": str(int(time.time()) + 55),
                   "nonce": "0x" + uuid.uuid4().hex + uuid.uuid4().hex}}}
    return {"PAYMENT-SIGNATURE": encode_header(payload)}


def transfer(amount=30000, block=100, index=1):
    return {"address": server.USDC_ASSET, "topics": [server.TRANSFER_TOPIC,
            "0x" + "0" * 24 + "1" * 40, "0x" + "0" * 24 + "2" * 40],
            "data": "0x" + format(amount * 1_000_000, "064x"),
            "transactionHash": "0x" + format(index, "064x"), "blockNumber": hex(block)}


class TestPublicAndPayment(unittest.TestCase):
    def test_self_test_reports_configuration_without_network_or_credentials(self):
        client = TestClient(server.app)
        facilitator = server.payment.facilitator
        with patch.object(facilitator, "bearer_token", "diagnostic-fixture-secret"), \
             patch.object(facilitator, "verify", new_callable=AsyncMock) as verify, \
             patch.object(facilitator, "settle", new_callable=AsyncMock) as settle:
            for url in ("", "https://diagnostic-facilitator.invalid"):
                with self.subTest(configured=bool(url)), patch.object(facilitator, "url", url):
                    response = client.get("/self-test")
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()["facilitator_configured"], bool(url))
                    self.assertNotIn("diagnostic-facilitator", response.text)
                    self.assertNotIn("diagnostic-fixture-secret", response.text)
            verify.assert_not_awaited()
            settle.assert_not_awaited()

    def setUp(self):
        self.client = TestClient(server.app)

    def test_public_endpoints(self):
        for path in ("/", "/health", "/.well-known/x402", "/openapi.json"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)
        self.assertEqual(self.client.get("/").json()["payee"], server.PAYEE_ADDRESS)

    def test_only_two_canonical_discovery_resources(self):
        resources = self.client.get("/.well-known/x402").json()["resources"]
        self.assertEqual(len(resources), 2)
        self.assertEqual({item["method"] for item in resources}, {"GET"})
        paths = self.client.get("/openapi.json").json()["paths"]
        for path in ("/v1/whales", "/v1/whale-stats"):
            self.assertEqual(set(paths[path]), {"get"})
            self.assertIn("402", paths[path]["get"]["responses"])
            self.assertIn("parameters", paths[path]["get"])

    def test_unpaid_challenge(self):
        for path in ("/v1/whales", "/v1/whale-stats"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 402)
            challenge = json.loads(base64.b64decode(response.headers["PAYMENT-REQUIRED"]))
            self.assertEqual(challenge["x402Version"], 2)
            expected = challenge["accepts"][0]
            self.assertEqual(expected["network"], server.CHAIN_ID)
            self.assertEqual(expected["asset"], server.USDC_ASSET)
            self.assertEqual(expected["amount"], "20000")
            self.assertEqual(expected["payTo"], server.PAYEE_ADDRESS)

    def test_fake_header_does_not_unlock_resource(self):
        """A header string must never unlock any canonical or compatibility route."""
        with patch("server.fetch_onchain_whales", new_callable=AsyncMock) as business:
            for header in ("Payment-Response", "x-payment-response", "Authorization", "Payment-Receipt", "X-PAYMENT"):
                for path in ("/v1/whales", "/v1/whale-stats"):
                    for method in ("GET", "POST", "HEAD"):
                        with self.subTest(header=header, path=path, method=method):
                            response = self.client.request(method, path, headers={header: "hello"}, json={} if method == "POST" else None)
                            self.assertEqual(response.status_code, 402)
                            self.assertNotIn("PAYMENT-RESPONSE", response.headers)
            business.assert_not_awaited()

    def test_malformed_payment_signature_never_calls_business(self):
        with patch("server.fetch_onchain_whales", new_callable=AsyncMock) as business:
            for value in ("hello", "e30=", "bnVsbA=="):
                response = self.client.get("/v1/whales", headers={"PAYMENT-SIGNATURE": value})
                self.assertEqual(response.status_code, 400)
            business.assert_not_awaited()

    def test_self_test_does_not_expose_paid_data(self):
        with patch("server.fetch_onchain_whales", new_callable=AsyncMock) as business:
            response = self.client.get("/self-test")
            self.assertEqual(response.json()["status"], "configuration_only")
            self.assertFalse(response.json()["live_upstream_tested"])
            business.assert_not_awaited()

    def test_verified_payment_with_failed_upstream_never_settles(self):
        with patch.object(server.payment.facilitator, "verify", AsyncMock(return_value={"isValid": True})) as verify, \
             patch.object(server.payment.facilitator, "settle", new_callable=AsyncMock) as settle, \
             patch("server.fetch_onchain_whales", AsyncMock(side_effect=server.UpstreamUnavailable())) as business:
            for path in ("/v1/whales", "/v1/whale-stats"):
                for method in ("GET", "POST"):
                    with self.subTest(path=path, method=method):
                        headers = signed_headers(self.client, path, method)
                        response = self.client.request(method, path, headers=headers, json={} if method == "POST" else None)
                        self.assertEqual(response.status_code, 502)
                        self.assertFalse(response.json()["success"])
                        self.assertEqual(response.json()["error"]["code"], "upstream_unavailable")
                        self.assertNotIn("PAYMENT-RESPONSE", response.headers)
            self.assertEqual(verify.await_count, 4)
            self.assertEqual(business.await_count, 4)
            settle.assert_not_awaited()

    def test_invalid_paid_input_is_sanitized_422_without_upstream_or_settlement(self):
        with patch.object(server.payment.facilitator, "verify", AsyncMock(return_value={"isValid": True})), \
             patch.object(server.payment.facilitator, "settle", new_callable=AsyncMock) as settle, \
             patch("server.fetch_onchain_whales", new_callable=AsyncMock) as business:
            for body in ('{"min_usd":NaN}', '{"min_usd":Infinity}', '{"blocks":51}', '{"limit":-1}', '{broken'):
                with self.subTest(body=body):
                    headers = signed_headers(self.client, "/v1/whales", "POST")
                    response = self.client.post("/v1/whales", headers={**headers, "Content-Type": "application/json"}, content=body)
                    self.assertEqual(response.status_code, 422)
                    self.assertEqual(response.json()["error"]["code"], "invalid_request")
                    self.assertNotIn("PAYMENT-RESPONSE", response.headers)
            path = "/v1/whales?min_usd=NaN"
            response = self.client.get(path, headers=signed_headers(self.client, path))
            self.assertEqual(response.status_code, 422)
            business.assert_not_awaited()
            settle.assert_not_awaited()

    def test_bounded_finite_inputs(self):
        for value in (-1, float("nan"), float("inf"), 1e16):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                server.WhaleInput(min_usd=value)
        for field, value in (("blocks", 0), ("blocks", 51), ("limit", 0), ("limit", 101)):
            with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                server.WhaleInput(**{field: value})


class TestWhaleData(unittest.IsolatedAsyncioTestCase):
    async def fetch(self, logs, **kwargs):
        self.requests = []

        def handler(request):
            payload = json.loads(request.content)
            self.requests.append(payload)
            return httpx.Response(200, json={"result": "0x64" if payload["method"] == "eth_blockNumber" else logs})

        client = REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler))
        with patch("server.httpx.AsyncClient", return_value=client):
            return await server.fetch_onchain_whales(**kwargs)

    async def test_transfer_parsing_and_exact_block_range(self):
        data = await self.fetch([transfer(30000), transfer(50000, index=2), transfer(10, index=3)], num_blocks=25)
        self.assertEqual(data["whale_count"], 2)
        self.assertEqual(data["total_whale_volume_usd"], 80000)
        self.assertEqual(data["transactions"][0]["amount_tokens"], 50000)
        self.assertEqual(data["transactions"][0]["from_address"], "0x" + "1" * 40)
        self.assertEqual(data["transactions"][0]["amount_atomic"], "50000000000")
        self.assertEqual(self.requests[1]["params"][0]["fromBlock"], hex(76))
        self.assertEqual(self.requests[1]["params"][0]["toBlock"], hex(100))
        self.assertEqual(data["blocks_scanned"], 25)
        self.assertEqual(data["valuation_source"], "stablecoin_peg_assumption")

    async def test_aggregation_is_not_truncated_by_display_limit(self):
        data = await self.fetch([transfer(30000 + number, index=number) for number in range(60)], limit=1)
        self.assertEqual(len(data["transactions"]), 1)
        self.assertEqual(data["whale_count"], 60)
        self.assertEqual(data["average_whale_transfer_usd"], 30029.5)
        with patch("server.fetch_onchain_whales", AsyncMock(return_value=data)):
            stats = await server.stats_data(server.StatsInput())
        self.assertEqual(stats["total_whale_transactions"], 60)
        self.assertEqual(stats["average_whale_transfer_usd"], 30029.5)

    async def test_zero_transfers_is_valid_data(self):
        data = await self.fetch([])
        self.assertEqual(data["whale_count"], 0)
        self.assertEqual(data["transactions"], [])
        self.assertIn("fetched_at", data)

    async def test_malformed_or_missing_logs_fail(self):
        for logs in (None, {}, [{"data": "0x0"}]):
            with self.subTest(logs=logs), self.assertRaises(server.UpstreamUnavailable):
                await self.fetch(logs)

    async def test_removed_logs_are_not_counted(self):
        event = transfer()
        event["removed"] = True
        self.assertEqual((await self.fetch([event]))["whale_count"], 0)

    async def test_out_of_range_logs_fail(self):
        with self.assertRaises(server.UpstreamUnavailable):
            await self.fetch([transfer(block=10)])

    async def test_rpc_failure_is_not_empty_success(self):
        def handler(request):
            return httpx.Response(200, json={"error": {"message": "secret RPC endpoint detail"}})

        client = REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler))
        with patch("server.httpx.AsyncClient", return_value=client), self.assertRaises(server.UpstreamUnavailable):
            await server.fetch_onchain_whales()
        with patch("server.fetch_onchain_whales", AsyncMock(side_effect=server.UpstreamUnavailable())):
            response = await server.whale_data(server.WhaleInput())
        self.assertEqual(response.status_code, 502)
        self.assertFalse(json.loads(response.body)["success"])
        self.assertNotIn("secret", response.body.decode())

    async def test_http_timeout_is_explicit_upstream_failure(self):
        def handler(request):
            raise httpx.ReadTimeout("secret", request=request)

        client = REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler))
        with patch("server.httpx.AsyncClient", return_value=client), self.assertRaises(server.UpstreamUnavailable):
            await server.fetch_onchain_whales()


if __name__ == "__main__":
    unittest.main()
