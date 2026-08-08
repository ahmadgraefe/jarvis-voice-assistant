"""
Jarvis V2 — Finanz-Tracking (Tier 3, Punkt 15)

Arbeitet gegen das neue, eigenstaendige "Luna Vale — Kosten & Einnahmen"-
Spreadsheet (gebaut 2026-08-08 als Zwischenschritt, mit echten Formeln und
Ahmads echter Fanplace-Payout-Historie vorbefuellt) — eigenes Modul statt
Erweiterung von sheets_tools.py, weil das ein ANDERES Spreadsheet ist als
dessen fest verdrahtetes Winner-Tracking-Workbook. Nutzt aber
sheets_tools._get_service() mit, derselbe OAuth-Scope (spreadsheets) deckt
jedes Spreadsheet des Kontos ab, kein neuer Scope noetig.
"""

import asyncio
import json
import os
import time

import httpx

import sheets_tools

SPREADSHEET_ID = "14iP5lEGTRMXxYiTO-FjdueuyxOF5EYykCHUZf_ZwdwM"
SYNCED_PAYOUTS_PATH = os.path.join(os.path.dirname(__file__), "memory", "fanplace_payouts_synced.json")

VARIABLE_KOSTEN_TAB = "Variable Kosten"
EINNAHMEN_TAB = "Einnahmen"
EINSTELLUNGEN_TAB = "Einstellungen"
MONATSUEBERSICHT_TAB = "Monatsübersicht"


async def fetch_usd_eur_rate() -> float:
    """Echte FX-API statt Web-Suche — dieser Code laeuft unbeaufsichtigt im
    Hintergrund, eine Suche waere fragiler und braeuchte einen LLM-Call zum
    Auswerten. frankfurter.dev (frueher frankfurter.app, migriert): EZB-Kurse,
    kostenlos, kein Key noetig."""
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        response = await client.get("https://api.frankfurter.dev/v1/latest", params={"from": "USD", "to": "EUR"})
        response.raise_for_status()
        return response.json()["rates"]["EUR"]


def _update_fx_rate_sync(rate: float):
    service = sheets_tools._get_service()
    today = time.strftime("%d.%m.%Y")
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={
            "valueInputOption": "USER_ENTERED",
            "data": [
                {"range": f"'{EINSTELLUNGEN_TAB}'!B4", "values": [[rate]]},
                {"range": f"'{EINSTELLUNGEN_TAB}'!D4", "values": [[f"{today} (Quelle: frankfurter.app, EZB-Kurs)"]]},
            ],
        },
    ).execute()


async def update_fx_rate(rate: float) -> str:
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _update_fx_rate_sync, rate)
    return f"Kurs aktualisiert: {rate}"


def _first_empty_row_sync(tab: str, start_row: int, max_row: int = 200) -> int:
    """Erste Zeile ohne Wert in Spalte A, innerhalb des Datenblocks — Zeilen
    fuer neue Eintraege wurden beim Sheet-Aufbau bewusst vorgehalten
    (leere, gelb hinterlegte Zeilen)."""
    service = sheets_tools._get_service()
    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, range=f"'{tab}'!A{start_row}:A{max_row}"
    ).execute()
    values = result.get("values", [])
    for i in range(max_row - start_row + 1):
        if i >= len(values) or not values[i] or not values[i][0]:
            return start_row + i
    return max_row + 1  # Datenblock voll — schreibt hinter das vorgesehene Ende, besser als Datenverlust


def _append_variable_cost_sync(datum: str, beschreibung: str, kategorie: str, betrag_eur: float, waehrung: str, betrag_original, notiz: str) -> str:
    row = _first_empty_row_sync(VARIABLE_KOSTEN_TAB, start_row=5, max_row=24)
    service = sheets_tools._get_service()
    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID, range=f"'{VARIABLE_KOSTEN_TAB}'!A{row}:G{row}",
        valueInputOption="USER_ENTERED",
        body={"values": [[datum, beschreibung, kategorie, betrag_eur, waehrung, betrag_original, notiz]]},
    ).execute()
    return f"Eingetragen in Variable Kosten, Zeile {row}."


