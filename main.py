from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
import requests, json
from datetime import datetime, timedelta
from helpers import (
    to_utc_iso,
    extract_detail_from_response,
    fetch_tx_review_and_docskill,
)

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

# Globals
bearer_token = None
vantage_host = None
skills_cache = []
transactions_cache = []
review_summary = {"with_manual_review": 0, "straight_through": 0}
docskill_summary = {}
last_error = ""


# ---------- HTML Dashboard ----------
@app.get("/", response_class=HTMLResponse)
def dashboard():
    global bearer_token, skills_cache, transactions_cache, review_summary, docskill_summary, last_error

    css_link = '<link rel="stylesheet" href="/static/style.css">'
    chartjs = '<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>'

    # If not authenticated → show login form
    if not bearer_token:
        return f"""
        <html><head><title>ABBYY Vantage Dashboard</title>{css_link}</head>
        <body>
            <div class="container">
                <h1>ABBYY Vantage Dashboard</h1>
                <div class="card">
                    <h2>Authenticate</h2>
                    <form method="post" action="/authenticate">
                        <label>Vantage Host:</label>
                        <input type="text" name="url" required placeholder="e.g., vantage-us.abbyy.com"><br>
                        <label>Client ID:</label>
                        <input type="text" name="client_id" required><br>
                        <label>Client Secret:</label>
                        <input type="password" name="client_secret" required><br>
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

    # --- Results if transactions exist ---
    if transactions_cache:
        total_pages = sum(int(tx.get("pageCount") or 0) for tx in transactions_cache)
        straight = review_summary["straight_through"]
        manual = review_summary["with_manual_review"]

        # Colors mapping for charts/tables
        review_colors = {"Straight Through": "#27ae60", "Manual Review": "#e74c3c"}
        doc_colors = ["#2e86c1", "#e67e22", "#27ae60", "#8e44ad", "#c0392b"]

        # --- Results summary ---
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

        # --- Doc skill summary ---
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

        # --- Transactions table ---
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
            mr = tx.get("manualReview", "")
            results_html += f"""
            <tr>
                <td>{tx.get('transactionId','')}</td>
                <td>{source_file}</td>
                <td>{tx.get('status','')}</td>
                <td>{tx.get('pageCount','')}</td>
                <td>{tx.get('createTimeUtc','')}</td>
                <td>{mr}</td>
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

    return f"""
    <html>
    <head>
        <title>ABBYY Vantage Dashboard</title>
        {css_link}{chartjs}
        <script>
        // CSV Export
        function exportCSV() {{
            let table = document.getElementById("txTable");
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
        window.onload = showPage;
        </script>
    </head>
    <body>
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
                    <form method="post" action="/transactions">
                        <label>Process Skill:</label>
                        <select name="skill_id" required>{skill_opts}</select>
                        <label>Transaction Status:</label>
                        <select name="transaction_type">
                            <option value="Processed">Processed</option>
                            <option value="Failed">Failed</option>
                        </select>
                        <label>Start Date:</label>
                        <input type="date" name="start_date" value="{start_default}" min="{min_date}" max="{max_date}">
                        <label>End Date:</label>
                        <input type="date" name="end_date" value="{end_default}" min="{min_date}" max="{max_date}">
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
    global bearer_token, vantage_host, skills_cache, last_error
    vantage_host = url.strip()
    last_error = ""

    token_url = f"https://{vantage_host}/auth2/connect/token"
    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "client_credentials",
        "scope": "openid permissions global.wildcard",
    }
    headers = {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}

    try:
        r = requests.post(token_url, data=payload, headers=headers)
        r.raise_for_status()
    except requests.exceptions.RequestException as e:
        last_error = f"Authentication request failed: {e}"
        return RedirectResponse("/", status_code=303)

    if r.status_code != 200:
        last_error = f"Auth failed ({r.status_code}): {extract_detail_from_response(r)}"
        return RedirectResponse("/", status_code=303)

    bearer_token = r.json().get("access_token")

    # Load skills
    skills_url = f"https://{vantage_host}/api/publicapi/v1/skills"
    headers = {"Authorization": f"Bearer {bearer_token}"}
    try:
        r = requests.get(skills_url, headers=headers)
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
    global bearer_token, vantage_host, transactions_cache, review_summary, docskill_summary, last_error
    if not bearer_token or not vantage_host:
        return RedirectResponse("/", status_code=303)

    headers = {"Authorization": f"Bearer {bearer_token}"}
    start_iso = to_utc_iso(start_date)
    end_iso = to_utc_iso(end_date, is_end=True)
    last_error = ""

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
            r = requests.get(url, headers=headers, params=params)
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
    tx_info_map = fetch_tx_review_and_docskill(vantage_host, skill_id, start_iso, end_iso, headers)

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
    docskill_summary = {k: {"pages": pages_by_docskill.get(k, 0), "txcount": txcount_by_docskill.get(k, 0)} for k in set(pages_by_docskill) | set(txcount_by_docskill)}

    return RedirectResponse("/", status_code=303)


# ---------- API: Logout ----------
@app.post("/logout")
def logout():
    global bearer_token, vantage_host, skills_cache, transactions_cache, review_summary, docskill_summary, last_error
    bearer_token = None
    vantage_host = None
    skills_cache = []
    transactions_cache = []
    review_summary = {"with_manual_review": 0, "straight_through": 0}
    docskill_summary = {}
    last_error = ""
    return RedirectResponse("/", status_code=303)
