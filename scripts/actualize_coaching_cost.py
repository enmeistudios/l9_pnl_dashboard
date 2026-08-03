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


def _parse_gviz_date(val):
    """gviz returns date cells as 'Date(YYYY,MM,DD)' with MM zero-indexed."""
    if isinstance(val, str) and val.startswith("Date("):
        m = re.match(r"Date\((\d+),(\d+),(\d+)\)", val)
        if m:
            y, mo, d = map(int, m.groups())
            return pd.Timestamp(year=y, month=mo + 1, day=d)
    return val


def load_rates_from_sheet(sheet_id: str = None, gid: str = None) -> pd.DataFrame:
    """
    Reads the coach rates table straight from the published Google Sheet.
    Returns a clean DataFrame: coach_name (str), effective_date (datetime),
    rate_per_class (float) — ready to pass into actualize_coaching_cost().
    """
    sheet_id = sheet_id or RATES_SHEET_ID
    gid = gid or RATES_SHEET_GID

    if sheet_id == "PASTE_YOUR_RATES_SHEET_ID_HERE":
        raise RuntimeError("Set RATES_SHEET_ID at the top of this file to your actual rates sheet ID.")

    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:json&gid={gid}"
    resp = requests.get(url, timeout=30)
    text = resp.text

    if "accounts.google.com" in text or "ServiceLogin" in text:
        raise RuntimeError(
            "Rates sheet isn't public yet. On that sheet: File -> Share -> Publish to web, "
            "AND Share -> General access -> 'Anyone with the link' -> Viewer."
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

    # Normalize expected columns regardless of exact header casing/spacing
    df.columns = [str(c).strip().lower() for c in df.columns]
    required = {"coach_name", "effective_date", "rate_per_class"}
    missing = required - set(df.columns)
    if missing:
        raise RuntimeError(f"Rates sheet is missing expected column(s): {missing}. Found: {list(df.columns)}")

    df["coach_name"] = df["coach_name"].astype(str).str.strip()
    df["effective_date"] = pd.to_datetime(df["effective_date"].apply(_parse_gviz_date))
    df["rate_per_class"] = pd.to_numeric(df["rate_per_class"], errors="coerce")

    return df[["coach_name", "effective_date", "rate_per_class"]]



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



def pull_taught_classes(month_start: str, month_end: str, max_pages: int = 50) -> pd.DataFrame:
    """
    month_start / month_end: 'YYYY-MM-DD' strings, inclusive.
    Pulls class_sessions and filters to classes that actually happened.

    max_pages is a SAFETY CAP: the server-side date filter below is UNCONFIRMED
    (I guessed the param names) — if the API silently ignores it, get_all()
    would otherwise page through the studio's entire class history instead of
    one month. 50 pages x 200 rows = 10,000 rows, comfortably more than a
    single studio-month of classes should ever produce. If the log shows this
    cap being hit, the date filter isn't working and needs fixing before
    trusting the results.
    """
    df = get_all(
        "class_sessions",
        page_size=200,
        max_pages=max_pages,
        filters={"start_date__gte": month_start, "start_date__lte": month_end},
    )

    if df.empty:
        return df

    df["start_date"] = pd.to_datetime(df["start_date"])

    # Defensive client-side filter too, in case the server-side filter keys
    # above don't match what the API actually accepts — confirm on first run
    # by checking len(df) and the min/max of start_date printed below.
    df = df[
        (df["start_date"] >= pd.to_datetime(month_start))
        & (df["start_date"] <= pd.to_datetime(month_end))
    ]

    # A class that was cancelled or archived didn't actually get taught
    df = df[df["cancellation_datetime"].isna()]
    df = df[df["archived_at"].isna()]

    print(f"Pulled {len(df)} taught class sessions "
          f"({df['start_date'].min()} to {df['start_date'].max()})")

    return df


def split_cotaught(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Returns (single_instructor_df, cotaught_df).
    Co-taught classes (instructor_names has != 1 entry) are pulled out and
    NOT included in the cost calc — flagged for manual review instead.
    """
    is_single = df["instructor_names"].apply(lambda x: isinstance(x, list) and len(x) == 1)
    single = df[is_single].copy()
    single["instructor"] = single["instructor_names"].apply(lambda x: x[0])

    cotaught = df[~is_single].copy()
    return single, cotaught


def attach_rates(df: pd.DataFrame, rates_df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    rates_df columns required: coach_name, effective_date, rate_per_class
    Looks up the rate in effect on each class's date (most recent
    effective_date <= class date). Rows with no matching rate are pulled
    out into unmatched_df rather than silently costed at 0.
    """
    rates_df = rates_df.copy()
    rates_df["effective_date"] = pd.to_datetime(rates_df["effective_date"])
    rates_df = rates_df.sort_values(["coach_name", "effective_date"])

    def lookup_rate(row):
        applicable = rates_df[
            (rates_df["coach_name"] == row["instructor"])
            & (rates_df["effective_date"] <= row["start_date"])
        ]
        if applicable.empty:
            return None
        return applicable.iloc[-1]["rate_per_class"]

    df = df.copy()
    df["rate"] = df.apply(lookup_rate, axis=1)

    matched = df[df["rate"].notna()].copy()
    unmatched = df[df["rate"].isna()].copy()

    matched["cost"] = matched["rate"]
    return matched, unmatched


def summarize_by_studio(matched_df: pd.DataFrame) -> pd.DataFrame:
    return (
        matched_df.groupby("location_display")
        .agg(classes_taught=("id", "count"), coaching_cost=("cost", "sum"))
        .reset_index()
        .rename(columns={"location_display": "studio"})
    )


def actualize_coaching_cost(month_start: str, month_end: str, rates_df: pd.DataFrame):
    raw = pull_taught_classes(month_start, month_end)
    single, cotaught = split_cotaught(raw)
    matched, unmatched = attach_rates(single, rates_df)
    studio_costs = summarize_by_studio(matched)

    print("\n=== Actualized coaching cost by studio ===")
    print(studio_costs.to_string(index=False))

    if len(cotaught):
        print(f"\n[FLAG] {len(cotaught)} co-taught class(es) excluded from cost — review manually:")
        print(cotaught[["id", "start_date", "instructor_names", "location_display", "class_type_display"]].to_string(index=False))

    if len(unmatched):
        print(f"\n[FLAG] {len(unmatched)} class(es) with no matching rate — excluded, resolve and re-run:")
        print(unmatched[["id", "start_date", "instructor", "location_display"]].to_string(index=False))

    return studio_costs, cotaught, unmatched


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
    studio_costs, cotaught, unmatched = actualize_coaching_cost(month_start, month_end, rates_df)

