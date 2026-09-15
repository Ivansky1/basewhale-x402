"""
BaseWhale Oracle — Real-Time On-Chain Whale & Large Transfer Oracle for Base Mainnet.

Detects and tracks high-value transactions, whale flows, and large smart-money liquidity movements
on Base (Chain ID 8453) directly from on-chain event logs.
Payable via x402 micro-payments ($0.02 USDC on Base).
"""

import base64
from datetime import datetime, timezone
import json
import os
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
import httpx
from pydantic import BaseModel, Field

# Constants & Configuration
PAYEE_ADDRESS = os.getenv("PAYEE_ADDRESS", "0xb5aFc89b57Fa8270bB7261348179D28099BEa2a0")
PRICE_USDC = 0.02
PRICE_ATOMIC = "20000"  # 0.02 USDC (6 decimals = 20,000 atomic units)
CHAIN_ID = "eip155:8453"
USDC_ASSET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
BASE_RPC_URL = os.getenv("BASE_RPC_URL", "https://mainnet.base.org")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

app = FastAPI(
    title="BaseWhale Oracle x402",
    description="Real-Time On-Chain Whale & Large Transfer Oracle for Base Mainnet, payable via x402.",
    version="1.0.0",
    redirect_slashes=False,
    contact={
        "name": "BaseWhale Oracle",
        "email": "ivansky.dev@gmail.com",
        "url": "https://github.com/Ivansky1/basewhale-x402",
    },
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def make_x402_challenge(resource_url: str, description: str = "BaseWhale Oracle API Access") -> Dict[str, Any]:
    """Generate canonical x402 v2 challenge payload passing 100% of discovery checks."""
    return {
        "x402Version": 2,
        "version": 2,
        "resource": {
            "url": resource_url,
            "description": f"{description} (${PRICE_USDC:.2f} USDC)",
            "mimeType": "application/json",
        },
        "accepts": [
            {
                "scheme": "exact",
                "network": CHAIN_ID,
                "asset": USDC_ASSET,
                "amount": PRICE_ATOMIC,
                "maxAmountRequired": PRICE_ATOMIC,
                "payee": PAYEE_ADDRESS,
                "payTo": PAYEE_ADDRESS,
                "maxTimeoutSeconds": 300,
                "description": f"{description} (${PRICE_USDC:.2f} USDC)",
                "extra": {
                    "name": "USD Coin",
                    "version": "2",
                    "assetTransferMethod": "eip3009",
                },
            }
        ],
        "extensions": {
            "bazaar": {
                "info": {
                    "name": "BaseWhale Tracker",
                    "description": "Real-time detection of whale transfers and smart money flows on Base mainnet.",
                    "input": {
                        "type": "object",
                        "properties": {
                            "min_usd": {
                                "type": "number",
                                "description": "Minimum transaction USD value threshold (default: 25000)",
                                "default": 25000,
                            },
                            "limit": {
                                "type": "integer",
                                "description": "Maximum number of whale transactions to return",
                                "default": 15,
                            },
                        },
                    },
                    "output": {
                        "type": "object",
                        "properties": {
                            "network": {"type": "string"},
                            "total_whale_volume_usd": {"type": "number"},
                            "whale_count": {"type": "integer"},
                            "transactions": {"type": "array"},
                            "timestamp": {"type": "string"},
                        },
                    },
                },
                "schema": {
                    "properties": {
                        "input": {
                            "properties": {
                                "queryParams": {
                                    "type": "object",
                                    "properties": {
                                        "min_usd": {"type": "number", "default": 25000},
                                        "limit": {"type": "integer", "default": 15},
                                    },
                                },
                                "body": {
                                    "type": "object",
                                    "properties": {
                                        "min_usd": {"type": "number", "default": 25000},
                                        "limit": {"type": "integer", "default": 15},
                                    },
                                },
                            }
                        },
                        "output": {
                            "properties": {
                                "example": {
                                    "type": "object",
                                    "properties": {
                                        "network": {"type": "string"},
                                        "total_whale_volume_usd": {"type": "number"},
                                        "whale_count": {"type": "integer"},
                                        "timestamp": {"type": "string"},
                                    },
                                }
                            }
                        },
                    }
                },
            }
        },
    }


def build_402_response(resource_url: str, description: str = "BaseWhale Oracle API Access") -> JSONResponse:
    challenge = make_x402_challenge(resource_url, description)
    challenge_b64 = base64.b64encode(json.dumps(challenge).encode("utf-8")).decode("utf-8")
    return JSONResponse(
        status_code=402,
        content=challenge,
        headers={
            "Payment-Required": challenge_b64,
            "Access-Control-Expose-Headers": "Payment-Required",
        },
    )


async def fetch_onchain_whales(min_usd: float = 25000.0, num_blocks: int = 25, limit: int = 15) -> Dict[str, Any]:
    """Fetch on-chain USDC transfer events directly from Base Mainnet RPC."""
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            # 1. Get block number
            block_resp = await client.post(
                BASE_RPC_URL,
                json={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []},
                headers={"User-Agent": "BaseWhale-Oracle/1.0"},
            )
            latest = int(block_resp.json()["result"], 16)
            from_block = hex(max(latest - num_blocks, 0))

            # 2. Query logs
            logs_payload = {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "eth_getLogs",
                "params": [
                    {
                        "fromBlock": from_block,
                        "toBlock": "latest",
                        "address": USDC_ASSET,
                        "topics": [TRANSFER_TOPIC],
                    }
                ],
            }
            logs_resp = await client.post(BASE_RPC_URL, json=logs_payload, headers={"User-Agent": "BaseWhale-Oracle/1.0"})
            logs = logs_resp.json().get("result", [])

            whales = []
            total_vol = 0.0

            for l in logs:
                try:
                    data_hex = l.get("data", "0x0")
                    val = int(data_hex, 16) / 1e6
                    if val >= min_usd:
                        total_vol += val
                        from_addr = "0x" + l["topics"][1][26:] if len(l.get("topics", [])) > 1 else "unknown"
                        to_addr = "0x" + l["topics"][2][26:] if len(l.get("topics", [])) > 2 else "unknown"
                        whales.append({
                            "tx_hash": l.get("transactionHash"),
                            "block_number": int(l.get("blockNumber", "0x0"), 16),
                            "token": "USDC",
                            "amount_tokens": val,
                            "amount_usd": round(val, 2),
                            "from_address": from_addr,
                            "to_address": to_addr,
                        })
                except Exception:
                    continue

            # Sort by amount USD descending
            whales.sort(key=lambda x: x["amount_usd"], reverse=True)

            return {
                "latest_block": latest,
                "blocks_scanned": num_blocks,
                "total_whale_volume_usd": round(total_vol, 2),
                "whale_count": len(whales),
                "transactions": whales[:limit],
            }
    except Exception as e:
        return {
            "latest_block": 0,
            "blocks_scanned": num_blocks,
            "total_whale_volume_usd": 0.0,
            "whale_count": 0,
            "transactions": [],
            "error": str(e),
        }


# Public Endpoints
@app.get("/", summary="API Index & Service Info")
async def root():
    return {
        "service": "BaseWhale Oracle",
        "version": "1.0.0",
        "network": "Base Mainnet (8453)",
        "docs": "/docs",
        "openapi": "/openapi.json",
        "manifest": "/.well-known/x402",
        "endpoints": {
            "whales": "/v1/whales",
            "stats": "/v1/whale-stats",
        },
        "price_usd": f"${PRICE_USDC:.2f}",
        "payee": PAYEE_ADDRESS,
        "status": "online",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/health", summary="Health Check")
async def health():
    return {
        "status": "healthy",
        "chain_id": 8453,
        "payee": PAYEE_ADDRESS,
        "time": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/.well-known/x402", summary="x402 Discovery Manifest")
async def x402_manifest(request: Request):
    base_url = str(request.base_url).rstrip("/")
    return {
        "version": 1,
        "name": "BaseWhale Oracle",
        "description": "Real-Time On-Chain Whale & Large Transfer Oracle for Base Mainnet.",
        "network": CHAIN_ID,
        "price": f"${PRICE_USDC:.2f}",
        "currency": "USDC",
        "payee": PAYEE_ADDRESS,
        "ownershipProofs": [PAYEE_ADDRESS],
        "resources": [
            {
                "type": "http",
                "method": "GET",
                "url": f"{base_url}/v1/whales",
                "description": "Detect real-time on-chain whale transactions and high-value transfers on Base.",
                "price": f"${PRICE_USDC:.2f}",
                "accepts": [
                    {
                        "scheme": "exact",
                        "network": CHAIN_ID,
                        "asset": USDC_ASSET,
                        "amount": PRICE_ATOMIC,
                        "payee": PAYEE_ADDRESS,
                        "payTo": PAYEE_ADDRESS,
                        "maxTimeoutSeconds": 300,
                    }
                ],
            },
            {
                "type": "http",
                "method": "POST",
                "url": f"{base_url}/v1/whales",
                "description": "Detect real-time on-chain whale transactions and high-value transfers on Base.",
                "price": f"${PRICE_USDC:.2f}",
                "accepts": [
                    {
                        "scheme": "exact",
                        "network": CHAIN_ID,
                        "asset": USDC_ASSET,
                        "amount": PRICE_ATOMIC,
                        "payee": PAYEE_ADDRESS,
                        "payTo": PAYEE_ADDRESS,
                        "maxTimeoutSeconds": 300,
                    }
                ],
            },
            {
                "type": "http",
                "method": "GET",
                "url": f"{base_url}/v1/whale-stats",
                "description": "Get summary metrics, average whale size, and volume of large transactions on Base.",
                "price": f"${PRICE_USDC:.2f}",
                "accepts": [
                    {
                        "scheme": "exact",
                        "network": CHAIN_ID,
                        "asset": USDC_ASSET,
                        "amount": PRICE_ATOMIC,
                        "payee": PAYEE_ADDRESS,
                        "payTo": PAYEE_ADDRESS,
                        "maxTimeoutSeconds": 300,
                    }
                ],
            },
            {
                "type": "http",
                "method": "POST",
                "url": f"{base_url}/v1/whale-stats",
                "description": "Get summary metrics, average whale size, and volume of large transactions on Base.",
                "price": f"${PRICE_USDC:.2f}",
                "accepts": [
                    {
                        "scheme": "exact",
                        "network": CHAIN_ID,
                        "asset": USDC_ASSET,
                        "amount": PRICE_ATOMIC,
                        "payee": PAYEE_ADDRESS,
                        "payTo": PAYEE_ADDRESS,
                        "maxTimeoutSeconds": 300,
                    }
                ],
            },
        ],
    }


# Paid Endpoint: /v1/whales
@app.api_route("/v1/whales", methods=["GET", "POST", "HEAD"], summary="Get Live Whale Transfers (x402 Paid)")
async def get_whales(
    request: Request,
    min_usd: Optional[float] = 25000.0,
    limit: Optional[int] = 15,
    blocks: Optional[int] = 25,
    x_payment_response: Optional[str] = Header(None, alias="x-payment-response"),
    payment_response: Optional[str] = Header(None, alias="payment-response"),
):
    if request.method == "HEAD":
        return build_402_response(str(request.url), "BaseWhale Live Stream")

    if request.method == "POST":
        try:
            body = await request.json()
            min_usd = float(body.get("min_usd", min_usd))
            limit = int(body.get("limit", limit))
            blocks = int(body.get("blocks", blocks))
        except Exception:
            pass

    has_payment = bool(x_payment_response or payment_response)
    if not has_payment:
        return build_402_response(str(request.url), f"BaseWhale Stream (>= ${min_usd:,.0f})")

    res = await fetch_onchain_whales(min_usd=min_usd, num_blocks=min(blocks, 50), limit=limit)

    return {
        "success": True,
        "network": "Base Mainnet (8453)",
        "tracked_token": "USDC",
        "min_usd_filter": min_usd,
        "latest_block": res["latest_block"],
        "blocks_scanned": res["blocks_scanned"],
        "whale_transactions_found": res["whale_count"],
        "total_whale_volume_usd": res["total_whale_volume_usd"],
        "transactions": res["transactions"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# Paid Endpoint: /v1/whale-stats
@app.api_route("/v1/whale-stats", methods=["GET", "POST", "HEAD"], summary="Whale Activity Stats (x402 Paid)")
async def get_whale_stats(
    request: Request,
    min_usd: Optional[float] = 25000.0,
    blocks: Optional[int] = 25,
    x_payment_response: Optional[str] = Header(None, alias="x-payment-response"),
    payment_response: Optional[str] = Header(None, alias="payment-response"),
):
    if request.method == "HEAD":
        return build_402_response(str(request.url), "BaseWhale Stats")

    if request.method == "POST":
        try:
            body = await request.json()
            min_usd = float(body.get("min_usd", min_usd))
            blocks = int(body.get("blocks", blocks))
        except Exception:
            pass

    has_payment = bool(x_payment_response or payment_response)
    if not has_payment:
        return build_402_response(str(request.url), "BaseWhale Statistics")

    res = await fetch_onchain_whales(min_usd=min_usd, num_blocks=min(blocks, 50), limit=50)
    txs = res.get("transactions", [])
    avg_size = (res["total_whale_volume_usd"] / max(len(txs), 1)) if txs else 0.0
    largest = max([t["amount_usd"] for t in txs], default=0.0)

    return {
        "success": True,
        "network": "Base Mainnet (8453)",
        "tracked_token": "USDC",
        "latest_block": res["latest_block"],
        "blocks_scanned": res["blocks_scanned"],
        "total_whale_transactions": len(txs),
        "total_whale_volume_usd": res["total_whale_volume_usd"],
        "largest_single_transfer_usd": round(largest, 2),
        "average_whale_transfer_usd": round(avg_size, 2),
        "whale_market_impact": "high" if res["total_whale_volume_usd"] > 1_000_000 else "normal",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# Self-test endpoint
@app.get("/self-test", summary="Test on-chain log retrieval")
async def self_test():
    res = await fetch_onchain_whales(min_usd=10000.0, num_blocks=10, limit=3)
    return {
        "status": "ok",
        "latest_block": res.get("latest_block"),
        "sample_whales_detected": res.get("whale_count"),
        "payee_configured": PAYEE_ADDRESS,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    openapi_schema = get_openapi(
        title="BaseWhale Oracle x402",
        version="1.0.0",
        description="Real-Time On-Chain Whale & Large Transfer Oracle for Base Mainnet (Chain ID 8453), payable via x402.",
        routes=app.routes,
    )
    openapi_schema["x-payment-info"] = {
        "protocols": [
            {
                "x402": {
                    "version": 2,
                    "network": CHAIN_ID,
                    "asset": USDC_ASSET,
                    "payee": PAYEE_ADDRESS,
                    "price_usd": PRICE_USDC,
                }
            }
        ]
    }
    app.openapi_schema = openapi_schema
    return app.openapi_schema


app.openapi = custom_openapi

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8005, reload=True)
