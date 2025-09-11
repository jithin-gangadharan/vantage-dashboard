from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
import requests, json
from datetime import datetime, timedelta
from helpers import (
    to_utc_iso,
    extract_detail_from_response,  # kept in case you use it later
    fetch_tx_review_and_docskill,
)

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

# Globals (okay for single-user PoC; switch to per-session storage for multi-user)
bearer_token = None
vantage_host = None
skills_cache = []
transactions_cache = []
review_summary = {"with_manual_review": 0, "straight_through": 0}
docskill_summary = {}
last_error = ""
search_attempted = False  # show "no results" banner only after a search


# ---------- Debug helpers ----------
def _mask_headers(h: dict | None) -> dict | None:
    """Mask sensitive header values (e.g., Authorization)."""
    if not h:
        return h
    safe = dict(h)
    auth = safe.get("Authorization")
    if isinstance(auth, str) and auth.startswith("Bearer "):
        token = auth.split(" ", 1)[1]
        safe["Authorization"] = "Bearer " + (token[:8] + "…") if token else "Bearer …"
    return safe

def _mask_payload(p: dict | None) -> dict | None:
    """Mask sensitive payload values (e.g., client_secret)."""
    if not p:
        return p
    safe = dict(p)
    if "client_secret" in safe:
        safe["client_secret"] = "••••••••"
    return safe


