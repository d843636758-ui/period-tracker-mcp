"""period-tracker + MCP · self-hosted menstrual cycle tracker.

Original project: chaodeng060-source/period-tracker
Single-file derivative: REST API + embedded web UI + Streamable HTTP MCP endpoint.

Environment variables:
    PERIOD_DATA               Data file path (default: ./period_state.json)
    PERIOD_PORT               HTTP port (default: 8080)
    PERIOD_HOST               Listen host (default: 127.0.0.1)
    PERIOD_TZ                 Timezone used for "today" (default: Asia/Shanghai)
    PERIOD_TOKEN              Optional shared secret protecting /api/* and /mcp
    PERIOD_ALLOW_QUERY_TOKEN  If true, allow ?token=... for MCP as a fallback
                              when the client cannot send Authorization headers.
                              Prefer Bearer auth; query tokens may appear in logs.
    PERIOD_PUBLIC_HOST        Optional public hostname used for MCP Host allowlist.
                              Example: period.example.com
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import hmac
import html
import json
import logging
import os
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

logger = logging.getLogger("period")

ROOT = Path(__file__).resolve().parent
STATE_PATH = Path(os.environ.get("PERIOD_DATA", ROOT / "period_state.json"))
PERIOD_TOKEN = os.environ.get("PERIOD_TOKEN", "").strip()
ALLOW_QUERY_TOKEN = os.environ.get("PERIOD_ALLOW_QUERY_TOKEN", "0").lower() in {"1", "true", "yes", "on"}
PUBLIC_HOST = os.environ.get("PERIOD_PUBLIC_HOST", "").strip()

DEFAULT_CYCLE = 28
DEFAULT_LENGTH = 5
_STATE_LOCK = threading.RLock()


def _timezone() -> ZoneInfo:
    name = os.environ.get("PERIOD_TZ", "Asia/Shanghai")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        logger.warning("unknown PERIOD_TZ=%s; falling back to UTC", name)
        return ZoneInfo("UTC")


TZ = _timezone()


def local_today() -> dt.date:
    return dt.datetime.now(TZ).date()


def load_state() -> dict:
    with _STATE_LOCK:
        try:
            if STATE_PATH.exists():
                data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception as e:  # noqa: BLE001
            logger.info("load period state failed: %s", e)
        return {"starts": [], "period_length": DEFAULT_LENGTH}


def save_state(state: dict) -> None:
    """Atomic-ish local write so a restart/write does not leave half JSON."""
    with _STATE_LOCK:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATE_PATH)


def period_phase(day: int | None, cycle_len: int, period_length: int) -> str | None:
    """Map day_of_cycle to menstrual / follicular / ovulation / luteal / late."""
    if day is None or day < 1:
        return None
    ovulation = max(period_length + 1, cycle_len - 14)
    if day <= period_length:
        return "menstrual"
    if day < ovulation - 1:
        return "follicular"
    if day <= ovulation + 1:
        return "ovulation"
    if day <= cycle_len + 2:
        return "luteal"
    return "late"


def derive(state: dict, today: dt.date | None = None) -> dict:
    starts = sorted({s for s in (state.get("starts") or []) if isinstance(s, str)})
    try:
        period_length = int(state.get("period_length") or DEFAULT_LENGTH)
    except (TypeError, ValueError):
        period_length = DEFAULT_LENGTH
    period_length = min(max(period_length, 1), 14)

    parsed: list[dt.date] = []
    for s in starts:
        try:
            parsed.append(dt.date.fromisoformat(s))
        except ValueError:
            continue

    # Start dates less than 15 days apart are treated as duplicate clicks
    # in the same bleed; keep the earliest date.
    dedup: list[dt.date] = []
    for d in parsed:
        if dedup and (d - dedup[-1]).days < 15:
            continue
        dedup.append(d)
    parsed = dedup

    diffs = [
        (b - a).days
        for a, b in zip(parsed, parsed[1:])
        if 15 <= (b - a).days <= 60
    ]
    cycle_len = round(sum(diffs) / len(diffs)) if diffs else DEFAULT_CYCLE
    last_start = parsed[-1].isoformat() if parsed else None
    next_due = (parsed[-1] + dt.timedelta(days=cycle_len)).isoformat() if parsed else None
    today = today or local_today()
    day_of_cycle = (today - parsed[-1]).days + 1 if parsed else None
    days_until_next = (dt.date.fromisoformat(next_due) - today).days if next_due else None
    return {
        "last_start": last_start,
        "next_due": next_due,
        "recorded": len(parsed),
        "cycles": max(0, len(parsed) - 1),
        "avg_cycle": cycle_len,
        "period_length": period_length,
        "day_of_cycle": day_of_cycle,
        "days_until_next": days_until_next,
        "phase": period_phase(day_of_cycle, cycle_len, period_length),
    }


def snapshot() -> dict:
    state = load_state()
    return {
        "today": local_today().isoformat(),
        "timezone": str(TZ),
        "state": state,
        "derived": derive(state),
        "note": "Cycle and ovulation dates are estimates only, not medical or contraceptive advice.",
    }


def log_start(action: str = "start", date_str: str | None = None) -> dict:
    action = (action or "start").lower()
    if action not in {"start", "undo"}:
        raise ValueError("action must be 'start' or 'undo'")
    date_str = date_str or local_today().isoformat()
    try:
        dt.date.fromisoformat(date_str)
    except (ValueError, TypeError) as e:
        raise ValueError("date must be YYYY-MM-DD") from e

    with _STATE_LOCK:
        state = load_state()
        starts = {s for s in (state.get("starts") or []) if isinstance(s, str)}
        if action == "undo":
            starts.discard(date_str)
        else:
            starts.add(date_str)
        state["starts"] = sorted(starts)
        state.setdefault("period_length", DEFAULT_LENGTH)
        save_state(state)
    return snapshot()


def set_period_length_value(days: int) -> dict:
    if not 1 <= int(days) <= 14:
        raise ValueError("period_length must be between 1 and 14 days")
    with _STATE_LOCK:
        state = load_state()
        state["period_length"] = int(days)
        save_state(state)
    return snapshot()


def _session_cookie_value() -> str:
    if not PERIOD_TOKEN:
        return ""
    return hashlib.sha256(("period-session:" + PERIOD_TOKEN).encode("utf-8")).hexdigest()


def _request_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    if hmac.compare_digest(request.cookies.get("period_session", ""), _session_cookie_value()):
        return PERIOD_TOKEN
    if ALLOW_QUERY_TOKEN:
        return request.query_params.get("token", "")
    return ""


def _authorized(request: Request) -> bool:
    if not PERIOD_TOKEN:
        return True
    supplied = _request_token(request)
    return bool(supplied) and hmac.compare_digest(supplied, PERIOD_TOKEN)


# MCP transport security. With an explicit public host, keep DNS-rebinding
# protection on. Without one (common behind managed reverse proxies such as
# Zeabur), rely on the proxy + our Bearer middleware instead of rejecting the
# real Host header with 421.
if PUBLIC_HOST:
    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[PUBLIC_HOST, f"{PUBLIC_HOST}:*"],
        allowed_origins=[f"https://{PUBLIC_HOST}", f"https://{PUBLIC_HOST}:*"],
    )
else:
    transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)

period_mcp = FastMCP(
    "period-tracker",
    instructions=(
        "Private menstrual-cycle tracker. Read the recorded dates exactly. "
        "Predictions and phase labels are estimates only; never treat them as "
        "diagnosis, contraception, or proof of ovulation."
    ),
    stateless_http=True,
    json_response=True,
)


@period_mcp.tool()
def get_period_state() -> dict:
    """Read all recorded period starts plus current derived cycle status."""
    return snapshot()


@period_mcp.tool()
def get_period_summary() -> dict:
    """Read a compact cycle summary for conversation use."""
    s = snapshot()
    d = s["derived"]
    return {
        "today": s["today"],
        "timezone": s["timezone"],
        "last_start": d["last_start"],
        "next_due": d["next_due"],
        "avg_cycle": d["avg_cycle"],
        "period_length": d["period_length"],
        "day_of_cycle": d["day_of_cycle"],
        "days_until_next": d["days_until_next"],
        "phase": d["phase"],
        "recorded_periods": d["recorded"],
        "note": s["note"],
    }


@period_mcp.tool()
def record_period_start(date: str | None = None) -> dict:
    """Record a period start date. Omit date to use today in PERIOD_TZ."""
    return log_start("start", date)


@period_mcp.tool()
def undo_period_start(date: str) -> dict:
    """Remove one recorded period start date (YYYY-MM-DD)."""
    return log_start("undo", date)


@period_mcp.tool()
def set_period_length(days: int) -> dict:
    """Set the usual bleed length in days (1-14) used for phase display."""
    return set_period_length_value(days)


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    async with period_mcp.session_manager.run():
        yield


app = FastAPI(title="period-tracker", lifespan=lifespan)


@app.middleware("http")
async def protect_private_routes(request: Request, call_next):
    path = request.url.path
    if PERIOD_TOKEN and (path.startswith("/api/") or path == "/mcp" or path.startswith("/mcp/")):
        if not _authorized(request):
            return JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
    return await call_next(request)


@app.get("/api/period/state")
async def period_state():
    return JSONResponse(snapshot())


@app.post("/api/period/log")
async def period_log(req: Request):
    try:
        body = await req.json()
    except Exception:  # noqa: BLE001
        body = {}
    body = body if isinstance(body, dict) else {}
    try:
        return JSONResponse(log_start(body.get("action", "start"), body.get("date")))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/period/length")
async def period_length(req: Request):
    try:
        body = await req.json()
        days = int(body.get("days"))
        return JSONResponse(set_period_length_value(days))
    except (ValueError, TypeError, AttributeError) as e:
        return JSONResponse({"error": str(e) or "bad days"}, status_code=400)


LOGIN_PAGE = """<!doctype html>
<html lang=\"zh-CN\"><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
<title>period-tracker</title>
<style>body{font-family:-apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif;background:#f6fbf7;color:#33433b;margin:0;display:grid;place-items:center;min-height:100vh}.box{width:min(86vw,360px);background:#fff;border:1px solid #e7f0e9;border-radius:20px;padding:26px;box-shadow:0 12px 36px rgba(30,71,48,.07)}h1{font-size:20px;margin:0 0 8px;color:#4d8065}p{font-size:13px;color:#87928d;margin:0 0 18px}input,button{box-sizing:border-box;width:100%;height:44px;border-radius:14px}input{border:1px solid #dfe9e2;padding:0 14px;font-size:15px;margin-bottom:10px}button{border:0;background:#4d8065;color:#fff;font-size:15px}</style>
<div class=\"box\"><h1>period-tracker</h1><p>输入访问令牌后进入。令牌只用于这台自托管服务。</p><form method=\"post\" action=\"/unlock\"><input type=\"password\" name=\"token\" autocomplete=\"current-password\" required><button>解锁</button></form></div>
</html>"""


@app.post("/unlock")
async def unlock(req: Request):
    if not PERIOD_TOKEN:
        return RedirectResponse("/", status_code=303)
    body = (await req.body()).decode("utf-8", errors="replace")
    token = parse_qs(body).get("token", [""])[0]
    if not hmac.compare_digest(token, PERIOD_TOKEN):
        return HTMLResponse(LOGIN_PAGE.replace("输入访问令牌后进入。", "令牌不对，再试一次。"), status_code=401)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(
        "period_session",
        _session_cookie_value(),
        httponly=True,
        secure=req.url.scheme == "https",
        samesite="strict",
        max_age=60 * 60 * 24 * 90,
    )
    return resp


APP_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>经期小记</title>
<style>
:root{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;color:#34443b;background:#f6fbf7}
*{box-sizing:border-box}body{margin:0;min-height:100vh;padding:24px 16px}.wrap{max-width:520px;margin:auto}.card{background:#fff;border:1px solid #e5efe8;border-radius:24px;padding:22px;box-shadow:0 12px 36px rgba(47,92,64,.08);margin-bottom:14px}h1{font-size:24px;margin:0 0 4px;color:#47785e}.sub{font-size:13px;color:#8a9890;margin-bottom:18px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.stat{background:#f8fbf9;border:1px solid #edf3ef;border-radius:17px;padding:14px}.k{font-size:12px;color:#91a098}.v{font-size:18px;margin-top:5px;font-weight:650;color:#466452}.wide{grid-column:1/-1}button{width:100%;border:0;border-radius:16px;height:48px;font-size:15px;font-weight:650;cursor:pointer}.primary{background:#4f8067;color:white}.secondary{background:#eef5f1;color:#4f6f5e;margin-top:9px}.row{display:flex;gap:8px;margin-top:12px}input{width:100%;height:44px;border:1px solid #dce8e0;border-radius:14px;padding:0 12px;font-size:15px}.small{font-size:12px;color:#8d9892;line-height:1.55}.error{color:#a44}.ok{color:#4f8067}.phase{display:inline-block;padding:5px 9px;border-radius:999px;background:#eef5f1;color:#4f8067;font-size:12px}.footer{text-align:center;color:#98a29d;font-size:11px;padding:6px 0 20px}
</style>
</head>
<body><div class="wrap">
  <div class="card">
    <h1>经期小记 🌿</h1><div class="sub">私有、自托管；预测仅作记录参考。</div>
    <div id="summary" class="grid"><div class="small">正在读取…</div></div>
  </div>
  <div class="card">
    <button class="primary" onclick="logToday()">今天来月经了</button>
    <div class="row"><input id="date" type="date"><button class="secondary" style="margin:0;width:145px" onclick="logDate()">补记日期</button></div>
    <button class="secondary" onclick="undoDate()">撤销这个日期</button>
    <div class="row"><input id="length" type="number" min="1" max="14" placeholder="经期通常持续几天"><button class="secondary" style="margin:0;width:145px" onclick="setLength()">保存天数</button></div>
    <p id="msg" class="small"></p>
  </div>
  <div class="card small">MCP endpoint：<b>/mcp</b><br>REST：<b>/api/period/state</b><br>如设置 PERIOD_TOKEN，网页和 MCP 都会受保护。</div>
  <div class="footer">period-tracker · single-file MCP edition</div>
</div>
<script>
const phaseNames={menstrual:'经期',follicular:'卵泡期（估算）',ovulation:'排卵窗口（估算）',luteal:'黄体期（估算）',late:'周期已超过平均值'};
function esc(x){return x==null?'—':String(x)}
async function api(url,opt){const r=await fetch(url,opt);const j=await r.json();if(!r.ok)throw new Error(j.error||('HTTP '+r.status));return j}
function render(s){const d=s.derived;document.getElementById('date').value=s.today;document.getElementById('length').value=d.period_length||5;document.getElementById('summary').innerHTML=`
<div class="stat"><div class="k">上次开始</div><div class="v">${esc(d.last_start)}</div></div>
<div class="stat"><div class="k">当前周期第几天</div><div class="v">${d.day_of_cycle?('第 '+d.day_of_cycle+' 天'):'—'}</div></div>
<div class="stat"><div class="k">预计下次</div><div class="v">${esc(d.next_due)}</div></div>
<div class="stat"><div class="k">平均周期</div><div class="v">${d.avg_cycle?d.avg_cycle+' 天':'—'}</div></div>
<div class="stat wide"><div class="k">当前阶段</div><div class="v"><span class="phase">${phaseNames[d.phase]||'暂无足够记录'}</span></div></div>
<div class="stat wide"><div class="k">已记录</div><div class="v">${d.recorded} 次开始日期</div></div>`}
async function refresh(){try{render(await api('/api/period/state'))}catch(e){msg(e.message,true)}}
function msg(t,bad=false){const el=document.getElementById('msg');el.textContent=t;el.className='small '+(bad?'error':'ok')}
async function writeLog(action,date){try{const s=await api('/api/period/log',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({action,date})});render(s);msg(action==='undo'?'已撤销。':'记好啦。')}catch(e){msg(e.message,true)}}
function logToday(){writeLog('start',null)}
function logDate(){writeLog('start',document.getElementById('date').value)}
function undoDate(){writeLog('undo',document.getElementById('date').value)}
async function setLength(){try{const days=Number(document.getElementById('length').value);const s=await api('/api/period/length',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({days})});render(s);msg('经期天数已保存。')}catch(e){msg(e.message,true)}}
refresh();
</script></body></html>"""


@app.get("/")
async def index(req: Request):
    if PERIOD_TOKEN and not _authorized(req):
        return HTMLResponse(LOGIN_PAGE)
    return HTMLResponse(APP_PAGE)


@app.get("/health")
async def health():
    return JSONResponse({"status": "ok"})


# Mount the MCP ASGI app last. Using a root mount preserves its own /mcp path
# while leaving the explicit REST/UI routes above intact.
app.mount("/", period_mcp.streamable_http_app(transport_security=transport_security))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("PERIOD_HOST", "127.0.0.1"),
        port=int(os.environ.get("PERIOD_PORT", "8080")),
    )
