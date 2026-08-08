"""
One-off: visual design pass over the native Tracking Sheet. Run once after
the sheets_tools.py rewrite was fully verified against live data (Ahmad's
explicit instruction: get functionality 100% right first, THEN make it look
good). Colored headers per tab, zebra striping, frozen header/ID columns,
sensible column widths, number/percent formats, and color-coded conditional
formatting on the columns that actually drive decisions (Decision, Outlier,
Status, Trend) so the sheet is readable at a glance, not just correct.
"""

import asyncio

import sheets_tools

# Accent color per tab — also becomes the tab color for quick navigation.
TAB_CONFIG = {
    "Accounts Overview": dict(
        accent="#2563EB", num_cols=6, freeze_cols=1,
        col_widths={0: 150, 1: 150, 2: 180, 3: 150, 4: 340, 5: 340},
        wrap_cols=[4, 5],
    ),
    "Instructions": dict(
        accent="#6B7280", num_cols=2, freeze_cols=0,
        col_widths={0: 900},
        wrap_cols=[0],
    ),
    "Daily Production List": dict(
        accent="#0D9488", num_cols=10, freeze_cols=2,
        col_widths={0: 110, 1: 140, 2: 110, 3: 220, 4: 240, 5: 200, 6: 140, 7: 280, 8: 240, 9: 120},
        wrap_cols=[4, 5, 7, 8],
    ),
    "Winner Tracking": dict(
        accent="#D4A017", num_cols=14, freeze_cols=2,
        col_widths={0: 110, 1: 130, 2: 260, 3: 90, 4: 110, 5: 100, 6: 90, 7: 90, 8: 100,
                    9: 110, 10: 120, 11: 110, 12: 240, 13: 300},
        wrap_cols=[12, 13],
        percent_cols=[9, 10],
        number_cols={3: "#,##0", 4: "#,##0", 5: "0.00", 7: "#,##0", 8: "#,##0"},
    ),
    "Link Funnel": dict(
        accent="#8B5CF6", num_cols=8, freeze_cols=2,
        col_widths={0: 90, 1: 130, 2: 120, 3: 110, 4: 110, 5: 130, 6: 100, 7: 160},
        wrap_cols=[],
        percent_cols=[4, 5, 7],
        number_cols={2: "#,##0", 3: "#,##0", 6: "#,##0"},
    ),
    "Scaling Log": dict(
        accent="#F97316", num_cols=7, freeze_cols=2,
        col_widths={0: 110, 1: 130, 2: 260, 3: 320, 4: 150, 5: 220, 6: 260},
        wrap_cols=[3, 5, 6],
        number_cols={4: "#,##0"},
    ),
    "Trial Reel Waves": dict(
        accent="#EC4899", num_cols=13, freeze_cols=2,
        col_widths={0: 110, 1: 130, 2: 80, 3: 130, 4: 220, 5: 180, 6: 220, 7: 240,
                    8: 120, 9: 120, 10: 90, 11: 130, 12: 220},
        wrap_cols=[4, 6, 12],
        number_cols={8: "#,##0", 9: "#,##0"},
    ),
    "Target Creator List": dict(
        accent="#10B981", num_cols=10, freeze_cols=1,
        col_widths={0: 160, 1: 180, 2: 110, 3: 160, 4: 150, 5: 110, 6: 130, 7: 130, 8: 220, 9: 300},
        wrap_cols=[8, 9],
        number_cols={5: "0", 6: "0"},
    ),
}

HEADER_TEXT_COLOR = {"red": 1, "green": 1, "blue": 1}
BAND_FIRST_COLOR = {"red": 1, "green": 1, "blue": 1}
BAND_SECOND_COLOR = {"red": 0.953, "green": 0.957, "blue": 0.965}  # #F3F4F6

DATA_ROW_COUNT = 300  # banding/formatting reach — plenty of headroom over current data, cheap either way


def _hex_to_rgb(hex_color: str) -> dict:
    hex_color = hex_color.lstrip("#")
    return {
        "red": int(hex_color[0:2], 16) / 255,
        "green": int(hex_color[2:4], 16) / 255,
        "blue": int(hex_color[4:6], 16) / 255,
    }


