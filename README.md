# BaseWhale Oracle (x402 v2)

Detects **large USDC transfers** in a bounded recent Base Mainnet block range. Each paid request costs **0.02 USDC** (`20000` atomic units).

## What the data means

- Fetches the latest block and canonical USDC `Transfer` logs from Base RPC. Each request scans 1–50 blocks, with both ends fixed to the same snapshot.
- Returns transfers, their aggregate volume, count, largest value and average. Aggregate statistics include every qualifying transfer, even when the displayed list is limited.
- **USDC only.** WETH scanning and wallet/smart-money classification are not implemented.
- USD amounts assume **1 USDC = 1 USD**, identified as `valuation_source: "stablecoin_peg_assumption"`. This is not a live USDC/USD price feed. `amount_atomic` preserves the exact USDC units; display amounts are rounded.
- `source: "base_rpc_eth_getLogs"` and `fetched_at` identify provenance. Recent blocks may be reorganized. This is a per-request snapshot, not a continuous event stream.
- RPC errors, malformed results and timeouts return HTTP 502 with `success: false` and `error.code: "upstream_unavailable"`. A failed RPC request never becomes a successful empty transfer list.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/v1/whales` | Large transfers; `min_usd=25000`, `blocks=25`, `limit=15` |
| GET | `/v1/whale-stats` | Aggregate large-transfer statistics; `min_usd=25000`, `blocks=25` |

`min_usd` must be finite and between 0 and 1e15; `blocks` is 1–50; `limit` is 1–100. Hidden POST compatibility routes accept the same parameters in a JSON object and use the same payment gate. HEAD receives a challenge without running transfer scans. Only the two canonical GET operations are advertised.

Public routes: `/`, `/health`, `/docs`, `/openapi.json`, `/.well-known/x402`. `/self-test` reports configuration only (`live_upstream_tested: false`); it does not expose paid transfer data or claim live connectivity.

## Payment configuration

| Setting | Value |
|---|---|
| Protocol | x402 v2, `exact`, EIP-3009 |
| Network | `eip155:8453` (Base Mainnet) |
| Asset | `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` |
| Default payee | `0xb5aFc89b57Fa8270bB7261348179D28099BEa2a0` |
| Price | `20000` atomic units = 0.02 USDC |

Environment variables:

- `PAYEE_ADDRESS`: override the payee consistently in runtime, challenges and discovery.
- `X402_FACILITATOR_URL`: **required for paid operation**; configure a trusted HTTPS facilitator that supports x402 v2 Base Mainnet USDC. There is no implicit mainnet facilitator and no acceptance of unverified signatures when it is missing.
- `X402_PUBLIC_BASE_URL`: optional fixed public deployment URL for consistent resource URLs behind proxies.
- `X402_FACILITATOR_BEARER_TOKEN`: optional operator-provisioned token for facilitators accepting bearer authentication. Automatic CDP JWT generation/refresh is not implemented.
- `BASE_RPC_URL`: optional trusted operator-controlled Base RPC URL; defaults to `https://mainnet.base.org`. Public request parameters cannot choose an upstream URL.

## x402 request flow

1. Request the resource without a payment. HTTP 402 contains the canonical challenge as base64 JSON in `PAYMENT-REQUIRED`, with a machine-readable error body.
2. An x402 v2 client creates the EIP-3009 authorization for the offered network, asset, exact price and payee, then retries with base64 JSON in **`PAYMENT-SIGNATURE`**.
3. The local payment module validates the payload and expected requirements, including resource metadata, and asks the configured facilitator to `/verify` it. Only successful verification permits the RPC scan.
4. A successful business response is buffered while the facilitator `/settle` call completes. Only successful settlement releases the paid JSON with **`PAYMENT-RESPONSE`**.

`Authorization`, `Payment-Receipt`, `Payment-Response`, `x-payment-response` and `X-PAYMENT` are not accepted as payment proof. No legacy header compatibility is enabled. Verification/settlement failures never return paid success. Business errors are not settled. Both canonical response headers are exposed through CORS.

Unpaid discovery example:

```bash
curl -i 'http://localhost:8005/v1/whales?min_usd=25000'
```

Use the [official Python client examples](https://github.com/x402-foundation/x402/tree/main/examples/python/clients) to sign the returned challenge. Do not substitute an arbitrary string or a settlement receipt for a signed payment.

### Protocol and operational limits

- EIP-3009 signs transfer authorization fields, **not the resource URL**. The server checks the submitted resource URL and requirements, but does not claim cryptographic URI binding. Settled authorizations cannot be reused because the on-chain authorization nonce is consumed; settlement must succeed before any paid result is released.
- Multiple instances can perform duplicate upstream work before one settlement wins. No persistent result cache or payment retry recovery is added here. A lost connection after settlement can mean payment occurred without client delivery; check the transaction before retrying with a new authorization.
- Facilitator trust, supported-chain configuration, deployment origin/proxy setup and upstream availability remain deployment responsibilities. Unit tests mock all external services and spend no USDC; they do not establish live facilitator or on-chain settlement readiness.
- `/.well-known/x402` is a service discovery manifest. Canonical challenges carry the official Bazaar extension; OpenAPI includes per-operation payment details. Listing/acceptance by external crawlers is not guaranteed.

References: [x402 v2 specification](https://github.com/x402-foundation/x402/blob/main/specs/x402-specification-v2.md), [HTTP transport](https://github.com/x402-foundation/x402/blob/main/specs/transports-v2/http.md), [Bazaar extension](https://github.com/x402-foundation/x402/blob/main/specs/extensions/bazaar.md).

## Run and test

```bash
python -m pip install -r requirements.txt
python -m pip install pytest
python -m uvicorn server:app --host 0.0.0.0 --port 8005
python -m pytest -q
python -m compileall -q .
```

Tests include `test_fake_header_does_not_unlock_resource` across canonical and hidden compatibility methods, mocked payment verification/settlement, malformed payment payloads, transfer parsing, exact block bounds, statistics beyond the response limit and explicit upstream failures. No funded wallet is required. Existing `vercel.json` remains the deployment entry point.

## License

MIT

## Deployment verification notes

The payment dependency is pinned to official `x402==2.23.0`. Tests use a mocked
facilitator: they prove the local payment boundary, not live settlement. Configure
`X402_FACILITATOR_URL` to a trusted HTTPS facilitator that supports exact payments
on `eip155:8453`. No mainnet facilitator is assumed. If the provider requires
short-lived CDP JWTs, supply a maintained authentication adapter/token provisioning;
this service does not generate CDP JWTs automatically.

`X402_PUBLIC_BASE_URL` should be the deployed HTTPS origin. A settlement timeout
can be ambiguous after broadcast: reconcile the authorization/chain before paying
again. The local nonce guard is bounded and process-local; on-chain authorization
consumption and facilitator verification remain necessary across replicas/restarts.
No distributed rate limiter or durable payment/recovery database is introduced.
Request bodies are limited to 64 KiB, buffered responses to 2 MiB. HEAD is discovery
only (402); it never verifies, executes a paid operation, or settles.

CI runs the entire pytest suite on Python 3.12, including
`test_fake_header_does_not_unlock_resource`, on pushes and pull requests. This
workflow must be enabled and configured as a required check in repository hosting
to block merges; merely adding the workflow does not change branch protections.