async def append_variable_cost(datum: str, beschreibung: str, kategorie: str, betrag_eur: float, waehrung: str, betrag_original, notiz: str = "") -> str:
    """Traegt eine neue variable Kostenposition ein — genutzt sowohl fuer
    per E-Mail erkannte Rechnungen (background_brain.py's gmail_reply_pass)
    als auch fuer alles, was Ahmad selbst per Sprache diktiert. Betrag_eur
    ist ein FIXER Wert (kein Formel-Verweis) — historische Ausgaben sollen
    nicht rueckwirkend mit einem spaeteren Kurs neu berechnet werden."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, _append_variable_cost_sync, datum, beschreibung, kategorie, betrag_eur, waehrung, betrag_original, notiz
    )


def _load_synced_payouts() -> set:
    if not os.path.exists(SYNCED_PAYOUTS_PATH):
        return set()
    try:
        with open(SYNCED_PAYOUTS_PATH) as fh:
            return set(json.load(fh))
    except (json.JSONDecodeError, OSError):
        return set()


def _save_synced_payouts(keys: set):
    os.makedirs(os.path.dirname(SYNCED_PAYOUTS_PATH), exist_ok=True)
    with open(SYNCED_PAYOUTS_PATH, "w") as fh:
        json.dump(sorted(keys), fh)


def _parse_money(text: str) -> float:
    """'$175.98' / '-$70.40' / '-' -> float, robust gegen das Fanplace-Format."""
    if not text or text.strip() == "-":
        return 0.0
    return float(text.replace("$", "").replace(",", "").strip())


async def sync_fanplace_payouts(fx_rate: float) -> list:
    """Holt Ahmads echte Payout-Historie (fanplace.get_payout_history),
    korrigiert Zeilen deren Fees/Netto nicht zum bestaetigten 40%-Fee-Satz
    passen (Ahmad, 2026-08-08: 'Fanplace bekommt immer 40%, 60% gehoert
    mir') — exakt der Fix, der am 08.08.2026 einmal manuell an einer
    Fanplace-UI-Eigenheit (Netto-Spalte einer 'Complete'-Zeile zeigte den
    kumulierten 'Paid Out'-Gesamtwert statt des Perioden-Nettos) gemacht
    wurde. Dedupliziert ueber Periode+Status, ein Pending->Complete-
    Uebergang bekommt bewusst eine NEUE Zeile statt eines In-Place-Updates."""
    import fanplace

    payouts = await fanplace.get_payout_history()
    already_synced = _load_synced_payouts()
    new_rows = []

    for p in payouts:
        key = f"{p['period']}|{p['status']}"
        if key in already_synced:
            continue

        gross = _parse_money(p["gross"])
        fees = _parse_money(p["fees"])
        net = _parse_money(p["net"])
        notiz = ""
        if gross and abs((gross + fees) - net) > 1.0:  # fees ist bereits negativ, >1$ Abweichung von der bestaetigten 40/60-Regel
            fees = round(gross * -0.4, 2)
            net = round(gross * 0.6, 2)
            notiz = (
                f"Fanplace zeigte einen abweichenden Netto-Wert (vermutlich kumulierter 'Paid Out'-Wert "
                f"statt Perioden-Netto) — automatisch auf Basis der bestaetigten 40%-Fee-Regel korrigiert."
            )

        net_eur = round(net * fx_rate, 2)
        row_data = {
            "datum": p["date"], "period": p["period"], "status": p["status"], "method": p["method"],
            "gross": gross, "fees": fees, "net": net, "net_eur": net_eur, "notiz": notiz,
        }
        new_rows.append(row_data)
        already_synced.add(key)

    if new_rows:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _append_payout_rows_sync, new_rows)
        _save_synced_payouts(already_synced)

    return new_rows


def _append_payout_rows_sync(rows: list):
    row = _first_empty_row_sync(EINNAHMEN_TAB, start_row=5, max_row=24)
    service = sheets_tools._get_service()
    values = [[r["datum"], r["period"], r["status"], r["method"], r["gross"], r["fees"] or "-", r["net"], r["net_eur"], r["notiz"]] for r in rows]
    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID, range=f"'{EINNAHMEN_TAB}'!A{row}:I{row + len(rows) - 1}",
        valueInputOption="USER_ENTERED", body={"values": values},
    ).execute()


def _get_recent_month_summary_sync() -> dict:
    service = sheets_tools._get_service()
    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, range=f"'{MONATSUEBERSICHT_TAB}'!A5:G16"
    ).execute()
    rows = [r for r in result.get("values", []) if len(r) >= 4 and r[3]]  # Gesamtkosten (D) gefuellt
    if len(rows) < 2:
        return {}
    current, previous = rows[-1], rows[-2]
    return {
        "current_month": current[0], "current_total_cost": float(str(current[3]).replace(",", ".") or 0),
        "current_netto": float(str(current[5]).replace(",", ".") or 0) if len(current) > 5 else None,
        "previous_month": previous[0], "previous_total_cost": float(str(previous[3]).replace(",", ".") or 0),
        "previous_netto": float(str(previous[5]).replace(",", ".") or 0) if len(previous) > 5 else None,
    }


async def get_recent_month_summary() -> dict:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _get_recent_month_summary_sync)
