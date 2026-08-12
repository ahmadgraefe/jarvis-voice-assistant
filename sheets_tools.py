"""
Jarvis V2 — Google Sheets (Tracking-Workbook)
Native Google Sheet — direct read/write via the Sheets API, no more
download/local-edit/human-import roundtrip. That workaround was only ever
needed because the OLD workbook was an uploaded .xlsx sitting in Google
Drive's Office-compatibility mode, which the Sheets API couldn't touch at
all (confirmed: even a metadata GET failed). Ahmad created a fresh native
Sheet and everything was migrated over on 2026-08-05 — same tabs, same
formulas, all preserved — so this now writes directly, live, no import
click needed.

Locale gotcha (cost real debugging time, documenting it so it doesn't
happen again): this Sheet is de_DE — formulas need ';' as the argument
separator and ',' as the decimal marker, NOT the US/Excel ',' and '.'.
_to_de_locale_formula() below converts any formula Jarvis writes.
"""

import asyncio
import json
import os
import re
import time

import instagram_tools

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
TOKEN_PATH = os.path.join(os.path.dirname(__file__), "sheets_token.json")

SPREADSHEET_ID = "1VvQpaYSUF668MQjr-w7VYwiThQozXmR3zdruw5C_V08"
TARGET_CREATOR_TAB = "Target Creator List"
WINNER_TRACKING_TAB = "Winner Tracking"
SCALING_LOG_TAB = "Scaling Log"
TRIAL_REEL_TAB = "Trial Reel Waves"
INSIGHTS_INBOX_TAB = "Insights Eingang"
LINK_FUNNEL_TAB = "Link Funnel"
DAILY_PRODUCTION_TAB = "Daily Production List"


def _get_credentials() -> Credentials:
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        with open(CONFIG_PATH) as f:
            config = json.load(f)
        client_config = {
            "installed": {
                "client_id": config["google_client_id"],
                "client_secret": config["google_client_secret"],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        }
        flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
        creds = flow.run_local_server(port=0)

    with open(TOKEN_PATH, "w") as f:
        f.write(creds.to_json())
    return creds


def _get_service():
    creds = _get_credentials()
    return build("sheets", "v4", credentials=creds)


# The Sheets HTTP connection has a 60s socket timeout and, by default, NO
# retries — so one transient network hiccup killed a whole background pass
# (2026-08-08 21:49: "FEHLER bei Insights-Eingang: timeout: The read
# operation timed out", a single blip that was gone on the next tick).
# googleapiclient's execute(num_retries=) retries exactly this class of
# failure (socket.timeout / SSL / connection errors) with randomized
# exponential backoff.
#
# Deliberately NOT applied to the three non-idempotent calls below —
# deleteDimension, values().append(INSERT_ROWS) and addSheet. If the first
# attempt actually reached Google and only the RESPONSE read timed out, a
# retry would delete extra rows / append a duplicate row. A failed pass that
# simply repeats on the next tick is much cheaper than corrupted data.
SHEETS_NUM_RETRIES = 3


def _col_letter(n: int) -> str:
    """1-indexed column number -> A1-notation letter(s)."""
    letters = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _to_de_locale_formula(formula: str) -> str:
    """German Sheets locale needs ';' as the function-argument separator
    and ',' as the decimal marker — converts a US/Excel-style formula,
    respecting quoted string literals (a comma or '.' inside "..." must
    NOT be touched)."""
    if not formula or not formula.startswith("="):
        return formula
    result = []
    in_quotes = False
    i, n = 0, len(formula)
    while i < n:
        ch = formula[i]
        if ch == '"':
            in_quotes = not in_quotes
            result.append(ch)
            i += 1
        elif not in_quotes and ch.isdigit():
            j = i
            while j < n and (formula[j].isdigit() or formula[j] == "."):
                j += 1
            result.append(formula[i:j].replace(".", ","))
            i = j
        elif ch == "," and not in_quotes:
            result.append(";")
            i += 1
        else:
            result.append(ch)
            i += 1
    return "".join(result)


def _shift_formula_row(formula: str, from_row: int, to_row: int) -> str:
    """Shift same-row cell references (e.g. 'D2' -> 'D57') when copying a
    formula to a new row — WITHOUT touching numeric literals that happen to
    contain the same digits (e.g. the '2' inside '0.2'). A naive string
    replace of the row number would corrupt those literals; this only
    matches a column-letter run immediately followed by the row number."""
    if not formula or not formula.startswith("="):
        return formula
    pattern = re.compile(rf'([A-Z]+){from_row}(?!\d)')
    return pattern.sub(rf'\g<1>{to_row}', formula)


def _find_column(header: list, *name_fragments) -> int:
    """0-indexed column matching a header fragment, or None. Header cells
    often contain '\\n' (wrapped titles), so matching is deliberately loose."""
    for i, h in enumerate(header):
        if h and any(frag.lower() in str(h).lower() for frag in name_fragments):
            return i
    return None


def _cell(row: list, idx: int):
    """Sheets API omits trailing empty cells, so a row list can be shorter
    than the header — safe indexed access instead of an IndexError."""
    if idx is None or idx >= len(row):
        return None
    return row[idx] if row[idx] != "" else None


def _read_tab_sync(tab: str) -> list:
    """Default render: COMPUTED values (e.g. Decision shows 'KEEP', not the
    formula) — what every read-for-display caller wants."""
    service = _get_service()
    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, range=f"'{tab}'!A1:Z2000"
    ).execute(num_retries=SHEETS_NUM_RETRIES)
    return result.get("values", [])