# ---------- HTML Dashboard ----------
@app.get("/", response_class=HTMLResponse)
def dashboard():
    global bearer_token, skills_cache, transactions_cache, review_summary, docskill_summary, last_error, search_attempted

    css_link = '<link rel="stylesheet" href="/static/style.css">'
    chartjs = '<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>'
    loader_css = """
    <style>
      .loading-overlay {
        position: fixed; inset: 0; display: none; align-items: center; justify-content: center;
        background: rgba(255,255,255,0.8); backdrop-filter: blur(2px); z-index: 9999;
      }
      .loading-box { padding: 16px 20px; border-radius: 12px; box-shadow: 0 10px 30px rgba(0,0,0,0.15);
        background: #fff; text-align: center; font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial; }
      .spinner { width: 28px; height: 28px; border: 3px solid #ddd; border-top-color: #333; border-radius: 50%;
        margin: 0 auto 10px; animation: spin 0.9s linear infinite; }
      @keyframes spin { to { transform: rotate(360deg); } }
    </style>
    """

    # If not authenticated → show login form
    if not bearer_token:
        error_html = f"<div class='error'>{last_error}</div>" if last_error else ""
        return f"""
        <html><head><title>ABBYY Vantage Dashboard</title>{css_link}{loader_css}</head>
        <body>
            <div id="loadingOverlay" class="loading-overlay" aria-live="polite" aria-busy="true">
              <div class="loading-box">
                <div class="spinner"></div>
                <div>Fetching transactions…</div>
              </div>
            </div>
            <div class="container">
                <h1>ABBYY Vantage Dashboard</h1>
                {error_html}
                <div class="card">
                    <h2>Login</h2>
                    <form method="post" action="/authenticate">
                        <label>Vantage Host</label>
                        <select name="url" required>
                            <option value="vantage-au.abbyy.com">vantage-au.abbyy.com</option>
                            <option value="vantage-us.abbyy.com">vantage-us.abbyy.com</option>
                            <option value="vantage-eu.abbyy.com">vantage-eu.abbyy.com</option>
                        </select><br>
                        <label>Client ID</label>
                        <input type="text" name="client_id" required><br>
                        <label>Client Secret</label>
                        <input type="password" name="client_secret" required><br><br>
                        <button type="submit">Authenticate</button>
                    </form>
                </div>
            </div>
        </body></html>
        """

    # --- Show logout button if authenticated ---
    logout_btn = """
    <div class="logout-container">
        <form method="post" action="/logout">
            <button type="submit" class="logout-btn">Logout</button>
        </form>
    </div>
    """

    # --- Skills dropdown (filter only Process type) ---
    skill_opts = "".join(
        f"<option value='{s['id']}'>{s['name']}</option>"
        for s in skills_cache if s.get("type") == "Process"
    )

    today = datetime.now().date()
    start_default = (today - timedelta(days=13)).isoformat()
    end_default = today.isoformat()
    min_date = (today - timedelta(days=13)).isoformat()
    max_date = today.isoformat()

    error_html = f"<div class='error'>{last_error}</div>" if last_error else ""
    results_html = ""

    if transactions_cache:
        # Normal results rendering...
        total_pages = sum(int(tx.get("pageCount") or 0) for tx in transactions_cache)
        straight = review_summary["straight_through"]
        manual = review_summary["with_manual_review"]

        # Colors mapping
        review_colors = {"Straight Through": "#27ae60", "Manual Review": "#e74c3c"}
        doc_colors = ["#2e86c1", "#e67e22", "#27ae60", "#8e44ad", "#c0392b"]

        # Results summary
        results_html += f"""
        <div class="grid">
        <div class="half">
            <h3>📊 Results</h3>
            <table class="summary-table">
                <tr><td></td><th>Total Transactions</th><td>{len(transactions_cache)}</td></tr>
                <tr><td></td><th>Total Pages Consumed</th><td>{total_pages}</td></tr>
                <tr><td style="color:{review_colors['Straight Through']}">⬤</td><th>Straight Through</th><td>{straight}</td></tr>
                <tr><td style="color:{review_colors['Manual Review']}">⬤</td><th>Manual Review</th><td>{manual}</td></tr>
            </table>
        </div>
        <div class="half">
            <canvas id="reviewChart"></canvas>
        </div>
        </div>
        <script>
        new Chart(document.getElementById('reviewChart'), {{
            type: 'doughnut',
            data: {{
                labels: ['Straight Through', 'Manual Review'],
                datasets: [{{
                    data: [{straight}, {manual}],
                    backgroundColor: ['{review_colors["Straight Through"]}','{review_colors["Manual Review"]}']
                }}]
            }},
            options: {{
                responsive: true,
                maintainAspectRatio: false,
                plugins: {{ legend: {{ display: false }} }}
            }}
        }});
        </script>
        """

        # Document skills summary
        if docskill_summary:
            labels = list(docskill_summary.keys())
            txcounts = [docskill_summary[k]["txcount"] for k in labels]
            pages = [docskill_summary[k]["pages"] for k in labels]

            results_html += f"""
            <div class="grid">
                <div class="half">
                    <h3>📑 Document Skills</h3>
                    <table>
                        <tr><th></th><th>Document Skill</th><th>Documents</th><th>Pages</th></tr>
                        {''.join(f"<tr><td style='color:{doc_colors[i % len(doc_colors)]}'>⬤</td><td>{k}</td><td>{docskill_summary[k]['txcount']}</td><td>{docskill_summary[k]['pages']}</td></tr>" for i,k in enumerate(labels))}
                    </table>
                </div>
                <div class="half">
                    <canvas id="docSkillChart"></canvas>
                </div>
            </div>
            <script>
            new Chart(document.getElementById('docSkillChart'), {{
                type: 'doughnut',
                data: {{
                    labels: {json.dumps(labels)},
                    datasets: [{{
                        data: {json.dumps(txcounts)},
                        backgroundColor: {json.dumps(doc_colors[:len(labels)])}
                    }}]
                }},
                options: {{
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {{ legend: {{ display: false }} }}
                }}
            }});
            </script>
            """

        # Transactions table
        results_html += """
        <h3>📂 Transactions</h3>
        <table id="txTable">
            <thead>
                <tr>
                    <th>Transaction ID</th><th>Source File</th><th>Status</th><th>Pages</th>
                    <th>Created</th><th>Manual Review</th><th>Document Skill</th>
                </tr>
            </thead>
            <tbody>
        """
        for tx in transactions_cache[:50]:
            source_file = ""
            for fp in tx.get("fileParameters", []):
                if fp.get("key") == "SourceFileName":
                    source_file = fp.get("value", "")
            results_html += f"""
            <tr>
                <td>{tx.get('transactionId','')}</td>
                <td>{source_file}</td>
                <td>{tx.get('status','')}</td>
                <td>{tx.get('pageCount','')}</td>
                <td>{tx.get('createTimeUtc','')}</td>
                <td>{tx.get('manualReview','')}</td>
                <td>{tx.get('documentSkillName','')}</td>
            </tr>
            """
        results_html += "</tbody></table>"
        results_html += """
        <div class="table-actions">
            <button onclick="exportCSV()">Export to CSV</button>
            <button onclick="prevPage()">Prev</button>
            <button onclick="nextPage()">Next</button>
        </div>
        """
    elif search_attempted:  # show empty message only after user searched
        results_html = "<div class='error'>⚠️ No transactions found for the selected criteria.</div>"

    return f"""
    <html>
    <head>
        <title>ABBYY Vantage Dashboard</title>
        {css_link}{chartjs}{loader_css}
        <script>
        // CSV Export
        function exportCSV() {{
            let table = document.getElementById("txTable");
            if (!table) return;
            let rows = Array.from(table.querySelectorAll("tr"));
            let csv = rows.map(r => Array.from(r.querySelectorAll("th,td"))
                            .map(c => '"' + c.innerText.replace(/"/g,'""') + '"').join(","))
                            .join("\\n");
            let blob = new Blob([csv], {{ type: "text/csv" }});
            let url = window.URL.createObjectURL(blob);
            let a = document.createElement("a");
            a.href = url;
            a.download = "transactions.csv";
            a.click();
        }}
        // Pagination
        let currentPage = 0;
        const pageSize = 20;
        function showPage() {{
            let rows = document.querySelectorAll("#txTable tbody tr");
            rows.forEach((row,i)=> row.style.display = (i>=currentPage*pageSize && i<(currentPage+1)*pageSize) ? "" : "none");
        }}
        function prevPage() {{ if(currentPage>0){{ currentPage--; showPage(); }} }}
        function nextPage() {{
            let rows = document.querySelectorAll("#txTable tbody tr");
            if((currentPage+1)*pageSize < rows.length) {{ currentPage++; showPage(); }}
        }}
        // Loading overlay
        function showLoading() {{
          const overlay = document.getElementById("loadingOverlay");
          if (overlay) overlay.style.display = "flex";
          const submitBtn = document.querySelector('#fetchForm button[type="submit"]');
          if (submitBtn) {{ submitBtn.disabled = true; submitBtn.textContent = "Fetching…"; }}
        }}
        window.onload = showPage;
        </script>
    </head>
    <body>
        <div id="loadingOverlay" class="loading-overlay" aria-live="polite" aria-busy="true">
          <div class="loading-box">
            <div class="spinner"></div>
            <div>Fetching transactions…</div>
          </div>
        </div>
        {logout_btn}
        <div class="container">
            <h1>ABBYY Vantage Dashboard</h1>
            {error_html}
            <div class="card" id="fetchCard">
                <div class="section-header" onclick="toggleFetch()">
                  <h2>📊 Fetch Transactions</h2>
                  <span id="toggle-arrow" class="toggle-arrow">⬇️</span>
                </div>
                <div id="fetchForm">
                    <form method="post" action="/transactions" onsubmit="showLoading()">
                        <label>Process Skill</label>
                        <select name="skill_id" required>{skill_opts}</select>
                        <label>Transaction Status</label>
                        <select name="transaction_type">
                            <option value="Processed">Processed</option>
                            <option value="Failed">Failed</option>
                        </select>
                        <label>Start Date</label>
                        <input type="date" name="start_date" value="{start_default}" min="{min_date}" max="{max_date}">
                        <label>End Date</label>
                        <input type="date" name="end_date" value="{end_default}" min="{min_date}" max="{max_date}"><br><br>
                        <button type="submit">Get Transactions</button>
                    </form>
                </div>
            </div>
            {results_html}
        </div>
        <script>
        function toggleFetch() {{
            let form = document.getElementById("fetchForm");
            let arrow = document.getElementById("toggle-arrow");
            if (form.style.display === "none") {{
                form.style.display = "block";
                arrow.textContent = "⬇️";
            }} else {{
                form.style.display = "none";
                arrow.textContent = "➡️";
            }}
        }}
        if({len(transactions_cache)}>0) {{
            document.getElementById("fetchForm").style.display="none";
            document.getElementById("toggle-arrow").textContent = "➡️";
        }}
        </script>
    </body>
    </html>
    """


