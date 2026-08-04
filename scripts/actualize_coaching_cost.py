"""
Actualize coaching cost by studio, using real taught-class data from Mariana Tek
instead of manually-allocated salary lines.

DROP THIS INTO YOUR EXISTING NOTEBOOK — it assumes `get_all()`, `BASE_URL`, and
`HEADERS` are already defined exactly as in your working Mariana Tek setup.

Model: cost = (# classes actually taught by coach X at studio Y in the month)
              x (coach X's flat per-class rate in effect on that date)

Rate table lives in a SEPARATE Google Sheet (not the public P&L sheet), with
columns: coach_name | effective_date | rate_per_class
  - effective_date = the date a rate STARTS applying. If a coach got a raise
    starting 2026-03-01, add a new row rather than editing the old one, so
    history is preserved and past months still cost out correctly.

Output:
  1. studio_costs      -> the actual number to compare against the manual
                          salary line in the sheet
  2. flagged_cotaught  -> classes with >1 instructor, excluded from cost,
                          for manual review
  3. flagged_unmatched -> classes taught by someone with no matching row in
                          the rate table (typo, new hire not added yet, etc.)
                          — excluded from cost until resolved, NOT silently
                          zeroed out, so nothing goes missing unnoticed
"""

import re
import json
import time
import requests
import pandas as pd

# ────────────────────────────────────────────────────────────────
# RATES SHEET — read via gviz, same trick the dashboard itself uses for
# the P&L Google Sheet. No service account, no API key. Requires the
# rates sheet to be Published to web (File -> Share -> Publish to web)
# AND shared as "Anyone with the link can view."
#
# NOTE: this makes the rates sheet link-readable by anyone who has the
# URL — not indexed/searchable, but not private either. Flagging this
# once since it's pay-rate data; fine to proceed if that tradeoff is
# acceptable to you.
# ────────────────────────────────────────────────────────────────
RATES_SHEET_ID = "1PiacHBuqNHnJFQPiAv_ZK7HqCc1EMYU0RzeHBNAd72A"
RATES_SHEET_GID = "1417395469"  # from the sheet's URL — more stable than a tab name
ROSTER_SHEET_GID = "PASTE_YOUR_ROSTER_TAB_GID_HERE"  # add a 'Roster' tab: coach_name, employee_id, start_date

# Apps Script Web App — set up once by following the setup steps, then paste
# the URL and your chosen secret here (or via env vars, same pattern as
# MARIANA_TEK_KEY, for the GitHub Actions version)
SHEET_WRITE_URL = "https://script.google.com/macros/s/AKfycbwyYTsc_PGRkCpDjKvydgANpZvdyLxXE8Oq6eHNJNLMYVpVhhiBMGd8Qag7hUexeKzsKw/exec"

try:
    from google.colab import userdata
    SHEET_WRITE_SECRET = userdata.get('SHEET_WRITE_SECRET')
except ImportError:
    import os
    SHEET_WRITE_SECRET = os.environ.get('SHEET_WRITE_SECRET')


def _parse_gviz_date(val):
    """gviz returns date cells as 'Date(YYYY,MM,DD)' with MM zero-indexed."""
    if isinstance(val, str) and val.startswith("Date("):
        m = re.match(r"Date\((\d+),(\d+),(\d+)\)", val)
        if m:
            y, mo, d = map(int, m.groups())
            return pd.Timestamp(year=y, month=mo + 1, day=d)
    return val


def _fetch_gviz_table(sheet_id: str, gid: str) -> pd.DataFrame:
    """
    Shared low-level fetch for any published-to-web Google Sheet tab.
    Returns a raw DataFrame with lowercased, stripped column names —
    callers validate their own required columns on top of this.
    """
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:json&gid={gid}"
    resp = requests.get(url, timeout=30)
    text = resp.text

    if "accounts.google.com" in text or "ServiceLogin" in text:
        raise RuntimeError(
            f"Sheet {sheet_id} (gid {gid}) isn't public yet. On that sheet: File -> Share -> "
            "Publish to web, AND Share -> General access -> 'Anyone with the link' -> Viewer."
        )

    match = re.search(r"google\.visualization\.Query\.setResponse\((.*)\);?\s*$", text, re.S)
    if not match:
        raise RuntimeError(f"Unexpected response from sheet {sheet_id}, gid '{gid}': {text[:300]}")

    table = json.loads(match.group(1))["table"]
    cols = [c.get("label") or f"col{i}" for i, c in enumerate(table["cols"])]
    rows = []
    for r in table["rows"]:
        row = [(cell.get("v") if cell else None) for cell in (r.get("c") or [])]
        rows.append(row)
    df = pd.DataFrame(rows, columns=cols)
    df.columns = [str(c).strip().lower() for c in df.columns]
    return df


