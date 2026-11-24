#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
💎 Live Financial Tracker Bot — Turbo Pro Edition (FIXED)
Engine: Background Sync + Stale-While-Revalidate (0ms Cache) + Concurrent Updates
UI: 100% Colored Native Buttons + Quick Switch Bar + Full Admin Panel + Multi-Channel Force Sub
"""

import os
import json
import html
import time
import inspect
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from typing import Any, Optional

import httpx
import aiosqlite
from dotenv import load_dotenv
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes,
)

load_dotenv()
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
log = logging.getLogger("bot")

START_TIME = time.monotonic()

# ═══════════════════════════════════════════════════════════
# ⚙️ CONFIG
# ═══════════════════════════════════════════════════════════

def _csv_ints(v: str) -> set[int]:
    return {int(x.strip()) for x in v.split(",") if x.strip().isdigit()}

@dataclass(frozen=True)
class Settings:
    token: str = os.getenv("BOT_TOKEN", os.getenv("TELEGRAM_BOT_TOKEN", ""))
    admin_ids: set[int] = field(default_factory=lambda: _csv_ints(os.getenv("ADMIN_IDS", "")))
    db_path: str = os.getenv("DB_PATH", "financial_tracker.sqlite3")
    tz: str = os.getenv("TIMEZONE", "Africa/Cairo")
    crypto_ttl: int = int(os.getenv("CRYPTO_TTL", "12"))
    movers_ttl: int = int(os.getenv("MOVERS_TTL", "60"))
    metals_ttl: int = int(os.getenv("METALS_TTL", "25"))
    fx_ttl: int = int(os.getenv("FX_TTL", "600"))
    oil_ttl: int = int(os.getenv("OIL_TTL", "45"))
    timeout: float = float(os.getenv("REQUEST_TIMEOUT", "8"))
    alert_interval: int = int(os.getenv("ALERT_INTERVAL", "20"))
    webapp_url: str = os.getenv("WEBAPP_URL", "")

S = Settings()
TZ = ZoneInfo(S.tz)
OZ_G = 31.1034768

# ═══════════════════════════════════════════════════════════
# ⚡ IN-MEMORY FAST CACHES (Zero-Latency RAM Layer)
# ═══════════════════════════════════════════════════════════

RUNTIME_CFG: dict[str, Any] = {
    "fsub_enabled": False,
    "maintenance": False,
    "colored_buttons": True,
    "gold_spread_pct": 0.5,
    "welcome_text": "",
    "dynamic_admins": set(),
    "banned_users": set(),
    "fsub_channels": [],
}

_FSUB_OK_CACHE: dict[int, float] = {}
_TOUCH_CACHE: dict[int, float] = {}
_LAST_REFRESH: dict[int, float] = {}
_BG_SYNC_COUNT: int = 0
_WARMUP_TASK: Optional[asyncio.Task] = None

_BTN_HAS_STYLE = "style" in inspect.signature(InlineKeyboardButton.__init__).parameters

def btn(
    text: str,
    callback_data: Optional[str] = None,
    url: Optional[str] = None,
    style: str = "primary",
) -> InlineKeyboardButton:
    """إنشاء زر ملون 100% متوافق مع جميع إصدارات python-telegram-bot."""
    kwargs: dict[str, Any] = {"text": text}
    if callback_data is not None:
        kwargs["callback_data"] = callback_data
    if url is not None:
        kwargs["url"] = url
    if style in ("primary", "success", "danger") and RUNTIME_CFG.get("colored_buttons", True):
        if _BTN_HAS_STYLE:
            kwargs["style"] = style
        # else: نتجاهل الـ style بدل ما نرمي exception
    return InlineKeyboardButton(**kwargs)

def is_admin(uid: int) -> bool:
    return uid in S.admin_ids or uid in RUNTIME_CFG["dynamic_admins"]

# ═══════════════════════════════════════════════════════════
# 🎨 FORMATTING & HELPERS
# ═══════════════════════════════════════════════════════════

COIN_ICON = {
    "BTC": "🟠", "ETH": "🔷", "SOL": "🟪", "BNB": "🟨",
    "TON": "💎", "XRP": "⚪", "DOGE": "🐕", "USDT": "🟢",
}
DEFAULT_COINS = ["BTC", "ETH", "SOL", "BNB", "TON", "XRP", "DOGE", "USDT"]

ASSET_LABELS = {
    "XAU": "أونصة الذهب (USD)",
    "XAG": "أونصة الفضة (USD)",
    "GOLD_24": "ذهب عيار 24 (EGP)",
    "GOLD_21": "ذهب عيار 21 (EGP)",
    "GOLD_18": "ذهب عيار 18 (EGP)",
    "GOLD_COIN": "الجنيه الذهب (EGP)",
    "SILVER_999": "فضة 999 (EGP)",
    "SILVER_925": "فضة 925 (EGP)",
    "USD_EGP": "الدولار (EGP)",
    "BRENT": "خام برنت (USD)",
    "BTC": "بيتكوين BTC (USD)",
    "ETH": "إيثيريوم ETH (USD)",
    "SOL": "سولانا SOL (USD)",
    "BNB": "بينانس BNB (USD)",
    "TON": "تون TON (USD)",
}

def esc(v: Any) -> str:
    return html.escape(str(v or ""))

def fmt(v: Optional[float], d: int = 2) -> str:
    if v is None:
        return "—"
    if d == 2 and 0 < abs(v) < 1:
        d = 4
    return f"{v:,.{d}f}"

def now_local() -> datetime:
    return datetime.now(TZ)

def now_hm() -> str:
    return now_local().strftime("%H:%M:%S")

def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def format_uptime() -> str:
    sec = int(time.monotonic() - START_TIME)
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if d: parts.append(f"{d}ي")
    if h: parts.append(f"{h}س")
    if m: parts.append(f"{m}د")
    parts.append(f"{s}ث")
    return " ".join(parts)

def trend(pct: Optional[float]) -> tuple[str, str]:
    if pct is None:
        return "⚪ →", "░" * 8
    if pct > 0.05:
        ic = "🟢 ▲"
    elif pct < -0.05:
        ic = "🔴 ▼"
    else:
        ic = "⚪ •"
    norm = max(0.0, min(1.0, (pct + 5.0) / 10.0))
    fill = int(norm * 8)
    return ic, "█" * fill + "░" * (8 - fill)

def head(emoji: str, title: str, badge: str = "⚡ مباشر") -> str:
    return (
        f"╭──────────────────────╮\n"
        f" {emoji} {title}\n"
        f" {badge} • 🕐 {now_hm()}\n"
        f"╰──────────────────────╯"
    )

async def safe_answer(q, text: Optional[str] = None, show_alert: bool = False):
    try:
        await q.answer(text=text, show_alert=show_alert)
    except TelegramError:
        pass

def answer_bg(q, text: Optional[str] = None, show_alert: bool = False):
    asyncio.create_task(safe_answer(q, text, show_alert))

async def safe_edit(q, text: str, kb=None):
    try:
        await q.edit_message_text(
            text,
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except BadRequest as e:
        msg = str(e).lower()
        if "not modified" in msg:
            return
        if "message to edit not found" in msg or "there is no text" in msg:
            try:
                await q.message.reply_text(
                    text, reply_markup=kb, parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except TelegramError:
                pass
        else:
            log.warning(f"safe_edit error: {e}")
    except TelegramError as e:
        log.warning(f"safe_edit telegram error: {e}")

# ═══════════════════════════════════════════════════════════
# 💾 DATABASE (Persistent WAL + RAM Sync)
# ═══════════════════════════════════════════════════════════

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA temp_store=MEMORY;
PRAGMA busy_timeout=10000;

CREATE TABLE IF NOT EXISTS users(
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    joined_at TEXT DEFAULT CURRENT_TIMESTAMP,
    last_seen TEXT,
    is_banned INTEGER DEFAULT 0,
    is_blocked INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS alerts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('above','below')),
    target REAL NOT NULL,
    active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    triggered_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_alert_active ON alerts(active, symbol);
CREATE TABLE IF NOT EXISTS holdings(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    quantity REAL NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, symbol)
);
CREATE TABLE IF NOT EXISTS digest(
    user_id INTEGER PRIMARY KEY,
    enabled INTEGER DEFAULT 0,
    local_time TEXT DEFAULT '09:00',
    last_sent TEXT
);
CREATE TABLE IF NOT EXISTS api_status(
    provider TEXT PRIMARY KEY,
    ok INTEGER,
    checked_at TEXT,
    detail TEXT,
    latency_ms INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings(
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS fsub_channels(
    chat_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    invite_link TEXT NOT NULL,
    added_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS admins(
    user_id INTEGER PRIMARY KEY,
    added_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

class DB:
    def __init__(self, path: str):
        self.path = path
        self.conn: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    async def init(self):
        self.conn = await aiosqlite.connect(self.path, timeout=15)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        for sql in (
            "ALTER TABLE users ADD COLUMN is_banned INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN is_blocked INTEGER DEFAULT 0",
            "ALTER TABLE api_status ADD COLUMN latency_ms INTEGER DEFAULT 0",
        ):
            try:
                await self.conn.execute(sql)
            except Exception:
                pass
        await self.conn.commit()
        await self.load_runtime_cfg()

    async def close(self):
        if self.conn:
            await self.conn.close()
            self.conn = None

    async def checkpoint(self):
        async with self._lock:
            await self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            await self.conn.commit()

    async def load_runtime_cfg(self):
        cur = await self.conn.execute("SELECT key, value FROM settings")
        rows = {r["key"]: r["value"] for r in await cur.fetchall()}
        RUNTIME_CFG["fsub_enabled"] = rows.get("fsub_enabled", "0") == "1"
        RUNTIME_CFG["maintenance"] = rows.get("maintenance", "0") == "1"
        RUNTIME_CFG["colored_buttons"] = rows.get("colored_buttons", "1") == "1"
        try:
            RUNTIME_CFG["gold_spread_pct"] = float(rows.get("gold_spread_pct", "0.5"))
        except ValueError:
            RUNTIME_CFG["gold_spread_pct"] = 0.5
        RUNTIME_CFG["welcome_text"] = rows.get("welcome_text", "")

        cur2 = await self.conn.execute("SELECT user_id FROM admins")
        RUNTIME_CFG["dynamic_admins"] = {r["user_id"] for r in await cur2.fetchall()}

        cur3 = await self.conn.execute("SELECT user_id FROM users WHERE is_banned=1")
        RUNTIME_CFG["banned_users"] = {r["user_id"] for r in await cur3.fetchall()}

        cur4 = await self.conn.execute("SELECT * FROM fsub_channels ORDER BY added_at ASC")
        RUNTIME_CFG["fsub_channels"] = [dict(r) for r in await cur4.fetchall()]

    async def set_setting(self, key: str, value: str):
        async with self._lock:
            await self.conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )
            await self.conn.commit()
        await self.load_runtime_cfg()

    # ---------- USERS ----------
    def touch_user_bg(self, uid: int, uname: Optional[str], fname: Optional[str]):
        now_t = time.monotonic()
        if now_t - _TOUCH_CACHE.get(uid, 0.0) < 60.0:
            return
        _TOUCH_CACHE[uid] = now_t
        asyncio.create_task(self.touch_user(uid, uname, fname))

    async def touch_user(self, uid: int, uname: Optional[str], fname: Optional[str]):
        try:
            async with self._lock:
                now = now_utc_iso()
                await self.conn.execute(
                    "INSERT INTO users(user_id, username, first_name, joined_at, last_seen, is_blocked) "
                    "VALUES(?, ?, ?, ?, ?, 0) "
                    "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, "
                    "first_name=excluded.first_name, last_seen=excluded.last_seen, is_blocked=0",
                    (uid, uname or "", fname or "مستخدم", now, now),
                )
                await self.conn.commit()
        except Exception as e:
            log.warning(f"touch_user err: {e}")

    async def get_user(self, uid: int) -> Optional[dict]:
        cur = await self.conn.execute("SELECT * FROM users WHERE user_id=?", (uid,))
        r = await cur.fetchone()
        return dict(r) if r else None

    async def find_user(self, query: str) -> Optional[dict]:
        q = query.strip().lstrip("@")
        if q.isdigit():
            return await self.get_user(int(q))
        cur = await self.conn.execute(
            "SELECT * FROM users WHERE LOWER(username)=LOWER(?)", (q,)
        )
        r = await cur.fetchone()
        return dict(r) if r else None

    def is_banned_fast(self, uid: int) -> bool:
        return (not is_admin(uid)) and (uid in RUNTIME_CFG["banned_users"])

    async def set_ban(self, uid: int, banned: bool):
        async with self._lock:
            await self.conn.execute(
                "UPDATE users SET is_banned=? WHERE user_id=?",
                (1 if banned else 0, uid),
            )
            await self.conn.commit()
        if banned:
            RUNTIME_CFG["banned_users"].add(uid)
        else:
            RUNTIME_CFG["banned_users"].discard(uid)

    async def mark_blocked(self, uid: int, blocked: bool = True):
        async with self._lock:
            await self.conn.execute(
                "UPDATE users SET is_blocked=? WHERE user_id=?",
                (1 if blocked else 0, uid),
            )
            await self.conn.commit()

    async def recent_users(self, limit: int = 10) -> list[dict]:
        cur = await self.conn.execute(
            "SELECT * FROM users ORDER BY last_seen DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in await cur.fetchall()]

    async def banned_users(self) -> list[dict]:
        cur = await self.conn.execute(
            "SELECT * FROM users WHERE is_banned=1 ORDER BY last_seen DESC LIMIT 20"
        )
        return [dict(r) for r in await cur.fetchall()]

    async def broadcast_targets(self) -> list[int]:
        cur = await self.conn.execute(
            "SELECT user_id FROM users WHERE is_banned=0 AND is_blocked=0"
        )
        return [r["user_id"] for r in await cur.fetchall()]

    async def stats(self) -> dict[str, int]:
        since_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
        today_utc = datetime.now(timezone.utc).date().isoformat()
        q = {
            "total": "SELECT COUNT(*) FROM users",
            "active_24h": "SELECT COUNT(*) FROM users WHERE last_seen >= ?",
            "new_today": "SELECT COUNT(*) FROM users WHERE joined_at >= ?",
            "banned": "SELECT COUNT(*) FROM users WHERE is_banned=1",
            "blocked": "SELECT COUNT(*) FROM users WHERE is_blocked=1",
            "alerts": "SELECT COUNT(*) FROM alerts WHERE active=1",
            "portfolios": "SELECT COUNT(DISTINCT user_id) FROM holdings",
            "digest": "SELECT COUNT(*) FROM digest WHERE enabled=1",
            "fsub_count": "SELECT COUNT(*) FROM fsub_channels",
        }
        out = {}
        for k, sql in q.items():
            if k == "active_24h":
                cur = await self.conn.execute(sql, (since_24h,))
            elif k == "new_today":
                cur = await self.conn.execute(sql, (today_utc,))
            else:
                cur = await self.conn.execute(sql)
            out[k] = (await cur.fetchone())[0]
        return out

    # ---------- ADMINS ----------
    async def add_admin(self, uid: int):
        async with self._lock:
            await self.conn.execute(
                "INSERT OR IGNORE INTO admins(user_id) VALUES(?)", (uid,)
            )
            await self.conn.commit()
        await self.load_runtime_cfg()

    async def remove_admin(self, uid: int):
        async with self._lock:
            await self.conn.execute("DELETE FROM admins WHERE user_id=?", (uid,))
            await self.conn.commit()
        await self.load_runtime_cfg()

    # ---------- FORCE SUBSCRIBE ----------
    async def add_fsub_channel(self, chat_id: str, title: str, invite_link: str):
        async with self._lock:
            await self.conn.execute(
                "INSERT INTO fsub_channels(chat_id, title, invite_link) VALUES(?, ?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, invite_link=excluded.invite_link",
                (str(chat_id), title, invite_link),
            )
            await self.conn.commit()
        _FSUB_OK_CACHE.clear()
        await self.load_runtime_cfg()

    async def remove_fsub_channel(self, chat_id: str):
        async with self._lock:
            await self.conn.execute(
                "DELETE FROM fsub_channels WHERE chat_id=?", (str(chat_id),)
            )
            await self.conn.commit()
        _FSUB_OK_CACHE.clear()
        await self.load_runtime_cfg()

    # ---------- ALERTS ----------
    async def add_alert(self, uid: int, sym: str, d: str, target: float) -> int:
        async with self._lock:
            cur = await self.conn.execute(
                "INSERT INTO alerts(user_id, symbol, direction, target) VALUES(?, ?, ?, ?)",
                (uid, sym.upper(), d, target),
            )
            await self.conn.commit()
            return cur.lastrowid

    async def active_alerts(self) -> list[dict]:
        cur = await self.conn.execute("SELECT * FROM alerts WHERE active=1")
        return [dict(r) for r in await cur.fetchall()]

    async def deactivate(self, aid: int):
        async with self._lock:
            await self.conn.execute(
                "UPDATE alerts SET active=0, triggered_at=? WHERE id=?",
                (now_utc_iso(), aid),
            )
            await self.conn.commit()

    async def user_alerts(self, uid: int) -> list[dict]:
        cur = await self.conn.execute(
            "SELECT * FROM alerts WHERE user_id=? AND active=1 ORDER BY id DESC", (uid,)
        )
        return [dict(r) for r in await cur.fetchall()]

    async def cancel_alert(self, uid: int, aid: int) -> bool:
        async with self._lock:
            cur = await self.conn.execute(
                "UPDATE alerts SET active=0 WHERE id=? AND user_id=? AND active=1",
                (aid, uid),
            )
            await self.conn.commit()
            return cur.rowcount > 0

    # ---------- PORTFOLIO ----------
    async def add_holding(self, uid: int, sym: str, qty: float):
        async with self._lock:
            await self.conn.execute(
                "INSERT INTO holdings(user_id, symbol, quantity) VALUES(?, ?, ?) "
                "ON CONFLICT(user_id, symbol) DO UPDATE SET quantity=quantity+excluded.quantity",
                (uid, sym.upper(), qty),
            )
            await self.conn.commit()

    async def remove_holding(self, uid: int, sym: str):
        async with self._lock:
            await self.conn.execute(
                "DELETE FROM holdings WHERE user_id=? AND symbol=?",
                (uid, sym.upper()),
            )
            await self.conn.commit()

    async def holdings(self, uid: int) -> list[dict]:
        cur = await self.conn.execute(
            "SELECT * FROM holdings WHERE user_id=? ORDER BY symbol", (uid,)
        )
        return [dict(r) for r in await cur.fetchall()]

    async def clear_holdings(self, uid: int):
        async with self._lock:
            await self.conn.execute("DELETE FROM holdings WHERE user_id=?", (uid,))
            await self.conn.commit()

    # ---------- DIGEST ----------
    async def get_digest(self, uid: int) -> Optional[dict]:
        cur = await self.conn.execute("SELECT * FROM digest WHERE user_id=?", (uid,))
        r = await cur.fetchone()
        return dict(r) if r else None

    async def set_digest(self, uid: int, enabled: bool, t: str = "09:00"):
        async with self._lock:
            await self.conn.execute(
                "INSERT INTO digest(user_id, enabled, local_time) VALUES(?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET enabled=excluded.enabled, local_time=excluded.local_time",
                (uid, 1 if enabled else 0, t),
            )
            await self.conn.commit()

    async def due_digest(self, hm: str, today: str) -> list[int]:
        cur = await self.conn.execute(
            "SELECT user_id FROM digest WHERE enabled=1 AND local_time=? "
            "AND (last_sent IS NULL OR last_sent<>?)",
            (hm, today),
        )
        return [r["user_id"] for r in await cur.fetchall()]

    async def mark_digest(self, uid: int, today: str):
        async with self._lock:
            await self.conn.execute(
                "UPDATE digest SET last_sent=? WHERE user_id=?", (today, uid)
            )
            await self.conn.commit()

    # ---------- API STATUS ----------
    def mark_api_bg(self, name: str, ok: bool, detail: str = "", latency_ms: int = 0):
        asyncio.create_task(self.mark_api(name, ok, detail, latency_ms))

    async def mark_api(self, name: str, ok: bool, detail: str = "", latency_ms: int = 0):
        try:
            async with self._lock:
                await self.conn.execute(
                    "INSERT INTO api_status(provider, ok, checked_at, detail, latency_ms) VALUES(?, ?, ?, ?, ?) "
                    "ON CONFLICT(provider) DO UPDATE SET ok=excluded.ok, "
                    "checked_at=excluded.checked_at, detail=excluded.detail, latency_ms=excluded.latency_ms",
                    (name, 1 if ok else 0, now_hm(), detail[:160], int(latency_ms)),
                )
                await self.conn.commit()
        except Exception:
            pass

    async def api_statuses(self) -> list[dict]:
        cur = await self.conn.execute("SELECT * FROM api_status ORDER BY provider")
        return [dict(r) for r in await cur.fetchall()]

db = DB(S.db_path)

# ═══════════════════════════════════════════════════════════
# 🌐 TURBO PRICE ENGINE (SWR 0ms Cache + Background Sync)
# ═══════════════════════════════════════════════════════════

BINANCE_MAP = {
    "BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT",
    "BNB": "BNBUSDT", "TON": "TONUSDT", "XRP": "XRPUSDT",
    "DOGE": "DOGEUSDT",
}

class Prices:
    def __init__(self):
        self.client = httpx.AsyncClient(
            timeout=S.timeout,
            headers={"User-Agent": "Mozilla/5.0 LiveFinancialBot/4.0"},
            follow_redirects=True,
            limits=httpx.Limits(max_connections=40, max_keepalive_connections=20),
        )
        self.cache: dict[str, tuple[float, Any]] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self._refreshing: set[str] = set()

    async def close(self):
        await self.client.aclose()

    def clear_cache(self):
        self.cache.clear()

    def cache_age(self, key: str) -> Optional[int]:
        c = self.cache.get(key)
        return int(time.monotonic() - c[0]) if c else None

    async def _get(self, url: str, params=None, headers=None):
        r = await self.client.get(url, params=params, headers=headers)
        r.raise_for_status()
        return r.json()

    def _trigger_bg_refresh(self, key: str, factory):
        if key in self._refreshing:
            return
        self._refreshing.add(key)

        async def _runner():
            try:
                val = await factory()
                self.cache[key] = (time.monotonic(), val)
            except Exception as e:
                log.debug(f"BG refresh failed for {key}: {e}")
            finally:
                self._refreshing.discard(key)

        asyncio.create_task(_runner())

    async def _cached(self, key: str, ttl: int, factory, force: bool = False):
        cached = self.cache.get(key)
        if not force and cached is not None:
            age = time.monotonic() - cached[0]
            if age >= ttl:
                self._trigger_bg_refresh(key, factory)
            return cached[1]

        lock = self.locks.setdefault(key, asyncio.Lock())
        async with lock:
            if not force:
                cached = self.cache.get(key)
                if cached is not None:
                    return cached[1]
            try:
                val = await factory()
                self.cache[key] = (time.monotonic(), val)
                return val
            except Exception:
                if cached is not None:
                    log.warning(f"Serving stale cache for {key}")
                    return cached[1]
                raise

    # ---------- CRYPTO ----------
    async def _fetch_binance_batch(self, symbols: list[str]) -> dict[str, dict]:
        pairs = [BINANCE_MAP[s] for s in symbols if s in BINANCE_MAP]
        out: dict[str, dict] = {}
        if pairs:
            rev = {v: k for k, v in BINANCE_MAP.items()}
            payload = json.dumps(pairs, separators=(",", ":"))
            data = await self._get(
                "https://api.binance.com/api/v3/ticker/24hr",
                {"symbols": payload},
            )
            for item in data:
                sym = rev.get(item["symbol"])
                if sym:
                    out[sym] = {
                        "price": float(item["lastPrice"]),
                        "change_24h": float(item["priceChangePercent"]),
                        "high_24h": float(item["highPrice"]),
                        "low_24h": float(item["lowPrice"]),
                        "source": "Binance",
                    }
        if "USDT" in symbols:
            out["USDT"] = {"price": 1.0, "change_24h": 0.0, "source": "Pegged"}
        return out

    async def _fetch_bybit(self, symbols: list[str]) -> dict[str, dict]:
        d = await self._get("https://api.bybit.com/v5/market/tickers", {"category": "spot"})
        wanted = {BINANCE_MAP[s]: s for s in symbols if s in BINANCE_MAP}
        out: dict[str, dict] = {}
        for item in d["result"]["list"]:
            if item["symbol"] in wanted:
                s = wanted[item["symbol"]]
                out[s] = {
                    "price": float(item["lastPrice"]),
                    "change_24h": float(item["price24hPcnt"]) * 100,
                    "source": "Bybit",
                }
        if "USDT" in symbols and "USDT" not in out:
            out["USDT"] = {"price": 1.0, "change_24h": 0.0, "source": "Pegged"}
        return out

    async def crypto(self, symbols: Optional[list[str]] = None, force: bool = False) -> dict[str, dict]:
        target_syms = DEFAULT_COINS

        async def fetch():
            t0 = time.monotonic()
            try:
                r = await self._fetch_binance_batch(target_syms)
                ms = int((time.monotonic() - t0) * 1000)
                if len(r) >= 4:
                    db.mark_api_bg("Binance", True, f"{len(r)}/{len(target_syms)} batch", ms)
                    return r
                raise ValueError(f"partial {len(r)}")
            except Exception as e:
                db.mark_api_bg("Binance", False, str(e))

            t1 = time.monotonic()
            try:
                r = await self._fetch_bybit(target_syms)
                ms = int((time.monotonic() - t1) * 1000)
                if r:
                    db.mark_api_bg("Bybit", True, f"{len(r)}/{len(target_syms)}", ms)
                    return r
                raise ValueError("empty")
            except Exception as e:
                db.mark_api_bg("Bybit", False, str(e))
                raise RuntimeError("All crypto sources failed")

        data = await self._cached("crypto:main", S.crypto_ttl, fetch, force)
        if symbols:
            return {k: v for k, v in data.items() if k in symbols}
        return data

    async def top_movers(self, force: bool = False) -> dict[str, list[dict]]:
        async def fetch():
            data = await self._get("https://api.binance.com/api/v3/ticker/24hr")
            valid = []
            skip = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT", "USDCUSDT", "FDUSDUSDT", "TUSDUSDT")
            for x in data:
                sym = x.get("symbol", "")
                if not sym.endswith("USDT") or sym.endswith(skip):
                    continue
                try:
                    qvol = float(x["quoteVolume"])
                    price = float(x["lastPrice"])
                    chg = float(x["priceChangePercent"])
                except (ValueError, KeyError):
                    continue
                if qvol >= 5_000_000 and price > 0:
                    valid.append({"symbol": sym[:-4], "price": price, "change": chg})
            valid.sort(key=lambda i: i["change"], reverse=True)
            return {"gainers": valid[:5], "losers": list(reversed(valid[-5:]))}

        return await self._cached("movers", S.movers_ttl, fetch, force)

    async def search_coin(self, query: str, force: bool = False) -> Optional[dict]:
        q = query.upper().strip().replace("USDT", "")
        if not q.isalnum():
            return None

        async def fetch():
            try:
                d = await self._get(
                    "https://api.binance.com/api/v3/ticker/24hr", {"symbol": f"{q}USDT"}
                )
                return {
                    "symbol": q,
                    "price": float(d["lastPrice"]),
                    "change_24h": float(d["priceChangePercent"]),
                    "high_24h": float(d["highPrice"]),
                    "low_24h": float(d["lowPrice"]),
                    "volume_usdt": float(d["quoteVolume"]),
                    "source": "Binance",
                }
            except Exception:
                return None

        return await self._cached(f"search:{q}", 25, fetch, force)

    # ---------- METALS ----------
    async def metals(self, force: bool = False) -> dict[str, Any]:
        async def fetch():
            t0 = time.monotonic()
            try:
                g, s = await asyncio.gather(
                    self._get("https://api.gold-api.com/price/XAU"),
                    self._get("https://api.gold-api.com/price/XAG"),
                )
                ms = int((time.monotonic() - t0) * 1000)
                out = {
                    "gold_oz": float(g["price"]),
                    "silver_oz": float(s["price"]),
                    "source": "Gold-API",
                }
                db.mark_api_bg("Gold-API", True, "XAU + XAG", ms)
                return out
            except Exception as e:
                db.mark_api_bg("Gold-API", False, str(e))
                raise

        return await self._cached("metals", S.metals_ttl, fetch, force)

    # ---------- FX ----------
    async def fx(self, force: bool = False) -> dict[str, Any]:
        async def fetch():
            t0 = time.monotonic()
            try:
                d = await self._get(
                    "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/usd.json"
                )
                u = d["usd"]
                ms = int((time.monotonic() - t0) * 1000)
                out = {
                    "USD": 1.0,
                    "EGP": float(u["egp"]),
                    "EUR": float(u["eur"]),
                    "GBP": float(u["gbp"]),
                    "SAR": float(u["sar"]),
                    "AED": float(u["aed"]),
                    "KWD": float(u["kwd"]),
                    "QAR": float(u["qar"]),
                    "date": d.get("date"),
                }
                db.mark_api_bg("Currency-FX", True, "fawazahmed0 CDN", ms)
                return out
            except Exception as e:
                db.mark_api_bg("Currency-FX", False, str(e))
                raise

        return await self._cached("fx", S.fx_ttl, fetch, force)

    # ---------- OIL ----------
    async def oil(self, force: bool = False) -> dict[str, Any]:
        async def fetch():
            t0 = time.monotonic()
            try:
                d = await self._get(
                    "https://query1.finance.yahoo.com/v8/finance/chart/BZ=F",
                    {"range": "1d", "interval": "5m"},
                )
                m = d["chart"]["result"][0]["meta"]
                price = float(m.get("regularMarketPrice"))
                prev = m.get("chartPreviousClose") or m.get("previousClose")
                ch = ((price - prev) / prev * 100) if (price and prev) else 0.0
                ms = int((time.monotonic() - t0) * 1000)
                out = {
                    "price": price,
                    "previous_close": float(prev) if prev else None,
                    "change_24h": ch,
                    "source": "Yahoo (BZ=F)",
                }
                db.mark_api_bg("Yahoo Brent", True, "BZ=F", ms)
                return out
            except Exception as e:
                db.mark_api_bg("Yahoo Brent", False, str(e))
                raise

        return await self._cached("oil", S.oil_ttl, fetch, force)

    # ---------- EGYPT LOCAL METALS ----------
    async def egypt_metals(self, force: bool = False) -> dict[str, Any]:
        m, f = await asyncio.gather(self.metals(force), self.fx(force))
        egp = f["EGP"]
        g24 = m["gold_oz"] * egp / OZ_G
        s999 = m["silver_oz"] * egp / OZ_G
        spread = max(0.0, float(RUNTIME_CFG.get("gold_spread_pct", 0.5))) / 100.0
        karats = {}
        for k in (24, 21, 18, 14):
            base = g24 * k / 24
            karats[k] = {
                "mid": base,
                "sell": base * (1 + spread / 2),
                "buy": base * (1 - spread / 2),
            }
        coin_mid = karats[21]["mid"] * 8.0
        return {
            "gold": karats,
            "coin": coin_mid,
            "coin_sell": karats[21]["sell"] * 8.0,
            "coin_buy": karats[21]["buy"] * 8.0,
            "silver_999": s999,
            "silver_925": s999 * 0.925,
            "gold_oz_usd": m["gold_oz"],
            "silver_oz_usd": m["silver_oz"],
            "usd_egp": egp,
            "spread_pct": spread * 100,
        }

    async def price_of(self, symbol: str, force: bool = False) -> Optional[tuple[float, str, str]]:
        s = symbol.upper()
        if s in ("XAU", "GOLD"):
            m = await self.metals(force)
            return m["gold_oz"], "USD/oz", "USD"
        if s in ("XAG", "SILVER"):
            m = await self.metals(force)
            return m["silver_oz"], "USD/oz", "USD"
        if s in ("GOLD_24", "GOLD_21", "GOLD_18", "GOLD_14"):
            k = int(s.split("_")[1])
            em = await self.egypt_metals(force)
            return em["gold"][k]["mid"], "EGP/g", "EGP"
        if s == "GOLD_COIN":
            em = await self.egypt_metals(force)
            return em["coin"], "EGP", "EGP"
        if s == "SILVER_999":
            em = await self.egypt_metals(force)
            return em["silver_999"], "EGP/g", "EGP"
        if s == "SILVER_925":
            em = await self.egypt_metals(force)
            return em["silver_925"], "EGP/g", "EGP"
        if s == "USD_EGP":
            f = await self.fx(force)
            return f["EGP"], "EGP", "EGP"
        if s in ("BRENT", "OIL"):
            o = await self.oil(force)
            return o["price"], "USD/bbl", "USD"
        if s in DEFAULT_COINS:
            c = await self.crypto(force=force)
            if s in c:
                return c[s]["price"], "USD", "USD"
        r = await self.search_coin(s, force)
        return (r["price"], "USD", "USD") if r else None

prices = Prices()
# ═══════════════════════════════════════════════════════════
# 🔒 FORCE SUBSCRIBE & ACCESS GATE (RAM-Accelerated)
# ═══════════════════════════════════════════════════════════

async def check_fsub_missing(bot, user_id: int, bypass_cache: bool = False) -> list[dict]:
    if is_admin(user_id) or not RUNTIME_CFG["fsub_enabled"]:
        return []
    channels = RUNTIME_CFG["fsub_channels"]
    if not channels:
        return []

    if not bypass_cache:
        ok_ts = _FSUB_OK_CACHE.get(user_id)
        if ok_ts and (time.monotonic() - ok_ts < 300.0):
            return []

    async def _check_one(ch: dict) -> Optional[dict]:
        try:
            cid = int(ch["chat_id"]) if ch["chat_id"].lstrip("-").isdigit() else ch["chat_id"]
            member = await bot.get_chat_member(cid, user_id)
            status = getattr(member.status, "value", str(member.status)).lower()
            if status in ("member", "administrator", "creator", "owner"):
                return None
            if status == "restricted" and getattr(member, "is_member", False):
                return None
            return ch
        except TelegramError as e:
            log.warning(f"FSub check skipped for {ch['chat_id']}: {e}")
            return None

    results = await asyncio.gather(*[_check_one(c) for c in channels])
    missing = [c for c in results if c is not None]
    if not missing:
        _FSUB_OK_CACHE[user_id] = time.monotonic()
    return missing


def build_fsub_gate(missing: list[dict]) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "🔒 الاشتراك الإجباري مطلوب\n\n"
        "للاستفادة من أسعار البوت المباشرة والتنبيهات المجانية، "
        "يرجى الاشتراك أولاً في القنوات التالية ثم الضغط على زر التحقق:"
    )
    rows = []
    for i, ch in enumerate(missing, 1):
        rows.append([btn(f"📢 {i}. {ch['title']}", url=ch["invite_link"], style="primary")])
    rows.append([btn("✅ اشتركت — تحقق الآن", callback_data="fsub:verify", style="success")])
    return text, InlineKeyboardMarkup(rows)


async def gate_check(update: Update, context: ContextTypes.DEFAULT_TYPE, is_cb: bool = False) -> bool:
    u = update.effective_user
    if not u:
        return False

    if db.is_banned_fast(u.id):
        if is_cb:
            answer_bg(update.callback_query, "🚫 حسابك محظور.", show_alert=True)
        else:
            await update.message.reply_text(
                "🚫 <b>تم تقييد وصولك لهذا البوت من قِبل الإدارة.</b>",
                parse_mode=ParseMode.HTML,
            )
        return False

    if RUNTIME_CFG["maintenance"] and not is_admin(u.id):
        if is_cb:
            answer_bg(update.callback_query, "🛠️ البوت تحت الصيانة حالياً.", show_alert=True)
        else:
            await update.message.reply_text(
                "🛠️ <b>البوت في وضع الصيانة والتحديث حالياً</b>\n"
                "سنعود للعمل خلال دقائق، شكراً لتفهمك.",
                parse_mode=ParseMode.HTML,
            )
        return False

    missing = await check_fsub_missing(context.bot, u.id)
    if missing:
        text, kb = build_fsub_gate(missing)
        if is_cb:
            answer_bg(update.callback_query, "🔒 يرجى الاشتراك في القنوات أولاً.")
            await safe_edit(update.callback_query, text, kb)
        else:
            await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return False
    return True


# ═══════════════════════════════════════════════════════════
# 🎛️ KEYBOARDS
# ═══════════════════════════════════════════════════════════

def kb_main(uid: int = 0) -> InlineKeyboardMarkup:
    rows = [
        [btn("⚡ ملخص السوق الآن", callback_data="m:snap", style="success")],
        [
            btn("🟡 الذهب والفضة", callback_data="m:metals", style="primary"),
            btn("🟣 العملات الرقمية", callback_data="m:crypto", style="primary"),
        ],
        [
            btn("🔵 العملات الأجنبية", callback_data="m:fx", style="primary"),
            btn("🟤 النفط (برنت)", callback_data="m:oil", style="primary"),
        ],
        [
            btn("🔔 التنبيهات", callback_data="menu:alerts", style="success"),
            btn("💼 محفظتي", callback_data="menu:portfolio", style="success"),
        ],
        [
            btn("🧮 الحاسبة الذكية", callback_data="menu:calc", style="primary"),
            btn("📰 النشرة اليومية", callback_data="menu:digest", style="primary"),
        ],
        [
            btn("🔎 بحث عن عملة", callback_data="search:start", style="success"),
            btn("ℹ️ مساعدة", callback_data="help", style="primary"),
        ],
    ]
    if S.webapp_url:
        rows.insert(0, [InlineKeyboardButton("🚀 فتح التطبيق (Live App)", web_app=WebAppInfo(url=S.webapp_url))])

    if is_admin(uid):
        rows.append([btn("👑 لوحة تحكم الأدمن", callback_data="admin:home", style="danger")])
    return InlineKeyboardMarkup(rows)


def kb_back(target: str = "main", label: str = "🔙 القائمة الرئيسية") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[btn(label, callback_data=f"back:{target}", style="danger")]])


def kb_market(section: str, extra: Optional[list] = None) -> InlineKeyboardMarkup:
    quick_tabs = [
        ("metals", "🟡 ذهب"),
        ("crypto", "🟣 كريبتو"),
        ("fx", "🔵 عملات"),
        ("oil", "🟤 نفط"),
    ]
    tab_row = [
        btn(
            label,
            callback_data=f"m:{sec_id}",
            style="success" if sec_id == section else "primary",
        )
        for sec_id, label in quick_tabs
    ]
    rows = list(extra or [])
    rows.append(tab_row)
    rows.append([
        btn("🔄 تحديث لحظي", callback_data=f"refresh:{section}", style="success"),
        btn("🔙 القائمة", callback_data="back:main", style="danger"),
    ])
    return InlineKeyboardMarkup(rows)


# ═══════════════════════════════════════════════════════════
# 🖼️ SCREEN BUILDERS
# ═══════════════════════════════════════════════════════════

async def screen_snapshot(force: bool = False) -> str:
    m_res, c_res, o_res = await asyncio.gather(
        prices.egypt_metals(force),
        prices.crypto(["BTC", "ETH", "SOL", "TON"], force=force),
        prices.oil(force),
        return_exceptions=True,
    )
    lines = [head("⚡", "ملخص السوق اللحظي")]
    if not isinstance(m_res, Exception):
        lines += [
            "",
            "🟡 المعادن والدولار (مصر):",
            f" • ذهب عيار 21: {fmt(m_res['gold'][21]['mid'])} EGP",
            f" • ذهب عيار 24: {fmt(m_res['gold'][24]['mid'])} EGP",
            f" • أونصة الذهب: ${fmt(m_res['gold_oz_usd'])}",
            f" • فضة 999: {fmt(m_res['silver_999'])} EGP",
            f" • الدولار USD: {fmt(m_res['usd_egp'])} EGP",
        ]
    if not isinstance(c_res, Exception):
        lines.append("\n🟣 أبرز العملات الرقمية:")
        for sym in ("BTC", "ETH", "SOL", "TON"):
            it = c_res.get(sym)
            if it:
                ic, _ = trend(it["change_24h"])
                lines.append(
                    f" {COIN_ICON[sym]} {sym}: ${fmt(it['price'])} {ic} {it['change_24h']:+.2f}%"
                )
    if not isinstance(o_res, Exception):
        ic, _ = trend(o_res["change_24h"])
        lines.append(f"\n🟤 خام برنت: ${fmt(o_res['price'])} {ic} {o_res['change_24h']:+.2f}%")
    return "\n".join(lines)


async def screen_metals(force: bool = False) -> str:
    try:
        d = await prices.egypt_metals(force)
    except Exception:
        return "⚠️ تعذر جلب أسعار المعادن حالياً. حاول بعد ثوانٍ."
    g = d["gold"]
    sp = d["spread_pct"]
    lines = [
        head("🟡", "الذهب والفضة — مباشر"),
        f"🌍 أونصة الذهب: ${fmt(d['gold_oz_usd'])}",
        f"🌍 أونصة الفضة: ${fmt(d['silver_oz_usd'])}",
        f"💵 سعر الدولار: {fmt(d['usd_egp'])} EGP",
        "",
        "🟡 أسعار الذهب في مصر (EGP/جرام):",
    ]
    for k in (24, 21, 18, 14):
        if sp > 0:
            lines.append(
                f" • عيار {k}: بيع {fmt(g[k]['sell'])} │ شراء {fmt(g[k]['buy'])}"
            )
        else:
            lines.append(f" • عيار {k}: {fmt(g[k]['mid'])} EGP")
    lines += [
        f" • الجنيه الذهب: {fmt(d['coin'])} EGP",
        "",
        "⚪ أسعار الفضة (EGP/جرام):",
        f" • عيار 999: {fmt(d['silver_999'])} EGP",
        f" • عيار 925: {fmt(d['silver_925'])} EGP",
    ]
    if sp > 0:
        lines.append(f"\n💡 الأسعار تشمل هامش بيع/شراء {sp:.2f}% بدون مصنعية.")
    return "\n".join(lines)


async def screen_bullion(force: bool = False) -> str:
    try:
        d = await prices.egypt_metals(force)
    except Exception:
        return "⚠️ تعذر جلب أسعار السبائك حالياً."
    g24 = d["gold"][24]["sell"]
    weights = [
        ("1 جرام", 1.0), ("2.5 جرام", 2.5), ("5 جرام", 5.0),
        ("10 جرام", 10.0), ("20 جرام", 20.0), ("أونصة (31.1g)", 31.1),
        ("50 جرام", 50.0), ("100 جرام", 100.0),
    ]
    lines = [
        head("🧱", "أسعار سبائك الذهب عيار 24"),
        f"📌 سعر جرام 24: {fmt(g24)} EGP\n",
    ]
    for label, w in weights:
        lines.append(f" ▫️ {label}: {fmt(g24 * w, 0)} EGP")
    lines.append("\n💡 الأسعار بدون مصنعية السبيكة (تُضاف حسب الشركة).")
    return "\n".join(lines)


async def screen_crypto(force: bool = False) -> str:
    try:
        d = await prices.crypto(DEFAULT_COINS, force=force)
    except Exception:
        return "⚠️ تعذر جلب أسعار العملات الرقمية حالياً."
    lines = [head("🟣", "العملات الرقمية — مباشر")]
    for sym in DEFAULT_COINS:
        item = d.get(sym)
        if not item:
            continue
        ic, bar = trend(item["change_24h"])
        icon = COIN_ICON.get(sym, "🪙")
        lines.append(
            f"{icon} {sym:<4} ${fmt(item['price'])}\n"
            f" {ic} {item['change_24h']:+.2f}% {bar}"
        )
    src = next(iter(d.values()))["source"] if d else "—"
    lines.append(f"\n📡 المصدر: {src} • محرك فوري")
    return "\n".join(lines)


async def screen_movers(force: bool = False) -> str:
    try:
        m = await prices.top_movers(force)
    except Exception:
        return "⚠️ تعذر جلب حركة السوق حالياً."
    lines = [head("🔥", "الأعلى صعوداً وهبوطاً (24س)")]
    lines.append("\n🚀 أكثر العملات صعوداً:")
    for x in m["gainers"]:
        lines.append(f" 🟢 {x['symbol']} ${fmt(x['price'])} (+{x['change']:.2f}%)")
    lines.append("\n📉 أكثر العملات هبوطاً:")
    for x in m["losers"]:
        lines.append(f" 🔴 {x['symbol']} ${fmt(x['price'])} ({x['change']:.2f}%)")
    lines.append("\n📡 فلتر السيولة: حجم تداول يتجاوز 5 مليون دولار (Binance)")
    return "\n".join(lines)


async def screen_fx(force: bool = False) -> str:
    try:
        d = await prices.fx(force)
    except Exception:
        return "⚠️ تعذر جلب أسعار العملات الأجنبية."
    names = {
        "USD": ("🇺🇸", "دولار أمريكي"),
        "EUR": ("🇪🇺", "يورو أوروبي"),
        "GBP": ("🇬🇧", "جنيه إسترليني"),
        "SAR": ("🇸🇦", "ريال سعودي"),
        "AED": ("🇦🇪", "درهم إماراتي"),
        "KWD": ("🇰🇼", "دينار كويتي"),
        "QAR": ("🇶🇦", "ريال قطري"),
    }
    lines = [head("🔵", "العملات الأجنبية (مقابل EGP)")]
    egp_per_usd = d["EGP"]
    for code, (flag, name) in names.items():
        rate = d.get(code)
        val = egp_per_usd if code == "USD" else (egp_per_usd / rate if rate else None)
        lines.append(f"{flag} {name} ({code}): {fmt(val)} EGP")
    lines.append(f"\n📅 التاريخ المرجعي: {d.get('date', '—')}")
    return "\n".join(lines)


async def screen_oil(force: bool = False) -> str:
    try:
        d = await prices.oil(force)
    except Exception:
        return "⚠️ تعذر جلب سعر النفط حالياً."
    ic, bar = trend(d["change_24h"])
    return "\n".join([
        head("🟤", "خام برنت العالمي — مباشر"),
        f"💵 السعر الحالي: ${fmt(d['price'])} / برميل",
        f"{ic} التغير اليومي: {d['change_24h']:+.2f}% {bar}",
        f"📉 الإغلاق السابق: ${fmt(d['previous_close'])}",
        f"\n📡 المصدر: {d['source']}",
    ])


def welcome_message(first_name: str) -> str:
    custom = RUNTIME_CFG.get("welcome_text", "").strip()
    if custom:
        return custom.replace("{name}", esc(first_name))
    return (
        f"👋 أهلاً بك يا {esc(first_name)}!\n\n"
        "💎 بوت الأسعار المباشرة — Turbo Pro\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🟡 أسعار الذهب والسبائك والفضة لايف\n"
        "🟣 العملات الرقمية + الأعلى صعوداً وهبوطاً\n"
        "🔵 العملات الأجنبية وخام برنت\n"
        "🔔 تنبيهات ذكية + 💼 محفظة مزدوجة العملة\n\n"
        "👇 اختر من القائمة بالأسفل:"
    )


# ═══════════════════════════════════════════════════════════
# 🧭 USER COMMANDS
# ═══════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    u = update.effective_user
    db.touch_user_bg(u.id, u.username, u.first_name)
    if not await gate_check(update, context, is_cb=False):
        return
    await update.message.reply_text(
        welcome_message(u.first_name),
        reply_markup=kb_main(u.id),
        parse_mode=ParseMode.HTML,
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "✅ تم إلغاء العملية الحالية.",
        reply_markup=kb_main(update.effective_user.id),
        parse_mode=ParseMode.HTML,
    )


async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    db.touch_user_bg(u.id, u.username, u.first_name)
    if not await gate_check(update, context, is_cb=False):
        return
    if not context.args:
        return await update.message.reply_text(
            "🔎 الاستعلام السريع:\n"
            "اكتب: /price BTC\n"
            "أمثلة: BTC, ETH, XAU, GOLD_21, BRENT, USD_EGP",
            parse_mode=ParseMode.HTML,
        )
    sym = context.args[0].upper()
    r = await prices.price_of(sym)
    if not r:
        return await update.message.reply_text(
            f"⚠️ لم يتم العثور على الرمز {esc(sym)}.",
            parse_mode=ParseMode.HTML,
        )
    price, unit, _ = r
    label = ASSET_LABELS.get(sym, sym)
    await update.message.reply_text(
        f"💎 {label}\n"
        f"💵 {fmt(price)} {unit}\n"
        f"🕐 {now_hm()}",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_main(u.id),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await gate_check(update, context, is_cb=False):
        return
    await update.message.reply_text(
        "❓ دليل الاستخدام السريع\n\n"
        "• ⚡ ملخص السوق: أهم الأسعار في شاشة واحدة.\n"
        "• 🔄 تحديث لحظي: يجلب السعر فوراً متجاوزاً الكاش.\n"
        "• 🔔 التنبيهات: فحص تلقائي كل 20 ثانية وإشعار فوري.\n"
        "• 💼 محفظتي: تحسب ثروتك بالدولار والجنيه معاً.\n"
        "• أوامر سريعة: /price BTC أو /cancel.",
        reply_markup=kb_back(),
        parse_mode=ParseMode.HTML,
    )


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not is_admin(u.id):
        return
    context.user_data.clear()
    text, kb = await build_admin_home()
    await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


# ═══════════════════════════════════════════════════════════
# 🛡️ ADMIN PANEL BUILDERS
# ═══════════════════════════════════════════════════════════

async def build_admin_home() -> tuple[str, InlineKeyboardMarkup]:
    st = await db.stats()
    fsub_st = "🟢 مفعّل" if RUNTIME_CFG["fsub_enabled"] else "🔴 متوقف"
    maint_st = "🟠 مفعّل" if RUNTIME_CFG["maintenance"] else "🟢 متوقف"
    text = (
        f"{head('👑', 'لوحة تحكم الأدمن الشاملة', '🛡️ Admin')}\n\n"
        f"👥 المستخدمون: {st['total']} (نشط 24س: {st['active_24h']})\n"
        f"🆕 جدد اليوم: {st['new_today']} │ 🚫 محظور: {st['banned']}\n"
        f"🔔 تنبيهات نشطة: {st['alerts']} │ 💼 محافظ: {st['portfolios']}\n"
        f"📢 الاشتراك الإجباري: {fsub_st} ({st['fsub_count']} قناة)\n"
        f"🛠️ وضع الصيانة: {maint_st}\n"
        f"⚡ محرك التحديث الخلفي: {_BG_SYNC_COUNT} دورة\n"
        f"⏱️ مدة التشغيل: {format_uptime()}"
    )
    kb = InlineKeyboardMarkup([
        [
            btn("📊 الإحصائيات التفصيلية", callback_data="admin:stats", style="primary"),
            btn("📡 حالة الـ APIs", callback_data="admin:apis", style="primary"),
        ],
        [
            btn(
                "📢 الاشتراك الإجباري",
                callback_data="admin:fsub",
                style="success" if RUNTIME_CFG["fsub_enabled"] else "danger",
            ),
            btn("📣 إذاعة شاملة", callback_data="admin:bc", style="success"),
        ],
        [
            btn("👤 إدارة المستخدمين", callback_data="admin:users", style="primary"),
            btn("⚙️ إعدادات البوت", callback_data="admin:settings", style="primary"),
        ],
        [
            btn("👑 إدارة الأدمن", callback_data="admin:admins", style="primary"),
            btn("💾 نسخة احتياطية DB", callback_data="admin:backup", style="success"),
        ],
        [btn("🏠 العودة للبوت", callback_data="back:main", style="danger")],
    ])
    return text, kb


async def render_admin_stats(q):
    st = await db.stats()
    c_age = prices.cache_age("crypto:main")
    m_age = prices.cache_age("metals")
    text = (
        f"{head('📊', 'الإحصائيات والأداء', '🛡️ Admin')}\n\n"
        f"👥 إجمالي المستخدمين: {st['total']}\n"
        f"⚡ نشطين (آخر 24 ساعة): {st['active_24h']}\n"
        f"🆕 مستخدمين جدد اليوم: {st['new_today']}\n"
        f"🚫 محظورين من الإدارة: {st['banned']}\n"
        f"🔕 حظروا البوت: {st['blocked']}\n"
        f"────────────────────\n"
        f"🔔 تنبيهات نشطة: {st['alerts']} │ 💼 محافظ: {st['portfolios']}\n"
        f"📰 مشتركو النشرة: {st['digest']} │ 📢 قنوات: {st['fsub_count']}\n"
        f"────────────────────\n"
        f"🚀 عمر الكاش: كريبتو {c_age if c_age is not None else '—'}ث │ معادن {m_age if m_age is not None else '—'}ث\n"
        f"🧠 عناصر الرام: {len(prices.cache)} │ دورات التحديث: {_BG_SYNC_COUNT}\n"
        f"⏱️ Uptime: {format_uptime()}"
    )
    kb = InlineKeyboardMarkup([
        [btn("🔄 تحديث الإحصائيات", callback_data="admin:stats", style="success")],
        [btn("🔙 لوحة الأدمن", callback_data="admin:home", style="danger")],
    ])
    await safe_edit(q, text, kb)


async def render_admin_apis(q, run_check: bool = False):
    if run_check:
        await asyncio.gather(
            prices.crypto(force=True),
            prices.metals(force=True),
            prices.fx(force=True),
            prices.oil(force=True),
            return_exceptions=True,
        )
        await asyncio.sleep(0.05)
    statuses = await db.api_statuses()
    lines = [head("📡", "مراقبة مصادر الأسعار (APIs)", "🛡️ Admin"), ""]
    if not statuses:
        lines.append("💤 لم يتم تسجيل فحوصات بعد. اضغط على فحص الكل.")
    else:
        for s in statuses:
            ic = "🟢" if s["ok"] else "🔴"
            ms = f" ({s['latency_ms']}ms)" if s.get("latency_ms") else ""
            lines.append(
                f"{ic} {esc(s['provider'])}{ms}\n"
                f" 🕐 {s['checked_at']} • {esc(s['detail'])}"
            )
    lines.append(f"\n🧠 حجم الكاش: {len(prices.cache)} عنصر")
    kb = InlineKeyboardMarkup([
        [
            btn("⚡ فحص كل الـ APIs الآن", callback_data="admin:apis:check", style="success"),
            btn("🧹 مسح الكاش", callback_data="admin:cache:clear", style="danger"),
        ],
        [btn("🔙 لوحة الأدمن", callback_data="admin:home", style="primary")],
    ])
    await safe_edit(q, "\n".join(lines), kb)


async def render_admin_fsub(q):
    channels = RUNTIME_CFG["fsub_channels"]
    enabled = RUNTIME_CFG["fsub_enabled"]
    status = "🟢 مفعّل الآن" if enabled else "🔴 متوقف حالياً"
    lines = [
        head("📢", "إدارة الاشتراك الإجباري", "🛡️ Admin"),
        f"\n📌 الحالة: {status}",
        f"📺 القنوات المضافة ({len(channels)}):\n",
    ]
    kb_rows = []
    if not channels:
        lines.append("💤 لا توجد قنوات مضافة بعد.")
    else:
        for idx, ch in enumerate(channels, 1):
            lines.append(f" {idx}. {esc(ch['title'])}\n ID: {esc(ch['chat_id'])}")
            kb_rows.append([
                btn(f"🔗 {ch['title'][:18]}", url=ch["invite_link"], style="primary"),
                btn("🗑️ حذف", callback_data=f"admin:fsub:del:{ch['chat_id']}", style="danger"),
            ])

    kb_rows.append([
        btn(
            "🔴 إيقاف الاشتراك الإجباري" if enabled else "🟢 تفعيل الاشتراك الإجباري",
            callback_data="admin:fsub:toggle",
            style="danger" if enabled else "success",
        )
    ])
    kb_rows.append([btn("➕ إضافة قناة اشتراك جديدة", callback_data="admin:fsub:add", style="primary")])
    kb_rows.append([btn("🔙 لوحة الأدمن", callback_data="admin:home", style="danger")])
    await safe_edit(q, "\n".join(lines), InlineKeyboardMarkup(kb_rows))


async def render_admin_settings(q):
    maint = RUNTIME_CFG["maintenance"]
    colors = RUNTIME_CFG["colored_buttons"]
    spread = RUNTIME_CFG["gold_spread_pct"]
    has_welcome = "✅ مخصصة" if RUNTIME_CFG["welcome_text"] else "⚪ افتراضية"

    text = (
        f"{head('⚙️', 'إعدادات البوت الحية', '🛡️ Admin')}\n\n"
        f"🛠️ <b>وضع الصيانة:</b> {'🔴 مفعّل (متوقف للمستخدمين)' if maint else '🟢 متوقف (شغال طبيعي)'}\n"
        f"🎨 <b>تلوين الأزرار:</b> {'🟢 مفعّل' if colors else '⚪ متوقف'}\n"
        f"⚖️ <b>هامش بيع/شراء الذهب:</b> <code>{spread:.2f}%</code>\n"
        f"💬 <b>رسالة الترحيب:</b> {has_welcome}"
    )
    kb = InlineKeyboardMarkup([
        [
            btn(
                "🟢 إيقاف وضع الصيانة" if maint else "🔴 تفعيل وضع الصيانة",
                callback_data="admin:set:maint",
                style="success" if maint else "danger",
            )
        ],
        [
            btn(
                "🎨 تلوين الأزرار: مفعّل ✅" if colors else "🎨 تلوين الأزرار: متوقف ❌",
                callback_data="admin:set:colors",
                style="success" if colors else "primary",
            )
        ],
        [btn("⚖️ تعديل هامش الذهب (%)", callback_data="admin:set:spread", style="primary")],
        [
            btn("✏️ تعديل الترحيب", callback_data="admin:set:welcome", style="primary"),
            btn("♻️ ترحيب افتراضي", callback_data="admin:set:welcome_reset", style="danger"),
        ],
        [btn("🔙 لوحة الأدمن", callback_data="admin:home", style="danger")],
    ])
    await safe_edit(q, text, kb)


async def render_admin_users(q):
    recent = await db.recent_users(8)
    lines = [head("👤", "إدارة المستخدمين", "🛡️ Admin"), "\n🕒 آخر المستخدمين تفاعلاً:"]
    for u in recent:
        un = f"@{u['username']}" if u["username"] else "بدون يوزر"
        flag = "🚫" if u["is_banned"] else "🟢"
        lines.append(f" {flag} {u['user_id']} • {esc(u['first_name'])} ({esc(un)})")
    kb = InlineKeyboardMarkup([
        [btn("🔎 بحث عن مستخدم (ID أو @يوزر)", callback_data="admin:users:find", style="primary")],
        [btn("🚫 قائمة المحظورين", callback_data="admin:users:banned", style="danger")],
        [btn("🔙 لوحة الأدمن", callback_data="admin:home", style="danger")],
    ])
    await safe_edit(q, "\n".join(lines), kb)


async def render_admin_user_card(target_obj, user_row: dict, is_cb: bool = True):
    uid = user_row["user_id"]
    alerts_c = len(await db.user_alerts(uid))
    holds_c = len(await db.holdings(uid))
    un = f"@{user_row['username']}" if user_row["username"] else "—"
    st = "🚫 محظور" if user_row["is_banned"] else "🟢 نشط"
    text = (
        f"{head('👤', 'بطاقة المستخدم', '🛡️ Admin')}\n\n"
        f"🆔 ID: {uid}\n"
        f"👤 الاسم: {esc(user_row['first_name'])}\n"
        f"🔗 اليوزر: {esc(un)}\n"
        f"📌 الحالة: {st}\n"
        f"📅 تاريخ الانضمام: {user_row['joined_at']}\n"
        f"🕐 آخر ظهور: {user_row['last_seen']}\n"
        f"🔔 تنبيهات: {alerts_c} │ 💼 أصول بالمحفظة: {holds_c}"
    )
    kb = InlineKeyboardMarkup([
        [
            btn(
                "✅ فك الحظر" if user_row["is_banned"] else "🚫 حظر المستخدم",
                callback_data=f"admin:u:ban:{uid}:{0 if user_row['is_banned'] else 1}",
                style="success" if user_row["is_banned"] else "danger",
            ),
            btn("✉️ رسالة خاصة", callback_data=f"admin:u:dm:{uid}", style="primary"),
        ],
        [btn("🔙 إدارة المستخدمين", callback_data="admin:users", style="danger")],
    ])
    if is_cb:
        await safe_edit(target_obj, text, kb)
    else:
        await target_obj.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def render_admin_admins(q):
    lines = [head("👑", "إدارة المشرفين (Admins)", "🛡️ Admin"), ""]
    lines.append("🔒 المالك الأساسي (من الإعدادات):")
    for aid in sorted(S.admin_ids):
        lines.append(f" • {aid}")
    dyn = sorted(RUNTIME_CFG["dynamic_admins"] - S.admin_ids)
    lines.append("\n🛡️ الأدمن المضافون من اللوحة:")
    kb_rows = []
    if not dyn:
        lines.append(" 💤 لا يوجد أدمن إضافيون.")
    else:
        for aid in dyn:
            lines.append(f" • {aid}")
            kb_rows.append([btn(f"🗑️ إزالة {aid}", callback_data=f"admin:adm:del:{aid}", style="danger")])
    kb_rows.append([btn("➕ إضافة أدمن جديد (بالـ ID)", callback_data="admin:adm:add", style="success")])
    kb_rows.append([btn("🔙 لوحة الأدمن", callback_data="admin:home", style="danger")])
    await safe_edit(q, "\n".join(lines), InlineKeyboardMarkup(kb_rows))


# ═══════════════════════════════════════════════════════════
# 📄 USER SUB-MENUS
# ═══════════════════════════════════════════════════════════

async def render_alerts(q):
    rows = await db.user_alerts(q.from_user.id)
    lines = [head("🔔", "تنبيهاتك النشطة")]
    kb_rows = []
    if not rows:
        lines.append("\n💤 لا توجد تنبيهات نشطة حالياً.\nأضف تنبيهاً وسأخبرك فور وصول السعر!")
    else:
        for a in rows:
            arrow = "⬆️ أعلى من" if a["direction"] == "above" else "⬇️ أقل من"
            lbl = ASSET_LABELS.get(a["symbol"], a["symbol"])
            lines.append(f" #{a['id']} • {lbl}\n {arrow} {fmt(a['target'])}")
            kb_rows.append([btn(f"🗑️ إلغاء تنبيه #{a['id']}", callback_data=f"alert:cancel:{a['id']}", style="danger")])
    kb_rows.append([btn("➕ إضافة تنبيه جديد", callback_data="alert:new", style="success")])
    kb_rows.append([btn("🔙 القائمة الرئيسية", callback_data="back:main", style="danger")])
    await safe_edit(q, "\n".join(lines), InlineKeyboardMarkup(kb_rows))


async def alert_start(q):
    kb = InlineKeyboardMarkup([
        [
            btn("🟡 ذهب عيار 21", callback_data="alert:asset:GOLD_21", style="primary"),
            btn("🟡 ذهب عيار 24", callback_data="alert:asset:GOLD_24", style="primary"),
        ],
        [
            btn("🌍 أونصة الذهب", callback_data="alert:asset:XAU", style="primary"),
            btn("⚪ فضة 999 (جرام)", callback_data="alert:asset:SILVER_999", style="primary"),
        ],
        [
            btn("🟠 BTC", callback_data="alert:asset:BTC", style="success"),
            btn("🔷 ETH", callback_data="alert:asset:ETH", style="success"),
            btn("🟪 SOL", callback_data="alert:asset:SOL", style="success"),
        ],
        [
            btn("💵 الدولار (EGP)", callback_data="alert:asset:USD_EGP", style="primary"),
            btn("🟤 خام برنت", callback_data="alert:asset:BRENT", style="primary"),
        ],
        [btn("❌ إلغاء", callback_data="menu:alerts", style="danger")],
    ])
    await safe_edit(q, "🔔 اختر الأصل الذي تريد مراقبته:", kb)


async def render_portfolio(q):
    rows = await db.holdings(q.from_user.id)
    lines = [head("💼", "محفظتي الاستثمارية")]
    kb_rows = []
    if not rows:
        lines.append("\n📭 محفظتك فارغة حالياً.\nأضف ما تملكه من ذهب أو فضة أو عملات لحساب قيمتها لايف!")
    else:
        try:
            fx = await prices.fx()
            usd_egp = fx["EGP"]
        except Exception:
            usd_egp = 50.0
        total_usd = 0.0
        total_egp = 0.0
        res_list = await asyncio.gather(
            *[prices.price_of(r["symbol"]) for r in rows], return_exceptions=True
        )
        for r, p in zip(rows, res_list):
            lbl = ASSET_LABELS.get(r["symbol"], r["symbol"])
            if not isinstance(p, Exception) and p:
                price, unit, cur = p
                val = price * r["quantity"]
                if cur == "EGP":
                    val_egp = val
                    val_usd = val / usd_egp
                else:
                    val_usd = val
                    val_egp = val * usd_egp
                total_usd += val_usd
                total_egp += val_egp
                lines.append(
                    f" • {lbl}\n"
                    f" الكمية: {r['quantity']:g} │ "
                    f"القيمة: {fmt(val_egp, 0)} EGP (${fmt(val_usd)})"
                )
            else:
                lines.append(f" • {lbl}: {r['quantity']:g} (السعر غير متاح)")
            kb_rows.append([btn(f"🗑️ حذف {r['symbol']}", callback_data=f"port:del:{r['symbol']}", style="danger")])
        lines += [
            "────────────────────",
            f"💰 الإجمالي بالجنيه: {fmt(total_egp, 0)} EGP",
            f"💵 الإجمالي بالدولار: ${fmt(total_usd)}",
        ]
        kb_rows.append([
            btn("➕ إضافة أصل", callback_data="port:new", style="success"),
            btn("🔄 تحديث", callback_data="menu:portfolio", style="primary"),
        ])
        kb_rows.append([btn("🧹 مسح المحفظة بالكامل", callback_data="port:clear", style="danger")])
    if not rows:
        kb_rows.append([btn("➕ إضافة أصل", callback_data="port:new", style="success")])
    kb_rows.append([btn("🔙 القائمة الرئيسية", callback_data="back:main", style="danger")])
    await safe_edit(q, "\n".join(lines), InlineKeyboardMarkup(kb_rows))


async def port_start(q):
    kb = InlineKeyboardMarkup([
        [
            btn("🟡 ذهب 21 (جرام)", callback_data="port:asset:GOLD_21", style="primary"),
            btn("🟡 ذهب 24 (جرام)", callback_data="port:asset:GOLD_24", style="primary"),
        ],
        [
            btn("🪙 جنيه ذهب", callback_data="port:asset:GOLD_COIN", style="primary"),
            btn("⚪ فضة 999 (جرام)", callback_data="port:asset:SILVER_999", style="primary"),
        ],
        [
            btn("🟠 BTC", callback_data="port:asset:BTC", style="success"),
            btn("🔷 ETH", callback_data="port:asset:ETH", style="success"),
            btn("🟪 SOL", callback_data="port:asset:SOL", style="success"),
        ],
        [
            btn("💎 TON", callback_data="port:asset:TON", style="primary"),
            btn("💵 دولار (USD)", callback_data="port:asset:USD_EGP", style="primary"),
        ],
        [btn("❌ إلغاء", callback_data="menu:portfolio", style="danger")],
    ])
    await safe_edit(q, "💼 اختر الأصل لإضافته لمحفظتك:", kb)


async def render_digest(q):
    d = await db.get_digest(q.from_user.id)
    enabled = bool(d and d["enabled"])
    cur_t = d["local_time"] if d else "09:00"
    st = f"🟢 مفعّلة يومياً الساعة {cur_t}" if enabled else "🔴 متوقفة حالياً"
    text = (
        f"{head('📰', 'النشرة اليومية التلقائية')}\n\n"
        f"📌 حالتك الآن: {st}\n"
        "ستصلك يومياً رسالة ملخصة بأهم أسعار الذهب والفضة والعملات بتوقيت القاهرة."
    )
    kb = InlineKeyboardMarkup([
        [
            btn("🕘 09:00 ص", callback_data="digest:set:09:00", style="success"),
            btn("🕑 14:00 ظ", callback_data="digest:set:14:00", style="primary"),
            btn("🕗 20:00 م", callback_data="digest:set:20:00", style="primary"),
        ],
        [btn("❌ إيقاف النشرة", callback_data="digest:off", style="danger")],
        [btn("🔙 القائمة الرئيسية", callback_data="back:main", style="danger")],
    ])
    await safe_edit(q, text, kb)


async def render_calc(q):
    kb = InlineKeyboardMarkup([
        [btn("🟡 حاسبة الذهب بالمصنعية", callback_data="calc:gold", style="primary")],
        [btn("💱 محول العملات الأجنبية", callback_data="calc:fx", style="success")],
        [btn("🟣 تحويل عملة رقمية للمصري", callback_data="calc:crypto", style="primary")],
        [btn("🔙 القائمة الرئيسية", callback_data="back:main", style="danger")],
    ])
    await safe_edit(q, head("🧮", "الحاسبة الذكية") + "\n\nاختر نوع الحساب:", kb)


# ═══════════════════════════════════════════════════════════
# 🔀 CALLBACK ROUTER
# ═══════════════════════════════════════════════════════════

async def cb_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    d = q.data or ""
    uid = q.from_user.id

    db.touch_user_bg(uid, q.from_user.username, q.from_user.first_name)

    # ── fsub:verify ──
    if d == "fsub:verify":
        missing = await check_fsub_missing(context.bot, uid, bypass_cache=True)
        if missing:
            answer_bg(q, "⚠️ لم تكمل الاشتراك في كل القنوات بعد!", show_alert=True)
            text, kb = build_fsub_gate(missing)
            return await safe_edit(q, text, kb)
        answer_bg(q, "✅ تم التحقق بنجاح! أهلاً بك.")
        return await safe_edit(q, welcome_message(q.from_user.first_name), kb_main(uid))

    if not await gate_check(update, context, is_cb=True):
        return

    if not d.startswith(("alert:dir:", "admin:bc:go")):
        context.user_data.pop("waiting_for", None)

    # ── NAVIGATION ──
    if d.startswith("back:"):
        answer_bg(q)
        return await safe_edit(q, welcome_message(q.from_user.first_name), kb_main(uid))

    if d == "help":
        answer_bg(q)
        return await safe_edit(
            q,
            "❓ <b>مساعدة سريعة</b>\n\n"
            "• استجابة فورية بفضل محرك التحديث الخلفي.\n"
            "• استخدم الشريط السفلي للتنقل السريع بين الأسواق.\n"
            "• زر <b>🔄 تحديث لحظي</b> يجلب السعر لايف فوراً.\n"
            "• أوامر سريعة: <code>/price BTC</code> أو <code>/cancel</code>.",
            kb_back(),
        )

    # ── MARKETS & REFRESH ──
    if d in ("m:snap", "m:metals", "m:bullion", "m:crypto", "m:movers", "m:fx", "m:oil") or d.startswith("refresh:"):
        is_refresh = d.startswith("refresh:")
        sec = d.split(":")[1]
        if is_refresh:
            now_t = time.monotonic()
            if now_t - _LAST_REFRESH.get(uid, 0.0) < 1.5:
                return answer_bg(q, "⏳ انتظر ثانية قبل التحديث مرة أخرى.")
            _LAST_REFRESH[uid] = now_t
            answer_bg(q, "🔄 تحديث لحظي...")
        else:
            answer_bg(q)
        extra = None
        if sec == "snap":
            text = await screen_snapshot(is_refresh)
        elif sec == "metals":
            text = await screen_metals(is_refresh)
            extra = [[btn("🧱 أسعار السبائك (عيار 24)", callback_data="m:bullion", style="primary")]]
        elif sec == "bullion":
            text = await screen_bullion(is_refresh)
            extra = [[btn("🟡 العودة لأسعار الذهب", callback_data="m:metals", style="primary")]]
        elif sec == "crypto":
            text = await screen_crypto(is_refresh)
            extra = [[
                btn("🔥 الأعلى صعوداً وهبوطاً", callback_data="m:movers", style="primary"),
                btn("🔎 بحث عن عملة", callback_data="search:start", style="success"),
            ]]
        elif sec == "movers":
            text = await screen_movers(is_refresh)
            extra = [[btn("🟣 العودة للعملات الرقمية", callback_data="m:crypto", style="primary")]]
        elif sec == "fx":
            text = await screen_fx(is_refresh)
        else:
            text = await screen_oil(is_refresh)
        return await safe_edit(q, text, kb_market(sec, extra))

    # ── USER MENUS ──
    if d == "menu:alerts":
        answer_bg(q)
        return await render_alerts(q)
    if d == "menu:portfolio":
        answer_bg(q)
        return await render_portfolio(q)
    if d == "menu:digest":
        answer_bg(q)
        return await render_digest(q)
    if d == "menu:calc":
        answer_bg(q)
        return await render_calc(q)

    # ── ALERTS FLOW ──
    if d == "alert:new":
        answer_bg(q)
        return await alert_start(q)
    if d.startswith("alert:asset:"):
        akey = d.split(":", 2)[2]
        context.user_data["a_asset"] = akey
        answer_bg(q)
        cur = await prices.price_of(akey)
        cur_txt = f"\n📌 <b>السعر الحالي:</b> <code>{fmt(cur[0])}</code> {cur[1]}" if cur else ""
        return await safe_edit(
            q,
            f"🎯 <b>{ASSET_LABELS.get(akey, akey)}</b>{cur_txt}\n\nحدد شرط التنبيه:",
            InlineKeyboardMarkup([
                [
                    btn("⬆️ عندما يرتفع فوق", callback_data="alert:dir:above", style="success"),
                    btn("⬇️ عندما ينخفض تحت", callback_data="alert:dir:below", style="danger"),
                ],
                [btn("❌ إلغاء", callback_data="menu:alerts", style="danger")],
            ]),
        )
    if d.startswith("alert:dir:"):
        if "a_asset" not in context.user_data:
            answer_bg(q, "⚠️ ابدأ من جديد.")
            return await alert_start(q)
        context.user_data["a_dir"] = d.split(":")[2]
        context.user_data["waiting_for"] = "alert_price"
        answer_bg(q)
        akey = context.user_data["a_asset"]
        cur = await prices.price_of(akey)
        hint = f"\n💡 السعر الحالي: <code>{fmt(cur[0])}</code> {cur[1]}" if cur else ""
        return await safe_edit(
            q,
            f"💰 <b>أرسل السعر المستهدف</b> (رقم فقط):{hint}",
            InlineKeyboardMarkup([[btn("❌ إلغاء", callback_data="menu:alerts", style="danger")]]),
        )
    if d.startswith("alert:cancel:"):
        aid = int(d.split(":")[2])
        ok = await db.cancel_alert(uid, aid)
        answer_bg(q, "✅ تم إلغاء التنبيه" if ok else "⚠️ لم يُعثر عليه")
        return await render_alerts(q)

    # ── PORTFOLIO FLOW ──
    if d == "port:new":
        answer_bg(q)
        return await port_start(q)
    if d.startswith("port:asset:"):
        akey = d.split(":", 2)[2]
        context.user_data["p_asset"] = akey
        context.user_data["waiting_for"] = "port_qty"
        answer_bg(q)
        return await safe_edit(
            q,
            f"💼 <b>{ASSET_LABELS.get(akey, akey)}</b>\n\n"
            f"أرسل الكمية التي تملكها (مثال: <code>5</code> أو <code>0.25</code>):",
            InlineKeyboardMarkup([[btn("❌ إلغاء", callback_data="menu:portfolio", style="danger")]]),
        )
    if d.startswith("port:del:"):
        sym = d.split(":", 2)[2]
        await db.remove_holding(uid, sym)
        answer_bg(q, f"🗑️ تم حذف {sym}")
        return await render_portfolio(q)
    if d == "port:clear":
        answer_bg(q)
        return await safe_edit(
            q,
            "⚠️ <b>هل أنت متأكد من مسح جميع الأصول في محفظتك؟</b>",
            InlineKeyboardMarkup([
                [
                    btn("✅ نعم، امسح الكل", callback_data="port:clear:do", style="danger"),
                    btn("❌ إلغاء", callback_data="menu:portfolio", style="primary"),
                ]
            ]),
        )
    if d == "port:clear:do":
        await db.clear_holdings(uid)
        answer_bg(q, "✅ تم تفريغ المحفظة")
        return await render_portfolio(q)

    # ── CALC & SEARCH ──
    if d == "calc:fx":
        context.user_data["waiting_for"] = "calc_fx"
        answer_bg(q)
        return await safe_edit(
            q,
            "💱 <b>محول العملات الأجنبية</b>\n\n"
            "أرسل المبلغ والعملتين هكذا:\n<code>100 USD EGP</code>\n"
            "العملات المدعومة: <code>USD, EGP, EUR, GBP, SAR, AED, KWD, QAR</code>",
            InlineKeyboardMarkup([[btn("❌ إلغاء", callback_data="menu:calc", style="danger")]]),
        )
    if d == "calc:gold":
        context.user_data["waiting_for"] = "calc_gold"
        answer_bg(q)
        return await safe_edit(
            q,
            "🟡 <b>حاسبة الذهب بالمصنعية</b>\n\n"
            "أرسل: <code>الجرامات العيار المصنعية</code>\n"
            "مثال: <code>10 21 150</code>\n"
            "(يعني 10 جرام عيار 21 ومصنعية 150 جنيه للجرام)",
            InlineKeyboardMarkup([[btn("❌ إلغاء", callback_data="menu:calc", style="danger")]]),
        )
    if d == "calc:crypto":
        context.user_data["waiting_for"] = "calc_crypto"
        answer_bg(q)
        return await safe_edit(
            q,
            "🟣 <b>محول الكريبتو للمصري والدولار</b>\n\n"
            "أرسل: <code>الكمية الرمز</code>\n"
            "مثال: <code>0.05 BTC</code> أو <code>100 TON</code>",
            InlineKeyboardMarkup([[btn("❌ إلغاء", callback_data="menu:calc", style="danger")]]),
        )
    if d == "search:start":
        context.user_data["waiting_for"] = "search"
        answer_bg(q)
        return await safe_edit(
            q,
            "🔎 <b>البحث المباشر عن أي عملة رقمية</b>\n\n"
            "أرسل رمز العملة على Binance (مثل: <code>PEPE</code> أو <code>ADA</code> أو <code>AVAX</code>):",
            InlineKeyboardMarkup([[btn("❌ إلغاء", callback_data="back:main", style="danger")]]),
        )

    # ── DIGEST ──
    if d.startswith("digest:set:"):
        t = d.split(":", 2)[2]
        await db.set_digest(uid, True, t)
        answer_bg(q, f"✅ تم التفعيل الساعة {t}")
        return await render_digest(q)
    if d == "digest:off":
        await db.set_digest(uid, False)
        answer_bg(q, "🔴 تم إيقاف النشرة")
        return await render_digest(q)

    # ══════════════════════════════════════════════════════
    # 👑 ADMIN CALLBACKS
    # ══════════════════════════════════════════════════════
    if d.startswith("admin:"):
        if not is_admin(uid):
            return answer_bg(q, "🚫 صلاحية أدمن فقط.", show_alert=True)

        if d == "admin:home":
            answer_bg(q)
            text, kb = await build_admin_home()
            return await safe_edit(q, text, kb)
        if d == "admin:stats":
            answer_bg(q)
            return await render_admin_stats(q)
        if d == "admin:apis":
            answer_bg(q)
            return await render_admin_apis(q, run_check=False)
        if d == "admin:apis:check":
            answer_bg(q, "⚡ جاري فحص جميع المصادر...")
            return await render_admin_apis(q, run_check=True)
        if d == "admin:cache:clear":
            prices.clear_cache()
            answer_bg(q, "🧹 تم مسح الكاش!")
            global _WARMUP_TASK
            _WARMUP_TASK = asyncio.create_task(_warmup_cache())
            return await render_admin_apis(q, run_check=False)

        # ----- Force Sub Admin -----
        if d == "admin:fsub":
            answer_bg(q)
            return await render_admin_fsub(q)
        if d == "admin:fsub:toggle":
            new_val = "0" if RUNTIME_CFG["fsub_enabled"] else "1"
            await db.set_setting("fsub_enabled", new_val)
            _FSUB_OK_CACHE.clear()
            answer_bg(q, "✅ تم تحديث حالة الاشتراك الإجباري")
            return await render_admin_fsub(q)
        if d == "admin:fsub:add":
            context.user_data["waiting_for"] = "admin_add_fsub"
            answer_bg(q)
            return await safe_edit(
                q,
                "📢 <b>إضافة قناة اشتراك إجباري</b>\n\n"
                "1️⃣ ارفع البوت <b>أدمن (Admin)</b> في القناة أولاً.\n"
                "2️⃣ ثم أرسل لي أحد التالي:\n"
                " • يوزر القناة مثل: <code>@MyChannel</code>\n"
                " • أو معرّف القناة مثل: <code>-1001234567890</code>\n"
                " • أو قم بعمل <b>توجيه (Forward)</b> لأي منشور من القناة هنا.",
                InlineKeyboardMarkup([[btn("❌ إلغاء", callback_data="admin:fsub", style="danger")]]),
            )
        if d.startswith("admin:fsub:del:"):
            cid = d.split(":", 3)[3]
            await db.remove_fsub_channel(cid)
            answer_bg(q, "🗑️ تم حذف القناة")
            return await render_admin_fsub(q)

        # ----- Broadcast Admin -----
        if d == "admin:bc":
            context.user_data["waiting_for"] = "admin_broadcast"
            answer_bg(q)
            return await safe_edit(
                q,
                "📣 <b>الإذاعة الشاملة لجميع المستخدمين</b>\n\n"
                "أرسل الآن الرسالة التي تريد إذاعتها (نص، صورة، فيديو، ملف، أو صوت).\n"
                "<i>ستظهر لك شاشة تأكيد قبل بدء الإرسال.</i>",
                InlineKeyboardMarkup([[btn("❌ إلغاء", callback_data="admin:home", style="danger")]]),
            )
        if d == "admin:bc:go":
            bc_info = context.user_data.pop("bc_payload", None)
            if not bc_info:
                return answer_bg(q, "⚠️ انتهت صلاحية الرسالة.", show_alert=True)
            answer_bg(q, "🚀 بدء الإذاعة...")
            targets = await db.broadcast_targets()
            total = len(targets)
            await safe_edit(q, f"📣 <b>جاري الإذاعة...</b>\n0 / {total}")
            ok_c = blocked_c = fail_c = 0
            for idx, target_uid in enumerate(targets, 1):
                try:
                    await context.bot.copy_message(
                        chat_id=target_uid,
                        from_chat_id=bc_info["chat_id"],
                        message_id=bc_info["message_id"],
                    )
                    ok_c += 1
                except Forbidden:
                    blocked_c += 1
                    await db.mark_blocked(target_uid, True)
                except RetryAfter as e:
                    await asyncio.sleep(float(e.retry_after) + 0.5)
                    try:
                        await context.bot.copy_message(
                            chat_id=target_uid,
                            from_chat_id=bc_info["chat_id"],
                            message_id=bc_info["message_id"],
                        )
                        ok_c += 1
                    except Exception:
                        fail_c += 1
                except Exception:
                    fail_c += 1
                if idx % 25 == 0 or idx == total:
                    await safe_edit(
                        q,
                        f"📣 <b>جاري الإذاعة...</b> ({idx}/{total})\n"
                        f"✅ وصلت: <code>{ok_c}</code> │ 🔕 حظروا البوت: <code>{blocked_c}</code> │ ❌ فشل: <code>{fail_c}</code>",
                    )
                await asyncio.sleep(0.04)
            return await safe_edit(
                q,
                f"✅ <b>اكتملت الإذاعة بنجاح!</b>\n\n"
                f"👥 الإجمالي المستهدف: <code>{total}</code>\n"
                f"✅ تم التسليم: <code>{ok_c}</code>\n"
                f"🔕 حظروا البوت: <code>{blocked_c}</code>\n"
                f"❌ أخطاء أخرى: <code>{fail_c}</code>",
                InlineKeyboardMarkup([[btn("🔙 لوحة الأدمن", callback_data="admin:home", style="primary")]]),
            )

        # ----- Users Admin -----
        if d == "admin:users":
            answer_bg(q)
            return await render_admin_users(q)
        if d == "admin:users:find":
            context.user_data["waiting_for"] = "admin_find_user"
            answer_bg(q)
            return await safe_edit(
                q,
                "🔎 أرسل <b>ID المستخدم</b> أو <b>@username</b>:",
                InlineKeyboardMarkup([[btn("❌ إلغاء", callback_data="admin:users", style="danger")]]),
            )
        if d == "admin:users:banned":
            answer_bg(q)
            b_list = await db.banned_users()
            lines = [head("🚫", "قائمة المحظورين", "🛡️ Admin"), ""]
            kb_rows = []
            if not b_list:
                lines.append("🟢 لا يوجد مستخدمون محظورون.")
            else:
                for u in b_list:
                    lines.append(f" • <code>{u['user_id']}</code> — {esc(u['first_name'])}")
                    kb_rows.append([btn(f"✅ فك حظر {u['user_id']}", callback_data=f"admin:u:ban:{u['user_id']}:0", style="success")])
            kb_rows.append([btn("🔙 إدارة المستخدمين", callback_data="admin:users", style="danger")])
            return await safe_edit(q, "\n".join(lines), InlineKeyboardMarkup(kb_rows))
        if d.startswith("admin:u:ban:"):
            parts = d.split(":")
            t_uid, flag = int(parts[3]), int(parts[4]) == 1
            if is_admin(t_uid):
                return answer_bg(q, "⚠️ لا يمكن حظر أدمن!", show_alert=True)
            await db.set_ban(t_uid, flag)
            answer_bg(q, "🚫 تم الحظر" if flag else "✅ تم فك الحظر")
            u_row = await db.get_user(t_uid)
            if u_row:
                return await render_admin_user_card(q, u_row, is_cb=True)
            return await render_admin_users(q)
        if d.startswith("admin:u:dm:"):
            t_uid = int(d.split(":")[3])
            context.user_data["dm_target"] = t_uid
            context.user_data["waiting_for"] = "admin_dm_user"
            answer_bg(q)
            return await safe_edit(
                q,
                f"✉️ أرسل الآن الرسالة التي تريد إرسالها للمستخدم <code>{t_uid}</code>:",
                InlineKeyboardMarkup([[btn("❌ إلغاء", callback_data="admin:users", style="danger")]]),
            )

        # ----- Settings Admin -----
        if d == "admin:settings":
            answer_bg(q)
            return await render_admin_settings(q)
        if d == "admin:set:maint":
            await db.set_setting("maintenance", "0" if RUNTIME_CFG["maintenance"] else "1")
            answer_bg(q, "✅ تم تحديث وضع الصيانة")
            return await render_admin_settings(q)
        if d == "admin:set:colors":
            await db.set_setting("colored_buttons", "0" if RUNTIME_CFG["colored_buttons"] else "1")
            answer_bg(q, "✅ تم تحديث تلوين الأزرار")
            return await render_admin_settings(q)
        if d == "admin:set:spread":
            context.user_data["waiting_for"] = "admin_spread"
            answer_bg(q)
            return await safe_edit(
                q,
                "⚖️ <b>تعديل هامش البيع والشراء للذهب (%)</b>\n\n"
                "أرسل النسبة المئوية (مثلاً <code>0.5</code> أو <code>0</code> للسعر العالمي الصافي بدون هامش):",
                InlineKeyboardMarkup([[btn("❌ إلغاء", callback_data="admin:settings", style="danger")]]),
            )
        if d == "admin:set:welcome":
            context.user_data["waiting_for"] = "admin_welcome"
            answer_bg(q)
            return await safe_edit(
                q,
                "✏️ <b>أرسل رسالة الترحيب الجديدة</b>\n"
                "<i>يمكنك استخدام</i> <code>{name}</code> <i>ليضع البوت اسم المستخدم تلقائياً.</i>",
                InlineKeyboardMarkup([[btn("❌ إلغاء", callback_data="admin:settings", style="danger")]]),
            )
        if d == "admin:set:welcome_reset":
            await db.set_setting("welcome_text", "")
            answer_bg(q, "♻️ تم استرجاع رسالة الترحيب الافتراضية")
            return await render_admin_settings(q)

        # ----- Admins Management -----
        if d == "admin:admins":
            answer_bg(q)
            return await render_admin_admins(q)
        if d == "admin:adm:add":
            context.user_data["waiting_for"] = "admin_add_admin"
            answer_bg(q)
            return await safe_edit(
                q,
                "👑 أرسل <b>ID</b> الشخص الذي تريد ترقيته كأدمن:",
                InlineKeyboardMarkup([[btn("❌ إلغاء", callback_data="admin:admins", style="danger")]]),
            )
        if d.startswith("admin:adm:del:"):
            aid = int(d.split(":")[3])
            await db.remove_admin(aid)
            answer_bg(q, "🗑️ تم إزالة الأدمن")
            return await render_admin_admins(q)

        # ----- DB Backup -----
        if d == "admin:backup":
            answer_bg(q, "💾 جاري تجهيز النسخة الاحتياطية...")
            try:
                await db.checkpoint()
                with open(S.db_path, "rb") as f:
                    await context.bot.send_document(
                        chat_id=uid,
                        document=f,
                        filename=f"backup_{now_local().strftime('%Y%m%d_%H%M')}.sqlite3",
                        caption=f"💾 نسخة احتياطية لقاعدة البيانات — <code>{now_hm()}</code>",
                        parse_mode=ParseMode.HTML,
                    )
            except Exception as e:
                await q.message.reply_text(
                    f"❌ تعذر إرسال النسخة: {esc(e)}", parse_mode=ParseMode.HTML
                )
            return


# ═══════════════════════════════════════════════════════════
# ✍️ UNIFIED INPUT ROUTER (Text + Media + Forward)
# ═══════════════════════════════════════════════════════════

async def input_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    waiting = context.user_data.get("waiting_for")
    if not waiting:
        return

    msg = update.message
    uid = update.effective_user.id

    if not waiting.startswith("admin_"):
        if not await gate_check(update, context, is_cb=False):
            return

    if waiting == "admin_broadcast":
        if not is_admin(uid):
            return
        context.user_data.pop("waiting_for", None)
        context.user_data["bc_payload"] = {"chat_id": msg.chat_id, "message_id": msg.message_id}
        targets = await db.broadcast_targets()
        return await msg.reply_text(
            f"📣 <b>تأكيد الإذاعة</b>\n\nسيتم إرسال رسالتك أعلاه إلى <code>{len(targets)}</code> مستخدم.\nهل تريد المتابعة؟",
            reply_markup=InlineKeyboardMarkup([
                [
                    btn("✅ تأكيد وإرسال الآن", callback_data="admin:bc:go", style="success"),
                    btn("❌ إلغاء", callback_data="admin:home", style="danger"),
                ]
            ]),
            parse_mode=ParseMode.HTML,
        )

    if waiting == "admin_add_fsub":
        if not is_admin(uid):
            return
        chat_ref = None
        f_origin = getattr(msg, "forward_origin", None)
        if f_origin and hasattr(f_origin, "chat") and f_origin.chat:
            chat_ref = f_origin.chat.id
        elif getattr(msg, "forward_from_chat", None):
            chat_ref = msg.forward_from_chat.id
        elif msg.text:
            raw = msg.text.strip()
            if raw.startswith("https://t.me/"):
                raw = "@" + raw.split("t.me/", 1)[1].strip("/").split("/")[0]
            chat_ref = int(raw) if raw.lstrip("-").isdigit() else raw
        if not chat_ref:
            return await msg.reply_text("⚠️ أرسل `@username` أو ID القناة أو وجّه رسالة منها.")
        try:
            chat = await context.bot.get_chat(chat_ref)
            me = await context.bot.get_chat_member(chat.id, context.bot.id)
            me_status = getattr(me.status, "value", str(me.status)).lower()
            if me_status not in ("administrator", "creator", "owner"):
                return await msg.reply_text(
                    "⚠️ البوت <b>ليس أدمن</b> في هذه القناة! ارفعه أدمن أولاً ثم أعد المحاولة.",
                    parse_mode=ParseMode.HTML,
                )
            invite = f"https://t.me/{chat.username}" if chat.username else (chat.invite_link or "")
            if not invite:
                try:
                    invite = await context.bot.export_chat_invite_link(chat.id)
                except TelegramError:
                    return await msg.reply_text(
                        "⚠️ القناة خاصة ولم أستطع استخراج رابط دعوة. فعّل صلاحية (دعوة المستخدمين عبر الرابط) للبوت ثم أعد المحاولة."
                    )
            await db.add_fsub_channel(str(chat.id), chat.title or str(chat.id), invite)
            if not RUNTIME_CFG["fsub_enabled"]:
                await db.set_setting("fsub_enabled", "1")
            context.user_data.pop("waiting_for", None)
            return await msg.reply_text(
                f"✅ <b>تمت إضافة القناة وتفعيل الاشتراك الإجباري!</b>\n\n"
                f"📢 <b>القناة:</b> {esc(chat.title)}\n"
                f"🆔 <b>ID:</b> <code>{chat.id}</code>\n"
                f"🔗 <b>الرابط:</b> {esc(invite)}",
                reply_markup=InlineKeyboardMarkup([[btn("📢 إدارة الاشتراك الإجباري", callback_data="admin:fsub", style="primary")]]),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception as e:
            return await msg.reply_text(
                f"❌ تعذر الوصول للقناة. تأكد من رفع البوت أدمن وصحة المعرّف.\n<code>{esc(e)}</code>",
                parse_mode=ParseMode.HTML,
            )

    text = (msg.text or "").strip()
    if not text:
        return

    if waiting == "admin_find_user" and is_admin(uid):
        u_row = await db.find_user(text)
        context.user_data.pop("waiting_for", None)
        if not u_row:
            return await msg.reply_text(
                "⚠️ لم يتم العثور على هذا المستخدم.",
                reply_markup=InlineKeyboardMarkup([[btn("🔙 إدارة المستخدمين", callback_data="admin:users", style="danger")]]),
            )
        return await render_admin_user_card(msg, u_row, is_cb=False)

    if waiting == "admin_dm_user" and is_admin(uid):
        t_uid = context.user_data.pop("dm_target", None)
        context.user_data.pop("waiting_for", None)
        if not t_uid:
            return
        try:
            await context.bot.send_message(
                t_uid,
                f"📩 <b>رسالة من إدارة البوت:</b>\n\n{esc(text)}",
                parse_mode=ParseMode.HTML,
            )
            return await msg.reply_text(
                f"✅ تم إرسال رسالتك إلى <code>{t_uid}</code> بنجاح.",
                reply_markup=InlineKeyboardMarkup([[btn("🔙 لوحة الأدمن", callback_data="admin:home", style="primary")]]),
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            return await msg.reply_text(f"❌ فشل الإرسال: <code>{esc(e)}</code>", parse_mode=ParseMode.HTML)

    if waiting == "admin_spread" and is_admin(uid):
        try:
            val = float(text.replace(",", "."))
            if not (0 <= val <= 15):
                raise ValueError
        except ValueError:
            return await msg.reply_text("⚠️ أرسل رقماً بين 0 و 15 (مثال: 0.5).")
        await db.set_setting("gold_spread_pct", str(val))
        context.user_data.pop("waiting_for", None)
        return await msg.reply_text(
            f"✅ تم ضبط هامش البيع والشراء للذهب إلى <code>{val:.2f}%</code>.",
            reply_markup=InlineKeyboardMarkup([[btn("⚙️ الإعدادات", callback_data="admin:settings", style="primary")]]),
            parse_mode=ParseMode.HTML,
        )

    if waiting == "admin_welcome" and is_admin(uid):
        await db.set_setting("welcome_text", text)
        context.user_data.pop("waiting_for", None)
        return await msg.reply_text(
            "✅ تم حفظ رسالة الترحيب الجديدة بنجاح.",
            reply_markup=InlineKeyboardMarkup([[btn("⚙️ الإعدادات", callback_data="admin:settings", style="primary")]]),
        )

    if waiting == "admin_add_admin" and is_admin(uid):
        if not text.isdigit():
            return await msg.reply_text("⚠️ أرسل رقم ID صحيح.")
        await db.add_admin(int(text))
        context.user_data.pop("waiting_for", None)
        return await msg.reply_text(
            f"✅ تمت ترقية <code>{text}</code> كأدمن.",
            reply_markup=InlineKeyboardMarkup([[btn("👑 إدارة الأدمن", callback_data="admin:admins", style="primary")]]),
            parse_mode=ParseMode.HTML,
        )

    if waiting == "alert_price":
        try:
            price = float(text.replace(",", ""))
            if price <= 0:
                raise ValueError
        except ValueError:
            return await msg.reply_text("⚠️ أرسل رقماً موجباً صحيحاً:")
        akey = context.user_data.pop("a_asset", None)
        direction = context.user_data.pop("a_dir", None)
        context.user_data.pop("waiting_for", None)
        if not akey or not direction:
            return await msg.reply_text("⚠️ انتهت الجلسة، أعد المحاولة.", reply_markup=kb_main(uid))
        await db.add_alert(uid, akey, direction, price)
        arrow = "⬆️ أعلى من" if direction == "above" else "⬇️ أقل من"
        return await msg.reply_text(
            f"✅ <b>تم تفعيل التنبيه بنجاح!</b>\n\n"
            f"🎯 <b>{ASSET_LABELS.get(akey, akey)}</b>\n"
            f"{arrow} <code>{fmt(price)}</code>",
            reply_markup=kb_main(uid),
            parse_mode=ParseMode.HTML,
        )

    if waiting == "port_qty":
        try:
            qty = float(text.replace(",", ""))
            if qty <= 0:
                raise ValueError
        except ValueError:
            return await msg.reply_text("⚠️ أرسل كمية صحيحة أكبر من الصفر:")
        akey = context.user_data.pop("p_asset", None)
        context.user_data.pop("waiting_for", None)
        if not akey:
            return await msg.reply_text("⚠️ انتهت الجلسة.", reply_markup=kb_main(uid))
        await db.add_holding(uid, akey, qty)
        return await msg.reply_text(
            f"✅ تمت إضافة <b>{qty:g}</b> من <b>{ASSET_LABELS.get(akey, akey)}</b> لمحفظتك.",
            reply_markup=InlineKeyboardMarkup([
                [btn("💼 عرض محفظتي", callback_data="menu:portfolio", style="success")],
                [btn("🏠 القائمة الرئيسية", callback_data="back:main", style="danger")],
            ]),
            parse_mode=ParseMode.HTML,
        )

    if waiting == "calc_fx":
        parts = text.split()
        if len(parts) != 3:
            return await msg.reply_text("⚠️ الصيغة الصحيحة: <code>100 USD EGP</code>", parse_mode=ParseMode.HTML)
        try:
            amount = float(parts[0].replace(",", ""))
        except ValueError:
            return await msg.reply_text("⚠️ المبلغ غير صحيح.")
        frm, to = parts[1].upper(), parts[2].upper()
        fx = await prices.fx()
        usd = amount if frm == "USD" else (amount / fx[frm] if fx.get(frm) else None)
        result = None if usd is None else (usd if to == "USD" else (usd * fx[to] if fx.get(to) else None))
        if result is None:
            return await msg.reply_text("⚠️ عملة غير مدعومة. المدعوم: USD, EGP, EUR, GBP, SAR, AED, KWD, QAR")
        context.user_data.pop("waiting_for", None)
        return await msg.reply_text(
            f"💱 <b>نتيجة التحويل</b>\n\n"
            f"<code>{amount:,.2f} {frm}</code> = <b><code>{result:,.2f} {to}</code></b>",
            reply_markup=kb_main(uid),
            parse_mode=ParseMode.HTML,
        )

    if waiting == "calc_gold":
        parts = text.split()
        if len(parts) not in (2, 3):
            return await msg.reply_text("⚠️ الصيغة: <code>10 21 150</code> (جرامات عيار مصنعية)", parse_mode=ParseMode.HTML)
        try:
            grams = float(parts[0].replace(",", ""))
            karat = int(parts[1])
            making = float(parts[2].replace(",", "")) if len(parts) == 3 else 0.0
        except ValueError:
            return await msg.reply_text("⚠️ تأكد من كتابة أرقام صحيحة.")
        if karat not in (24, 21, 18, 14):
            return await msg.reply_text("⚠️ العيارات المتاحة: 24 أو 21 أو 18 أو 14.")
        d = await prices.egypt_metals()
        gram_p = d["gold"][karat]["sell"]
        raw_total = gram_p * grams
        making_total = making * grams
        grand_total = raw_total + making_total
        context.user_data.pop("waiting_for", None)
        return await msg.reply_text(
            f"🧮 <b>حاسبة الذهب — عيار {karat}</b>\n\n"
            f"• سعر الجرام: <code>{fmt(gram_p)}</code> EGP\n"
            f"• قيمة الذهب ({grams:g} جم): <code>{fmt(raw_total)}</code> EGP\n"
            f"• إجمالي المصنعية: <code>{fmt(making_total)}</code> EGP\n"
            f"────────────────────\n"
            f"💰 <b>الإجمالي النهائي:</b> <code>{fmt(grand_total)}</code> EGP",
            reply_markup=kb_main(uid),
            parse_mode=ParseMode.HTML,
        )

    if waiting == "calc_crypto":
        parts = text.split()
        if len(parts) != 2:
            return await msg.reply_text("⚠️ الصيغة: <code>0.5 ETH</code>", parse_mode=ParseMode.HTML)
        try:
            qty = float(parts[0].replace(",", ""))
        except ValueError:
            return await msg.reply_text("⚠️ الكمية غير صحيحة.")
        sym = parts[1].upper()
        p, fx = await asyncio.gather(prices.price_of(sym), prices.fx())
        if not p or p[2] != "USD":
            return await msg.reply_text(f"⚠️ لم أجد العملة الرقمية <b>{esc(sym)}</b>.", parse_mode=ParseMode.HTML)
        val_usd = qty * p[0]
        val_egp = val_usd * fx["EGP"]
        context.user_data.pop("waiting_for", None)
        return await msg.reply_text(
            f"🟣 <b>تحويل {qty:g} {esc(sym)}</b>\n\n"
            f"💵 بالدولار: <code>${fmt(val_usd)}</code>\n"
            f"🇪🇬 بالجنيه: <code>{fmt(val_egp)}</code> EGP",
            reply_markup=kb_main(uid),
            parse_mode=ParseMode.HTML,
        )

    if waiting == "search":
        r = await prices.search_coin(text)
        context.user_data.pop("waiting_for", None)
        if not r:
            return await msg.reply_text(
                f"⚠️ لم أجد العملة <b>{esc(text.upper())}</b> على Binance.",
                reply_markup=kb_main(uid),
                parse_mode=ParseMode.HTML,
            )
        ic, bar = trend(r["change_24h"])
        return await msg.reply_text(
            f"{COIN_ICON.get(r['symbol'], '🪙')} <b>{r['symbol']} / USDT</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💵 <b>السعر:</b> <code>${fmt(r['price'])}</code>\n"
            f"{ic} <b>التغير 24س:</b> <code>{r['change_24h']:+.2f}%</code> {bar}\n"
            f"📈 <b>أعلى 24س:</b> <code>${fmt(r['high_24h'])}</code>\n"
            f"📉 <b>أقل 24س:</b> <code>${fmt(r['low_24h'])}</code>\n"
            f"💧 <b>السيولة 24س:</b> <code>${fmt(r['volume_usdt'], 0)}</code>\n"
            f"📡 <i>المصدر: {r['source']}</i>",
            reply_markup=kb_main(uid),
            parse_mode=ParseMode.HTML,
        )


# ═══════════════════════════════════════════════════════════
# ⏰ BACKGROUND JOBS
# ═══════════════════════════════════════════════════════════

async def job_market_sync(context: ContextTypes.DEFAULT_TYPE):
    global _BG_SYNC_COUNT
    _BG_SYNC_COUNT += 1
    tasks = [prices.crypto(force=True)]
    if _BG_SYNC_COUNT % 2 == 0 or "metals" not in prices.cache:
        tasks.append(prices.metals(force=True))
    if _BG_SYNC_COUNT % 3 == 0 or "oil" not in prices.cache:
        tasks.append(prices.oil(force=True))
    if _BG_SYNC_COUNT % 4 == 0 or "movers" not in prices.cache:
        tasks.append(prices.top_movers(force=True))
    if _BG_SYNC_COUNT % 40 == 0 or "fx" not in prices.cache:
        tasks.append(prices.fx(force=True))
    await asyncio.gather(*tasks, return_exceptions=True)


async def job_alerts(context: ContextTypes.DEFAULT_TYPE):
    alerts = await db.active_alerts()
    if not alerts:
        return
    unique_syms = list({a["symbol"] for a in alerts})
    res = await asyncio.gather(*[prices.price_of(s) for s in unique_syms], return_exceptions=True)
    price_map = {s: r for s, r in zip(unique_syms, res) if not isinstance(r, Exception) and r}

    for a in alerts:
        p = price_map.get(a["symbol"])
        if not p:
            continue
        cur, unit, _ = p
        trig = (a["direction"] == "above" and cur >= a["target"]) or \
               (a["direction"] == "below" and cur <= a["target"])
        if not trig:
            continue
        arrow = "⬆️ ارتفع فوق" if a["direction"] == "above" else "⬇️ انخفض تحت"
        lbl = ASSET_LABELS.get(a["symbol"], a["symbol"])
        try:
            await context.bot.send_message(
                a["user_id"],
                f"🔔 <b>تنبيه سعري فوري!</b>\n\n"
                f"🎯 <b>{lbl}</b>\n"
                f"{arrow}: <code>{fmt(a['target'])}</code> {unit}\n"
                f"⚡ السعر الآن: <b><code>{fmt(cur)}</code> {unit}</b>\n"
                f"🕐 <code>{now_hm()}</code>",
                parse_mode=ParseMode.HTML,
            )
            await db.deactivate(a["id"])
        except Forbidden:
            await db.mark_blocked(a["user_id"], True)
            await db.deactivate(a["id"])
        except Exception as e:
            log.warning(f"Alert {a['id']} send error: {e}")


async def job_digest(context: ContextTypes.DEFAULT_TYPE):
    now_dt = now_local()
    hm = now_dt.strftime("%H:%M")
    today = now_dt.date().isoformat()
    users = await db.due_digest(hm, today)
    if not users:
        return
    try:
        snap = await screen_snapshot(force=True)
    except Exception:
        return
    text = f"📰 نشرتك اليومية — {today}\n\n{snap}"
    for uid in users:
        try:
            await context.bot.send_message(uid, text, parse_mode=ParseMode.HTML)
            await db.mark_digest(uid, today)
        except Forbidden:
            await db.mark_blocked(uid, True)
        except Exception:
            pass
        await asyncio.sleep(0.05)


# ═══════════════════════════════════════════════════════════
# 🚀 APP LIFECYCLE & MAIN
# ═══════════════════════════════════════════════════════════

_WEB_RUNNER = None


async def _api_market(request):
    force = request.query.get("force") == "1"
    m, c, f, o = await asyncio.gather(
        prices.egypt_metals(force),
        prices.crypto(force=force),
        prices.fx(force),
        prices.oil(force),
        return_exceptions=True,
    )
    return web.json_response({
        "updated_at": now_hm(),
        "metals": None if isinstance(m, Exception) else m,
        "crypto": None if isinstance(c, Exception) else c,
        "fx": None if isinstance(f, Exception) else f,
        "oil": None if isinstance(o, Exception) else o,
    })


async def _serve_index(request):
    path = os.path.join(os.path.dirname(__file__), "index.html")
    if os.path.exists(path):
        return web.FileResponse(path)
    return web.Response(
        text="✅ Bot is running",
        content_type="text/plain",
        status=200,
    )


async def _start_webapp():
    global _WEB_RUNNER
    try:
        wapp = web.Application()
        wapp.router.add_get("/", _serve_index)
        wapp.router.add_get("/api/market", _api_market)
        _WEB_RUNNER = web.AppRunner(wapp)
        await _WEB_RUNNER.setup()
        port = int(os.getenv("PORT", "8080"))
        await web.TCPSite(_WEB_RUNNER, "0.0.0.0", port).start()
        log.info(f"🌐 Mini App running on port {port}")
    except Exception as e:
        log.warning(f"⚠️ WebApp skipped (non-fatal): {e}")
        _WEB_RUNNER = None


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled exception:", exc_info=context.error)


async def _warmup_cache():
    await asyncio.gather(
        prices.crypto(force=True),
        prices.metals(force=True),
        prices.fx(force=True),
        prices.oil(force=True),
        prices.top_movers(force=True),
        return_exceptions=True,
    )
    log.info("⚡ Turbo Cache 100% warmed up")


async def _post_init(app: Application):
    global _WARMUP_TASK
    await db.init()
    log.info("✅ SQLite WAL + RAM Cache ready")
    if S.webapp_url or os.getenv("PORT"):
        await _start_webapp()
    _WARMUP_TASK = asyncio.create_task(_warmup_cache())


async def _post_shutdown(app: Application):
    global _WARMUP_TASK
    if _WARMUP_TASK and not _WARMUP_TASK.done():
        _WARMUP_TASK.cancel()
    if _WEB_RUNNER:
        try:
            await _WEB_RUNNER.cleanup()
        except Exception:
            pass
    await prices.close()
    await db.close()
    log.info("🛑 Clean shutdown complete")


def main():
    if not S.token or S.token == "ضع_توكن_البوت_هنا":
        raise SystemExit("❌ ضع BOT_TOKEN في ملف .env أو داخل Settings.token أولاً.")

    req = HTTPXRequest(
        connection_pool_size=32,
        connect_timeout=10.0,
        read_timeout=15.0,
        write_timeout=15.0,
        pool_timeout=10.0,
    )
    app = (
        Application.builder()
        .token(S.token)
        .request(req)
        .concurrent_updates(True)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("price", cmd_price))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CallbackQueryHandler(cb_router))
    app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & ~filters.COMMAND, input_router))
    app.add_error_handler(error_handler)

    jq = app.job_queue
    if jq is None:
        log.error('❌ JobQueue غير متاح — ثبّت: pip install "python-telegram-bot[job-queue]"')
        log.error('⚠️ البوت هيشتغل بدون تنبيهات/نشرة تلقائية!')
    else:
        jq.run_repeating(job_market_sync, interval=15, first=15)
        jq.run_repeating(job_alerts, interval=S.alert_interval, first=18)
        jq.run_repeating(job_digest, interval=60, first=30)
        log.info("✅ Background jobs scheduled")

    log.info("🚀 Turbo Pro Financial Bot running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
