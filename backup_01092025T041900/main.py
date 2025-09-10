from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
import requests
import csv
from io import StringIO
from datetime import datetime

# ✅ import helpers directly (no relative import)
from helpers import (
    to_utc_iso,
    get_kv,
    extract_detail_from_response,
    fetch_manual_review_tx_ids,
)

app = FastAPI()

# Serve static CSS files
app.mount("/static", StaticFiles(directory="static"), name="static")

# ---------- Global state ----------
bearer_token = None
vantage_host = None
skills_cache = []
transactions_cache = []
review_summary = {"with_manual_review": 0, "straight_through": 0}
last_error = ""


def html_page(body: str) -> HTMLResponse:
    """Wrap HTML with CSS include."""
    css = '<link rel="stylesheet" href="/static/style.css">'
    return HTMLResponse(f"<html><head>{css}</head><body>{body}</body></html>")


@app.get("/", response_class=HTMLResponse)
def dashboard():
    global bearer_token, skills_cache, transactions_cache, review_summary, last_error

    if not bearer_token:
        return html_page(f"""
        <h1>ABBYY Vantage Dashboard</h1>
        {'<div class="err">'+last_error+'</div>' if last_error else ''}
        <div class="card">
            <h2>Authenticate</h2>
            <form method="post" action="/authenticate">
                <label>Vantage Host:</label>
                <input type="text" name="url" required><br>
                <label>Client ID:</label>
                <input type="text" name="client_id" required><br>
                <label>Client Secret:</label>
                <input type="password" name="client_secret" required><br>
                <button type="submit">Authenticate</button>
            </form>
        </div>
        """)

    # If authenticated
    skills_options = "".join(f"<option value='{s.get('id')}'>{s.get('name')}</option>" for s in skills_cache)

    results_html = ""
    if transactions_cache:
        total_pages = sum(int(tx.get("pageCount") or 0) for tx in transactions_cache)
        results_html += f"""
        <div class="card">
            <p>Total: {len(transactions_cache)} | Pages: {total_pages} |
            With Manual Review: {review_summary.get("with_manual_review",0)} |
            Straight Through: {review_summary.get("straight_through",0)}</p>
            <table>
                <tr><th>ID</th><th>Status</th><th>Pages</th><th>Created</th><th>File</th><th>Manual Review</th></tr>
        """
        for tx in transactions_cache[:50]:
            results_html += f"<tr><td>{tx.get('id')}</td><td>{tx.get('status')}</td><td>{tx.get('pageCount')}</td><td>{tx.get('created')}</td><td>{tx.get('sourceFileName')}</td><td>{tx.get('manualReview')}</td></tr>"
        results_html += "</table></div>"

    return html_page(f"""
    <h1>ABBYY Vantage Dashboard</h1>
    {'<div class="err">'+last_error+'</div>' if last_error else ''}
    <div class="card">
        <h2>Fetch Transactions</h2>
        <form method="post" action="/transactions">
            <label>Skill:</label>
            <select name="skill_id">{skills_options}</select><br>
            <label>Status:</label>
            <select name="transaction_type">
              <option value="Processed">Processed</option>
              <option value="Succeeded">Succeeded</option>
              <option value="Failed">Failed</option>
              <option value="All">All</option>
            </select><br>
            <label>Start Date:</label>
            <input type="date" name="start_date" value="{datetime.now().date()}"><br>
            <label>End Date:</label>
            <input type="date" name="end_date" value="{datetime.now().date()}"><br>
            <button type="submit">Get Transactions</button>
        </form>
    </div>
    {results_html}
    """)


@app.post("/authenticate")
def authenticate(client_id: str = Form(...), client_secret: str = Form(...), url: str = Form(...)):
    global bearer_token, vantage_host, last_error
    vantage_host = url.strip()
    token_url = f"https://{vantage_host}/auth2/connect/token"
    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "client_credentials",
        "scope": "openid permissions global.wildcard",
    }
    headers = {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}
    r = requests.post(token_url, data=payload, headers=headers)
    if r.status_code != 200:
        last_error = extract_detail_from_response(r)
        bearer_token = None
    else:
        bearer_token = r.json().get("access_token")
        last_error = ""
    return RedirectResponse("/", status_code=303)


@app.get("/skills")
def load_skills():
    global skills_cache, bearer_token, vantage_host
    headers = {"Authorization": f"Bearer {bearer_token}"}
    r = requests.get(f"https://{vantage_host}/api/publicapi/v1/skills", headers=headers)
    if r.status_code == 200:
        skills_cache = r.json() or []
    return RedirectResponse("/", status_code=303)


@app.post("/transactions")
def get_transactions(skill_id: str = Form(...), transaction_type: str = Form(...), start_date: str = Form(...), end_date: str = Form(...)):
    global transactions_cache, review_summary, bearer_token, vantage_host, last_error
    headers = {"Authorization": f"Bearer {bearer_token}"}
    start_iso = to_utc_iso(start_date, is_end=False)
    end_iso = to_utc_iso(end_date, is_end=True)

    url = f"https://{vantage_host}/api/publicapi/v1/transactions/completed"
    params = {"skillId": skill_id, "StartDate": start_iso, "EndDate": end_iso, "Limit": 1000}
    if transaction_type.lower() != "all":
        params["TransactionStatus"] = transaction_type

    r = requests.get(url, headers=headers, params=params)
    if r.status_code != 200:
        last_error = extract_detail_from_response(r)
        transactions_cache = []
        return RedirectResponse("/", status_code=303)

    data = r.json()
    items = data.get("items", [])
    reviewed_ids = fetch_manual_review_tx_ids(vantage_host, skill_id, start_iso, end_iso, headers)

    with_mr = 0
    straight = 0
    results = []
    for it in items:
        mr = "Yes" if it.get("transactionId") in reviewed_ids else "No"
        if mr == "Yes":
            with_mr += 1
        else:
            straight += 1
        results.append({
            "id": it.get("transactionId"),
            "created": it.get("createTimeUtc"),
            "status": it.get("status"),
            "pageCount": it.get("pageCount", 0),
            "sourceFileName": get_kv(it.get("fileParameters", []), "SourceFileName", ""),
            "manualReview": mr,
        })
    transactions_cache = results
    review_summary = {"with_manual_review": with_mr, "straight_through": straight}
    last_error = ""
    return RedirectResponse("/", status_code=303)


@app.get("/export.csv")
def export_csv():
    """Export transactions to CSV."""
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