def load_rates_from_sheet(sheet_id: str = None, gid: str = None) -> pd.DataFrame:
    """
    Reads the coach rates table straight from the published Google Sheet.
    Returns: coach_name (str), employee_id (float), effective_date (datetime),
    rate_per_class (float) — ready to pass into actualize_coaching_cost().
    """
    sheet_id = sheet_id or RATES_SHEET_ID
    gid = gid or RATES_SHEET_GID

    if sheet_id == "PASTE_YOUR_RATES_SHEET_ID_HERE":
        raise RuntimeError("Set RATES_SHEET_ID at the top of this file to your actual rates sheet ID.")

    df = _fetch_gviz_table(sheet_id, gid)

    required = {"coach_name", "employee_id", "effective_date", "rate_per_class"}
    missing = required - set(df.columns)
    if missing:
        raise RuntimeError(f"Rates sheet is missing expected column(s): {missing}. Found: {list(df.columns)}")

    df["coach_name"] = df["coach_name"].astype(str).str.strip()
    df["employee_id"] = pd.to_numeric(df["employee_id"], errors="coerce")
    df["effective_date"] = pd.to_datetime(df["effective_date"].apply(_parse_gviz_date))
    df["rate_per_class"] = pd.to_numeric(df["rate_per_class"], errors="coerce")

    return df[["coach_name", "employee_id", "effective_date", "rate_per_class"]]


def load_roster_from_sheet(sheet_id: str = None, gid: str = None) -> pd.DataFrame:
    """
    Reads a separate 'Roster' tab: coach_name, employee_id, start_date.
    This is what build_tenure_rates() needs to know WHEN each coach started,
    so it can compute which milestones they've crossed as of today.
    New coaches only need adding here once — the automation takes it from there.
    """
    sheet_id = sheet_id or RATES_SHEET_ID
    gid = gid or ROSTER_SHEET_GID

    if gid == "PASTE_YOUR_ROSTER_TAB_GID_HERE":
        raise RuntimeError("Set ROSTER_SHEET_GID at the top of this file to your roster tab's gid.")

    df = _fetch_gviz_table(sheet_id, gid)

    required = {"coach_name", "employee_id", "start_date"}
    missing = required - set(df.columns)
    if missing:
        raise RuntimeError(f"Roster sheet is missing expected column(s): {missing}. Found: {list(df.columns)}")

    df["coach_name"] = df["coach_name"].astype(str).str.strip()
    df["employee_id"] = pd.to_numeric(df["employee_id"], errors="coerce")
    df["start_date"] = pd.to_datetime(df["start_date"].apply(_parse_gviz_date))

    return df[["coach_name", "employee_id", "start_date"]]


def push_new_rate_rows(new_rows_df: pd.DataFrame, webapp_url: str, secret: str, sheet_name: str = None) -> dict:
    """
    Sends newly-due rate rows to the Apps Script Web App, which appends
    them directly to the rates sheet. This is the ONLY function that
    writes anything — everything else in this file only reads.

    new_rows_df needs: coach_name, employee_id, effective_date, rate_per_class
    """
    if new_rows_df.empty:
        print("No new rate rows to push — nothing due yet.")
        return {"status": "ok", "added": 0}

    rows = new_rows_df[["coach_name", "employee_id", "effective_date", "rate_per_class"]].values.tolist()
    resp = requests.post(
        webapp_url,
        json={"secret": secret, "sheet_name": sheet_name or "Sheet1", "rows": rows},
        timeout=30,
    )
    resp.raise_for_status()
    result = resp.json()
    if "error" in result:
        raise RuntimeError(f"Apps Script rejected the write: {result['error']}")
    print(f"Pushed {result.get('added', 0)} new rate row(s) to the sheet.")
    return result



# ────────────────────────────────────────────────────────────────
# AUTH — Colab secrets manager
# ────────────────────────────────────────────────────────────────
# Requires a Colab secret named MARIANA_TEK_KEY (left sidebar -> key icon ->
# "Add new secret" -> toggle "Notebook access" ON for this notebook).
# Falls back to an environment variable of the same name if not in Colab,
# so this script also runs outside Colab without editing anything below.
try:
    from google.colab import userdata
    API_KEY = userdata.get('MARIANA_TEK_KEY')
except ImportError:
    import os
    API_KEY = os.environ.get('MARIANA_TEK_KEY')