def _read_tab_formulas_sync(tab: str) -> list:
    """Formula render: raw '=IFERROR(...)' text instead of computed
    results — needed specifically when copying a formula pattern to a new
    row. Using the default (computed-value) read here would silently see
    e.g. '' or 'KEEP' instead of the formula and skip copying entirely —
    exactly the bug that shipped first (caught by testing, not guessing)."""
    service = _get_service()
    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, range=f"'{tab}'!A1:Z2000", valueRenderOption="FORMULA"
    ).execute(num_retries=SHEETS_NUM_RETRIES)
    return result.get("values", [])


async def read_tab(tab: str) -> list:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _read_tab_sync, tab)


def _write_row_sync(tab: str, row_idx_1based: int, values: dict):
    """values: {0-indexed column: cell value}. Writes only the given
    columns, each as its own targeted range — never touches neighboring
    cells (including formula columns not mentioned)."""
    service = _get_service()
    data = [
        {"range": f"'{tab}'!{_col_letter(col + 1)}{row_idx_1based}", "values": [[val]]}
        for col, val in values.items()
    ]
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={"valueInputOption": "USER_ENTERED", "data": data},
    ).execute(num_retries=SHEETS_NUM_RETRIES)


def _append_row_sync(tab: str, row_idx_1based: int, values: dict, num_cols: int):
    """Write a full new row at a specific row index (values dict as above)."""
    full_row = [""] * num_cols
    for col, val in values.items():
        full_row[col] = val
    service = _get_service()
    range_str = f"'{tab}'!A{row_idx_1based}:{_col_letter(num_cols)}{row_idx_1based}"
    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID, range=range_str,
        valueInputOption="USER_ENTERED", body={"values": [full_row]},
    ).execute(num_retries=SHEETS_NUM_RETRIES)


def _get_sheet_id_sync(tab: str) -> int:
    """The values() API (used everywhere above) addresses tabs by name — but
    actually DELETING a row needs the tab's internal numeric sheetId, which
    only the metadata API exposes. No caller needed this before add-only
    functions existed, hence it living here rather than next to _get_service."""
    service = _get_service()
    meta = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute(num_retries=SHEETS_NUM_RETRIES)
    for s in meta["sheets"]:
        if s["properties"]["title"] == tab:
            return s["properties"]["sheetId"]
    raise ValueError(f"Tab '{tab}' nicht gefunden.")


def _delete_rows_sync(tab: str, row_indices_1based: list):
    """Deletes one or more rows by their 1-indexed row number (same indexing
    as everywhere else in this file). Deletes from the BOTTOM up — deleting
    top-to-bottom would shift every later row's index out from under the
    next deleteDimension call in the same pass."""
    if not row_indices_1based:
        return
    sheet_id = _get_sheet_id_sync(tab)
    service = _get_service()
    requests = [
        {"deleteDimension": {"range": {
            "sheetId": sheet_id, "dimension": "ROWS",
            "startIndex": idx - 1, "endIndex": idx,
        }}}
        for idx in sorted(row_indices_1based, reverse=True)
    ]
    service.spreadsheets().batchUpdate(spreadsheetId=SPREADSHEET_ID, body={"requests": requests}).execute()


# ---------------------------------------------------------------------------
# Target Creator List
# ---------------------------------------------------------------------------

def _sync_competitor_accounts_sync(handles: list, niches: dict = None) -> str:
    """niches: optional {handle: niche_label} — without it (or for a handle
    missing from it), falls back to 'Goth/alternative' since that's still
    true for most already-tracked competitors. Was unconditional before
    (2026-08-06 fix) — every new competitor got mislabeled Goth/alternative
    regardless of which niche search actually found them."""
    niches = niches or {}
    rows = _read_tab_sync(TARGET_CREATOR_TAB)
    if not rows:
        return f"ERROR: Tab '{TARGET_CREATOR_TAB}' konnte nicht gelesen werden."
    header = rows[0]
    col_handle = _find_column(header, "account handle", "handle") or 0
    col_niche = _find_column(header, "niche")
    col_notes = _find_column(header, "notes")

    existing = {str(_cell(r, col_handle)).strip().lower().lstrip("@") for r in rows[1:] if _cell(r, col_handle)}
    new_handles = [h.strip().lower().lstrip("@") for h in handles if h.strip().lower().lstrip("@") not in existing]
    new_handles = list(dict.fromkeys(new_handles))  # dedupe, keep order

    if not new_handles:
        return "Alle Konkurrenz-Accounts sind bereits in der Target Creator List."

    last_row = len(rows)  # 1-indexed: rows list includes header at index 0
    service = _get_service()
    new_rows = []
    for clean in new_handles:
        row = [""] * len(header)
        row[col_handle] = f"@{clean}"
        if col_niche is not None:
            row[col_niche] = niches.get(clean, "Goth/alternative")
        if col_notes is not None:
            row[col_notes] = f"Auto-discovered by Jarvis, {time.strftime('%Y-%m-%d')}"
        new_rows.append(row)

    service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID, range=f"'{TARGET_CREATOR_TAB}'!A{last_row}",
        valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS",
        body={"values": new_rows},
    ).execute()
    return f"{len(new_handles)} neue(r) Account(s) ({', '.join('@' + h for h in new_handles)}) in die Target Creator List eingetragen."


async def sync_competitor_accounts(handles: list, niches: dict = None) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_competitor_accounts_sync, handles, niches)