def _build_tab_requests(sheet_id: int, cfg: dict) -> list:
    accent = _hex_to_rgb(cfg["accent"])
    num_cols = cfg["num_cols"]
    requests = []

    # Tab color + frozen header row + frozen ID column(s)
    requests.append({
        "updateSheetProperties": {
            "properties": {
                "sheetId": sheet_id,
                "tabColor": accent,
                "gridProperties": {"frozenRowCount": 1, "frozenColumnCount": cfg["freeze_cols"]},
            },
            "fields": "tabColor,gridProperties.frozenRowCount,gridProperties.frozenColumnCount",
        }
    })

    # Header row: accent background, bold white text, wrapped, vertically centered
    requests.append({
        "repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1,
                       "startColumnIndex": 0, "endColumnIndex": num_cols},
            "cell": {
                "userEnteredFormat": {
                    "backgroundColor": accent,
                    "textFormat": {"foregroundColor": HEADER_TEXT_COLOR, "bold": True, "fontSize": 10},
                    "wrapStrategy": "WRAP",
                    "verticalAlignment": "MIDDLE",
                }
            },
            "fields": "userEnteredFormat(backgroundColor,textFormat,wrapStrategy,verticalAlignment)",
        }
    })
    requests.append({
        "updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "ROWS", "startIndex": 0, "endIndex": 1},
            "properties": {"pixelSize": 44},
            "fields": "pixelSize",
        }
    })

    # Zebra striping across the data area (header excluded — already styled above)
    requests.append({
        "addBanding": {
            "bandedRange": {
                "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": DATA_ROW_COUNT,
                           "startColumnIndex": 0, "endColumnIndex": num_cols},
                "rowProperties": {
                    "firstBandColor": BAND_FIRST_COLOR,
                    "secondBandColor": BAND_SECOND_COLOR,
                },
            }
        }
    })

    # Column widths
    for col_idx, width in cfg.get("col_widths", {}).items():
        requests.append({
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": col_idx, "endIndex": col_idx + 1},
                "properties": {"pixelSize": width},
                "fields": "pixelSize",
            }
        })

    # Wrap text on long-content columns (so Notes/Hook/Instruction don't get clipped)
    for col_idx in cfg.get("wrap_cols", []):
        requests.append({
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": DATA_ROW_COUNT,
                           "startColumnIndex": col_idx, "endColumnIndex": col_idx + 1},
                "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP", "verticalAlignment": "TOP"}},
                "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)",
            }
        })

    # Number / percent formats
    for col_idx, pattern in cfg.get("number_cols", {}).items():
        requests.append({
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": DATA_ROW_COUNT,
                           "startColumnIndex": col_idx, "endColumnIndex": col_idx + 1},
                "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": pattern}}},
                "fields": "userEnteredFormat.numberFormat",
            }
        })
    for col_idx in cfg.get("percent_cols", []):
        requests.append({
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": DATA_ROW_COUNT,
                           "startColumnIndex": col_idx, "endColumnIndex": col_idx + 1},
                "cell": {"userEnteredFormat": {"numberFormat": {"type": "PERCENT", "pattern": "0.0%"}}},
                "fields": "userEnteredFormat.numberFormat",
            }
        })

    return requests


def _text_rule(sheet_id, col_idx, contains, bg_hex, text_hex="#000000", bold=True):
    return {
        "addConditionalFormatRule": {
            "rule": {
                "ranges": [{"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": DATA_ROW_COUNT,
                            "startColumnIndex": col_idx, "endColumnIndex": col_idx + 1}],
                "booleanRule": {
                    "condition": {"type": "TEXT_CONTAINS", "values": [{"userEnteredValue": contains}]},
                    "format": {
                        "backgroundColor": _hex_to_rgb(bg_hex),
                        "textFormat": {"foregroundColor": _hex_to_rgb(text_hex), "bold": bold},
                    },
                },
            },
            "index": 0,
        }
    }