# ---------- API: Authenticate ----------
@app.post("/authenticate")
def authenticate(client_id: str = Form(...), client_secret: str = Form(...), url: str = Form(...)):
    global bearer_token, vantage_host, skills_cache, last_error, search_attempted
    vantage_host = url.strip()
    last_error = ""
    search_attempted = False  # reset on fresh login

    token_url = f"https://{vantage_host}/auth2/connect/token"
    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "client_credentials",
        "scope": "openid permissions global.wildcard",
    }
    headers = {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}

    try:
        print(f"[DEBUG] AUTH Request URL={token_url}, payload={_mask_payload(payload)}, headers={_mask_headers(headers)}")
        r = requests.post(token_url, data=payload, headers=headers)
        print(f"[DEBUG] AUTH Response {r.status_code}: {r.text[:1000]}")
        if r.status_code != 200:
            try:
                err_json = r.json()
                err_msg = err_json.get("error_description") or err_json.get("error") or r.text
            except Exception:
                err_msg = r.text
            last_error = f"Login Failed. Error: {err_msg}"
            return RedirectResponse("/", status_code=303)
    except requests.exceptions.RequestException as e:
        last_error = f"Login Failed. Error: {str(e)}"
        return RedirectResponse("/", status_code=303)

    bearer_token = r.json().get("access_token")

    # Load skills
    skills_url = f"https://{vantage_host}/api/publicapi/v1/skills"
    headers = {"Authorization": f"Bearer {bearer_token}"}
    try:
        print(f"[DEBUG] SKILLS Request URL={skills_url}, headers={_mask_headers(headers)}")
        r = requests.get(skills_url, headers=headers)
        print(f"[DEBUG] SKILLS Response {r.status_code}: {r.text[:1000]}")
        r.raise_for_status()
        skills_cache = r.json()
    except requests.exceptions.RequestException as e:
        skills_cache = []
        last_error = f"Failed to load skills: {e}"

    return RedirectResponse("/", status_code=303)