def _add_target_creator_note_sync(handle: str, note: str) -> str:
    rows = _read_tab_sync(TARGET_CREATOR_TAB)
    if not rows:
        return f"ERROR: Tab '{TARGET_CREATOR_TAB}' konnte nicht gelesen werden."
    header = rows[0]
    col_handle = _find_column(header, "account handle", "handle") or 0
    col_notes = _find_column(header, "notes")
    if col_notes is None:
        return "ERROR: Spalte 'Notes' nicht gefunden."

    clean = handle.strip().lower().lstrip("@")
    for i, row in enumerate(rows[1:], start=2):
        val = _cell(row, col_handle)
        if val and str(val).strip().lower().lstrip("@") == clean:
            existing = _cell(row, col_notes)
            combined = f"{existing} | {note}" if existing else note
            _write_row_sync(TARGET_CREATOR_TAB, i, {col_notes: combined})
            return f"Notiz fuer @{clean} in Target Creator List ergaenzt."

    return f"ERROR: @{clean} nicht in Target Creator List gefunden."


async def add_target_creator_note(handle: str, note: str) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _add_target_creator_note_sync, handle, note)


def _delete_target_creator_row_sync(handle: str) -> str:
    """Cleanup counterpart to sync_competitor_accounts — that function is
    append-only (2026-08-07 design choice, dedupes against existing rows but
    never removes any), so a mistakenly-added or test account had no way
    back out of the sheet until this existed."""
    rows = _read_tab_sync(TARGET_CREATOR_TAB)
    if not rows:
        return f"ERROR: Tab '{TARGET_CREATOR_TAB}' konnte nicht gelesen werden."
    header = rows[0]
    col_handle = _find_column(header, "account handle", "handle") or 0
    clean = handle.strip().lower().lstrip("@")
    matches = [
        i for i, row in enumerate(rows[1:], start=2)
        if _cell(row, col_handle) and str(_cell(row, col_handle)).strip().lower().lstrip("@") == clean
    ]
    if not matches:
        return f"ERROR: @{clean} nicht in Target Creator List gefunden."
    _delete_rows_sync(TARGET_CREATOR_TAB, matches)
    return f"{len(matches)} Zeile(n) fuer @{clean} aus Target Creator List geloescht."


async def delete_target_creator_row(handle: str) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _delete_target_creator_row_sync, handle)


# ---------------------------------------------------------------------------
# Winner Tracking
# ---------------------------------------------------------------------------

def _add_winner_tracking_entry_sync(entry: dict) -> str:
    """entry: {account, video_link, views, comments_total?, baseline_avg?, us_audience_pct?}.
    Copies the formula pattern (Multiplier/Outlier/Comment Quality/Decision)
    from the row above, DE-locale-corrected, translated to the new row —
    never computes those values itself."""
    rows = _read_tab_sync(WINNER_TRACKING_TAB)
    if not rows:
        return f"ERROR: Tab '{WINNER_TRACKING_TAB}' konnte nicht gelesen werden."
    header = rows[0]

    col_date = _find_column(header, "date")
    col_account = _find_column(header, "account")
    col_link = _find_column(header, "video link")
    col_views = _find_column(header, "views")
    col_baseline = _find_column(header, "baseline")
    col_multiplier = _find_column(header, "multiplier")
    col_outlier = _find_column(header, "outlier")
    col_comments_total = _find_column(header, "comments\ntotal", "comments total")
    col_comment_quality = _find_column(header, "quality")
    col_us_audience = _find_column(header, "us audience")
    col_decision = _find_column(header, "decision")

    if any(c is None for c in (col_date, col_account, col_link, col_views)):
        return "ERROR: Erwartete Spalten (Date/Account/Video Link/Views) nicht gefunden."

    # Scan for the actual last row with a Video Link, not just len(rows) —
    # a stray empty row (manual edit, leftover gap) would otherwise get
    # mistaken for real data and used as the formula template.
    last_row = 1
    for i, row in enumerate(rows[1:], start=2):
        if _cell(row, col_link):
            last_row = i
    template_row_idx = last_row if last_row >= 2 else 2
    new_row_1based = last_row + 1
    num_cols = len(header)

    values = {col_date: time.strftime("%Y-%m-%d"), col_account: entry["account"],
              col_link: entry["video_link"], col_views: entry["views"]}
    if entry.get("baseline_avg") is not None and col_baseline is not None:
        values[col_baseline] = entry["baseline_avg"]
    if entry.get("comments_total") is not None and col_comments_total is not None:
        values[col_comments_total] = entry["comments_total"]
    if entry.get("us_audience_pct") is not None and col_us_audience is not None:
        values[col_us_audience] = entry["us_audience_pct"]

    # Copy formulas from the template row (last existing data row, or row 2
    # if this is the very first entry) — MUST read with valueRenderOption=
    # FORMULA, the default read returns computed results (e.g. 'KEEP'),
    # which don't start with '=' and would silently skip every column.
    formula_rows = _read_tab_formulas_sync(WINNER_TRACKING_TAB)
    template_row = formula_rows[template_row_idx - 1] if template_row_idx - 1 < len(formula_rows) else []
    for col in (col_multiplier, col_outlier, col_comment_quality, col_decision):
        if col is None:
            continue
        template_formula = _cell(template_row, col)
        if template_formula and str(template_formula).startswith("="):
            # Template read straight from the sheet is ALREADY DE-locale
            # (every row got there either via the one-time migration fix or
            # via this same code path) — only the row number needs to
            # shift. Re-running _to_de_locale_formula here would corrupt
            # the decimal commas it already has (mistaking '0,2' for an
            # argument separator and turning it into '0;2') — caught by
            # testing, not obvious from reading the code.
            values[col] = _shift_formula_row(str(template_formula), template_row_idx, new_row_1based)

    _append_row_sync(WINNER_TRACKING_TAB, new_row_1based, values, num_cols)

    missing = []
    if entry.get("baseline_avg") is None:
        missing.append("Baseline Avg")
    if entry.get("us_audience_pct") is None:
        missing.append("US Audience % (aus Instagram Insights)")
    note = f" Fehlt noch: {', '.join(missing)}." if missing else ""
    return f"Neuer Eintrag fuer @{entry['account']} in Winner Tracking (Zeile {new_row_1based}).{note}"


