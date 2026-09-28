"""Telegram bot untuk membaca APR pool Meteora DLMM.

Bot ini read-only: tidak meminta private key, tidak menandatangani transaksi,
dan hanya memakai Meteora DLMM Data API serta Telegram Bot API.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any


METEORA_API_DEFAULT = "https://dlmm.datapi.meteora.ag"
SOLANA_ADDRESS_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
MAX_TELEGRAM_MESSAGE = 4096


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
    timeout: int = 45,
) -> Any:
    body = None
    headers = {"Accept": "application/json", "User-Agent": "meteora-apr-telegram-bot/1.0"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=body, headers=headers, method=method)
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


def pool_line(pool: PoolResult, index: int, mint: str) -> str:
    volume_24h = (pool.raw.get("volume") or {}).get("24h")
    volume_1h = (pool.raw.get("volume") or {}).get("1h")
    token = pool.token_x if pool.token_x.get("address", "").lower() == mint.lower() else pool.token_y
    symbol = token.get("symbol") or "token"
    return (
        f"{index}. {pool.name}\n"
        f"   Pool: {pool.address}\n"
        f"   Market cap {symbol}: {money(token.get('market_cap'))}\n"
        f"   TVL: {money(pool.raw.get('tvl'))}\n"
        f"   Volume 15m: {money(pool.raw.get('_volume_15m'))} | 1h: {money(volume_1h)}\n"
        f"   Volume 24h: {money(volume_24h)}\n"
        f"   Fee APR 24h: {percent(pool.fee_apr)} | farm APR: {percent(pool.farm_apr)}\n"
        f"   Estimasi total APR: {percent(pool.total_apr)}"
    )


def render_pools(mint: str, pools: list[PoolResult], partial: bool, max_items: int = 10) -> str:
    if not pools:
        return (
            "Tidak ada pool Meteora DLMM untuk mint ini.\n\n"
            "Pastikan contract address yang dikirim adalah mint token Solana, bukan alamat wallet."
        )

    token = pools[0].token_x if pools[0].token_x.get("address", "").lower() == mint.lower() else pools[0].token_y
    symbol = token.get("symbol") or "?"
    name = token.get("name") or "Unknown token"
    shown = pools[:max_items]
    lines = [
        "Meteora DLMM APR",
        f"Token: {name} ({symbol})",
        f"Mint: {mint}",
        f"Pool ditemukan: {len(pools)} | Ditampilkan: {len(shown)}",
        "",
    ]
    lines.extend(pool_line(pool, i, mint) for i, pool in enumerate(shown, 1))
    lines.extend(
        [
            "",
            "Catatan: APR berasal dari data 24 jam Meteora dan dapat berubah cepat.",
            "Estimasi total APR = fee APR + farm APR; bukan jaminan hasil dan belum memperhitungkan impermanent loss.",
        ]
    )
    if partial:
        lines.append("Peringatan: hasil pencarian dipotong oleh batas pagination bot.")

    message = "\n".join(lines)
    return message[:MAX_TELEGRAM_MESSAGE]


class TelegramBotApi:
    def __init__(self, token: str):
        self.base_url = f"https://api.telegram.org/bot{token}"

    def call(self, method: str, payload: dict[str, Any] | None = None, timeout: int = 45) -> Any:
        result = request_json(f"{self.base_url}/{method}", method="POST", payload=payload or {}, timeout=timeout)
        if not isinstance(result, dict) or not result.get("ok"):
            raise ApiError(f"Telegram API error: {result}")
        return result.get("result")

    def send(self, chat_id: int | str, text: str) -> None:
        self.call("sendMessage", {"chat_id": chat_id, "text": text})


def valid_mint(value: str) -> bool:
    return bool(SOLANA_ADDRESS_RE.fullmatch(value))


def command_argument(text: str) -> str:
    value = text.strip().replace("`", "")
    if value.lower().startswith("/apr"):
        value = value[4:].strip().split()[0] if value[4:].strip() else ""
    return value


def print_credit() -> None:
    print("  *==========================================*")
    print("    > Built by: Noya-xen (Github)")
    print("    > Follow me on X : @xinomixo")
    print("  *==========================================*\n")


def main() -> None:
    load_env_file()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN belum diisi. Salin .env.example menjadi .env lalu isi token BotFather.")

    allowed_raw = os.getenv("ALLOWED_USER_IDS", "").strip()
    allowed_user_ids = {int(item.strip()) for item in allowed_raw.split(",") if item.strip()} if allowed_raw else set()
    max_items = max(1, min(int(os.getenv("MAX_POOLS_IN_MESSAGE", "10")), 15))
    cooldown = max(0, int(os.getenv("USER_COOLDOWN_SECONDS", "5")))
    api = TelegramBotApi(token)
    meteora = MeteoraClient(os.getenv("METEORA_API_URL", METEORA_API_DEFAULT))
    last_update_id = 0
    last_request_by_user: dict[int, float] = {}

    print("Meteora APR Telegram Bot aktif — mode read-only")
    print_credit()

    while True:
        try:
            updates = api.call(
                "getUpdates",
                {"offset": last_update_id + 1, "timeout": 30, "allowed_updates": ["message"]},
                timeout=40,
            ) or []
            for update in updates:
                last_update_id = max(last_update_id, int(update.get("update_id", 0)))
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

                if text in ("/start", "/help"):
                    api.send(
                        chat_id,
                        "Kirim mint token Solana untuk cek APR Meteora DLMM.\n\n"
                        "Contoh:\n"
                        "So11111111111111111111111111111111111111112\n\n"
                        "Bisa juga: /apr <mint>",
                    )
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
                        meteora.enrich_with_15m_volume(pools[:max_items])
                        api.send(chat_id, render_pools(mint, pools, partial, max_items))
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
