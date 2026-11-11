import os
import json
import hashlib
import secrets
from datetime import datetime, timezone, timedelta
import aiosqlite

# ── BULLETPROOF DB PATH FIX ──────────────────────────────────────
DB_PATH = os.getenv("DB_PATH", "kiora.db")
db_dir = os.path.dirname(DB_PATH)

if db_dir and not os.path.exists(db_dir):
    try:
        os.makedirs(db_dir, exist_ok=True)
        print(f"✅ Created database directory: {db_dir}")
    except Exception as e:
        print(f"❌ Failed to create {db_dir}: {e}. Falling back to local.")
        DB_PATH = "kiora.db"
# ─────────────────────────────────────────────────────────────────

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "yousafcodes.de@gmail.com")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "03283877877@_1")

PLANS = {
    "free":      {"name": "Free",      "limit": 50,   "mass_limit": 20,    "days": 0},
    "basic":     {"name": "Basic",     "limit": 500,  "mass_limit": 200,   "days": 30},
    "pro":       {"name": "Pro",       "limit": 2000, "mass_limit": 1000,  "days": 30},
    "premium":   {"name": "Premium",   "limit": 5000, "mass_limit": 2500,  "days": 30},
    "unlimited": {"name": "Unlimited", "limit": 999999, "mass_limit": 10000, "days": 365},
}

def hash_pw(pw: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), b"kiora_salt_v1", 100_000).hex()

def verify_pw(pw: str, hashed: str) -> bool:
    return secrets.compare_digest(hash_pw(pw), hashed)

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _conn():
    return aiosqlite.connect(DB_PATH)