async def add_winner_tracking_entry(entry: dict) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _add_winner_tracking_entry_sync, entry)


def _update_winner_tracking_insights_sync(video_link: str, fields: dict) -> str:
    rows = _read_tab_sync(WINNER_TRACKING_TAB)
    if not rows:
        return f"ERROR: Tab '{WINNER_TRACKING_TAB}' konnte nicht gelesen werden."
    header = rows[0]
    col_link = _find_column(header, "video link")
    col_views = _find_column(header, "views")
    col_us_audience = _find_column(header, "us audience")
    col_baseline = _find_column(header, "baseline")
    col_comments_total = _find_column(header, "comments\ntotal", "comments total")
    col_notes = _find_column(header, "notes")

    target_id = instagram_tools.normalize_video_url(video_link)
    for i, row in enumerate(rows[1:], start=2):
        link = _cell(row, col_link)
        if link and instagram_tools.normalize_video_url(str(link)) == target_id:
            values = {}
            if fields.get("views") is not None and col_views is not None:
                values[col_views] = fields["views"]
            if fields.get("us_audience_pct") is not None and col_us_audience is not None:
                values[col_us_audience] = fields["us_audience_pct"]
            if fields.get("baseline_avg") is not None and col_baseline is not None:
                values[col_baseline] = fields["baseline_avg"]
            if fields.get("comments_total") is not None and col_comments_total is not None:
                values[col_comments_total] = fields["comments_total"]
            if fields.get("notes") and col_notes is not None:
                existing = _cell(row, col_notes)
                values[col_notes] = f"{existing} | {fields['notes']}" if existing else fields["notes"]
            if not values:
                return "Keine Insights-Felder zum Eintragen uebergeben."
            _write_row_sync(WINNER_TRACKING_TAB, i, values)
            return f"Insights fuer Zeile {i} eingetragen (US Audience: {fields.get('us_audience_pct', '?')})."

    return f"ERROR: Kein Eintrag mit Link '{video_link}' in Winner Tracking gefunden — erst mit WINNERTRACK anlegen."


async def update_winner_tracking_insights(video_link: str, fields: dict) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _update_winner_tracking_insights_sync, video_link, fields)


def _compute_baseline_avg_sync(account: str, video_link: str) -> str:
    rows = _read_tab_sync(WINNER_TRACKING_TAB)
    if not rows:
        return f"ERROR: Tab '{WINNER_TRACKING_TAB}' konnte nicht gelesen werden."
    header = rows[0]
    col_account = _find_column(header, "account")
    col_link = _find_column(header, "video link")
    col_views = _find_column(header, "views")
    col_baseline = _find_column(header, "baseline")

    target_id = instagram_tools.normalize_video_url(video_link)
    account_rows = []  # (1based_row_idx, views)
    target_idx = None
    for i, row in enumerate(rows[1:], start=2):
        acc = _cell(row, col_account)
        link = _cell(row, col_link)
        if not link or not acc or str(acc).strip().lower() != account.strip().lower():
            continue
        views_raw = _cell(row, col_views)
        try:
            views = float(str(views_raw).replace(",", ".")) if views_raw is not None else None
        except ValueError:
            views = None
        account_rows.append((i, views))
        if instagram_tools.normalize_video_url(str(link)) == target_id:
            target_idx = len(account_rows) - 1

    if target_idx is None:
        return f"ERROR: Video nicht in Winner Tracking fuer @{account} gefunden."

    before = [v for _, v in account_rows[max(0, target_idx - 6):target_idx] if v is not None]
    after = [v for _, v in account_rows[target_idx + 1:target_idx + 7] if v is not None]
    sample = before + after

    if len(sample) < 2:
        return f"Zu wenig Vergleichsdaten fuer @{account} ({len(sample)} Video(s)) — Baseline noch nicht gesetzt."

    baseline_avg = round(sum(sample) / len(sample))
    target_row = account_rows[target_idx][0]
    _write_row_sync(WINNER_TRACKING_TAB, target_row, {col_baseline: baseline_avg})
    return f"Baseline Avg fuer @{account} auf {baseline_avg} gesetzt ({len(sample)} Vergleichsvideos, Zeile {target_row})."


async def compute_baseline_avg(account: str, video_link: str) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _compute_baseline_avg_sync, account, video_link)


def _compute_account_avg_views_sync(account: str, sample_size: int = 6):
    """Plain average of the account's most recent Winner Tracking views —
    the comparison baseline for a Trial Reel Wave result, same 2x-bar
    concept as Winner Tracking's own Outlier rule. None if there's not
    enough data yet (never fabricate a baseline to force a comparison)."""
    rows = _read_tab_sync(WINNER_TRACKING_TAB)
    if not rows:
        return None
    header = rows[0]
    col_account = _find_column(header, "account")
    col_views = _find_column(header, "views")
    views = []
    for row in rows[1:]:
        acc = _cell(row, col_account)
        if not acc or str(acc).strip().lower() != account.strip().lower():
            continue
        raw = _cell(row, col_views)
        if raw is None:
            continue
        try:
            views.append(float(str(raw).replace(".", "").replace(",", "")))
        except ValueError:
            continue
    if not views:
        return None
    recent = views[-sample_size:]
    return round(sum(recent) / len(recent))


async def compute_account_avg_views(account: str, sample_size: int = 6):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _compute_account_avg_views_sync, account, sample_size)