if not API_KEY:
    raise RuntimeError(
        "MARIANA_TEK_KEY not found. In Colab: add it via the key icon in the "
        "left sidebar and turn on notebook access. Outside Colab: set it as "
        "an environment variable."
    )

BASE_URL = "https://enmei.marianatek.com/api"
HEADERS = {"Authorization": f"Bearer {API_KEY}"}


# ────────────────────────────────────────────────────────────────
# get_all() — paginated fetch, as already proven working against this API
# ────────────────────────────────────────────────────────────────
def get_all(resource, page_size=100, max_pages=None, filters=None, max_retries=3):
    all_records = []
    page = 1
    while True:
        params = {"page": page, "page_size": page_size}
        if filters:
            params.update(filters)

        for attempt in range(max_retries):
            try:
                resp = requests.get(f"{BASE_URL}/{resource}", headers=HEADERS, params=params, timeout=30)
                resp.raise_for_status()
                payload = resp.json()
                break
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                wait = 2 ** attempt
                print(f"page {page}: connection issue ({e}), retrying in {wait}s...")
                time.sleep(wait)
        else:
            raise Exception(f"Failed to fetch page {page} of {resource} after {max_retries} attempts")

        for record in payload["data"]:
            row = {"id": record["id"], **record["attributes"]}
            all_records.append(row)

        total_pages = payload["meta"]["pagination"]["pages"]
        print(f"page {page} of {total_pages}")

        if page >= total_pages:
            break
        if max_pages and page >= max_pages:
            print("Stopped early at max_pages limit")
            break
        page += 1
        time.sleep(0.2)

    return pd.DataFrame(all_records)


def fetch_instructor_payroll(month_start: str, month_end: str, page_size: int = 2000,
                              poll_interval: int = 2, max_wait: int = 60) -> pd.DataFrame:
    """
    Pulls the Instructor Payroll report (Mariana Tek report id 312) for the
    given date range. Confirmed real param names from watching the actual
    web UI network call: min_start_date_day / max_start_date_day (both
    'YYYY-MM-DD' strings) — NOT start_date/end_date, which the API silently
    ignores.

    This is a 3-step ASYNC flow, confirmed by inspecting real browser
    network calls:
      1. GET async_table_report_data -> {"data": [{"id": job_id}]}
      2. Poll GET async_table_report_data/job_status?id=job_id until status
         == "complete" -> returns a signed, temporary s3_link
      3. GET that s3_link directly (no auth headers needed — the signature
         in the URL itself is the auth) to get the actual report data

    Returns a DataFrame with the same columns as the exported CSV:
    Class ID, Instructor Display Name(s), Instructor First Name(s),
    Instructor Last Name(s), Employee ID, Is Substitute?, Class Date,
    Class Time, Class Day Of Week, Location, Class Type, Class Category,
    Class Name, Checked In Reservations, No Showed Reservations, Class Capacity
    """
    resp = requests.get(
        f"{BASE_URL}/async_table_report_data",
        headers=HEADERS,
        params={
            "id": 312,
            "slug": "instructor-payroll",
            "min_start_date_day": month_start,
            "max_start_date_day": month_end,
            "page_size": page_size,
        },
        timeout=30,
    )
    resp.raise_for_status()
    job_id = resp.json()["data"][0]["id"]

    waited = 0
    s3_link = None
    while waited < max_wait:
        status_resp = requests.get(f"{BASE_URL}/async_table_report_data/job_status", headers=HEADERS, params={"id": job_id}, timeout=30)
        status_resp.raise_for_status()
        status_data = status_resp.json()["data"]
        if status_data["status"] == "complete":
            s3_link = status_data["s3_link"]
            break
        if status_data["status"] == "failed":
            raise RuntimeError(f"Instructor payroll report job {job_id} failed")
        time.sleep(poll_interval)
        waited += poll_interval

    if s3_link is None:
        raise TimeoutError(f"Instructor payroll report job {job_id} did not complete within {max_wait}s")

    data_resp = requests.get(s3_link, timeout=30)
    data_resp.raise_for_status()
    payload = data_resp.json()

    PAYROLL_COLUMNS = [
        "class_id", "instructor_display_name", "instructor_first_name", "instructor_last_name",
        "employee_id", "is_substitute", "class_date", "class_time", "class_day_of_week",
        "location", "class_type", "class_category", "class_name",
        "checked_in", "no_showed", "capacity",
    ]

    if isinstance(payload, list) and payload and isinstance(payload[0], list):
        return pd.DataFrame(payload, columns=PAYROLL_COLUMNS)
    if isinstance(payload, list):
        return pd.DataFrame(payload)
    if isinstance(payload, dict) and "rows" in payload:
        cols = payload.get("columns") or payload.get("cols") or PAYROLL_COLUMNS
        return pd.DataFrame(payload["rows"], columns=cols)
    raise RuntimeError(
        f"Unrecognized instructor payroll report shape: {type(payload)} "
        f"keys={list(payload.keys()) if isinstance(payload, dict) else None}"
    )



