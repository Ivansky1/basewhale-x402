"""Bounded USDC large-transfer detection on Base, paid through x402 v2."""

from datetime import datetime, timezone
from decimal import Decimal
import logging
import os
import re

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
import httpx
from pydantic import BaseModel, ConfigDict, Field

from x402_payment import PaidOperation, PaymentGate, configured_payee

PAYEE_ADDRESS = configured_payee()
PRICE_USDC = 0.02
PRICE_ATOMIC = "20000"
CHAIN_ID = "eip155:8453"
USDC_ASSET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
BASE_RPC_URL = os.getenv("BASE_RPC_URL", "https://mainnet.base.org")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
logger = logging.getLogger("basewhale")


class WhaleInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    min_usd: float = Field(25000, ge=0, le=1e15)
    blocks: int = Field(25, ge=1, le=50)
    limit: int = Field(15, ge=1, le=100)


class StatsInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    min_usd: float = Field(25000, ge=0, le=1e15)
    blocks: int = Field(25, ge=1, le=50)


class Transfer(BaseModel):
    tx_hash: str
    block_number: int
    token: str
    amount_atomic: str
    amount_tokens: float
    amount_usd: float
    from_address: str
    to_address: str


class WhaleResponse(BaseModel):
    success: bool
    network: str
    tracked_token: str
    valuation_source: str
    source: str
    fetched_at: str
    min_usd_filter: float
    latest_block: int
    blocks_scanned: int
    whale_transactions_found: int
    total_whale_volume_usd: float
    transactions: list[Transfer]
    timestamp: str


class StatsResponse(BaseModel):
    success: bool
    network: str
    tracked_token: str
    valuation_source: str
    source: str
    fetched_at: str
    latest_block: int
    blocks_scanned: int
    total_whale_transactions: int
    total_whale_volume_usd: float
    largest_single_transfer_usd: float
    average_whale_transfer_usd: float
    volume_category: str
    timestamp: str


def response_schema(model):
    """Inline local model refs so this schema also works inside Bazaar metadata."""
    schema = model.model_json_schema()
    definitions = schema.get("$defs", {})

    def inline(value):
        if isinstance(value, dict):
            if "$ref" in value:
                return inline(definitions[value["$ref"].rsplit("/", 1)[-1]])
            return {key: inline(item) for key, item in value.items() if key != "$defs"}
        return [inline(item) for item in value] if isinstance(value, list) else value

    return inline(schema)


app = FastAPI(title="BaseWhale Oracle x402", version="1.1.0", redirect_slashes=False,
              description="Detects large USDC transfers in a bounded recent Base block range.")

payment = PaymentGate(service="BaseWhale", payee=PAYEE_ADDRESS, amount=PRICE_ATOMIC, operations=[
    PaidOperation(method="GET", path="/v1/whales",
                  description="Returns large USDC transfers from up to 50 recent Base blocks; USD values assume the USDC peg.",
                  input_schema=WhaleInput.model_json_schema(),
                  output_schema=response_schema(WhaleResponse),
                  example={"min_usd": 25000, "blocks": 25, "limit": 15}),
    PaidOperation(method="GET", path="/v1/whale-stats",
                  description="Returns count, aggregate volume, largest transfer and average for recent large USDC transfers.",
                  input_schema=StatsInput.model_json_schema(), output_schema=StatsResponse.model_json_schema(),
                  example={"min_usd": 25000, "blocks": 25}),
])


class UpstreamUnavailable(Exception):
    """The RPC did not return a complete, valid transfer snapshot."""


def upstream_error():
    return JSONResponse(status_code=502, content={"success": False, "error": {
        "code": "upstream_unavailable", "message": "Base transfer data is unavailable."}})


async def _rpc(client, method, params):
    response = await client.post(BASE_RPC_URL, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                                 headers={"User-Agent": "BaseWhale-Oracle/1.1"})
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("error") or "result" not in payload:
        raise UpstreamUnavailable()
    return payload["result"]