def _read_winner_tracking_sync(account: str = None, limit: int = 10) -> list:
    rows = _read_tab_sync(WINNER_TRACKING_TAB)
    if not rows:
        return [{"error": f"Tab '{WINNER_TRACKING_TAB}' konnte nicht gelesen werden."}]
    header = rows[0]
    col_account = _find_column(header, "account")
    col_link = _find_column(header, "video link")
    col_views = _find_column(header, "views")
    col_outlier = _find_column(header, "outlier")
    col_decision = _find_column(header, "decision")
    col_us_audience = _find_column(header, "us audience")

    result = []
    for row in rows[1:]:
        link = _cell(row, col_link)
        if not link:
            continue
        acc = _cell(row, col_account)
        if account and (not acc or account.lower() not in str(acc).lower()):
            continue
        result.append({
            "account": acc, "video_link": link,
            "views": _cell(row, col_views), "outlier": _cell(row, col_outlier),
            "decision": _cell(row, col_decision), "us_audience_pct": _cell(row, col_us_audience),
        })
    return result[-limit:]


async def read_winner_tracking(account: str = None, limit: int = 10) -> list:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _read_winner_tracking_sync, account, limit)


def _delete_winner_tracking_entry_sync(video_link: str) -> str:
    rows = _read_tab_sync(WINNER_TRACKING_TAB)
    if not rows:
        return f"ERROR: Tab '{WINNER_TRACKING_TAB}' konnte nicht gelesen werden."
    header = rows[0]
    col_link = _find_column(header, "video link")
    target_id = instagram_tools.normalize_video_url(video_link)
    matches = [
        i for i, row in enumerate(rows[1:], start=2)
        if _cell(row, col_link) and instagram_tools.normalize_video_url(str(_cell(row, col_link))) == target_id
    ]
    if not matches:
        return f"ERROR: Kein Eintrag mit Link '{video_link}' in Winner Tracking gefunden."
    _delete_rows_sync(WINNER_TRACKING_TAB, matches)
    return f"{len(matches)} Zeile(n) fuer '{video_link}' aus Winner Tracking geloescht."


async def delete_winner_tracking_entry(video_link: str) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _delete_winner_tracking_entry_sync, video_link)


# ---------------------------------------------------------------------------
# Scaling Log — plain columns, no formulas, straightforward append
# ---------------------------------------------------------------------------

def _add_scaling_log_entry_sync(entry: dict) -> str:
    rows = _read_tab_sync(SCALING_LOG_TAB)
    if not rows:
        return f"ERROR: Tab '{SCALING_LOG_TAB}' konnte nicht gelesen werden."
    header = rows[0]
    col_date = _find_column(header, "date")
    col_account = _find_column(header, "account")
    col_link = _find_column(header, "original winner", "winner")
    col_factor = _find_column(header, "virality factor", "identified virality")
    col_variations = _find_column(header, "number of new variations", "variations")
    col_next_step = _find_column(header, "next step")

    if any(c is None for c in (col_date, col_account, col_link, col_factor)):
        return "ERROR: Erwartete Spalten in Scaling Log nicht gefunden."

    last_row = 1
    for i, row in enumerate(rows[1:], start=2):
        if _cell(row, col_link):
            last_row = i
    new_row_1based = last_row + 1
    values = {col_date: time.strftime("%Y-%m-%d"), col_account: entry["account"],
              col_link: entry["original_winner_link"], col_factor: entry["virality_factor"]}
    if col_variations is not None:
        values[col_variations] = entry.get("num_variations", 1)
    if col_next_step is not None and entry.get("next_step"):
        values[col_next_step] = entry["next_step"]

    _append_row_sync(SCALING_LOG_TAB, new_row_1based, values, len(header))
    return f"Scaling-Log-Eintrag fuer @{entry['account']} angelegt (Zeile {new_row_1based}, Faktor: {entry['virality_factor']})."


async def add_scaling_log_entry(entry: dict) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _add_scaling_log_entry_sync, entry)


def _delete_scaling_log_entry_sync(video_link: str) -> str:
    rows = _read_tab_sync(SCALING_LOG_TAB)
    if not rows:
        return f"ERROR: Tab '{SCALING_LOG_TAB}' konnte nicht gelesen werden."
    header = rows[0]
    col_link = _find_column(header, "original winner", "winner")
    target_id = instagram_tools.normalize_video_url(video_link)
    matches = [
        i for i, row in enumerate(rows[1:], start=2)
        if _cell(row, col_link) and instagram_tools.normalize_video_url(str(_cell(row, col_link))) == target_id
    ]
    if not matches:
        return f"ERROR: Kein Eintrag mit Link '{video_link}' in Scaling Log gefunden."
    _delete_rows_sync(SCALING_LOG_TAB, matches)
    return f"{len(matches)} Zeile(n) fuer '{video_link}' aus Scaling Log geloescht."


async def delete_scaling_log_entry(video_link: str) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _delete_scaling_log_entry_sync, video_link)


# ---------------------------------------------------------------------------
# Trial Reel Waves — closes the loop Ahmad described (2026-08-06): a wave
# gets created the moment Jerome is briefed (variable/source known, result
# not yet), then UPDATED later once Ahmad reports the posted video's real
# numbers back through the self-chat (same flow as Winner Tracking Insights).
# ---------------------------------------------------------------------------

