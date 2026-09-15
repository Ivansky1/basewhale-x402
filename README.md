# BaseWhale Oracle (x402)

Real-Time **On-Chain Whale & Smart Money Transfer Oracle** for **Base Mainnet (Chain ID 8453)**, payable via **x402 micro-payments** ($0.02 USDC).

## Features
- **On-Chain Event Streaming**: Directly scans Base RPC blocks for high-value ERC-20 transfers (USDC, WETH).
- **Whale Metrics**: Real-time identification of transactions >= $25,000 USD, volume aggregation, largest transfers, and average whale sizes.
- **Full x402 Protocol Compliance**: Implements HTTP 402 challenge, `Payment-Required` base64 header, discovery manifest `/.well-known/x402`, and OpenAPI 3.1 with `x-payment-info`.

## Endpoints

### Public Endpoints (Free)
- `GET /` — API catalog & status
- `GET /health` — Service health check
- `GET /.well-known/x402` — Standard x402 discovery manifest
- `GET /docs` — Swagger UI API documentation
- `GET /self-test` — Live test of on-chain event logs

### Paid Endpoints ($0.02 USDC via x402)
- `GET /v1/whales?min_usd=25000` (or `POST`) — Fetch recent whale transfers
- `GET /v1/whale-stats?min_usd=25000` (or `POST`) — Fetch whale volume metrics and statistics

## License
MIT
