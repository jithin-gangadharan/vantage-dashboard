from datetime import datetime, time, timezone
import requests, csv
from io import StringIO
from typing import Dict


def to_utc_iso(date_str: str, is_end: bool = False) -> str:
    d = datetime.fromisoformat(date_str)
    t = time(23, 59, 59) if is_end else time(0, 0, 0)
    dt = datetime.combine(d.date(), t)
    dt = dt.astimezone()
    return dt.astimezone(timezone.utc).isoformat()


def extract_detail_from_response(resp: requests.Response) -> str:
    try:
        data = resp.json()
        if isinstance(data, dict):
            if data.get("detail"):
                return str(data["detail"])
            if data.get("error_description"):
                return str(data["error_description"])
            if data.get("title"):
                return str(data["title"])
    except Exception:
        pass
    return (resp.text or "").strip()


def fetch_tx_review_and_docskill(
    vantage_host: str,
    process_skill_id: str,
    start_iso: str,
    end_iso: str,
    headers: dict,
) -> Dict[str, dict]:
    """
    Fetch QA process-skills/documents report and extract:
      - HasManualReview
      - DocumentSkillName
    Returns { transactionId: {"manual_review": bool, "document_skill_name": str} }
    """

    url = f"https://{vantage_host}/api/reporting/v1/qa/process-skills/documents"
    params = {
        "processSkillId": process_skill_id,
        "startDate": start_iso,
        "endDate": end_iso,
    }

    r = requests.get(url, headers=headers, params=params, timeout=120)
    if r.status_code != 200 or not r.text:
        print(f"[ERROR] QA Reporting API failed {r.status_code}: {r.text[:200]}")
        return {}

    reader = csv.DictReader(StringIO(r.text))
    print(f"[DEBUG] CSV Headers: {reader.fieldnames}")

    result: Dict[str, dict] = {}
    for i, row in enumerate(reader):
        if not row:
            continue
        txid = row.get("TransactionId", "").strip()
        if not txid:
            continue

        has_mr = str(row.get("HasManualReview", "")).strip().lower() in {"true", "1", "yes"}
        dskill = row.get("DocumentSkillName", "").strip()

        if i < 5:  # log first few rows for debug
            print(f"[DEBUG] Row {i}: tx={txid}, docSkill={dskill}, mr={has_mr}")

        entry = result.get(txid, {"manual_review": False, "document_skill_name": ""})
        entry["manual_review"] = entry["manual_review"] or has_mr
        if dskill:
            entry["document_skill_name"] = dskill
        result[txid] = entry

    print(f"[DEBUG] Parsed {len(result)} transactions with doc skills (QA report)")
    return result