def _add_trial_reel_wave_sync(entry: dict) -> str:
    """entry: {account, wave, raw_file_source, changed_variable, kept_unchanged}.
    Creates the row with what's known AT BRIEFING TIME — video_link/views/
    hit_2x are filled in later by _update_trial_reel_wave_result_sync once
    Jerome has actually posted it and Ahmad reports back."""
    rows = _read_tab_sync(TRIAL_REEL_TAB)
    if not rows:
        return f"ERROR: Tab '{TRIAL_REEL_TAB}' konnte nicht gelesen werden."
    header = rows[0]
    col_date = _find_column(header, "date")
    col_account = _find_column(header, "account")
    col_wave = _find_column(header, "wave")
    col_source = _find_column(header, "raw file source", "raw file")
    col_changed = _find_column(header, "changed variable")
    col_kept = _find_column(header, "kept unchanged")

    if any(c is None for c in (col_date, col_account, col_wave, col_changed)):
        return "ERROR: Erwartete Spalten in Trial Reel Waves nicht gefunden."

    last_row = 1
    for i, row in enumerate(rows[1:], start=2):
        if _cell(row, col_account):
            last_row = i
    new_row_1based = last_row + 1

    values = {
        col_date: time.strftime("%Y-%m-%d"), col_account: entry["account"],
        col_wave: entry["wave"], col_changed: entry["changed_variable"],
    }
    if col_source is not None and entry.get("raw_file_source"):
        values[col_source] = entry["raw_file_source"]
    if col_kept is not None and entry.get("kept_unchanged"):
        values[col_kept] = entry["kept_unchanged"]

    _append_row_sync(TRIAL_REEL_TAB, new_row_1based, values, len(header))
    return f"Trial-Reel-Welle {entry['wave']} fuer @{entry['account']} angelegt (Zeile {new_row_1based})."


async def add_trial_reel_wave(entry: dict) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _add_trial_reel_wave_sync, entry)


def _read_trial_reel_waves_sync(account: str = None) -> list:
    rows = _read_tab_sync(TRIAL_REEL_TAB)
    if not rows:
        return []
    header = rows[0]
    col_date = _find_column(header, "date")
    col_account = _find_column(header, "account")
    col_wave = _find_column(header, "wave")
    col_source = _find_column(header, "raw file source", "raw file")
    col_changed = _find_column(header, "changed variable")
    col_kept = _find_column(header, "kept unchanged")
    col_link = _find_column(header, "video link")
    col_views = _find_column(header, "views after")
    col_avg = _find_column(header, "account avg")
    col_hit2x = _find_column(header, "hit 2x")
    col_confirmed = _find_column(header, "confirmed")
    col_next = _find_column(header, "next action")

    result = []
    for i, row in enumerate(rows[1:], start=2):
        acc = _cell(row, col_account)
        if not acc:
            continue
        if account and str(acc).strip().lower() != account.strip().lower():
            continue
        result.append({
            "row": i, "date": _cell(row, col_date), "account": acc, "wave": _cell(row, col_wave),
            "raw_file_source": _cell(row, col_source), "changed_variable": _cell(row, col_changed),
            "kept_unchanged": _cell(row, col_kept), "video_link": _cell(row, col_link),
            "views_after_3h": _cell(row, col_views), "account_avg": _cell(row, col_avg),
            "hit_2x": _cell(row, col_hit2x), "confirmed": _cell(row, col_confirmed),
            "next_action": _cell(row, col_next),
        })
    return result


async def read_trial_reel_waves(account: str = None) -> list:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _read_trial_reel_waves_sync, account)


async def read_open_trial_waves(account: str = None) -> list:
    """Waves that were briefed to Jerome but have no reported result yet —
    exactly what a caller needs to (a) decide whether to nudge Ahmad for an
    update, or (b) recognize an incoming self-chat link as a WAVE RESULT
    instead of a brand-new Winner Tracking entry."""
    waves = await read_trial_reel_waves(account)
    return [w for w in waves if not w["video_link"]]


def _update_trial_reel_wave_result_sync(
    account: str, wave, video_link: str, views_after_3h, account_avg, hit_2x: bool, next_action: str
) -> str:
    rows = _read_tab_sync(TRIAL_REEL_TAB)
    if not rows:
        return f"ERROR: Tab '{TRIAL_REEL_TAB}' konnte nicht gelesen werden."
    header = rows[0]
    col_account = _find_column(header, "account")
    col_wave = _find_column(header, "wave")
    col_link = _find_column(header, "video link")
    col_views = _find_column(header, "views after")
    col_avg = _find_column(header, "account avg")
    col_hit2x = _find_column(header, "hit 2x")
    col_next = _find_column(header, "next action")

    for i, row in enumerate(rows[1:], start=2):
        acc = _cell(row, col_account)
        row_wave = _cell(row, col_wave)
        if not acc or str(acc).strip().lower() != account.strip().lower():
            continue
        if str(row_wave).strip() != str(wave).strip():
            continue
        if _cell(row, col_link):
            continue  # this wave already has a result — not the one we're looking for
        values = {col_link: video_link, col_views: views_after_3h, col_avg: account_avg,
                  col_hit2x: "YES" if hit_2x else "NO"}
        if col_next is not None and next_action:
            values[col_next] = next_action
        _write_row_sync(TRIAL_REEL_TAB, i, values)
        return f"Wave {wave} fuer @{account} aktualisiert (Zeile {i}): Hit 2x={'YES' if hit_2x else 'NO'}."

    return f"ERROR: Keine offene Welle {wave} fuer @{account} gefunden."


async def update_trial_reel_wave_result(
    account: str, wave, video_link: str, views_after_3h, account_avg, hit_2x: bool, next_action: str = ""
) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, _update_trial_reel_wave_result_sync,
        account, wave, video_link, views_after_3h, account_avg, hit_2x, next_action,
    )


