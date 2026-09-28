"""Telegram bot untuk membaca APR pool Meteora DLMM.

Bot ini read-only: tidak meminta private key, tidak menandatangani transaksi,
dan hanya memakai Meteora DLMM Data API serta Telegram Bot API.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from html import escape as html_escape
from typing import Any


METEORA_API_DEFAULT = "https://dlmm.datapi.meteora.ag"
SOLANA_ADDRESS_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
EVM_ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
MAX_TELEGRAM_MESSAGE = 4096
ALERTS_FILE = "alerts.json"
UNISWAP_SUPPORTED_CHAINS_URL = "https://trade-api.gateway.uniswap.org/v1/supported_chains"
GRAPH_GATEWAY_DEFAULT = "https://gateway.thegraph.com/api"
UNISWAP_ALLOWED_CHAIN_IDS = (1, 42161, 56, 4663, 5042, 8453)


class ApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def load_env_file(path: str = ".env") -> None:
    """Load a small .env file without overriding existing environment values."""
    if not os.path.exists(path):
        return

    with open(path, "r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


def request_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 45,
) -> Any:
    body = None
    request_headers = {"Accept": "application/json", "User-Agent": "apr-telegram-bot/1.0"}
    if headers:
        request_headers.update(headers)
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8")[:300]
        except Exception:
            detail = ""
        raise ApiError(f"HTTP {exc.code}: {detail}", exc.code) from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ApiError(f"Network/API error: {exc}") from exc


def number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def money(value: Any) -> str:
    if value is None or value == "":
        return "-"
    amount = number(value)
    if amount >= 1_000_000_000:
        return f"${amount / 1_000_000_000:.2f}B"
    if amount >= 1_000_000:
        return f"${amount / 1_000_000:.2f}M"
    if amount >= 1_000:
        return f"${amount / 1_000:.2f}K"
    return f"${amount:,.2f}"


def percent(decimal_apr: Any) -> str:
    # Meteora API mengirim APR dalam bentuk desimal: 0.3344 = 33.44%.
    return f"{number(decimal_apr) * 100:,.2f}%"


def fee_percent(value: Any) -> str:
    # pool_config.base_fee_pct sudah dikirim Meteora dalam satuan persen.
    return f"{number(value):,.2f}%"


def short_address(address: str, chars: int = 6) -> str:
    if len(address) <= chars * 2:
        return address
    return f"{address[:chars]}…{address[-chars:]}"


@dataclass
class PoolResult:
    raw: dict[str, Any]

    @property
    def address(self) -> str:
        return str(self.raw.get("address", "-"))

    @property
    def name(self) -> str:
        token_x = self.raw.get("token_x") or {}
        token_y = self.raw.get("token_y") or {}
        return str(self.raw.get("name") or f"{token_x.get('symbol', '?')}-{token_y.get('symbol', '?')}")

    @property
    def fee_apr(self) -> float:
        return number(self.raw.get("apr"))

    @property
    def farm_apr(self) -> float:
        return number(self.raw.get("farm_apr"))

    @property
    def total_apr(self) -> float:
        return self.fee_apr + self.farm_apr

    @property
    def token_x(self) -> dict[str, Any]:
        return self.raw.get("token_x") or {}

    @property
    def token_y(self) -> dict[str, Any]:
        return self.raw.get("token_y") or {}


class MeteoraClient:
    def __init__(self, base_url: str = METEORA_API_DEFAULT):
        self.base_url = base_url.rstrip("/")
        self.page_size = int(os.getenv("METEORA_PAGE_SIZE", "1000"))
        self.max_pages = int(os.getenv("METEORA_MAX_PAGES", "20"))

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{query}"
        return request_json(url)

    def get_pool(self, address: str) -> PoolResult:
        data = self._get(f"/pools/{urllib.parse.quote(address, safe='')}")
        if not isinstance(data, dict):
            raise ApiError("Format data pool tidak dikenali")
        return PoolResult(data)

    def get_volume_history(self, pool_address: str, timeframe: str = "5m") -> dict[str, Any]:
        """Ambil candle volume dari API Meteora.

        API resmi menyediakan candle 5m, 30m, 1h, dan seterusnya, bukan 15m.
        Karena itu volume 15m dihitung dari tiga candle 5m terakhir yang sudah
        dimulai sebelum end_time.
        """
        now = int(time.time())
        data = self._get(
            f"/pools/{urllib.parse.quote(pool_address, safe='')}/volume/history",
            {"timeframe": timeframe, "start_time": now - 15 * 60, "end_time": now},
        )
        if not isinstance(data, dict):
            raise ApiError("Format volume history tidak dikenali")
        return data

    def enrich_with_15m_volume(self, pools: list[PoolResult]) -> None:
        """Tambahkan volume 15m ke pool yang akan ditampilkan.

        Error pada satu pool tidak menggagalkan seluruh hasil APR.
        """
        for pool in pools:
            try:
                history = self.get_volume_history(pool.address, "5m")
                rows = [row for row in (history.get("data") or []) if isinstance(row, dict)]
                end_time = int(history.get("end_time") or int(time.time()))
                start_time = end_time - 15 * 60
                volume_15m = sum(
                    number(row.get("volume"))
                    for row in rows
                    if start_time <= int(row.get("timestamp", 0)) < end_time
                )
                pool.raw["_volume_15m"] = volume_15m
            except (ApiError, ValueError, TypeError):
                pool.raw["_volume_15m"] = None

    def _get_mint_side(self, mint: str, side: str) -> tuple[list[PoolResult], bool]:
        pools: list[PoolResult] = []
        page = 1
        partial = False

        while page <= self.max_pages:
            response = self._get(
                "/pools",
                {
                    "page": page,
                    "page_size": self.page_size,
                    "filter_by": f"{side}={mint}",
                },
            )
            if not isinstance(response, dict):
                raise ApiError("Format response daftar pool tidak dikenali")

            rows = response.get("data") or []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                token = row.get(side) or {}
                if str(token.get("address", "")).lower() == mint.lower():
                    pools.append(PoolResult(row))

            total_pages = int(response.get("pages") or 1)
            if page >= total_pages or not rows:
                break
            page += 1

        if page <= self.max_pages and "total_pages" in locals() and total_pages > self.max_pages:
            partial = True
        return pools, partial

    def find_pools_for_mint(self, mint: str) -> tuple[list[PoolResult], bool]:
        by_address: dict[str, PoolResult] = {}
        partial = False
        for side in ("token_x", "token_y"):
            side_pools, side_partial = self._get_mint_side(mint, side)
            partial = partial or side_partial
            for pool in side_pools:
                by_address[pool.address] = pool

        pools = sorted(by_address.values(), key=lambda pool: pool.total_apr, reverse=True)
        return pools, partial


UNISWAP_CHAIN_NAMES = {
    1: "Ethereum",
    56: "BSC (BNB Smart Chain)",
    4663: "Robinhood Chain",
    5042: "Arc",
    8453: "Base",
    42161: "Arbitrum One",
}


@dataclass
class EvmPoolResult:
    raw: dict[str, Any]

    @property
    def address(self) -> str:
        return str(self.raw.get("address", "-"))

    @property
    def name(self) -> str:
        token0 = self.raw.get("token0") or {}
        token1 = self.raw.get("token1") or {}
        return str(self.raw.get("name") or f"{token0.get('symbol', '?')}-{token1.get('symbol', '?')}")

    @property
    def fee_tier(self) -> float:
        return number(self.raw.get("fee_tier")) / 10_000


class UniswapClient:
    """Read-only Uniswap V3 analytics through the official Graph subgraphs."""

    TOKEN_QUERY = """
    {
      token(id: \"{token}\") {
        id symbol name decimals totalSupply derivedETH totalValueLockedUSD
      }
      bundle(id: \"1\") { ethPriceUSD }
      pools0: pools(first: 50, orderBy: totalValueLockedUSD, orderDirection: desc,
        where: { token0: \"{token}\" }) {
        id feeTier totalValueLockedUSD volumeUSD feesUSD
        token0 { id symbol name decimals }
        token1 { id symbol name decimals }
      }
      pools1: pools(first: 50, orderBy: totalValueLockedUSD, orderDirection: desc,
        where: { token1: \"{token}\" }) {
        id feeTier totalValueLockedUSD volumeUSD feesUSD
        token0 { id symbol name decimals }
        token1 { id symbol name decimals }
      }
    }
    """

    TOKEN_QUERY_V4 = """
    {
      token(id: \"{token}\") {
        id symbol name decimals totalSupply derivedETH totalValueLockedUSD
      }
      bundle(id: \"1\") { ethPriceUSD }
      pools0: pools(first: 50, orderBy: totalValueLockedUSD, orderDirection: desc,
        where: { token0: \"{token}\" }) {
        id feeTier tickSpacing hooks totalValueLockedUSD volumeUSD feesUSD
        token0 { id symbol name decimals }
        token1 { id symbol name decimals }
      }
      pools1: pools(first: 50, orderBy: totalValueLockedUSD, orderDirection: desc,
        where: { token1: \"{token}\" }) {
        id feeTier tickSpacing hooks totalValueLockedUSD volumeUSD feesUSD
        token0 { id symbol name decimals }
        token1 { id symbol name decimals }
      }
    }
    """

    SWAPS_QUERY = """
    {
      swaps(first: 1000, orderBy: timestamp, orderDirection: desc,
        where: { pool: \"{pool}\", timestamp_gte: {since} }) {
        timestamp amountUSD
      }
    }
    """

    def __init__(self):
        self.api_key = os.getenv("UNISWAP_API_KEY", "").strip()
        self.graph_key = os.getenv("THE_GRAPH_API_KEY", "").strip()
        self.graph_base = os.getenv("THE_GRAPH_GATEWAY_URL", GRAPH_GATEWAY_DEFAULT).rstrip("/")
        self.max_pools = max(1, min(int(os.getenv("UNISWAP_MAX_POOLS", "10")), 20))
        self._chains_cache: list[dict[str, Any]] | None = None
        self.subgraph_ids = self._load_subgraph_ids(
            "UNISWAP_SUBGRAPH_IDS",
            "5zvR82QoaXYFyDEKLZ9t6v9adgnptxYpKpSbxtgVENFV",
        )
        self.v4_subgraph_ids = self._load_subgraph_ids(
            "UNISWAP_V4_SUBGRAPH_IDS",
            "DiYPVdygkfjDWhbxGSqAQxwBKmfKnkWQojqeM2rkLb3G",
        )

    @staticmethod
    def _load_subgraph_ids(env_name: str, default_mainnet_id: str) -> dict[int, str]:
        """Parse CHAIN_ID:SUBGRAPH_ID;CHAIN_ID:SUBGRAPH_ID from .env."""
        raw = os.getenv(env_name, "").strip()
        mapping: dict[int, str] = {}
        for item in raw.split(";"):
            if ":" not in item:
                continue
            chain_id, subgraph_id = item.split(":", 1)
            try:
                chain = int(chain_id.strip())
            except ValueError:
                continue
            if subgraph_id.strip():
                mapping[chain] = subgraph_id.strip()
        # Official Uniswap deployment documented by Uniswap for Ethereum mainnet.
        mapping.setdefault(1, default_mainnet_id)
        return mapping

    def _require_keys(self) -> None:
        missing = []
        if not self.api_key:
            missing.append("UNISWAP_API_KEY")
        if not self.graph_key:
            missing.append("THE_GRAPH_API_KEY")
        if missing:
            raise ApiError("API key EVM belum diisi: " + ", ".join(missing))

    def get_supported_chains(self, refresh: bool = False) -> list[dict[str, Any]]:
        self._require_keys()
        if self._chains_cache is not None and not refresh:
            return self._chains_cache
        response = request_json(
            UNISWAP_SUPPORTED_CHAINS_URL,
            headers={"x-api-key": self.api_key},
        )
        rows = response.get("chains") if isinstance(response, dict) else response
        if not isinstance(rows, list):
            raise ApiError("Format daftar chain Uniswap tidak dikenali")
        chains = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            chain_id = row.get("chainId", row.get("chain_id"))
            try:
                chain_id = int(chain_id)
            except (TypeError, ValueError):
                continue
            name = str(row.get("name") or row.get("chainName") or UNISWAP_CHAIN_NAMES.get(chain_id, f"Chain {chain_id}"))
            chains.append({
                "id": chain_id,
                "name": name,
                "v3_configured": chain_id in self.subgraph_ids,
                "v4_configured": chain_id in self.v4_subgraph_ids,
            })
        chains = [chain for chain in chains if chain["id"] in UNISWAP_ALLOWED_CHAIN_IDS]
        self._chains_cache = sorted(chains, key=lambda item: UNISWAP_ALLOWED_CHAIN_IDS.index(item["id"]))
        return self._chains_cache

    def _graph_query(self, chain_id: int, query: str, protocol: str = "v3") -> dict[str, Any]:
        self._require_keys()
        subgraph_map = self.v4_subgraph_ids if protocol == "v4" else self.subgraph_ids
        subgraph_id = subgraph_map.get(chain_id)
        if not subgraph_id:
            raise ApiError(
                f"Subgraph Uniswap {protocol.upper()} untuk chain {chain_id} belum dikonfigurasi. "
                f"Tambahkan CHAIN_ID:SUBGRAPH_ID ke {'UNISWAP_V4_SUBGRAPH_IDS' if protocol == 'v4' else 'UNISWAP_SUBGRAPH_IDS'}."
            )
        url = f"{self.graph_base}/{urllib.parse.quote(self.graph_key, safe='')}/subgraphs/id/{urllib.parse.quote(subgraph_id, safe='')}"
        response = request_json(
            url,
            method="POST",
            payload={"query": query},
            headers={"Authorization": f"Bearer {self.graph_key}"},
        )
        if not isinstance(response, dict):
            raise ApiError("Format response The Graph tidak dikenali")
        if response.get("errors"):
            message = response["errors"][0].get("message", "GraphQL error") if isinstance(response["errors"], list) else str(response["errors"])
            raise ApiError(f"The Graph: {message}")
        data = response.get("data")
        if not isinstance(data, dict):
            raise ApiError("The Graph tidak mengembalikan data")
        return data

    def _pool_swaps(self, chain_id: int, pool_address: str, since: int, protocol: str) -> tuple[float, bool]:
        data = self._graph_query(
            chain_id,
            self.SWAPS_QUERY.format(pool=pool_address.lower(), since=since),
            protocol,
        )
        rows = data.get("swaps") or []
        if not isinstance(rows, list):
            return 0.0, False
        amount = sum(number(row.get("amountUSD")) for row in rows if isinstance(row, dict))
        # 1000 is The Graph's common per-collection maximum; mark the value as partial.
        return amount, len(rows) >= 1000

    def find_pools_for_token(
        self,
        chain_id: int,
        token_address: str,
        protocol: str = "v3",
    ) -> tuple[dict[str, Any], list[EvmPoolResult]]:
        if protocol not in ("v3", "v4"):
            raise ApiError("Protocol Uniswap tidak valid")
        token_address = token_address.lower()
        query_template = self.TOKEN_QUERY_V4 if protocol == "v4" else self.TOKEN_QUERY
        data = self._graph_query(chain_id, query_template.format(token=token_address), protocol)
        token = data.get("token") or {}
        pool_rows: dict[str, dict[str, Any]] = {}
        for row in (data.get("pools0") or []) + (data.get("pools1") or []):
            if isinstance(row, dict) and row.get("id"):
                pool_rows[str(row["id"]).lower()] = row

        if not token and not pool_rows:
            return {}, []

        now = int(time.time())
        results: list[EvmPoolResult] = []
        for row in sorted(pool_rows.values(), key=lambda item: number(item.get("totalValueLockedUSD")), reverse=True)[: self.max_pools]:
            pool = EvmPoolResult(
                {
                    "address": str(row.get("id")),
                    "name": f"{(row.get('token0') or {}).get('symbol', '?')}-{(row.get('token1') or {}).get('symbol', '?')}",
                    "token0": row.get("token0") or {},
                    "token1": row.get("token1") or {},
                    "tvl": number(row.get("totalValueLockedUSD")),
                    "fee_tier": number(row.get("feeTier")),
                    "tick_spacing": row.get("tickSpacing"),
                    "hooks": row.get("hooks"),
                    "chain_id": chain_id,
                    "protocol": protocol,
                    "volume_24h": 0.0,
                    "volume_1h": 0.0,
                    "volume_15m": 0.0,
                    "fee_apr": None,
                    "swaps_partial": False,
                }
            )
            volume_24h, partial = self._pool_swaps(chain_id, pool.address, now - 24 * 3600, protocol)
            volume_1h, partial_1h = self._pool_swaps(chain_id, pool.address, now - 3600, protocol)
            volume_15m, partial_15m = self._pool_swaps(chain_id, pool.address, now - 15 * 60, protocol)
            raw = pool.raw
            raw["volume_24h"] = volume_24h
            raw["volume_1h"] = volume_1h
            raw["volume_15m"] = volume_15m
            raw["swaps_partial"] = partial or partial_1h or partial_15m
            raw["fees_24h"] = volume_24h * pool.fee_tier / 100
            raw["fee_apr"] = (raw["fees_24h"] / raw["tvl"] * 365) if raw["tvl"] > 0 else None
            results.append(pool)

        results.sort(key=lambda item: number(item.raw.get("fee_apr")), reverse=True)
        return {"token": token, "bundle": data.get("bundle") or {}}, results


def pool_line(pool: PoolResult, index: int, mint: str, min_tvl: float) -> str:
    volume_24h = (pool.raw.get("volume") or {}).get("24h")
    volume_1h = (pool.raw.get("volume") or {}).get("1h")
    token = pool.token_x if pool.token_x.get("address", "").lower() == mint.lower() else pool.token_y
    config = pool.raw.get("pool_config") or {}
    bin_step = config.get("bin_step", "-")
    tvl = number(pool.raw.get("tvl"))
    apr_is_reliable = tvl >= min_tvl and number(volume_24h) > 0
    fee_apr_text = percent(pool.fee_apr) if apr_is_reliable else "N/A (TVL rendah)"
    total_apr_text = percent(pool.total_apr) if apr_is_reliable else "N/A (TVL rendah)"
    return (
        f"<b>{index}. {html_escape(str(pool.name))}</b>\n"
        f"   🏊 Pool: <code>{html_escape(pool.address)}</code>\n"
        f"   💧 TVL: {money(pool.raw.get('tvl'))}\n"
        f"   📈 Volume 15m: {money(pool.raw.get('_volume_15m'))} | 1h: {money(volume_1h)}\n"
        f"   📅 Volume 24h: {money(volume_24h)}\n"
        f"   ⚙️ Fee: {fee_percent(config.get('base_fee_pct'))} | Bin step: {html_escape(str(bin_step))}\n"
        f"   💸 Fee APR 24h: <b>{fee_apr_text}</b>\n"
        f"   🌾 Farm APR: {percent(pool.farm_apr)}\n"
        f"   🚀 Estimasi total APR: <b>{total_apr_text}</b>"
    )


def select_pools_for_display(pools: list[PoolResult], min_tvl: float) -> tuple[list[PoolResult], int]:
    """Prioritaskan pool aktif dan sembunyikan pool dengan TVL sangat kecil."""
    usable = [
        pool
        for pool in pools
        if number(pool.raw.get("tvl")) >= min_tvl
        and number((pool.raw.get("volume") or {}).get("24h")) > 0
    ]
    if usable:
        usable.sort(key=lambda pool: pool.total_apr, reverse=True)
        return usable, len(pools) - len(usable)

    # Jika semua pool kecil, tetap tampilkan datanya tetapi APR akan ditandai N/A.
    fallback = sorted(pools, key=lambda pool: number(pool.raw.get("tvl")), reverse=True)
    return fallback, 0


def render_pools(
    mint: str,
    pools: list[PoolResult],
    partial: bool,
    max_items: int = 10,
    min_tvl: float = 1_000.0,
    filtered_count: int = 0,
) -> str:
    if not pools:
        return (
            "Tidak ada pool Meteora DLMM untuk mint ini.\n\n"
            "Pastikan contract address yang dikirim adalah mint token Solana, bukan alamat wallet."
        )

    token = pools[0].token_x if pools[0].token_x.get("address", "").lower() == mint.lower() else pools[0].token_y
    symbol = html_escape(str(token.get("symbol") or "?"))
    name = html_escape(str(token.get("name") or "Unknown token"))
    shown = pools[:max_items]
    lines = [
        "<b>🟠 METEORA DLMM</b>",
        "━━━━━━━━━━━━━━━━━━",
        "",
        "🪙 <b>Token</b>",
        f"{name} ({symbol})",
        f"🏷 Market Cap: <b>{money(token.get('market_cap'))}</b>",
        f"🔑 Mint: <code>{html_escape(mint)}</code>",
        f"🔎 Pool ditemukan: {len(pools) + filtered_count} | Ditampilkan: {len(shown)}",
        "",
    ]
    lines.extend(pool_line(pool, i, mint, min_tvl) for i, pool in enumerate(shown, 1))
    lines.append("")
    if filtered_count:
        lines.append(f"⚠️ {filtered_count} pool disembunyikan karena TVL di bawah {money(min_tvl)}.")
    if partial:
        lines.append("⚠️ Hasil pencarian dipotong oleh batas pagination bot.")

    message = "\n".join(lines)
    return message[:MAX_TELEGRAM_MESSAGE]


def evm_market_cap(token: dict[str, Any], bundle: dict[str, Any]) -> float | None:
    total_supply = number(token.get("totalSupply"))
    decimals = int(number(token.get("decimals"), 18))
    eth_price = number(bundle.get("ethPriceUSD"))
    derived_eth = number(token.get("derivedETH"))
    if total_supply <= 0 or eth_price <= 0 or derived_eth <= 0:
        return None
    return total_supply / (10 ** decimals) * derived_eth * eth_price


def evm_fee_percent(pool: EvmPoolResult) -> str:
    return f"{pool.fee_tier:,.4f}%"


def evm_pool_ref(pool_address: str) -> str:
    """Short deterministic reference for Telegram callback_data (pool IDs can be bytes32)."""
    return hashlib.sha256(pool_address.lower().encode("utf-8")).hexdigest()[:10]


def evm_pool_line(pool: EvmPoolResult, index: int) -> str:
    raw = pool.raw
    fee_apr = raw.get("fee_apr")
    fee_apr_text = percent(fee_apr) if fee_apr is not None else "N/A"
    partial_text = " ⚠️" if raw.get("swaps_partial") else ""
    protocol = str(raw.get("protocol", "v3")).upper()
    extra = f" | Tick spacing: {html_escape(str(raw.get('tick_spacing')))}" if protocol == "V4" else ""
    hooks = f"\n   🪝 Hook: <code>{html_escape(str(raw.get('hooks')))}</code>" if protocol == "V4" and raw.get("hooks") else ""
    return (
        f"<b>{index}. {html_escape(pool.name)}</b>\n"
        f"   🏊 Pool: <code>{html_escape(pool.address)}</code>\n"
        f"   💧 TVL: {money(raw.get('tvl'))}\n"
        f"   📈 Volume 15m: {money(raw.get('volume_15m'))} | 1h: {money(raw.get('volume_1h'))}{partial_text}\n"
        f"   📅 Volume 24h: {money(raw.get('volume_24h'))}\n"
        f"   ⚙️ Fee: {evm_fee_percent(pool)} | Protocol: Uniswap {protocol}{extra}{hooks}\n"
        f"   💸 Fee APR 24h: <b>{fee_apr_text}</b>\n"
        "   🌾 Farm APR: N/A (tidak ada data insentif Uniswap)\n"
        f"   🚀 Estimasi total APR: <b>{fee_apr_text}</b>"
    )


def render_evm_pools(
    token_address: str,
    chain_id: int,
    chain_name: str,
    token_data: dict[str, Any],
    pools: list[EvmPoolResult],
    max_items: int = 10,
) -> str:
    if not pools:
        return (
            "<b>🟣 UNISWAP V3</b>\n\n"
            f"Chain: <b>{html_escape(chain_name)}</b>\n"
            f"Token: <code>{html_escape(token_address)}</code>\n\n"
            "Tidak ada pool Uniswap V3 yang ditemukan untuk token ini."
        )

    token = token_data.get("token") or {}
    symbol = html_escape(str(token.get("symbol") or "?"))
    name = html_escape(str(token.get("name") or "Unknown token"))
    market_cap = evm_market_cap(token, token_data.get("bundle") or {})
    shown = pools[:max_items]
    lines = [
        f"<b>🟣 UNISWAP {html_escape(str((pools[0].raw.get('protocol') or 'v3')).upper())}</b>",
        "━━━━━━━━━━━━━━━━━━",
        "",
        f"🌐 <b>Chain:</b> {html_escape(chain_name)} ({chain_id})",
        "🪙 <b>Token</b>",
        f"{name} ({symbol})",
        f"🏷 Market Cap: <b>{money(market_cap)}</b>",
        f"🔑 Contract: <code>{html_escape(token_address)}</code>",
        f"🔎 Pool ditemukan: {len(pools)} | Ditampilkan: {len(shown)}",
        "",
    ]
    lines.extend(evm_pool_line(pool, index) for index, pool in enumerate(shown, 1))
    lines.append("")
    lines.append("ℹ️ Volume 15m/1h/24h dihitung dari swap yang terindeks The Graph.")
    if any(pool.raw.get("swaps_partial") for pool in shown):
        lines.append("⚠️ Sebagian pool memiliki lebih dari 1.000 swap pada periode tersebut; volume bisa terpotong.")
    return "\n".join(lines)[:MAX_TELEGRAM_MESSAGE]


class TelegramBotApi:
    def __init__(self, token: str):
        self.base_url = f"https://api.telegram.org/bot{token}"

    def call(self, method: str, payload: dict[str, Any] | None = None, timeout: int = 45) -> Any:
        result = request_json(f"{self.base_url}/{method}", method="POST", payload=payload or {}, timeout=timeout)
        if not isinstance(result, dict) or not result.get("ok"):
            raise ApiError(f"Telegram API error: {result}")
        return result.get("result")

    def send(
        self,
        chat_id: int | str,
        text: str,
        parse_mode: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup:
            payload["reply_markup"] = reply_markup
        self.call("sendMessage", payload)

    def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text
        self.call("answerCallbackQuery", payload)


def valid_mint(value: str) -> bool:
    return bool(SOLANA_ADDRESS_RE.fullmatch(value))


def valid_evm_address(value: str) -> bool:
    return bool(EVM_ADDRESS_RE.fullmatch(value))


def valid_uniswap_chain(chain_id: int) -> bool:
    return chain_id in UNISWAP_ALLOWED_CHAIN_IDS


def evm_command_parts(text: str) -> tuple[int | None, str]:
    """Return (chain_id, address) for /evm commands, or (None, address)."""
    value = text.strip().replace("`", "")
    if value.lower().startswith("/evm"):
        parts = value.split()
        parts = parts[1:]
    else:
        parts = value.split()
    if not parts:
        return None, ""
    if len(parts) == 1:
        return None, parts[0]
    try:
        return int(parts[0]), parts[1]
    except ValueError:
        return None, parts[-1]


def command_argument(text: str) -> str:
    value = text.strip().replace("`", "")
    if value.lower().startswith("/apr"):
        value = value[4:].strip().split()[0] if value[4:].strip() else ""
    return value


def load_alerts(path: str = ALERTS_FILE) -> list[dict[str, Any]]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def save_alerts(alerts: list[dict[str, Any]], path: str = ALERTS_FILE) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(alerts, handle, indent=2, ensure_ascii=False)


def alert_keyboard(mint: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "🔔 Set Alert", "callback_data": f"alertmenu:{mint}"}],
            [{"text": "📋 Alert Aktif", "callback_data": "alertlist"}],
        ]
    }


def evm_chain_keyboard(chains: list[dict[str, Any]]) -> dict[str, Any]:
    rows = []
    for chain in chains:
        versions = "/".join(
            version
            for version, configured in (("V3", chain.get("v3_configured")), ("V4", chain.get("v4_configured")))
            if configured
        ) or "setup"
        rows.append([{
            "text": f"{versions} · {chain.get('name')} ({chain.get('id')})",
            "callback_data": f"evmchain:{chain.get('id')}",
        }])
    return {"inline_keyboard": rows}


def evm_protocol_keyboard(chain_id: int, address: str, client: UniswapClient) -> dict[str, Any]:
    chain = next((item for item in client._chains_cache or [] if item.get("id") == chain_id), {})
    rows = []
    if chain.get("v3_configured"):
        rows.append([{"text": "🟣 Uniswap V3", "callback_data": f"evmprotocol:{chain_id}:v3"}])
    if chain.get("v4_configured"):
        rows.append([{"text": "🔵 Uniswap V4", "callback_data": f"evmprotocol:{chain_id}:v4"}])
    if not rows:
        rows.append([{"text": "⚙️ Subgraph belum dikonfigurasi", "callback_data": "cancelalert"}])
    rows.append([{"text": "✖️ Batal", "callback_data": "cancelalert"}])
    return {"inline_keyboard": rows}


def evm_alert_keyboard(chain_id: int, token_address: str, protocol: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": f"🔔 Set Alert Uniswap {protocol.upper()}", "callback_data": f"eam:{protocol}:{chain_id}:{token_address}"}],
            [{"text": "📋 Alert Aktif", "callback_data": "alertlist"}],
        ]
    }


def evm_pool_choice_keyboard(chain_id: int, pools: list[EvmPoolResult]) -> dict[str, Any]:
    rows = [
        [{
            "text": f"🔔 {pool.name} | TVL {money(pool.raw.get('tvl'))}",
            "callback_data": f"eap:{pool.raw.get('protocol', 'v3')}:{chain_id}:{evm_pool_ref(pool.address)}",
        }]
        for pool in pools
    ]
    rows.append([{"text": "✖️ Batal", "callback_data": "cancelalert"}])
    return {"inline_keyboard": rows}


def evm_interval_keyboard(protocol: str, chain_id: int, pool_ref: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "5 menit", "callback_data": f"ei:{protocol}:{chain_id}:{pool_ref}:5"},
                {"text": "15 menit (default)", "callback_data": f"ei:{protocol}:{chain_id}:{pool_ref}:15"},
            ],
            [
                {"text": "30 menit", "callback_data": f"ei:{protocol}:{chain_id}:{pool_ref}:30"},
                {"text": "60 menit", "callback_data": f"ei:{protocol}:{chain_id}:{pool_ref}:60"},
            ],
            [{"text": "✏️ Custom menit", "callback_data": f"ec:{protocol}:{chain_id}:{pool_ref}"}],
            [{"text": "✖️ Batal", "callback_data": "cancelalert"}],
        ]
    }


def evm_alert_control_keyboard(protocol: str, chain_id: int, pool_address: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [[{
            "text": "🔕 Matikan alert",
            "callback_data": f"es:{protocol}:{chain_id}:{evm_pool_ref(pool_address)}",
        }]]
    }


def pool_choice_keyboard(pools: list[PoolResult]) -> dict[str, Any]:
    rows = [
        [{"text": f"🔔 {pool.name} | TVL {money(pool.raw.get('tvl'))}", "callback_data": f"alertpool:{pool.address}"}]
        for pool in pools
    ]
    rows.append([{"text": "✖️ Batal", "callback_data": "cancelalert"}])
    return {"inline_keyboard": rows}


def interval_keyboard(pool_address: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "5 menit", "callback_data": f"interval:{pool_address}:5"},
                {"text": "15 menit (default)", "callback_data": f"interval:{pool_address}:15"},
            ],
            [
                {"text": "30 menit", "callback_data": f"interval:{pool_address}:30"},
                {"text": "60 menit", "callback_data": f"interval:{pool_address}:60"},
            ],
            [{"text": "✏️ Custom menit", "callback_data": f"custom:{pool_address}"}],
            [{"text": "✖️ Batal", "callback_data": "cancelalert"}],
        ]
    }


def alert_control_keyboard(pool_address: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [[{"text": "🔕 Matikan alert", "callback_data": f"stop:{pool_address}"}]]
    }


def alert_key(chat_id: int | str, pool_address: str) -> str:
    return f"{chat_id}:{pool_address}"


def upsert_alert(
    alerts: list[dict[str, Any]],
    *,
    chat_id: int | str,
    user_id: int,
    mint: str,
    pool: PoolResult,
    interval_minutes: int,
    provider: str = "meteora",
    chain_id: int | None = None,
) -> dict[str, Any]:
    now = time.time()
    record = {
        "id": alert_key(chat_id, pool.address),
        "chat_id": chat_id,
        "user_id": user_id,
        "mint": mint,
        "pool_address": pool.address,
        "pool_name": pool.name,
        "provider": provider,
        "chain_id": chain_id,
        "interval_minutes": interval_minutes,
        "next_run": now + interval_minutes * 60,
        "active": True,
    }
    for index, existing in enumerate(alerts):
        if existing.get("id") == record["id"]:
            alerts[index] = record
            return record
    alerts.append(record)
    return record


def upsert_evm_alert(
    alerts: list[dict[str, Any]],
    *,
    chat_id: int | str,
    user_id: int,
    token_address: str,
    chain_id: int,
    protocol: str,
    pool: EvmPoolResult,
    interval_minutes: int,
) -> dict[str, Any]:
    now = time.time()
    record = {
        "id": f"{chat_id}:uniswap:{protocol}:{chain_id}:{pool.address}",
        "chat_id": chat_id,
        "user_id": user_id,
        "mint": token_address,
        "pool_address": pool.address,
        "pool_name": pool.name,
        "provider": "uniswap",
        "chain_id": chain_id,
        "protocol": protocol,
        "interval_minutes": interval_minutes,
        "next_run": now + interval_minutes * 60,
        "active": True,
    }
    for index, existing in enumerate(alerts):
        if existing.get("id") == record["id"]:
            alerts[index] = record
            return record
    alerts.append(record)
    return record


def active_alerts_for_chat(alerts: list[dict[str, Any]], chat_id: int | str) -> list[dict[str, Any]]:
    return [
        alert
        for alert in alerts
        if str(alert.get("chat_id")) == str(chat_id) and alert.get("active", True)
    ]


def prepare_display_pools(
    mint: str,
    pools: list[PoolResult],
    meteora: MeteoraClient,
    max_items: int,
    min_pool_tvl: float,
) -> tuple[list[PoolResult], int]:
    display_pools, filtered_count = select_pools_for_display(pools, min_pool_tvl)
    display_pools = display_pools[:max_items]
    meteora.enrich_with_15m_volume(display_pools)
    return display_pools, filtered_count


def send_due_alerts(
    api: TelegramBotApi,
    meteora: MeteoraClient,
    uniswap: UniswapClient,
    alerts: list[dict[str, Any]],
    max_items: int,
    min_pool_tvl: float,
) -> None:
    now = time.time()
    changed = False
    for alert in alerts:
        if not alert.get("active", True) or number(alert.get("next_run")) > now:
            continue

        interval = max(1, int(alert.get("interval_minutes", 15)))
        try:
            if alert.get("provider") == "uniswap":
                chain_id = int(alert["chain_id"])
                token_address = str(alert["mint"])
                protocol = str(alert.get("protocol", "v3"))
                token_data, pools = uniswap.find_pools_for_token(chain_id, token_address, protocol)
                pool = next((item for item in pools if item.address.lower() == str(alert["pool_address"]).lower()), None)
                if pool is None:
                    raise ApiError("Pool Uniswap tidak ditemukan")
                alert_text = (
                    f"<b>🔔 ALERT UNISWAP — setiap {interval} menit</b>\n\n"
                    + render_evm_pools(
                        token_address,
                        chain_id,
                        UNISWAP_CHAIN_NAMES.get(chain_id, f"Chain {chain_id}"),
                        token_data,
                        [pool],
                        max_items=1,
                    )
                )
                markup = evm_alert_control_keyboard(protocol, chain_id, pool.address)
            else:
                pool = meteora.get_pool(str(alert["pool_address"]))
                meteora.enrich_with_15m_volume([pool])
                alert_text = (
                    f"<b>🔔 ALERT AKTIF — setiap {interval} menit</b>\n\n"
                    + render_pools(
                        str(alert.get("mint") or pool.token_x.get("address", "")),
                        [pool],
                        False,
                        max_items=1,
                        min_tvl=min_pool_tvl,
                        filtered_count=0,
                    )
                )
                markup = alert_control_keyboard(str(alert["pool_address"]))
            api.send(
                alert["chat_id"],
                alert_text,
                parse_mode="HTML",
                reply_markup=markup,
            )
            alert["next_run"] = now + interval * 60
            changed = True
        except (ApiError, KeyError, ValueError, TypeError) as exc:
            print(f"[ALERT] Gagal update {alert.get('pool_address')}: {exc}")
            # Coba lagi satu menit kemudian tanpa mengirim spam ke Telegram.
            alert["next_run"] = now + 60
            changed = True

    if changed:
        save_alerts(alerts)


def print_credit() -> None:
    print("  *==========================================*")
    print("    > Built by: Noya-xen (Github)")
    print("    > Follow me on X : @xinomixo")
    print("  *==========================================*\n")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    load_env_file()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN belum diisi. Salin .env.example menjadi .env lalu isi token BotFather.")

    allowed_raw = os.getenv("ALLOWED_USER_IDS", "").strip()
    allowed_user_ids = {int(item.strip()) for item in allowed_raw.split(",") if item.strip()} if allowed_raw else set()
    max_items = max(1, min(int(os.getenv("MAX_POOLS_IN_MESSAGE", "10")), 15))
    min_pool_tvl = max(0.0, float(os.getenv("MIN_POOL_TVL_USD", "1000")))
    cooldown = max(0, int(os.getenv("USER_COOLDOWN_SECONDS", "5")))
    api = TelegramBotApi(token)
    meteora = MeteoraClient(os.getenv("METEORA_API_URL", METEORA_API_DEFAULT))
    uniswap = UniswapClient()
    last_update_id = 0
    last_request_by_user: dict[int, float] = {}
    alerts = load_alerts()
    pool_catalog: dict[str, dict[str, Any]] = {}
    pending_custom_interval: dict[tuple[int, int], str] = {}
    pending_evm_address: dict[tuple[int, int], str] = {}

    def query_token(mint: str) -> tuple[list[PoolResult], bool, int]:
        try:
            pools = [meteora.get_pool(mint)]
            partial = False
        except ApiError:
            pools, partial = meteora.find_pools_for_mint(mint)

        display_pools, filtered_count = prepare_display_pools(
            mint, pools, meteora, max_items, min_pool_tvl
        )
        for pool in display_pools:
            pool_catalog[pool.address] = {"mint": mint, "pool": pool}
        return display_pools, partial, filtered_count

    def pool_choices_for_mint(mint: str) -> list[PoolResult]:
        choices = [
            entry["pool"]
            for entry in pool_catalog.values()
            if entry.get("mint") == mint and isinstance(entry.get("pool"), PoolResult)
        ]
        if choices:
            return choices[:max_items]
        try:
            choices, _, _ = query_token(mint)
            return choices
        except (ApiError, ValueError, TypeError):
            return []

    def chain_name(chain_id: int) -> str:
        for chain in uniswap._chains_cache or []:
            if int(chain.get("id", -1)) == chain_id:
                return str(chain.get("name"))
        return UNISWAP_CHAIN_NAMES.get(chain_id, f"Chain {chain_id}")

    def send_evm_report(chat_id: int, chain_id: int, address: str, protocol: str) -> None:
        token_data, pools = uniswap.find_pools_for_token(chain_id, address, protocol)
        api.send(
            chat_id,
            render_evm_pools(address, chain_id, chain_name(chain_id), token_data, pools, max_items),
            parse_mode="HTML",
            reply_markup=evm_alert_keyboard(chain_id, address, protocol) if pools else None,
        )

    def find_evm_pool_by_ref(
        chain_id: int,
        protocol: str,
        token_address: str,
        pool_ref: str,
    ) -> EvmPoolResult:
        _, pools = uniswap.find_pools_for_token(chain_id, token_address, protocol)
        pool = next((item for item in pools if evm_pool_ref(item.address) == pool_ref), None)
        if pool is None:
            raise ApiError("Pool Uniswap sudah berubah atau tidak ditemukan")
        return pool

    def enable_alert(chat_id: int, user_id: int, pool_address: str, minutes: int) -> None:
        entry = pool_catalog.get(pool_address)
        try:
            if entry:
                mint = str(entry["mint"])
                pool = entry["pool"]
            else:
                pool = meteora.get_pool(pool_address)
                mint = str(pool.token_x.get("address", ""))
                pool_catalog[pool_address] = {"mint": mint, "pool": pool}

            record = upsert_alert(
                alerts,
                chat_id=chat_id,
                user_id=user_id,
                mint=mint,
                pool=pool,
                interval_minutes=minutes,
            )
            save_alerts(alerts)
            api.send(
                chat_id,
                (
                    f"<b>✅ Alert aktif</b>\n\n"
                    f"Pool: <b>{html_escape(str(record['pool_name']))}</b>\n"
                    f"Interval: <b>{minutes} menit</b>\n\n"
                    "Bot akan mengirim APR dan volume terbaru secara berkala."
                ),
                parse_mode="HTML",
                reply_markup=alert_control_keyboard(pool_address),
            )
        except (ApiError, KeyError, ValueError, TypeError) as exc:
            api.send(chat_id, f"Gagal mengaktifkan alert: {exc}")

    def enable_evm_alert(chat_id: int, user_id: int, protocol: str, chain_id: int, token_address: str, pool_address: str, minutes: int) -> None:
        try:
            token_data, pools = uniswap.find_pools_for_token(chain_id, token_address, protocol)
            pool = next((item for item in pools if item.address.lower() == pool_address.lower()), None)
            if pool is None:
                raise ApiError("Pool Uniswap tidak ditemukan")
            record = upsert_evm_alert(
                alerts,
                chat_id=chat_id,
                user_id=user_id,
                token_address=token_address,
                chain_id=chain_id,
                protocol=protocol,
                pool=pool,
                interval_minutes=minutes,
            )
            save_alerts(alerts)
            api.send(
                chat_id,
                (
                    f"<b>✅ Alert Uniswap {protocol.upper()} aktif</b>\n\n"
                    f"Chain: <b>{html_escape(chain_name(chain_id))}</b>\n"
                    f"Pool: <b>{html_escape(str(record['pool_name']))}</b>\n"
                    f"Interval: <b>{minutes} menit</b>\n\n"
                    "Bot akan mengirim APR dan volume terbaru secara berkala."
                ),
                parse_mode="HTML",
                reply_markup=evm_alert_control_keyboard(protocol, chain_id, pool_address),
            )
        except (ApiError, KeyError, ValueError, TypeError) as exc:
            api.send(chat_id, f"Gagal mengaktifkan alert Uniswap: {html_escape(str(exc))}")

    def send_alert_list(chat_id: int) -> None:
        current = active_alerts_for_chat(alerts, chat_id)
        if not current:
            api.send(chat_id, "Belum ada alert aktif.")
            return

        lines = ["<b>📋 ALERT AKTIF</b>", ""]
        rows = []
        for index, alert in enumerate(current, 1):
            name = html_escape(str(alert.get("pool_name", "Pool")))
            minutes = int(alert.get("interval_minutes", 15))
            provider = str(alert.get("provider", "meteora"))
            chain_text = ""
            protocol_text = ""
            if provider == "uniswap" and alert.get("chain_id"):
                chain_text = f" · {chain_name(int(alert['chain_id']))}"
                protocol_text = f" · {str(alert.get('protocol', 'v3')).upper()}"
            lines.append(f"{index}. <b>{name}</b>{html_escape(chain_text + protocol_text)} — setiap {minutes} menit")
            if provider == "uniswap":
                callback_data = f"es:{alert.get('protocol', 'v3')}:{int(alert['chain_id'])}:{evm_pool_ref(str(alert.get('pool_address', '')))}"
            else:
                callback_data = f"stop:{alert.get('pool_address', '')}"
            rows.append([{"text": f"🔕 Matikan {index}", "callback_data": callback_data}])
        api.send(chat_id, "\n".join(lines), parse_mode="HTML", reply_markup={"inline_keyboard": rows})

    def handle_callback(callback: dict[str, Any]) -> None:
        callback_id = str(callback.get("id", ""))
        callback_message = callback.get("message") or {}
        callback_chat = callback_message.get("chat") or {}
        chat_id = callback_chat.get("id")
        sender = callback.get("from") or {}
        user_id = int(sender.get("id", chat_id or 0))
        data = str(callback.get("data") or "")
        if callback_id:
            try:
                api.answer_callback(callback_id)
            except ApiError:
                pass
        if chat_id is None:
            return

        if data.startswith("evmchain:"):
            try:
                chain_id = int(data.split(":", 1)[1])
            except ValueError:
                api.send(chat_id, "Chain ID tidak valid.")
                return
            if not valid_uniswap_chain(chain_id):
                api.send(chat_id, "Chain tersebut tidak diaktifkan untuk bot ini.")
                return
            address = pending_evm_address.pop((int(chat_id), user_id), "")
            if not address:
                api.send(chat_id, "Sesi pemilihan chain sudah habis. Kirim ulang /evm <contract>.")
                return
            pending_evm_address[(int(chat_id), user_id)] = address
            api.send(
                chat_id,
                f"Pilih versi Uniswap untuk {html_escape(chain_name(chain_id))}:",
                parse_mode="HTML",
                reply_markup=evm_protocol_keyboard(chain_id, address, uniswap),
            )
            return

        if data.startswith("evmprotocol:"):
            parts = data.split(":", 2)
            if len(parts) != 3 or parts[2] not in ("v3", "v4"):
                api.send(chat_id, "Versi Uniswap tidak valid.")
                return
            try:
                chain_id = int(parts[1])
            except ValueError:
                api.send(chat_id, "Chain ID tidak valid.")
                return
            address = pending_evm_address.pop((int(chat_id), user_id), "")
            if not address:
                api.send(chat_id, "Sesi pemilihan versi sudah habis. Kirim ulang contract EVM.")
                return
            protocol = parts[2]
            api.send(chat_id, f"Sedang mengambil data Uniswap {protocol.upper()} di {html_escape(chain_name(chain_id))}…")
            try:
                pending_evm_address[(int(chat_id), user_id)] = f"{chain_id}:{protocol}:{address}"
                send_evm_report(int(chat_id), chain_id, address, protocol)
            except ApiError as exc:
                api.send(chat_id, f"Gagal mengambil data Uniswap: {html_escape(str(exc))}")
            return

        if data == "cancelalert":
            pending_custom_interval.pop((int(chat_id), user_id), None)
            api.send(chat_id, "Pengaturan alert dibatalkan.")
            return

        if data == "alertlist":
            send_alert_list(chat_id)
            return

        if data.startswith("eam:"):
            parts = data.split(":", 3)
            if len(parts) != 4 or parts[1] not in ("v3", "v4"):
                api.send(chat_id, "Data alert Uniswap tidak valid.")
                return
            try:
                chain_id = int(parts[2])
            except ValueError:
                api.send(chat_id, "Chain ID tidak valid.")
                return
            protocol = parts[1]
            try:
                _, pools = uniswap.find_pools_for_token(chain_id, parts[3], protocol)
                if not pools:
                    api.send(chat_id, "Pool Uniswap tidak ditemukan.")
                    return
                api.send(
                    chat_id,
                    "<b>🔔 Pilih pool Uniswap yang ingin dimonitor:</b>",
                    parse_mode="HTML",
                    reply_markup=evm_pool_choice_keyboard(chain_id, pools),
                )
                pending_evm_address[(int(chat_id), user_id)] = f"{chain_id}:{protocol}:{parts[3]}"
            except ApiError as exc:
                api.send(chat_id, f"Gagal mengambil pool Uniswap: {html_escape(str(exc))}")
            return

        if data.startswith("eap:"):
            parts = data.split(":", 3)
            if len(parts) != 4 or parts[1] not in ("v3", "v4"):
                api.send(chat_id, "Data pool Uniswap tidak valid.")
                return
            try:
                chain_id = int(parts[2])
            except ValueError:
                api.send(chat_id, "Chain ID tidak valid.")
                return
            # The token address is attached to the most recent EVM report in this chat.
            state = pending_evm_address.get((int(chat_id), user_id), "")
            state_parts = state.split(":", 2)
            if len(state_parts) != 3:
                api.send(chat_id, "Sesi token sudah habis. Kirim ulang contract EVM.")
                return
            try:
                pool = find_evm_pool_by_ref(chain_id, parts[1], state_parts[2], parts[3])
            except ApiError as exc:
                api.send(chat_id, f"Gagal mengambil pool Uniswap: {html_escape(str(exc))}")
                return
            api.send(
                chat_id,
                f"Pilih interval alert untuk <b>{html_escape(pool.name)}</b>:",
                parse_mode="HTML",
                reply_markup=evm_interval_keyboard(parts[1], chain_id, parts[3]),
            )
            return

        if data.startswith("ei:"):
            parts = data.split(":", 4)
            if len(parts) != 5 or parts[1] not in ("v3", "v4"):
                api.send(chat_id, "Data interval Uniswap tidak valid.")
                return
            try:
                chain_id = int(parts[2])
                minutes = max(1, min(int(parts[4]), 1440))
            except ValueError:
                api.send(chat_id, "Interval alert tidak valid.")
                return
            state = pending_evm_address.pop((int(chat_id), user_id), "")
            state_parts = state.split(":", 2)
            if len(state_parts) != 3:
                api.send(chat_id, "Sesi token sudah habis. Kirim ulang contract EVM.")
                return
            try:
                pool = find_evm_pool_by_ref(chain_id, parts[1], state_parts[2], parts[3])
                enable_evm_alert(chat_id, user_id, parts[1], chain_id, state_parts[2], pool.address, minutes)
            except ApiError as exc:
                api.send(chat_id, f"Gagal mengaktifkan alert Uniswap: {html_escape(str(exc))}")
            return

        if data.startswith("ec:"):
            parts = data.split(":", 3)
            if len(parts) != 4 or parts[1] not in ("v3", "v4"):
                api.send(chat_id, "Data custom alert Uniswap tidak valid.")
                return
            try:
                chain_id = int(parts[2])
            except ValueError:
                api.send(chat_id, "Chain ID tidak valid.")
                return
            state = pending_evm_address.get((int(chat_id), user_id), "")
            state_parts = state.split(":", 2)
            if len(state_parts) != 3:
                api.send(chat_id, "Sesi token sudah habis. Kirim ulang contract EVM.")
                return
            try:
                pool = find_evm_pool_by_ref(chain_id, parts[1], state_parts[2], parts[3])
                pending_custom_interval[(int(chat_id), user_id)] = f"evm:{parts[1]}:{chain_id}:{state_parts[2]}:{pool.address}"
            except ApiError as exc:
                api.send(chat_id, f"Gagal mengambil pool Uniswap: {html_escape(str(exc))}")
                return
            api.send(chat_id, "Kirim angka interval dalam menit (1–1440). Contoh: 20")
            return

        if data.startswith("es:"):
            parts = data.split(":", 3)
            if len(parts) != 4 or parts[1] not in ("v3", "v4"):
                api.send(chat_id, "Data stop alert Uniswap tidak valid.")
                return
            try:
                chain_id = int(parts[2])
            except ValueError:
                api.send(chat_id, "Chain ID tidak valid.")
                return
            before = len(alerts)
            alerts[:] = [
                alert for alert in alerts
                if not (
                    str(alert.get("chat_id")) == str(chat_id)
                    and alert.get("provider") == "uniswap"
                    and str(alert.get("protocol", "v3")) == parts[1]
                    and int(alert.get("chain_id", -1)) == chain_id
                    and evm_pool_ref(str(alert.get("pool_address", ""))) == parts[3]
                )
            ]
            if len(alerts) != before:
                save_alerts(alerts)
                api.send(chat_id, "🔕 Alert Uniswap dimatikan.")
            else:
                api.send(chat_id, "Alert Uniswap tersebut sudah tidak aktif.")
            return

        if data.startswith("alertmenu:"):
            mint = data.split(":", 1)[1]
            choices = pool_choices_for_mint(mint)
            if not choices:
                api.send(chat_id, "Pool tidak ditemukan atau data pool sudah tidak tersedia.")
                return
            api.send(
                chat_id,
                "<b>🔔 Pilih pool yang ingin dimonitor:</b>",
                parse_mode="HTML",
                reply_markup=pool_choice_keyboard(choices),
            )
            return

        if data.startswith("alertpool:"):
            pool_address = data.split(":", 1)[1]
            if pool_address not in pool_catalog:
                try:
                    pool = meteora.get_pool(pool_address)
                    pool_catalog[pool_address] = {
                        "mint": str(pool.token_x.get("address", "")),
                        "pool": pool,
                    }
                except ApiError:
                    api.send(chat_id, "Pool sudah tidak ditemukan di Meteora.")
                    return
            pool = pool_catalog[pool_address]["pool"]
            api.send(
                chat_id,
                f"Pilih interval alert untuk <b>{html_escape(pool.name)}</b>:",
                parse_mode="HTML",
                reply_markup=interval_keyboard(pool_address),
            )
            return

        if data.startswith("interval:"):
            parts = data.split(":")
            if len(parts) == 3:
                try:
                    enable_alert(chat_id, user_id, parts[1], max(1, min(int(parts[2]), 1440)))
                except ValueError:
                    api.send(chat_id, "Interval alert tidak valid.")
            return

        if data.startswith("custom:"):
            pool_address = data.split(":", 1)[1]
            pending_custom_interval[(int(chat_id), user_id)] = pool_address
            api.send(chat_id, "Kirim angka interval dalam menit (1–1440). Contoh: 20")
            return

        if data.startswith("stop:"):
            pool_address = data.split(":", 1)[1]
            before = len(alerts)
            alerts[:] = [
                alert
                for alert in alerts
                if not (
                    str(alert.get("chat_id")) == str(chat_id)
                    and str(alert.get("pool_address")) == pool_address
                )
            ]
            if len(alerts) != before:
                save_alerts(alerts)
                api.send(chat_id, "🔕 Alert dimatikan.")
            else:
                api.send(chat_id, "Alert tersebut sudah tidak aktif.")

    print("Meteora APR Telegram Bot aktif — mode read-only")
    print_credit()

    while True:
        try:
            send_due_alerts(api, meteora, uniswap, alerts, max_items, min_pool_tvl)
            updates = api.call(
                "getUpdates",
                {
                    "offset": last_update_id + 1,
                    "timeout": 30,
                    "allowed_updates": ["message", "callback_query"],
                },
                timeout=40,
            ) or []
            for update in updates:
                last_update_id = max(last_update_id, int(update.get("update_id", 0)))
                if update.get("callback_query"):
                    callback = update["callback_query"]
                    sender = callback.get("from") or {}
                    callback_user_id = int(sender.get("id", 0))
                    if allowed_user_ids and callback_user_id not in allowed_user_ids:
                        try:
                            api.answer_callback(str(callback.get("id", "")), "Akses tidak diizinkan")
                        except ApiError:
                            pass
                        continue
                    handle_callback(callback)
                    continue

                message = update.get("message") or {}
                chat = message.get("chat") or {}
                sender = message.get("from") or {}
                chat_id = chat.get("id")
                user_id = int(sender.get("id", chat_id or 0))
                text = str(message.get("text") or "").strip()
                if chat_id is None:
                    continue

                if allowed_user_ids and user_id not in allowed_user_ids:
                    api.send(chat_id, "Akses bot ini belum diizinkan untuk Telegram ID kamu.")
                    continue

                if text == "/chains":
                    try:
                        chains = uniswap.get_supported_chains()
                        api.send(
                            chat_id,
                            "<b>🌐 Chain Uniswap yang tersedia</b>\n\n"
                            "V3/V4 menandakan versi yang sudah dikonfigurasi.\n"
                            "⚙️ setup = tambahkan subgraph ID di .env",
                            parse_mode="HTML",
                            reply_markup=evm_chain_keyboard(chains),
                        )
                    except ApiError as exc:
                        api.send(chat_id, f"Gagal mengambil daftar chain Uniswap: {html_escape(str(exc))}")
                    continue

                if text.startswith("/evm") or valid_evm_address(text):
                    requested_chain, evm_address = evm_command_parts(text)
                    if not valid_evm_address(evm_address):
                        api.send(
                            chat_id,
                            "Format EVM tidak valid. Contoh:\n/evm 1 0x0000000000000000000000000000000000000000",
                        )
                        continue
                    if requested_chain is None:
                        try:
                            chains = uniswap.get_supported_chains()
                            pending_evm_address[(int(chat_id), user_id)] = evm_address
                            api.send(
                                chat_id,
                                f"Pilih chain untuk contract <code>{html_escape(evm_address)}</code>:",
                                parse_mode="HTML",
                                reply_markup=evm_chain_keyboard(chains),
                            )
                        except ApiError as exc:
                            api.send(chat_id, f"Gagal mengambil daftar chain Uniswap: {html_escape(str(exc))}")
                        continue
                    if not valid_uniswap_chain(requested_chain):
                        api.send(chat_id, "Chain yang dipilih belum diaktifkan. Gunakan /chains untuk melihat pilihan yang tersedia.")
                        continue
                    try:
                        uniswap.get_supported_chains()
                        pending_evm_address[(int(chat_id), user_id)] = evm_address
                        api.send(
                            chat_id,
                            f"Pilih versi Uniswap untuk {html_escape(chain_name(requested_chain))}:",
                            parse_mode="HTML",
                            reply_markup=evm_protocol_keyboard(requested_chain, evm_address, uniswap),
                        )
                    except ApiError as exc:
                        api.send(chat_id, f"Gagal mengambil konfigurasi Uniswap: {html_escape(str(exc))}")
                    continue

                if text in ("/start", "/help"):
                    api.send(
                        chat_id,
                        "Kirim mint token Solana untuk cek APR Meteora DLMM.\n\n"
                        "Contoh:\n"
                        "So11111111111111111111111111111111111111112\n\n"
                        "Bisa juga: /apr <mint>\n\n"
                        "Untuk EVM/Uniswap: kirim contract address 0x… lalu pilih chain, "
                        "atau gunakan /evm <chain_id> <contract>.\n"
                        "Gunakan /chains untuk melihat chain Uniswap.\n\n"
                        "Setelah hasil muncul, tekan 🔔 Set Alert untuk menerima update APR dan volume berkala.\n"
                        "Gunakan /alerts untuk melihat alert aktif.",
                    )
                    continue

                if text == "/alerts":
                    send_alert_list(chat_id)
                    continue

                if text == "/stopalerts":
                    before = len(alerts)
                    alerts[:] = [alert for alert in alerts if str(alert.get("chat_id")) != str(chat_id)]
                    if len(alerts) != before:
                        save_alerts(alerts)
                    api.send(chat_id, "🔕 Semua alert di chat ini sudah dimatikan.")
                    continue

                pending_key = (int(chat_id), user_id)
                if pending_key in pending_custom_interval:
                    try:
                        minutes = int(text)
                        if not 1 <= minutes <= 1440:
                            raise ValueError
                        pending_alert = pending_custom_interval.pop(pending_key)
                        if pending_alert.startswith("evm:"):
                            _, protocol, chain_text, token_address, pool_address = pending_alert.split(":", 4)
                            enable_evm_alert(chat_id, user_id, protocol, int(chain_text), token_address, pool_address, minutes)
                        else:
                            enable_alert(chat_id, user_id, pending_alert, minutes)
                    except ValueError:
                        api.send(chat_id, "Masukkan angka menit antara 1 dan 1440. Contoh: 20")
                    continue

                mint = command_argument(text)
                if not valid_mint(mint):
                    api.send(chat_id, "Format address tidak valid. Kirim contract/mint Solana 32–44 karakter Base58.")
                    continue

                now = time.monotonic()
                remaining = cooldown - (now - last_request_by_user.get(user_id, 0))
                if remaining > 0:
                    api.send(chat_id, f"Tunggu {remaining:.0f} detik sebelum request berikutnya.")
                    continue
                last_request_by_user[user_id] = now

                api.send(chat_id, "Sedang mengambil data pool Meteora…")
                try:
                    try:
                        pools = [meteora.get_pool(mint)]
                        partial = False
                    except ApiError:
                        pools, partial = meteora.find_pools_for_mint(mint)
                    display_pools, filtered_count = prepare_display_pools(
                        mint, pools, meteora, max_items, min_pool_tvl
                    )
                    api.send(
                        chat_id,
                        render_pools(
                            mint,
                            display_pools,
                            partial,
                            max_items,
                            min_pool_tvl,
                            filtered_count,
                        ),
                        parse_mode="HTML",
                        reply_markup=alert_keyboard(mint) if display_pools else None,
                    )
                except ApiError as exc:
                    api.send(chat_id, f"Gagal mengambil data Meteora: {exc}")
                except Exception:
                    api.send(chat_id, "Terjadi error internal saat memproses request. Coba lagi nanti.")
        except KeyboardInterrupt:
            print("\nBot dihentikan.")
            return
        except Exception as exc:
            print(f"[{datetime.now().isoformat(timespec='seconds')}] polling error: {exc}")
            time.sleep(5)


if __name__ == "__main__":
    main()
