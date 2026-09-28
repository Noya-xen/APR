"""Telegram bot untuk membaca APR pool Meteora DLMM.

Bot ini read-only: tidak meminta private key, tidak menandatangani transaksi,
dan hanya memakai Meteora DLMM Data API serta Telegram Bot API.
"""

from __future__ import annotations

import json
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
MAX_TELEGRAM_MESSAGE = 4096
ALERTS_FILE = "alerts.json"


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
) -> dict[str, Any]:
    now = time.time()
    record = {
        "id": alert_key(chat_id, pool.address),
        "chat_id": chat_id,
        "user_id": user_id,
        "mint": mint,
        "pool_address": pool.address,
        "pool_name": pool.name,
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
            api.send(
                alert["chat_id"],
                alert_text,
                parse_mode="HTML",
                reply_markup=alert_control_keyboard(str(alert["pool_address"])),
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
    last_update_id = 0
    last_request_by_user: dict[int, float] = {}
    alerts = load_alerts()
    pool_catalog: dict[str, dict[str, Any]] = {}
    pending_custom_interval: dict[tuple[int, int], str] = {}

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
            lines.append(f"{index}. <b>{name}</b> — setiap {minutes} menit")
            rows.append([
                {
                    "text": f"🔕 Matikan {index}",
                    "callback_data": f"stop:{alert.get('pool_address', '')}",
                }
            ])
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

        if data == "cancelalert":
            pending_custom_interval.pop((int(chat_id), user_id), None)
            api.send(chat_id, "Pengaturan alert dibatalkan.")
            return

        if data == "alertlist":
            send_alert_list(chat_id)
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
            send_due_alerts(api, meteora, alerts, max_items, min_pool_tvl)
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

                if text in ("/start", "/help"):
                    api.send(
                        chat_id,
                        "Kirim mint token Solana untuk cek APR Meteora DLMM.\n\n"
                        "Contoh:\n"
                        "So11111111111111111111111111111111111111112\n\n"
                        "Bisa juga: /apr <mint>\n\n"
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
                        pool_address = pending_custom_interval.pop(pending_key)
                        enable_alert(chat_id, user_id, pool_address, minutes)
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