def _delete_trial_reel_wave_sync(account: str, wave) -> str:
    """Matches by account+wave (not video_link) — a freshly-briefed wave has
    no video_link yet (see add_trial_reel_wave's docstring), so link-based
    matching alone would miss exactly the rows a failed/test briefing left behind."""
    rows = _read_tab_sync(TRIAL_REEL_TAB)
    if not rows:
        return f"ERROR: Tab '{TRIAL_REEL_TAB}' konnte nicht gelesen werden."
    header = rows[0]
    col_account = _find_column(header, "account")
    col_wave = _find_column(header, "wave")
    matches = [
        i for i, row in enumerate(rows[1:], start=2)
        if _cell(row, col_account) and str(_cell(row, col_account)).strip().lower() == account.strip().lower()
        and str(_cell(row, col_wave)).strip() == str(wave).strip()
    ]
    if not matches:
        return f"ERROR: Keine Welle {wave} fuer @{account} in Trial Reel Waves gefunden."
    _delete_rows_sync(TRIAL_REEL_TAB, matches)
    return f"{len(matches)} Zeile(n) fuer @{account} Welle {wave} aus Trial Reel Waves geloescht."


async def delete_trial_reel_wave(account: str, wave) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _delete_trial_reel_wave_sync, account, wave)


# ---------------------------------------------------------------------------
# Insights Eingang — Ahmad's replacement for the WhatsApp-screenshot workflow
# (2026-08-07): reading numbers off a phone-screenshot via Vision proved
# unreliable in production (URLs invisible in WhatsApp's link-preview cards,
# cross-attribution between adjacent sequences, fragile UI scrolling). A
# plain Sheet row is typed text, not a picture — nothing to misread, and it
# reuses the same Sheets API that's been rock-solid all session. Ahmad pastes
# the link + types the 2-3 numbers off the same screenshot he was already
# looking at; a background pass (background_brain.py) picks up new rows
# automatically, no live "ich hab's geschickt" trigger needed at all.
# ---------------------------------------------------------------------------

INSIGHTS_INBOX_HEADER = ["Link", "US Audience %", "Reach", "Views", "Status"]


def _ensure_insights_inbox_tab_sync():
    service = _get_service()
    meta = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute(num_retries=SHEETS_NUM_RETRIES)
    titles = [s["properties"]["title"] for s in meta["sheets"]]
    if INSIGHTS_INBOX_TAB not in titles:
        service.spreadsheets().batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={"requests": [{"addSheet": {"properties": {"title": INSIGHTS_INBOX_TAB}}}]},
        ).execute()
        service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID, range=f"'{INSIGHTS_INBOX_TAB}'!A1:E1",
            valueInputOption="USER_ENTERED", body={"values": [INSIGHTS_INBOX_HEADER]},
        ).execute(num_retries=SHEETS_NUM_RETRIES)


async def ensure_insights_inbox_tab():
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _ensure_insights_inbox_tab_sync)


def _read_pending_insights_inbox_sync() -> list:
    """Rows with a Link but no Status yet — Status gets written back once a
    row is handled, so a re-run never reprocesses the same row twice."""
    rows = _read_tab_sync(INSIGHTS_INBOX_TAB)
    if not rows:
        return []
    header = rows[0]
    col_link = _find_column(header, "link")
    col_us = _find_column(header, "us audience")
    col_reach = _find_column(header, "reach")
    col_views = _find_column(header, "views")
    col_status = _find_column(header, "status")

    pending = []
    for i, row in enumerate(rows[1:], start=2):
        link = _cell(row, col_link)
        if not link or (col_status is not None and _cell(row, col_status)):
            continue
        pending.append({
            "row": i, "link": str(link).strip(),
            "us_audience_raw": _cell(row, col_us), "reach_raw": _cell(row, col_reach),
            "views_raw": _cell(row, col_views),
        })
    return pending


async def read_pending_insights_inbox() -> list:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _read_pending_insights_inbox_sync)


def _mark_insights_inbox_row_sync(row_idx_1based: int, status: str):
    rows = _read_tab_sync(INSIGHTS_INBOX_TAB)
    header = rows[0] if rows else INSIGHTS_INBOX_HEADER
    col_status = _find_column(header, "status")
    if col_status is None:
        col_status = 4  # fixed fallback: the 'Status' column as defined in INSIGHTS_INBOX_HEADER
    _write_row_sync(INSIGHTS_INBOX_TAB, row_idx_1based, {col_status: status})


async def mark_insights_inbox_row(row_idx_1based: int, status: str):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _mark_insights_inbox_row_sync, row_idx_1based, status)


# ---------------------------------------------------------------------------
# Link Funnel — Ahmad enters Week/Account/Profile Visits himself every
# Sunday (Instagram's own Profile-Visits stat isn't readable via any API
# Jarvis has); everything else (Link Clicks, the two ratio columns, New
# Subs) is computable/fetchable and filled in automatically.
# ---------------------------------------------------------------------------

def _read_pending_link_funnel_sync() -> list:
    rows = _read_tab_sync(LINK_FUNNEL_TAB)
    if not rows:
        return []
    header = rows[0]
    col_week = _find_column(header, "week")
    col_account = _find_column(header, "account")
    col_visits = _find_column(header, "profile visits")
    col_clicks = _find_column(header, "link clicks")

    pending = []
    for i, row in enumerate(rows[1:], start=2):
        week, account, visits = _cell(row, col_week), _cell(row, col_account), _cell(row, col_visits)
        if not week or not account or not visits:
            continue
        if _cell(row, col_clicks):
            continue  # already filled in
        pending.append({"row": i, "week": str(week).strip(), "account": str(account).strip(), "profile_visits": visits})
    return pending


async def read_pending_link_funnel() -> list:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _read_pending_link_funnel_sync)