def build_tenure_rates(coaches_df: pd.DataFrame, milestones: list, as_of=None) -> pd.DataFrame:
    """
    Generates coach_name | effective_date | rate_per_class rows for milestones
    a coach has ALREADY crossed as of `as_of` (defaults to today) — NOT future
    milestones. This matters because the milestone table itself (£50/£55/£60,
    or whatever it becomes) can change over time; pre-computing rows for
    milestones that haven't happened yet bakes in an assumption that today's
    rate structure will still apply months or years from now.

    coaches_df: columns coach_name, start_date (datetime)
    milestones: list of (relativedelta_offset_from_start, rate) tuples, using
        whatever the CURRENT rate structure is — e.g.
        from dateutil.relativedelta import relativedelta
        [(relativedelta(months=0), 50),
         (relativedelta(months=6), 55),
         (relativedelta(months=12), 60)]

    Run this periodically (e.g. monthly, alongside the coaching-cost job).
    Use append_new_rate_rows() to only add rows that don't already exist in
    the sheet, so re-running never duplicates or overwrites history.
    """
    as_of = pd.Timestamp(as_of or pd.Timestamp.today().date())
    out = []
    for _, row in coaches_df.iterrows():
        for offset, rate in milestones:
            eff_date = row["start_date"] + offset
            if eff_date <= as_of:
                out.append({
                    "coach_name": row["coach_name"],
                    "effective_date": eff_date.strftime("%Y-%m-%d"),
                    "rate_per_class": rate,
                })
    if not out:
        return pd.DataFrame(columns=["coach_name", "effective_date", "rate_per_class"])
    return pd.DataFrame(out).sort_values(["coach_name", "effective_date"]).reset_index(drop=True)


def append_new_rate_rows(existing_rates_df: pd.DataFrame, generated_rows_df: pd.DataFrame) -> pd.DataFrame:
    """
    Merges newly-due milestone rows into the existing rates table, skipping
    any (coach_name, effective_date) pair already present — so this is safe
    to run repeatedly (e.g. every month) without creating duplicates or
    disturbing rows that may have since been manually overridden (e.g. an
    individually-negotiated raise added by hand).
    """
    if existing_rates_df.empty:
        return generated_rows_df.copy()
    existing_keys = set(zip(existing_rates_df["coach_name"], existing_rates_df["effective_date"]))
    new_rows = generated_rows_df[
        ~generated_rows_df.apply(lambda r: (r["coach_name"], r["effective_date"]) in existing_keys, axis=1)
    ]
    if new_rows.empty:
        return existing_rates_df.copy()
    return pd.concat([existing_rates_df, new_rows], ignore_index=True).sort_values(
        ["coach_name", "effective_date"]
    ).reset_index(drop=True)



RECOVERY_BOOTS_EMPLOYEE_ID = 6733  # confirmed fixed ID for the equipment-booking pseudo-instructor