# ---------- API: Transactions ----------
@app.post("/transactions")
def get_transactions(skill_id: str = Form(...), transaction_type: str = Form(...),
                     start_date: str = Form(...), end_date: str = Form(...)):
    global bearer_token, vantage_host, transactions_cache, review_summary, docskill_summary, last_error, search_attempted
    if not bearer_token or not vantage_host:
        return RedirectResponse("/", status_code=303)

    headers = {"Authorization": f"Bearer {bearer_token}"}
    start_iso = to_utc_iso(start_date)
    end_iso = to_utc_iso(end_date, is_end=True)
    last_error = ""
    search_attempted = True

    # ---- call ABBYY Public API for transactions ----
    url = f"https://{vantage_host}/api/publicapi/v1/transactions/completed"
    limit, offset, total_items_fetched = 1000, 0, 0
    total_item_count = 1
    items_all = []

    while total_items_fetched < total_item_count:
        params = {
            "TransactionStatus": transaction_type,
            "skillId": skill_id,
            "StartDate": start_iso,
            "EndDate": end_iso,
            "Offset": offset,
            "Limit": limit,
        }
        try:
            print(f"[DEBUG] TX Request URL={url}, params={params}, headers={_mask_headers(headers)}")
            r = requests.get(url, headers=headers, params=params)
            print(f"[DEBUG] TX Response {r.status_code}: {r.text[:1000]}")
            r.raise_for_status()
        except requests.exceptions.RequestException as e:
            last_error = f"Transactions fetch failed: {e}"
            transactions_cache = []
            return RedirectResponse("/", status_code=303)

        parsed = r.json()
        items = parsed.get("items", [])
        total_item_count = parsed.get("totalItemCount", 0)
        items_all.extend(items)
        total_items_fetched += len(items)
        offset += limit

    # ---- QA report for manual review + doc skill ----
    print(f"[DEBUG] QA Report fetch_tx_review_and_docskill start skill={skill_id} range={start_iso}..{end_iso}")
    tx_info_map = fetch_tx_review_and_docskill(vantage_host, skill_id, start_iso, end_iso, headers)
    print(f"[DEBUG] QA Report fetched records={len(tx_info_map)}")

    with_mr, straight = 0, 0
    pages_by_docskill, txcount_by_docskill = {}, {}

    for row in items_all:
        txid = row.get("transactionId", "")
        info = tx_info_map.get(txid, {})
        mr = "Yes" if info.get("manual_review") else "No"
        dskill = info.get("document_skill_name", "")

        row["manualReview"] = mr
        row["documentSkillName"] = dskill

        if mr == "Yes":
            with_mr += 1
        else:
            straight += 1

        if dskill:
            pages_by_docskill[dskill] = pages_by_docskill.get(dskill, 0) + int(row.get("pageCount") or 0)
            txcount_by_docskill[dskill] = txcount_by_docskill.get(dskill, 0) + 1

    transactions_cache = items_all
    review_summary = {"with_manual_review": with_mr, "straight_through": straight}
    docskill_summary = {
        k: {"pages": pages_by_docskill.get(k, 0), "txcount": txcount_by_docskill.get(k, 0)}
        for k in set(pages_by_docskill) | set(txcount_by_docskill)
    }

    return RedirectResponse("/", status_code=303)


# ---------- API: Logout ----------
@app.post("/logout")
def logout():
    global bearer_token, vantage_host, skills_cache, transactions_cache, review_summary, docskill_summary, last_error, search_attempted
    bearer_token = None
    vantage_host = None
    skills_cache = []
    transactions_cache = []
    review_summary = {"with_manual_review": 0, "straight_through": 0}
    docskill_summary = {}
    last_error = ""
    search_attempted = False
    return RedirectResponse("/", status_code=303)