async def init_db():
    async with _conn() as c:
        await c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT DEFAULT 'user',
            plan TEXT DEFAULT 'free',
            plan_expires TEXT DEFAULT '',
            credits INTEGER DEFAULT 100,
            banned INTEGER DEFAULT 0,
            total_checks INTEGER DEFAULT 0,
            total_hits INTEGER DEFAULT 0,
            created_at TEXT DEFAULT '',
            last_login TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS proxies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            proxy TEXT,
            status TEXT DEFAULT 'unknown',
            latency REAL DEFAULT 0,
            checked_at TEXT DEFAULT '',
            created_at TEXT DEFAULT '',
            UNIQUE(user_id, proxy)
        );

        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            user_id INTEGER,
            total INTEGER DEFAULT 0,
            completed INTEGER DEFAULT 0,
            hits INTEGER DEFAULT 0,
            declined INTEGER DEFAULT 0,
            errors INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending',
            mode TEXT DEFAULT 'shopify',
            started_at TEXT DEFAULT '',
            ended_at TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS hits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            task_id TEXT,
            cc TEXT,
            response TEXT,
            status TEXT,
            gateway TEXT,
            amount REAL DEFAULT 0,
            currency TEXT DEFAULT 'USD',
            site TEXT DEFAULT '',
            bin_info TEXT DEFAULT '{}',
            created_at TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS api_endpoints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT,
            style TEXT DEFAULT 'params',
            cap INTEGER DEFAULT 900,
            enabled INTEGER DEFAULT 1,
            key TEXT DEFAULT '',
            site_required INTEGER DEFAULT 0,
            created_at TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS redeem_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE,
            plan TEXT,
            days INTEGER DEFAULT 30,
            max_uses INTEGER DEFAULT 1,
            used INTEGER DEFAULT 0,
            created_at TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            message TEXT,
            read INTEGER DEFAULT 0,
            created_at TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """)

        async with c.execute("SELECT id FROM users WHERE email=?", (ADMIN_EMAIL,)) as cur:
            row = await cur.fetchone()
        if not row:
            await c.execute(
                "INSERT INTO users (email, password_hash, role, plan, credits, created_at) VALUES (?,?,?,?,?,?)",
                (ADMIN_EMAIL, hash_pw(ADMIN_PASSWORD), "admin", "unlimited", 999999, now_iso()),
            )

        defaults = {
            "telegram_bot_token": "",
            "telegram_chat_id": "",
            "telegram_enabled": "0",
            "notification_message": "New hit found on KIORA",
            "site_mode": "auto",
            "lb_vps_enabled": "1",
            "lb_heroku_enabled": "1",
        }
        for k, v in defaults.items():
            await c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?,?)", (k, v))

        async with c.execute("SELECT COUNT(*) FROM api_endpoints") as cur:
            n = (await cur.fetchone())[0]
        if n == 0:
            defaults_api = [
                ("http://107.148.78.54:8083/shopify", "raw", 900, 0, ""),
                ("http://107.148.78.54:8084/shopify", "raw", 900, 0, ""),
            ]
            for url, style, cap, sr, key in defaults_api:
                await c.execute(
                    "INSERT INTO api_endpoints (url, style, cap, enabled, key, site_required, created_at) VALUES (?,?,?,1,?,?,?)",
                    (url, style, cap, key, sr, now_iso()),
                )

        await c.commit()

async def get_user_by_email(email: str):
    async with _conn() as c:
        c.row_factory = aiosqlite.Row
        async with c.execute("SELECT * FROM users WHERE email=?", (email,)) as cur:
            return await cur.fetchone()

async def get_user_by_id(uid: int):
    async with _conn() as c:
        c.row_factory = aiosqlite.Row
        async with c.execute("SELECT * FROM users WHERE id=?", (uid,)) as cur:
            return await cur.fetchone()

async def list_users():
    async with _conn() as c:
        c.row_factory = aiosqlite.Row
        async with c.execute("SELECT * FROM users ORDER BY id DESC") as cur:
            return await cur.fetchall()

async def create_user(email: str, password: str, plan: str = "free"):
    async with _conn() as c:
        await c.execute(
            "INSERT INTO users (email, password_hash, role, plan, credits, created_at) VALUES (?,?,?,?,?,?)",
            (email.lower().strip(), hash_pw(password), "user", plan, 100, now_iso()),
        )
        await c.commit()

async def update_user_plan(uid: int, plan: str, days: int = 30):
    expires = "" if plan == "unlimited" else (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    async with _conn() as c:
        await c.execute("UPDATE users SET plan=?, plan_expires=? WHERE id=?", (plan, expires, uid))
        await c.commit()

async def toggle_user_ban(uid: int):
    async with _conn() as c:
        async with c.execute("SELECT banned FROM users WHERE id=?", (uid,)) as cur:
            row = await cur.fetchone()
        new = 0 if (row and row[0]) else 1
        await c.execute("UPDATE users SET banned=? WHERE id=?", (new, uid))
        await c.commit()

async def delete_user(uid: int):
    async with _conn() as c:
        await c.execute("DELETE FROM users WHERE id=? AND role != 'admin'", (uid,))
        await c.execute("DELETE FROM proxies WHERE user_id=?", (uid,))
        await c.execute("DELETE FROM hits WHERE user_id=?", (uid,))
        await c.commit()

async def list_proxies(uid: int):
    async with _conn() as c:
        c.row_factory = aiosqlite.Row
        async with c.execute("SELECT * FROM proxies WHERE user_id=? ORDER BY id DESC", (uid,)) as cur:
            return await cur.fetchall()

async def add_proxy(uid: int, proxy: str):
    async with _conn() as c:
        await c.execute(
            "INSERT OR IGNORE INTO proxies (user_id, proxy, status, created_at) VALUES (?,?,?,?)",
            (uid, proxy, "unknown", now_iso()),
        )
        await c.commit()

async def update_proxy_status(pid: int, status: str, latency: float):
    async with _conn() as c:
        await c.execute(
            "UPDATE proxies SET status=?, latency=?, checked_at=? WHERE id=?",
            (status, latency, now_iso(), pid),
        )
        await c.commit()

async def delete_proxy(pid: int, uid: int):
    async with _conn() as c:
        await c.execute("DELETE FROM proxies WHERE id=? AND user_id=?", (pid, uid))
        await c.commit()

async def clear_proxies(uid: int):
    async with _conn() as c:
        await c.execute("DELETE FROM proxies WHERE user_id=?", (uid,))
        await c.commit()

async def live_proxies(uid: int):
    async with _conn() as c:
        c.row_factory = aiosqlite.Row
        async with c.execute("SELECT * FROM proxies WHERE user_id=? AND status='live' ORDER BY id", (uid,)) as cur:
            return await cur.fetchall()

async def list_apis():
    async with _conn() as c:
        c.row_factory = aiosqlite.Row
        async with c.execute("SELECT * FROM api_endpoints ORDER BY id") as cur:
            return await cur.fetchall()

async def enabled_apis():
    async with _conn() as c:
        c.row_factory = aiosqlite.Row
        async with c.execute("SELECT * FROM api_endpoints WHERE enabled=1 ORDER BY id") as cur:
            return await cur.fetchall()

async def add_api(url: str, style: str, cap: int, site_required: int, key: str = ""):
    async with _conn() as c:
        await c.execute(
            "INSERT INTO api_endpoints (url, style, cap, enabled, key, site_required, created_at) VALUES (?,?,?,1,?,?,?)",
            (url, style, cap, key, site_required, now_iso()),
        )
        await c.commit()

async def toggle_api(aid: int):
    async with _conn() as c:
        async with c.execute("SELECT enabled FROM api_endpoints WHERE id=?", (aid,)) as cur:
            row = await cur.fetchone()
        new = 0 if (row and row[0]) else 1
        await c.execute("UPDATE api_endpoints SET enabled=? WHERE id=?", (new, aid))
        await c.commit()

async def delete_api(aid: int):
    async with _conn() as c:
        await c.execute("DELETE FROM api_endpoints WHERE id=?", (aid,))
        await c.commit()

async def create_task(tid: str, uid: int, total: int, mode: str):
    async with _conn() as c:
        await c.execute(
            "INSERT INTO tasks (id, user_id, total, status, mode, started_at) VALUES (?,?,?,?,?,?)",
            (tid, uid, total, "running", mode, now_iso()),
        )
        await c.commit()

async def update_task(tid: str, **kw):
    if not kw: return
    fields = ", ".join(f"{k}=?" for k in kw)
    async with _conn() as c:
        await c.execute(f"UPDATE tasks SET {fields} WHERE id=?", (*kw.values(), tid))
        await c.commit()

async def get_task(tid: str):
    async with _conn() as c:
        c.row_factory = aiosqlite.Row
        async with c.execute("SELECT * FROM tasks WHERE id=?", (tid,)) as cur:
            return await cur.fetchone()

async def list_user_tasks(uid: int, limit: int = 20):
    async with _conn() as c:
        c.row_factory = aiosqlite.Row
        async with c.execute("SELECT * FROM tasks WHERE user_id=? ORDER BY started_at DESC LIMIT ?", (uid, limit)) as cur:
            return await cur.fetchall()

async def list_all_tasks(limit: int = 50):
    async with _conn() as c:
        c.row_factory = aiosqlite.Row
        async with c.execute("SELECT t.*, u.email FROM tasks t LEFT JOIN users u ON u.id = t.user_id ORDER BY t.started_at DESC LIMIT ?", (limit,)) as cur:
            return await cur.fetchall()

async def save_hit(uid: int, tid: str, cc: str, res: dict, site: str = ""):
    async with _conn() as c:
        await c.execute(
            "INSERT INTO hits (user_id, task_id, cc, response, status, gateway, amount, currency, site, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (uid, tid, cc, res.get("response", "")[:200], res.get("cls", ""),
             res.get("gateway", "Shopify"), float(res.get("amount", 0) or 0),
             res.get("currency", "USD"), site, now_iso()),
        )
        await c.execute("UPDATE users SET total_hits = total_hits + 1 WHERE id=?", (uid,))
        await c.commit()

async def list_user_hits(uid: int, limit: int = 200):
    async with _conn() as c:
        c.row_factory = aiosqlite.Row
        async with c.execute("SELECT * FROM hits WHERE user_id=? ORDER BY id DESC LIMIT ?", (uid, limit)) as cur:
            return await cur.fetchall()

async def list_all_hits(limit: int = 500):
    async with _conn() as c:
        c.row_factory = aiosqlite.Row
        async with c.execute("SELECT h.*, u.email FROM hits h LEFT JOIN users u ON u.id = h.user_id ORDER BY h.id DESC LIMIT ?", (limit,)) as cur:
            return await cur.fetchall()

async def delete_hit(hid: int, uid: int):
    async with _conn() as c:
        await c.execute("DELETE FROM hits WHERE id=? AND user_id=?", (hid, uid))
        await c.commit()

async def clear_user_hits(uid: int):
    async with _conn() as c:
        await c.execute("DELETE FROM hits WHERE user_id=?", (uid,))
        await c.commit()

async def get_setting(key: str, default: str = "") -> str:
    async with _conn() as c:
        async with c.execute("SELECT value FROM settings WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
        return row[0] if row else default

async def set_setting(key: str, value: str):
    async with _conn() as c:
        await c.execute("INSERT INTO settings(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        await c.commit()

async def create_code(code: str, plan: str, days: int, uses: int):
    async with _conn() as c:
        await c.execute(
            "INSERT INTO redeem_codes (code, plan, days, max_uses, created_at) VALUES (?,?,?,?,?)",
            (code, plan, days, uses, now_iso()),
        )
        await c.commit()

async def list_codes():
    async with _conn() as c:
        c.row_factory = aiosqlite.Row
        async with c.execute("SELECT * FROM redeem_codes ORDER BY id DESC") as cur:
            return await cur.fetchall()