def split_payroll_classes(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Takes the raw instructor payroll report and splits it into:
      - real: actual coached classes, ready for rate matching
      - unassigned: no instructor was ever assigned (employee_id is blank)
      - cotaught: two+ rows share the same class_id — genuinely co-taught,
        needs a human pay-split decision, not auto-calculated
    Recovery Boots (employee_id 6733) is dropped entirely here — it's an
    equipment booking, not a coached class.
    """
    df = df[df["employee_id"] != RECOVERY_BOOTS_EMPLOYEE_ID].copy()

    unassigned = df[df["employee_id"].isna()].copy()
    assigned = df[df["employee_id"].notna()].copy()

    dupe_class_ids = assigned["class_id"][assigned["class_id"].duplicated(keep=False)].unique()
    cotaught = assigned[assigned["class_id"].isin(dupe_class_ids)].copy()
    real = assigned[~assigned["class_id"].isin(dupe_class_ids)].copy()

    return real, unassigned, cotaught


def attach_rates_by_employee(df: pd.DataFrame, rates_df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    rates_df columns required: employee_id, effective_date, rate_per_class
    Matching on employee_id instead of name sidesteps every spelling
    mismatch (Bromley/Bromey, Vicary-Smith/Vicary Smith, etc.) found during
    real testing — employee_id is a stable numeric identifier, names aren't.
    """
    rates_df = rates_df.copy()
    rates_df["employee_id"] = pd.to_numeric(rates_df["employee_id"], errors="coerce")
    rates_df["effective_date"] = pd.to_datetime(rates_df["effective_date"])
    rates_df = rates_df.sort_values(["employee_id", "effective_date"])

    df = df.copy()
    df["class_date"] = pd.to_datetime(df["class_date"])

    def lookup_rate(row):
        applicable = rates_df[
            (rates_df["employee_id"] == row["employee_id"])
            & (rates_df["effective_date"] <= row["class_date"])
        ]
        if applicable.empty:
            return None
        return applicable.iloc[-1]["rate_per_class"]

    df["rate"] = df.apply(lookup_rate, axis=1)

    matched = df[df["rate"].notna()].copy()
    unmatched = df[df["rate"].isna()].copy()
    matched["cost"] = matched["rate"]
    return matched, unmatched


def sync_rate_milestones(milestones: list, as_of=None) -> dict:
    """
    The full automation: reads the roster + current rates, figures out
    which milestones are newly due, and pushes ONLY the new rows to the
    sheet via the Apps Script Web App. Safe to run repeatedly (e.g. daily
    or monthly via GitHub Actions) — never re-adds a row that's already
    there, since it filters by (employee_id, effective_date) before pushing.
    """
    roster = load_roster_from_sheet()
    current_rates = load_rates_from_sheet()

    due_rows = build_tenure_rates(roster, milestones, as_of=as_of)
    # build_tenure_rates() works off coach_name + start_date; attach employee_id
    # back on afterward by joining through the roster
    due_rows = due_rows.merge(roster[["coach_name", "employee_id"]], on="coach_name", how="left")

    existing_keys = set(zip(current_rates["employee_id"], current_rates["effective_date"].dt.strftime("%Y-%m-%d")))
    new_rows = due_rows[
        ~due_rows.apply(lambda r: (r["employee_id"], r["effective_date"]) in existing_keys, axis=1)
    ]

    return push_new_rate_rows(new_rows, SHEET_WRITE_URL, SHEET_WRITE_SECRET)



    return (
        matched_df.groupby("location")
        .agg(classes_taught=("class_id", "count"), coaching_cost=("cost", "sum"))
        .reset_index()
        .rename(columns={"location": "studio"})
    )


def actualize_coaching_cost(month_start: str, month_end: str, rates_df: pd.DataFrame):
    raw = fetch_instructor_payroll(month_start, month_end)
    print(f"Pulled {len(raw)} payroll rows ({raw['class_date'].min()} to {raw['class_date'].max()})")

    real, unassigned, cotaught = split_payroll_classes(raw)
    matched, unmatched = attach_rates_by_employee(real, rates_df)
    studio_costs = summarize_by_studio(matched)

    print("\n=== Actualized coaching cost by studio ===")
    print(studio_costs.to_string(index=False))

    if len(unassigned):
        print(f"\n[FLAG] {len(unassigned)} class(es) with NO instructor assigned — excluded, likely a scheduling gap:")
        print(unassigned[["class_id", "class_date", "location", "class_type"]].to_string(index=False))

    if len(cotaught):
        print(f"\n[FLAG] {len(cotaught)} row(s) from genuinely co-taught classes — excluded, needs a pay-split decision:")
        print(cotaught[["class_id", "class_date", "instructor_display_name", "employee_id", "location"]].to_string(index=False))

    if len(unmatched):
        distinct = unmatched[["employee_id", "instructor_display_name"]].drop_duplicates()
        print(f"\n[FLAG] {len(unmatched)} class(es) with no matching rate ({len(distinct)} distinct coach(es)) — excluded, resolve and re-run:")
        print(distinct.to_string(index=False))

    return studio_costs, unassigned, cotaught, unmatched


# ────────────────────────────────────────────────────────────────
# Entry point — runs when executed directly (python actualize_coaching_cost.py
# or via the GitHub Actions workflow). Defaults to the previous full calendar
# month, since that's the natural monthly cadence for this job.
# ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    import calendar

    if len(sys.argv) == 3:
        month_start, month_end = sys.argv[1], sys.argv[2]
    else:
        today = pd.Timestamp.today()
        first_of_this_month = today.replace(day=1)
        last_month_end = first_of_this_month - pd.Timedelta(days=1)
        month_start = last_month_end.replace(day=1).strftime("%Y-%m-%d")
        month_end = last_month_end.strftime("%Y-%m-%d")

    print(f"Running for {month_start} to {month_end}")
    rates_df = load_rates_from_sheet()
    studio_costs, unassigned, cotaught, unmatched = actualize_coaching_cost(month_start, month_end, rates_df)