def _build_conditional_requests(ids: dict) -> list:
    requests = []

    # Winner Tracking: Decision (col 11) — KEEP green, CAUTION amber, DELETE red
    wt = ids["Winner Tracking"]
    requests += [
        _text_rule(wt, 11, "KEEP", "#D1FAE5", "#065F46"),
        _text_rule(wt, 11, "CAUTION", "#FEF3C7", "#92400E"),
        _text_rule(wt, 11, "DELETE", "#FEE2E2", "#991B1B"),
        _text_rule(wt, 6, "YES", "#D1FAE5", "#065F46"),
    ]

    # Accounts Overview: Status (col 3)
    ao = ids["Accounts Overview"]
    requests += [
        _text_rule(ao, 3, "Active", "#D1FAE5", "#065F46"),
        _text_rule(ao, 3, "New", "#DBEAFE", "#1E40AF"),
        _text_rule(ao, 3, "Paused", "#FEE2E2", "#991B1B"),
    ]

    # Target Creator List: Last 5 Reels Trend (col 3)
    tc = ids["Target Creator List"]
    requests += [
        _text_rule(tc, 3, "rising", "#D1FAE5", "#065F46"),
        _text_rule(tc, 3, "falling", "#FEE2E2", "#991B1B"),
    ]

    # Trial Reel Waves: Hit 2x? (col 10), Confirmed Overnight? (col 11)
    trw = ids["Trial Reel Waves"]
    for col in (10, 11):
        requests += [
            _text_rule(trw, col, "YES", "#D1FAE5", "#065F46"),
            _text_rule(trw, col, "NO", "#FEE2E2", "#991B1B"),
        ]

    return requests


def _build_instructions_requests(sheet_id: int) -> list:
    """Instructions is prose, not a data table — title banner + readable
    body text instead of the header/banding treatment the other tabs get."""
    accent = _hex_to_rgb(TAB_CONFIG["Instructions"]["accent"])
    return [
        {
            "updateSheetProperties": {
                "properties": {"sheetId": sheet_id, "tabColor": accent,
                               "gridProperties": {"frozenRowCount": 0, "frozenColumnCount": 0}},
                "fields": "tabColor,gridProperties.frozenRowCount,gridProperties.frozenColumnCount",
            }
        },
        {
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1,
                           "startColumnIndex": 0, "endColumnIndex": 2},
                "cell": {"userEnteredFormat": {
                    "backgroundColor": accent,
                    "textFormat": {"foregroundColor": HEADER_TEXT_COLOR, "bold": True, "fontSize": 13},
                    "verticalAlignment": "MIDDLE",
                }},
                "fields": "userEnteredFormat(backgroundColor,textFormat,verticalAlignment)",
            }
        },
        {
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "ROWS", "startIndex": 0, "endIndex": 1},
                "properties": {"pixelSize": 36},
                "fields": "pixelSize",
            }
        },
        {
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": 20,
                           "startColumnIndex": 0, "endColumnIndex": 2},
                "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP", "textFormat": {"fontSize": 10}}},
                "fields": "userEnteredFormat(wrapStrategy,textFormat.fontSize)",
            }
        },
        {
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 3, "endRowIndex": 4,
                           "startColumnIndex": 0, "endColumnIndex": 1},
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True, "fontSize": 11}}},
                "fields": "userEnteredFormat.textFormat",
            }
        },
        {
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 11, "endRowIndex": 12,
                           "startColumnIndex": 0, "endColumnIndex": 1},
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True, "fontSize": 11}}},
                "fields": "userEnteredFormat.textFormat",
            }
        },
    ]


async def main():
    service = sheets_tools._get_service()
    meta = service.spreadsheets().get(spreadsheetId=sheets_tools.SPREADSHEET_ID, includeGridData=False).execute()
    ids = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta["sheets"]}
    print("Tabs gefunden:", ids)

    requests = []
    for tab, cfg in TAB_CONFIG.items():
        if tab == "Instructions":
            requests += _build_instructions_requests(ids[tab])
        else:
            requests += _build_tab_requests(ids[tab], cfg)

    requests += _build_conditional_requests(ids)

    print(f"Sende {len(requests)} Formatierungs-Requests...")
    # batchUpdate has a practical request-count comfort zone; chunk to be safe.
    CHUNK = 80
    for i in range(0, len(requests), CHUNK):
        chunk = requests[i:i + CHUNK]
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheets_tools.SPREADSHEET_ID, body={"requests": chunk}
        ).execute()
        print(f"  ...{min(i + CHUNK, len(requests))}/{len(requests)} gesendet")

    print("Fertig — Sheet ist formatiert.")


if __name__ == "__main__":
    asyncio.run(main())
