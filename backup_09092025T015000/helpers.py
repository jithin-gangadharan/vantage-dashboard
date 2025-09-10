from datetime import datetime, time, timezone
import requests
import csv
from io import StringIO

def to_utc_iso(date_str: str, is_end=False) -> str:
    """Convert YYYY-MM-DD string to ISO8601 UTC string."""
    d = datetime.fromisoformat(date_str)
    t = time(23, 59, 59) if is_end else time(0, 0, 0)
    dt = datetime.combine(d.date(), t)
    return dt.astimezone(timezone.utc).isoformat()

def get_kv(items: list, key: str, default: str = "") -> str:
    if not isinstance(items, list):
        return default
    for it in items:
        if isinstance(it, dict) and it.get("key") == key:
            return it.get("value", default)
    return default

def extract_detail_from_response(resp: requests.Response) -> str:
    """Return only the 'detail' field from ABBYY error responses."""
    try:
        data = resp.json()
        if data.get("detail"):
            return data["detail"]
        if data.get("error_description"):
            return data["error_description"]
        if data.get("title"):
            return data["title"]
    except Exception:
        pass
    return resp.text[:200]

def fetch_manual_review_tx_ids(vantage_host: str, skill_id: str, start_iso: str, end_iso: str, headers: dict) -> set[str]:
    """Return set of TransactionIds that had manual review steps."""
    url = f"https://{vantage_host}/api/reporting/v1/transaction-steps"
    params = {"skillId": skill_id, "startDate": start_iso, "endDate": end_iso}
    r = requests.get(url, headers=headers, params=params, timeout=120)
    if r.status_code != 200:
        return set()

    reviewed = set()
    reader = csv.DictReader(StringIO(r.text))
    for row in reader:
        if row.get("ManualReviewOperatorName") or row.get("ManualReviewOperatorEmail"):
            txid = (row.get("TransactionId") or "").strip()
            if txid:
                reviewed.add(txid)
    return reviewed