def _write_link_funnel_row_sync(row_idx_1based: int, values: dict):
    """values keys (all optional): link_clicks, click_rate_pct, benchmark_diff, new_subs, conversion_pct."""
    rows = _read_tab_sync(LINK_FUNNEL_TAB)
    header = rows[0] if rows else []
    col_map = {
        "link_clicks": _find_column(header, "link clicks"),
        "click_rate_pct": _find_column(header, "click rate"),
        "benchmark_diff": _find_column(header, "benchmark"),
        "new_subs": _find_column(header, "new subs"),
        "conversion_pct": _find_column(header, "conversion"),
    }
    cells = {col_map[key]: val for key, val in values.items() if val is not None and col_map.get(key) is not None}
    if cells:
        _write_row_sync(LINK_FUNNEL_TAB, row_idx_1based, cells)


async def write_link_funnel_row(row_idx_1based: int, values: dict):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _write_link_funnel_row_sync, row_idx_1based, values)


# ---------------------------------------------------------------------------
# Daily Production List — Jerome's structured per-video task list, filled in
# by content_strategy.py's daily brief pipeline alongside the WhatsApp
# message it already sends (2026-08-07: the tab sat completely empty since
# the brief only ever went out as prose text, never tracked here).
# ---------------------------------------------------------------------------

def _add_daily_production_row_sync(entry: dict) -> str:
    """entry keys: date, account, video_type, reference_link, hook_idea,
    outfit_setting, sound, instruction, caption. No formula columns here
    (unlike Winner Tracking) — plain append, no template-row copying needed."""
    rows = _read_tab_sync(DAILY_PRODUCTION_TAB)
    if not rows:
        return f"ERROR: Tab '{DAILY_PRODUCTION_TAB}' konnte nicht gelesen werden."
    header = rows[0]
    col_map = {
        "date": _find_column(header, "date"),
        "account": _find_column(header, "account"),
        "video_type": _find_column(header, "video type"),
        "reference_link": _find_column(header, "reference link"),
        "hook_idea": _find_column(header, "hook"),
        "outfit_setting": _find_column(header, "outfit"),
        "sound": _find_column(header, "sound"),
        "instruction": _find_column(header, "instruction"),
        "caption": _find_column(header, "caption"),
    }
    if col_map["reference_link"] is None:
        return "ERROR: Erwartete Spalte 'Reference Link' nicht gefunden."

    last_row = 1
    for i, row in enumerate(rows[1:], start=2):
        if _cell(row, col_map["reference_link"]):
            last_row = i
    new_row_1based = last_row + 1

    values = {col: entry[key] for key, col in col_map.items() if col is not None and entry.get(key)}
    _append_row_sync(DAILY_PRODUCTION_TAB, new_row_1based, values, len(header))
    return f"Eingetragen fuer @{entry.get('account')} in Daily Production List (Zeile {new_row_1based})."


async def add_daily_production_row(entry: dict) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _add_daily_production_row_sync, entry)


def _delete_daily_production_row_sync(reference_link: str) -> str:
    rows = _read_tab_sync(DAILY_PRODUCTION_TAB)
    if not rows:
        return f"ERROR: Tab '{DAILY_PRODUCTION_TAB}' konnte nicht gelesen werden."
    header = rows[0]
    col_link = _find_column(header, "reference link")
    target_id = instagram_tools.normalize_video_url(reference_link)
    matches = [
        i for i, row in enumerate(rows[1:], start=2)
        if _cell(row, col_link) and instagram_tools.normalize_video_url(str(_cell(row, col_link))) == target_id
    ]
    if not matches:
        return f"ERROR: Kein Eintrag mit Link '{reference_link}' in Daily Production List gefunden."
    _delete_rows_sync(DAILY_PRODUCTION_TAB, matches)
    return f"{len(matches)} Zeile(n) fuer '{reference_link}' aus Daily Production List geloescht."


async def delete_daily_production_row(reference_link: str) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _delete_daily_production_row_sync, reference_link)


# ---------------------------------------------------------------------------
# Target Creator List — accounts get auto-discovered (discovery_pass) with
# their analysis columns left empty; this fills them in with a real deep-
# analysis judgment instead of leaving them blank forever (2026-08-07).
# ---------------------------------------------------------------------------

def _read_target_creator_pending_sync() -> list:
    """Rows needing analysis — Overall Ranking still empty."""
    rows = _read_tab_sync(TARGET_CREATOR_TAB)
    if not rows:
        return []
    header = rows[0]
    col_handle = _find_column(header, "account handle", "handle")
    col_ranking = _find_column(header, "overall ranking")
    if col_handle is None:
        return []
    pending = []
    for i, row in enumerate(rows[1:], start=2):
        handle = _cell(row, col_handle)
        if not handle:
            continue
        if col_ranking is not None and _cell(row, col_ranking):
            continue  # already analyzed
        pending.append({"row": i, "handle": str(handle).lstrip("@").strip()})
    return pending


async def read_target_creator_pending() -> list:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _read_target_creator_pending_sync)


def _write_target_creator_row_sync(row_idx_1based: int, values: dict):
    """values keys (all optional): niche, body_match, trend, last_viral,
    hook_quality, comment_volume, overall_ranking, notes."""
    rows = _read_tab_sync(TARGET_CREATOR_TAB)
    header = rows[0] if rows else []
    col_map = {
        "niche": _find_column(header, "niche"),
        "body_match": _find_column(header, "body match"),
        "trend": _find_column(header, "trend"),
        "last_viral": _find_column(header, "last viral"),
        "hook_quality": _find_column(header, "hook quality"),
        "comment_volume": _find_column(header, "comment volume"),
        "overall_ranking": _find_column(header, "overall ranking"),
        "notes": _find_column(header, "notes"),
    }
    cells = {col_map[k]: v for k, v in values.items() if v is not None and col_map.get(k) is not None}
    if cells:
        _write_row_sync(TARGET_CREATOR_TAB, row_idx_1based, cells)


async def write_target_creator_row(row_idx_1based: int, values: dict):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _write_target_creator_row_sync, row_idx_1based, values)