async def fetch_onchain_whales(min_usd: float = 25000, num_blocks: int = 25, limit: int = 15) -> dict:
    """Aggregate exact token units before formatting a bounded USDC log snapshot."""
    WhaleInput(min_usd=min_usd, blocks=num_blocks, limit=limit)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0)) as client:
            block_result = await _rpc(client, "eth_blockNumber", [])
            if not isinstance(block_result, str) or not re.fullmatch(r"0x[0-9a-fA-F]+", block_result):
                raise UpstreamUnavailable()
            latest = int(block_result, 16)
            first = max(latest - num_blocks + 1, 0)
            logs = await _rpc(client, "eth_getLogs", [{"fromBlock": hex(first), "toBlock": hex(latest),
                                                      "address": USDC_ASSET, "topics": [TRANSFER_TOPIC]}])
        if not isinstance(logs, list):
            raise UpstreamUnavailable()
        whales = []
        total_atomic = largest_atomic = 0
        for event in logs:
            if not isinstance(event, dict):
                raise UpstreamUnavailable()
            if event.get("removed") is True:
                continue
            topics = event.get("topics")
            if (not isinstance(topics, list) or len(topics) != 3 or topics[0] != TRANSFER_TOPIC
                or any(not isinstance(topic, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", topic) for topic in topics)
                or any(topic[2:26] != "0" * 24 for topic in topics[1:])
                or not re.fullmatch(r"0x[0-9a-fA-F]{64}", str(event.get("data", "")))
                or not re.fullmatch(r"0x[0-9a-fA-F]{64}", str(event.get("transactionHash", "")))
                or not re.fullmatch(r"0x[0-9a-fA-F]+", str(event.get("blockNumber", "")))
                or event.get("address", "").lower() != USDC_ASSET.lower()):
                raise UpstreamUnavailable()
            block = int(event["blockNumber"], 16)
            if not first <= block <= latest:
                raise UpstreamUnavailable()
            atomic = int(event["data"], 16)
            value = Decimal(atomic) / Decimal(1_000_000)
            if value < Decimal(str(min_usd)):
                continue
            total_atomic += atomic
            largest_atomic = max(largest_atomic, atomic)
            whales.append({"tx_hash": event["transactionHash"], "block_number": block, "token": "USDC",
                           "amount_atomic": str(atomic), "amount_tokens": float(value), "amount_usd": round(float(value), 2),
                           "from_address": "0x" + topics[1][-40:], "to_address": "0x" + topics[2][-40:]})
        whales.sort(key=lambda event: int(event["amount_atomic"]), reverse=True)
        count = len(whales)
        return {"latest_block": latest, "blocks_scanned": latest - first + 1,
                "total_whale_volume_usd": round(float(Decimal(total_atomic) / 1_000_000), 2),
                "largest_single_transfer_usd": round(float(Decimal(largest_atomic) / 1_000_000), 2),
                "average_whale_transfer_usd": round(float(Decimal(total_atomic) / 1_000_000 / count), 2) if count else 0.0,
                "whale_count": count, "transactions": whales[:limit], "source": "base_rpc_eth_getLogs",
                "valuation_source": "stablecoin_peg_assumption", "fetched_at": datetime.now(timezone.utc).isoformat()}
    except (httpx.HTTPError, ValueError, TypeError, AttributeError, KeyError, UpstreamUnavailable) as exc:
        logger.warning("upstream_request_failed", extra={"service": "BaseWhale", "upstream": "base_rpc"})
        raise UpstreamUnavailable() from exc


@app.get("/", summary="API index")
async def root():
    return {"service": "BaseWhale Oracle", "version": "1.1.0", "network": "Base Mainnet (8453)",
            "docs": "/docs", "openapi": "/openapi.json", "manifest": "/.well-known/x402",
            "endpoints": {"whales": "/v1/whales", "stats": "/v1/whale-stats"},
            "price_usd": "$0.02", "payee": PAYEE_ADDRESS, "status": "online"}


@app.get("/health")
async def health():
    return {"status": "healthy", "chain_id": 8453, "payee": PAYEE_ADDRESS}


@app.get("/.well-known/x402")
async def manifest(request: Request):
    return payment.manifest(request)


async def whale_data(inputs: WhaleInput):
    try:
        data = await fetch_onchain_whales(inputs.min_usd, inputs.blocks, inputs.limit)
    except UpstreamUnavailable:
        return upstream_error()
    return {"success": True, "network": "Base Mainnet (8453)", "tracked_token": "USDC",
            "min_usd_filter": inputs.min_usd, "latest_block": data["latest_block"],
            "blocks_scanned": data["blocks_scanned"], "whale_transactions_found": data["whale_count"],
            "total_whale_volume_usd": data["total_whale_volume_usd"], "transactions": data["transactions"],
            "source": data["source"], "valuation_source": data["valuation_source"], "fetched_at": data["fetched_at"],
            "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/v1/whales", response_model=WhaleResponse)
async def get_whales(min_usd: float = Query(25000, ge=0, le=1e15, allow_inf_nan=False),
                     blocks: int = Query(25, ge=1, le=50), limit: int = Query(15, ge=1, le=100)):
    return await whale_data(WhaleInput(min_usd=min_usd, blocks=blocks, limit=limit))


@app.post("/v1/whales", include_in_schema=False)
async def post_whales(inputs: WhaleInput):
    return await whale_data(inputs)


async def stats_data(inputs: StatsInput):
    try:
        data = await fetch_onchain_whales(inputs.min_usd, inputs.blocks, 1)
    except UpstreamUnavailable:
        return upstream_error()
    return {"success": True, "network": "Base Mainnet (8453)", "tracked_token": "USDC",
            "latest_block": data["latest_block"], "blocks_scanned": data["blocks_scanned"],
            "total_whale_transactions": data["whale_count"], "total_whale_volume_usd": data["total_whale_volume_usd"],
            "largest_single_transfer_usd": data["largest_single_transfer_usd"],
            "average_whale_transfer_usd": data["average_whale_transfer_usd"],
            "volume_category": "high" if data["total_whale_volume_usd"] > 1_000_000 else "normal",
            "source": data["source"], "valuation_source": data["valuation_source"], "fetched_at": data["fetched_at"],
            "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/v1/whale-stats", response_model=StatsResponse)
async def get_whale_stats(min_usd: float = Query(25000, ge=0, le=1e15, allow_inf_nan=False),
                          blocks: int = Query(25, ge=1, le=50)):
    return await stats_data(StatsInput(min_usd=min_usd, blocks=blocks))


@app.post("/v1/whale-stats", include_in_schema=False)
async def post_whale_stats(inputs: StatsInput):
    return await stats_data(inputs)


@app.get("/self-test", summary="Configuration diagnostics; no live data request")
async def self_test():
    return {"status": "configuration_only", "payee_configured": PAYEE_ADDRESS,
            "facilitator_configured": bool(payment.facilitator.url),
            "live_upstream_tested": False, "tracked_token": "USDC"}


payment.install(app)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8005)
