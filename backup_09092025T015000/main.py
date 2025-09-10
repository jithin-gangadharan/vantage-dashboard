# main.py
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
import requests
from datetime import datetime, time, timezone
from io import StringIO
import csv

app = FastAPI(title="ABBYY Vantage Page Count – FastAPI (Forms Style)")

# CORS (relax as needed)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------- Global (demo) state --------
bearer_token = None
vantage_host = None  # host only, e.g. "vantage-au.abbyy.com"
skills_cache = []
transactions_cache = []
review_summary = {"with_manual_review": 0, "straight_through": 0}
last_error = ""  # store last error to show on UI


# -------- Helpers --------
def html_base(body: str, title: str = "ABBYY Vantage Dashboard") -> HTMLResponse:
    # ABBYY-ish red/white theme (#E3000F), black text, tighter spacing
    css = """
    <style>
      :root {
        --abbyy-red:#E3000F;
        --abbyy-red-dark:#BF000C;
        --border:#E6E6E6;
        --muted:#6B6B6B;
        --bg:#FFFFFF;
        --panel:#FFFFFF;
        --badge-bg:#F5F5F5;
        --badge-text:#111111;
        --err-bg:#FFF1F2;
        --err-text:#7F1D1D;
        --ok-bg:#F0FDF4;
        --ok-text:#14532D;
      }
      *{box-sizing:border-box}
      body{
        font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,Arial,sans-serif;
        margin:24px;
        background:var(--bg);
        color:#000;
      }
      .container{max-width:1200px;margin:0 auto}
      h1{margin:0 0 4px;font-weight:700}
      h2{margin:0 0 8px;font-weight:700; color:#000}
      .muted{color:var(--muted);font-size:.92rem;margin:0}
      .card{
        background:var(--panel);
        border:1px solid var(--border);
        border-radius:14px;
        padding:18px;
        margin:14px 0;
      }
      .topbar{display:flex;gap:12px;align-items:center;justify-content:space-between;margin-bottom:10px}
      .actions{display:flex;gap:10px;align-items:center}
      a.btn, button.btn{
        display:inline-block;text-decoration:none;
        background:var(--abbyy-red);color:#fff;
        padding:.55rem .9rem;border-radius:10px;border:none;cursor:pointer;
        font-weight:600;
      }
      a.btn:hover, button.btn:hover{background:var(--abbyy-red-dark)}
      .btn-secondary{
        background:#F3F4F6;color:#111;border:1px solid var(--border);
      }
      .btn-secondary:hover{background:#E5E7EB}
      form{margin:8px 0 8px}
      label{display:block;margin:.25rem 0 .2rem;font-weight:600}
      input, select{
        padding:.6rem .7rem;margin:.2rem 0 .6rem;
        width:100%;max-width:420px;border:1px solid var(--border);border-radius:10px;background:#fff;color:#000;
      }
      .grid{
        display:grid;gap:16px;
      }
      @media(min-width:900px){
        .grid-2{grid-template-columns:1fr 1fr}
        .grid-3{grid-template-columns:1fr 1fr 1fr}
      }
      .banner{margin:8px 0}
      .err{background:var(--err-bg);color:var(--err-text);padding:.6rem .8rem;border-radius:10px;border:1px solid #FFDADA}
      .ok{background:var(--ok-bg);color:var(--ok-text);padding:.6rem .8rem;border-radius:10px;border:1px solid #DCFCE7}
      .result{background:#FAFAFA;padding:12px;border-radius:12px;border:1px solid var(--border)}
      .metrics{display:grid;gap:10px}
      @media(min-width:700px){ .metrics{grid-template-columns:repeat(4, minmax(0,1fr));} }
      .badge{display:inline-block;background:var(--badge-bg);color:var(--badge-text);padding:.25rem .6rem;border-radius:999px;font-weight:700;border:1px solid var(--border)}
      table{border-collapse:collapse;width:100%;margin-top:12px;background:#fff;border:1px solid var(--border)}
      th,td{border-bottom:1px solid var(--border);padding:10px;text-align:left;vertical-align:top}
      th{background:#FFF;border-bottom:2px solid var(--border);font-weight:700}
      tr:hover td{background:#FAFAFA}
      .section-title{display:flex;align-items:center;gap:8px;margin-bottom:8px}
      .section-title .icon{width:18px;height:18px;display:inline-block;background:var(--abbyy-red);border-radius:4px}
      .spacer{height:4px}
    </style>
    """
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{title}</title>{css}</head>
<body>
<div class="container">
{body}
</div>
</body></html>
"""
    return HTMLResponse(html)


def set_error(msg: str):
    """Set a user-visible error banner message."""
    global last_error
    last_error = msg or ""


def banner() -> str:
    """Render banner: error (detail only) and connection info."""
    global last_error, bearer_token, vantage_host
    msgs = []
    if last_error:
        msgs.append(f'<div class="banner"><div class="err">{last_error}</div></div>')
    if bearer_token and vantage_host:
        msgs.append(f'<div class="banner"><div class="ok">Connected to <b>{vantage_host}</b></div></div>')
    return "\n".join(msgs)


def to_utc_iso(date_str: str, is_end=False) -> str:
    """
    Accepts 'YYYY-MM-DD' from HTML date input; returns ISO8601 UTC string.
    Start→00:00:00, End→23:59:59 (to include whole day).
    """
    try:
        # Let Python parse YYYY-MM-DD
        d = datetime.fromisoformat(date_str)
    except ValueError:
        d = datetime.strptime(date_str, "%Y-%m-%d")
    t = time(23, 59, 59) if is_end else time(0, 0, 0)
    dt = datetime.combine(d.date(), t)
    dt = dt.astimezone()  # localize to system tz
    return dt.astimezone(timezone.utc).isoformat()


def get_kv(items: list, key: str, default: str = "") -> str:
    """Return 'value' for the first dict in items where dict['key'] == key."""
    if not isinstance(items, list):
        return default
    for it in items:
        if isinstance(it, dict) and it.get("key") == key:
            return it.get("value", default)
    return default


def extract_detail_from_response(resp: requests.Response) -> str:
    """
    Attempt to return only the 'detail' field from ABBYY error responses.
    If not present/parseable, fall back to a short generic message.
    """
    try:
        data = resp.json()
        detail = data.get("detail")
        if isinstance(detail, str) and detail.strip():
            return detail
        # Sometimes description may be under another key
        if data.get("error_description"):
            return data["error_description"]
        if data.get("title"):
            return data["title"]
    except Exception:
        pass
    # Fallback (trim long bodies)
    txt = (resp.text or "").strip()
    if len(txt) > 300:
        txt = txt[:300] + "…"
    if txt:
        return txt
    return f"HTTP {resp.status_code}"


def fetch_manual_review_tx_ids(skill_id: str, start_iso: str, end_iso: str, headers: dict) -> set[str]:
    """
    Calls /api/reporting/v1/transaction-steps and returns a set of TransactionId
    that had manual review (any step with ManualReviewOperatorName/Email present).
    Expects CSV body.
    """
    global vantage_host
    url = f"https://{vantage_host}/api/reporting/v1/transaction-steps"
    params = {"skillId": skill_id, "startDate": start_iso, "endDate": end_iso}
    r = requests.get(url, headers=headers, params=params, timeout=120)
    if r.status_code != 200:
        # Surface *detail* (if any) but don't block main flow
        set_error(extract_detail_from_response(r))
        return set()

    text = r.text or ""
    reviewed = set()
    try:
        reader = csv.DictReader(StringIO(text))
        for row in reader:
            if not row:
                continue
            if row.get("ManualReviewOperatorName") or row.get("ManualReviewOperatorEmail"):
                txid = (row.get("TransactionId") or "").strip()
                if txid:
                    reviewed.add(txid)
    except Exception:
        # Soft-fail on CSV parse issues
        pass
    return reviewed


# -------- UI --------
@app.get("/", response_class=HTMLResponse)
def dashboard():
    global bearer_token, skills_cache, transactions_cache, review_summary

    # AUTH VIEW
    if not bearer_token:
        body = f"""
        <div class="topbar">
          <div>
            <h1>ABBYY Vantage Dashboard</h1>
            <p class="muted">Authorize → Load Skills → Fetch Transactions → Totals</p>
          </div>
        </div>
        {banner()}
        <div class="card">
          <div class="section-title"><span class="icon"></span><h2>Authenticate</h2></div>
          <form method="post" action="/authenticate">
            <label>Vantage Host (e.g., <code>vantage-au.abbyy.com</code>)</label>
            <input type="text" name="url" placeholder="your-tenant.abbyy.com" required>

            <div class="grid grid-2">
              <div>
                <label>Client ID</label>
                <input type="text" name="client_id" required>
              </div>
              <div>
                <label>Client Secret</label>
                <input type="password" name="client_secret" required>
              </div>
            </div>
            <button class="btn" type="submit">Get Token</button>
          </form>
        </div>
        """
        return html_base(body)

    # MAIN VIEW
    skills_options = "".join(
        f"<option value='{s.get('id','')}'>{(s.get('name') or s.get('id') or '')}</option>"
        for s in skills_cache
    )

    results_html = ""
    if transactions_cache:
        total_pages = sum(int(tx.get("pageCount") or 0) for tx in transactions_cache)
        with_mr = review_summary.get("with_manual_review", 0)
        straight = review_summary.get("straight_through", 0)

        results_html += f"""
        <div class='result'>
          <div class="metrics">
            <div>Total Transactions <span class="badge">{len(transactions_cache)}</span></div>
            <div>Total Pages Consumed <span class="badge">{total_pages}</span></div>
            <div>With Manual Review <span class="badge">{with_mr}</span></div>
            <div>Straight Through <span class="badge">{straight}</span></div>
          </div>
          <div class="spacer"></div>
          <div class="actions">
            <a class="btn btn-secondary" href="/skills">Reload Skills</a>
            <a class="btn btn-secondary" href="/export.csv" target="_blank">Export first 500 as CSV</a>
            <a class="btn btn-secondary" href="/logout">Logout</a>
          </div>
        </div>
        <table>
          <tr>
            <th>Transaction ID</th>
            <th>Status</th>
            <th>Pages</th>
            <th>Created (UTC)</th>
            <th>Source File</th>
            <th>Manual Review</th>
          </tr>
        """
        for tx in transactions_cache[:500]:
            results_html += f"""
            <tr>
              <td>{tx.get('id','')}</td>
              <td>{tx.get('status','')}</td>
              <td>{tx.get('pageCount','')}</td>
              <td>{tx.get('created','')}</td>
              <td>{tx.get('sourceFileName','')}</td>
              <td>{tx.get('manualReview','No')}</td>
            </tr>
            """
        results_html += "</table>"

    body = f"""
    <div class="topbar">
      <div>
        <h1>ABBYY Vantage Dashboard</h1>
        <p class="muted">Authorize → Load Skills → Fetch Transactions → Totals</p>
      </div>
      <div class="actions">
        <a class="btn btn-secondary" href="/skills">Reload Skills</a>
        <a class="btn btn-secondary" href="/logout">Logout</a>
      </div>
    </div>
    {banner()}

    <div class="card">
      <div class="section-title"><span class="icon"></span><h2>Fetch Transactions</h2></div>
      <form method="post" action="/transactions">
        <label>Skill</label>
        <select name="skill_id" required>{skills_options}</select>

        <div class="grid grid-2">
          <div>
            <label>Transaction Status</label>
            <select name="transaction_type">
              <option value="Processed">Processed</option>
              <option value="Succeeded">Succeeded</option>
              <option value="Failed">Failed</option>
              <option value="All">All</option>
            </select>
          </div>
          <div>
            <label>Limit per page (1–1000, default 1000)</label>
            <input type="number" name="limit" min="1" max="1000" value="1000">
          </div>
        </div>

        <div class="grid grid-2">
          <div>
            <label>Start Date (YYYY-MM-DD)</label>
            <input type="date" name="start_date" value="{datetime.now().date().isoformat()}">
          </div>
          <div>
            <label>End Date (YYYY-MM-DD)</label>
            <input type="date" name="end_date" value="{datetime.now().date().isoformat()}">
          </div>
        </div>

        <button class="btn" type="submit">Get Transactions</button>
      </form>
    </div>

    <div class="card">
      {results_html or '<span class="muted">No results yet. Fetch to see transactions.</span>'}
    </div>
    """
    return html_base(body)


# -------- Actions --------
@app.get("/logout")
def logout():
    global bearer_token, vantage_host, skills_cache, transactions_cache, review_summary, last_error
    bearer_token = None
    vantage_host = None
    skills_cache = []
    transactions_cache = []
    review_summary = {"with_manual_review": 0, "straight_through": 0}
    last_error = ""
    return RedirectResponse("/", status_code=303)


@app.post("/authenticate")
def authenticate(client_id: str = Form(...), client_secret: str = Form(...), url: str = Form(...)):
    """
    Get OAuth token and store in globals.
    """
    global bearer_token, vantage_host
    set_error("")
    vantage_host = url.strip()
    token_url = f"https://{vantage_host}/auth2/connect/token"
    payload = {
        "client_id": client_id.strip(),
        "client_secret": client_secret.strip(),
        "grant_type": "client_credentials",
        "scope": "openid permissions global.wildcard",
    }
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    try:
        r = requests.post(token_url, data=payload, headers=headers, timeout=60)
        if r.status_code != 200:
            # show only 'detail'
            set_error(extract_detail_from_response(r))
            return RedirectResponse("/", status_code=303)
        bearer_token = r.json().get("access_token")
        if not bearer_token:
            set_error("Authorization failed: access_token is missing.")
            return RedirectResponse("/", status_code=303)
    except Exception as ex:
        set_error(str(ex))
        return RedirectResponse("/", status_code=303)

    # auto-load skills
    return RedirectResponse("/skills", status_code=303)


@app.get("/skills")
def load_skills():
    """
    Populate the skills cache using the saved bearer token.
    """
    global bearer_token, vantage_host, skills_cache
    set_error("")
    if not bearer_token or not vantage_host:
        set_error("Not authenticated.")
        return RedirectResponse("/", status_code=303)

    url = f"https://{vantage_host}/api/publicapi/v1/skills"
    headers = {"Authorization": f"Bearer {bearer_token}"}

    try:
        r = requests.get(url, headers=headers, timeout=60)
        if r.status_code == 200:
            skills_cache = r.json() or []
        else:
            set_error(extract_detail_from_response(r))
            skills_cache = []
    except Exception as ex:
        set_error(str(ex))
        skills_cache = []

    return RedirectResponse("/", status_code=303)


@app.post("/transactions")
def get_transactions(
    skill_id: str = Form(...),
    transaction_type: str = Form(...),
    start_date: str = Form(...),
    end_date: str = Form(...),
    limit: int = Form(1000),
):
    """
    Paginate through /transactions/completed, normalize items for the UI,
    then call reporting /transaction-steps to mark manual review (Yes/No).
    """
    global bearer_token, vantage_host, transactions_cache, review_summary
    set_error("")
    if not bearer_token or not vantage_host:
        set_error("Not authenticated.")
        return RedirectResponse("/", status_code=303)

    # Build date window (UTC ISO strings)
    try:
        start_iso = to_utc_iso(start_date, is_end=False)
        end_iso = to_utc_iso(end_date, is_end=True)
    except Exception as ex:
        set_error(f"Invalid dates: {ex}")
        return RedirectResponse("/", status_code=303)

    base = f"https://{vantage_host}/api/publicapi/v1/transactions/completed"
    headers = {"Authorization": f"Bearer {bearer_token}"}

    limit = max(1, min(int(limit or 1000), 1000))
    offset = 0
    fetched = 0
    total_item_count = None
    items_all = []

    try:
        while True:
            params = {
                "skillId": skill_id,
                "StartDate": start_iso,
                "EndDate": end_iso,
                "Offset": offset,
                "Limit": limit,
            }
            # Only include TransactionStatus param if not 'All'
            if transaction_type and transaction_type.lower() != "all":
                params["TransactionStatus"] = transaction_type

            r = requests.get(base, headers=headers, params=params, timeout=120)
            if r.status_code != 200:
                set_error(extract_detail_from_response(r))  # only detail
                items_all = []
                break

            payload = r.json() or {}
            raw_items = payload.get("items", []) or []
            if total_item_count is None:
                total_item_count = int(payload.get("totalItemCount", 0))

            # Normalize fields to UI-friendly keys
            for it in raw_items:
                items_all.append({
                    "id": it.get("transactionId", ""),
                    "created": it.get("createTimeUtc", ""),
                    "status": it.get("status", ""),
                    "pageCount": it.get("pageCount", 0),
                    "skillId": it.get("skillId", ""),
                    "skillVersion": it.get("skillVersion", ""),
                    "documentCount": it.get("documentCount", 0),
                    "sourceFileName": get_kv(it.get("fileParameters", []), "SourceFileName", ""),
                    "sourceType": get_kv(it.get("fileParameters", []), "SourceType", ""),
                    "app": get_kv(it.get("transactionParameters", []), "App", ""),
                })

            fetched += len(raw_items)
            offset += limit
            if fetched >= (total_item_count or 0):
                break

        # === 2nd call: reporting steps → manual review map ===
        reviewed_tx_ids = fetch_manual_review_tx_ids(skill_id, start_iso, end_iso, headers)

        # Annotate and compute summary
        with_mr = 0
        straight = 0
        for row in items_all:
            mr = "Yes" if row.get("id") in reviewed_tx_ids else "No"
            row["manualReview"] = mr
            if mr == "Yes":
                with_mr += 1
            else:
                straight += 1

        review_summary = {
            "with_manual_review": with_mr,
            "straight_through": straight,
        }

    except Exception as ex:
        set_error(str(ex))
        items_all = []

    transactions_cache = items_all
    return RedirectResponse("/", status_code=303)


@app.get("/export.csv")
def export_csv():
    """
    Export (up to) first 500 transactions as CSV.
    """
    global transactions_cache
    out = StringIO()
    writer = csv.writer(out)
    writer.writerow(["id", "status", "pageCount", "created", "sourceFileName", "manualReview"])

    for tx in transactions_cache[:500]:
        writer.writerow([
            tx.get("id", ""),
            tx.get("status", ""),
            tx.get("pageCount", ""),
            tx.get("created", ""),
            tx.get("sourceFileName", ""),
            tx.get("manualReview", "No"),
        ])

    return PlainTextResponse(
        content=out.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=vantage_transactions.csv"},
    )
