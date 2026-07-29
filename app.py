"""mGrant AI Gateway — the single central service that holds the LLM key,
enforces per-tenant budgets, and logs usage/audit centrally (metadata only).

Run: uvicorn app:app --host 0.0.0.0 --port 8080
"""
import os, re, json, time, uuid, sqlite3, datetime
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
import anthropic
import prompt as P

DB = os.environ.get("MGRANT_AI_DB", "usage.db")
MODEL = os.environ.get("MGRANT_AI_MODEL", "claude-sonnet-4-5")
TENANTS = json.loads(os.environ.get("MGRANT_AI_TENANTS", "{}"))   # {token: {tenant_id, monthly_budget}}
RATES = json.loads(os.environ.get("MGRANT_AI_RATES", "{}"))       # {model: {in, out}}  (per 1M tokens)

client = anthropic.Anthropic()   # reads ANTHROPIC_API_KEY from env
app = FastAPI(title="mGrant AI Gateway")

# --- PII redaction: never let raw PII reach the log (we log metadata only anyway) ---
_PAN = re.compile(r"[A-Z]{5}[0-9]{4}[A-Z]")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"\b(?:\+?91[- ]?)?[6-9]\d{9}\b")
def redact(s):
    if not s: return s
    s = _PAN.sub("<PAN>", s); s = _EMAIL.sub("<EMAIL>", s); s = _PHONE.sub("<PHONE>", s)
    return s

def _db():
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS usage_log(
        request_id TEXT, ts TEXT, tenant TEXT, use_case TEXT,
        ref_doctype TEXT, ref_name TEXT, file_hash TEXT,
        model TEXT, prompt_version TEXT,
        input_tokens INTEGER, output_tokens INTEGER, cost REAL, currency TEXT,
        latency_ms INTEGER, status TEXT, error_code TEXT,
        verdict TEXT, flags_count INTEGER, overridden INTEGER)""")
    return c

def _month_spend(c, tenant):
    m = datetime.date.today().strftime("%Y-%m")
    row = c.execute("SELECT COALESCE(SUM(cost),0) FROM usage_log WHERE tenant=? AND substr(ts,1,7)=?",
                    (tenant, m)).fetchone()
    return row[0] or 0.0

def _log(c, rid, tenant, req, itok, otok, cost, latency, status, err, verdict, flags):
    vals = (
        rid, datetime.datetime.utcnow().isoformat(timespec="seconds"), tenant, req.use_case,
        req.ref.get("doctype"), req.ref.get("name"), (req.document or {}).get("file_hash"),
        MODEL, req.prompt_version, itok, otok, cost, "INR", latency, status, err,
        str(verdict) if verdict is not None else None,
        int(flags) if isinstance(flags, (int, float)) else 0, 0)
    vals = tuple(json.dumps(v) if isinstance(v, (dict, list)) else v for v in vals)
    c.execute("INSERT INTO usage_log VALUES (" + ",".join(["?"] * 19) + ")", vals)
    c.commit()

class CheckReq(BaseModel):
    use_case: str
    prompt_version: str = "atg-v1"
    document: dict = {}     # {text?, pages_base64?: [..], file_hash?}
    context: dict = {}
    ref: dict = {}          # {doctype, name}

def _content_blocks(req: CheckReq):
    blocks = [{"type": "text", "text": P.user_context(req.context)}]
    if req.document.get("text"):
        blocks.append({"type": "text", "text": "RECEIPT TEXT:\n" + req.document["text"]})
    for b64 in (req.document.get("pages_base64") or []):
        blocks.append({"type": "image",
                       "source": {"type": "base64", "media_type": "application/pdf" if req.document.get("is_pdf") else "image/jpeg", "data": b64}})
    return blocks

def _extract_json(text):
    text = text.strip()
    i, j = text.find("{"), text.rfind("}")
    return json.loads(text[i:j+1]) if i != -1 and j != -1 else {}

@app.post("/v1/check")
def check(req: CheckReq, authorization: str = Header(None)):
    token = (authorization or "").replace("Bearer ", "").strip()
    t = TENANTS.get(token)
    if not t:
        raise HTTPException(401, "unknown tenant token")
    c, rid, start = _db(), str(uuid.uuid4()), time.time()
    if t.get("monthly_budget") and _month_spend(c, t["tenant_id"]) >= t["monthly_budget"]:
        _log(c, rid, t["tenant_id"], req, 0, 0, 0, 0, "error", "budget_exceeded", None, None)
        raise HTTPException(402, "monthly budget exceeded for tenant")
    try:
        msg = client.messages.create(model=MODEL, max_tokens=1500,
                                     system=P.SYSTEM, messages=[{"role": "user", "content": _content_blocks(req)}])
    except Exception:
        _log(c, rid, t["tenant_id"], req, 0, 0, 0, int((time.time()-start)*1000), "error", "llm_unavailable", None, None)
        raise HTTPException(503, "LLM unavailable")
    out = "".join(b.text for b in msg.content if b.type == "text")
    result = _extract_json(out)
    u = msg.usage
    rate = RATES.get(MODEL, {"in": 0, "out": 0})
    cost = round(u.input_tokens/1e6*rate["in"] + u.output_tokens/1e6*rate["out"], 4)
    _log(c, rid, t["tenant_id"], req, u.input_tokens, u.output_tokens, cost,
         int((time.time()-start)*1000), "success", None, result.get("verdict"), result.get("flags_count"))
    return {"result": result,
            "usage": {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens, "model": MODEL, "cost": cost},
            "request_id": rid}

@app.post("/v1/override")
def mark_override(request_id: str, authorization: str = Header(None)):
    """Called when an NGO submits despite flags — flips the audit flag, no content."""
    if (authorization or "").replace("Bearer ", "").strip() not in TENANTS:
        raise HTTPException(401, "unknown tenant token")
    c = _db(); c.execute("UPDATE usage_log SET overridden=1 WHERE request_id=?", (request_id,)); c.commit()
    return {"ok": True}

@app.get("/admin/usage")
def usage(tenant: str, month: str = None):
    """Central admin panel data — aggregates, metadata only."""
    c = _db(); m = month or datetime.date.today().strftime("%Y-%m")
    row = c.execute("""SELECT COUNT(*), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0),
                       COALESCE(SUM(cost),0), SUM(overridden) FROM usage_log
                       WHERE tenant=? AND substr(ts,1,7)=?""", (tenant, m)).fetchone()
    verdicts = dict(c.execute("""SELECT verdict, COUNT(*) FROM usage_log
                       WHERE tenant=? AND substr(ts,1,7)=? GROUP BY verdict""", (tenant, m)).fetchall())
    return {"tenant": tenant, "month": m, "calls": row[0], "input_tokens": row[1],
            "output_tokens": row[2], "cost": round(row[3], 2), "overrides": row[4] or 0,
            "by_verdict": verdicts}
