"""Portfolio PML Optimizer - deployable web app.

Run:  python app.py        (then open http://localhost:5000)

Flow:
  1. Connect      - enter SQL Server connection (server/database/auth).
  2. Analyses     - summary of every analysis in RDM_analysis, with
                    account / policy / location granularity availability
                    cross-checked against the loss tables.
  3. Setup        - pick granularity + loss perspective, then run.
  4. Results      - EXCL tab (portfolio PML row, then one row per removed
                    entity) and DIFF tab (marginal impact per entity),
                    plus EP curve chart and CSV downloads. Result tables
                    are also written back to SQL Server.
"""

import math
import os
import secrets
import threading
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from datetime import timezone

import numpy as np
import pandas as pd
from flask import (Flask, Response, jsonify, redirect, render_template,
                   request, url_for)

from rdm import analysis as rdm_analysis
from rdm import db as rdm_db
from rdm import schema as rdm_schema
from rdm import client_match
from rdm import settings_store as rdm_settings
from rdm import exclude_pool
from rdm import results_store

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)
_RUN_LOCK = threading.Lock()
_RUNNING_ANALYSES = set()
_RUN_STARTED = {}

APP_CFG, SCHEMA = rdm_schema.load_config("config.yaml")

# In-memory only. Passwords live here and die with the process - nothing is
# ever written to disk. Keyed by a random browser cookie.
SESSIONS = {}
# run_id -> result bundle (kept in memory for the results page / chart API)
RUNS = {}


# ---------------------------------------------------------------- helpers

def _token():
    tok = request.cookies.get("pml_session")
    if not tok or tok not in SESSIONS:
        return None
    return tok


def _require_session():
    tok = _token()
    if not tok:
        return None, redirect(url_for("new_analysis"))
    return SESSIONS[tok], None


@app.context_processor
def _nav():
    return {"connected": _token() is not None}


def _saved_connect_cfg():
    return {"database": ""}


def _test_sql(settings, driver=None, database=None, use_windows_auth=None):
    conn = rdm_db.connect(
        server=settings["server"],
        database=database if database is not None else settings.get("database") or "master",
        driver=driver or settings.get("driver") or APP_CFG["odbc_driver"],
        use_windows_auth=settings["use_integrated_security"] if use_windows_auth is None else use_windows_auth,
        username=settings.get("username", ""),
        password=settings.get("password", ""))
    conn.close()


def _open(sess):
    p = sess["conn"]
    return rdm_db.connect(
        server=p["server"], database=p["database"], driver=p["driver"],
        use_windows_auth=p["use_windows_auth"],
        username=p.get("username", ""), password=p.get("password", ""))


def _schema_problems():
    return rdm_schema.validate(SCHEMA)


def _open_saved(saved, database, use_windows_auth):
    return rdm_db.connect(
        server=saved["server"],
        database=database,
        driver=saved.get("driver") or APP_CFG["odbc_driver"],
        use_windows_auth=use_windows_auth,
        username=saved.get("username", ""),
        password=saved.get("password", ""))


def _dpo_database():
    saved = rdm_settings.load()
    return (saved.get("database") or "BMS_DPO").strip() or "BMS_DPO"


def _open_dpo():
    """BMS_DPO lives on the RDM server and uses the Moody's SQL login."""
    moodys = _moodys_login()
    return _open_saved(moodys, _dpo_database(), False)


def _open_cms():
    """Client names stay in BMS_CMS on vmus-sql-099 with Windows authentication."""
    return rdm_db.connect(
        server=rdm_settings.ALLOWED_SERVERS[0],
        database="BMS_CMS",
        driver=APP_CFG["odbc_driver"],
        use_windows_auth=True)


def _load_clients():
    conn = _open_cms()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT ClientID, ClientName, ClientShortName FROM dbo.tClient "
            "WHERE ClientStatus = 'Active' ORDER BY ClientName")
        return [{"id": int(r[0]), "name": r[1], "short_name": (r[2] or "").strip()} for r in cur.fetchall()]
    finally:
        conn.close()


def _moodys_login():
    saved = rdm_settings.load_moodys()
    if not (saved.get("server") or "").strip() or not (saved.get("username") or "").strip():
        raise RuntimeError("Set the Moody's SQL login under Admin, Server, Moody's Server.")
    return saved


def _open_moodys(database):
    if not database:
        raise RuntimeError("RDM database is required.")
    return _open_saved(_moodys_login(), database, False)


def _first_value(cur):
    row = cur.fetchone()
    while row is None and cur.nextset():
        row = cur.fetchone()
    if not row or row[0] is None:
        raise RuntimeError("The database did not return an id.")
    return int(row[0])


def _dpo_form_lists():
    """Loss level and perspective lookups stored in BMS_DPO."""
    conn = _open_dpo()
    try:
        cur = conn.cursor()
        cur.execute("SELECT LossLevelName FROM dbo.tLossLevel ORDER BY LossLevelID")
        levels = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT LossPerspectiveCode FROM dbo.tLossPerspective ORDER BY LossPerspectiveID")
        perspectives = [r[0] for r in cur.fetchall()]
        return levels, perspectives
    finally:
        conn.close()


# ---------------------------------------------------------------- routes

@app.route("/")
def index():
    return render_template("home.html")


def _analysis_choices():
    conn = _open_dpo()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT AnalysisID, AnalysisName FROM dbo.tAnalysis ORDER BY AnalysisName, AnalysisID")
        return [{"id": int(r[0]), "name": r[1]} for r in cur.fetchall()]
    finally:
        conn.close()


_CHART_LIMITS = (5, 10, 15, 20, 25, 50, 100)


def _dashboard_for(analysis_id, return_period, chart_limit=10):
    """Latest complete run, ranked by marginal DIFF at one return period."""
    conn = _open_dpo()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT TOP 1
                r.AnalysisRunID, ll.LossLevelName, lp.LossPerspectiveCode,
                r.EntityCount, r.PortfolioAal, r.PortfolioStandardDeviation
            FROM dbo.tAnalysisRun AS r
            INNER JOIN dbo.tAnalysisSettings AS s
                ON s.AnalysisSettingsID = r.AnalysisSettingsID
            INNER JOIN dbo.tLossLevel AS ll ON ll.LossLevelID = s.LossLevelID
            INNER JOIN dbo.tLossPerspective AS lp
                ON lp.LossPerspectiveID = s.LossPerspectiveID
            WHERE s.AnalysisID = ? AND r.RunStatus = 'Complete'
            ORDER BY r.AnalysisRunID DESC
            """,
            analysis_id)
        run = cur.fetchone()
        if not run:
            return None
        cur.execute(
            """
            SELECT DISTINCT e.ReturnPeriod, e.SortOrder
            FROM dbo.tAnalysisEpPoint AS a
            INNER JOIN dbo.tEpPoint AS e ON e.EpPointID = a.EpPointID
            INNER JOIN dbo.tAnalysisSettings AS s
                ON s.AnalysisSettingsID = a.AnalysisSettingsID
            WHERE s.AnalysisID = ?
            """,
            analysis_id)
        defined = [(int(r[0]), int(r[1])) for r in cur.fetchall()]
        slots = [0] * 15
        if defined and all(1 <= sort <= 15 for _period, sort in defined) and len({sort for _period, sort in defined}) == len(defined):
            for period, sort in defined:
                slots[sort - 1] = period
        else:
            for index, period in enumerate(sorted({period for period, _sort in defined})):
                if index < 15:
                    slots[index] = period
        periods = [period for period in slots if period > 0]
        stored, file_periods = _read_result_curves(analysis_id)
        if not periods:
            periods = file_periods[:15]
            slots = _ep_slots(periods)
            periods = [period for period in slots if period > 0]
        if not periods:
            return None
        if return_period not in periods:
            return_period = 100 if 100 in periods else periods[len(periods) // 2]
        portfolio_diff = stored["DIFF"].get("PORTFOLIO", {})
        portfolio_excl = stored["EXCL"].get("PORTFOLIO", {})
        portfolio_by_period = {
            period: float(portfolio_diff.get(period, portfolio_excl.get(period, 0.0)) or 0.0)
            for period in periods
        }
        portfolio = portfolio_by_period.get(return_period, 0.0)
        rows = []
        for entity_id, by_period in stored["DIFF"].items():
            if entity_id == "PORTFOLIO":
                continue
            marginal = by_period.get(return_period, 0.0)
            rows.append({
                "entity_id": entity_id,
                "diff": marginal,
                "excl": stored["EXCL"].get(entity_id, {}).get(return_period, 0.0),
                "share": (marginal / portfolio) if portfolio else 0.0,
            })
        rows.sort(key=lambda item: item["diff"], reverse=True)
        if chart_limit not in _CHART_LIMITS:
            chart_limit = 10
        rows = rows[:chart_limit]
        exposure_rows, exposure_total = _location_exposure(
            analysis_id, limit=_EXPOSURE_LIST_LIMIT)
        peak = rows[0]["diff"] if rows else 0.0
        for item in rows:
            item["width"] = (item["diff"] / peak * 100.0) if peak else 0.0

        def _result_rows(kind):
            by_entity = stored[kind]
            names = [name for name in by_entity if name != "PORTFOLIO"]
            names.sort(key=lambda name: by_entity[name].get(return_period, 0.0), reverse=True)
            if "PORTFOLIO" in by_entity:
                names.insert(0, "PORTFOLIO")
            metric_aal = "excl_aal" if kind == "EXCL" else "diff_aal"
            metric_std = "excl_stddev" if kind == "EXCL" else "diff_stddev"
            table_rows = []
            for name in names:
                metric = stored.get("METRICS", {}).get(name, {})
                table_rows.append({
                    "entity_id": name,
                    "result_type": kind,
                    "aal": _money(metric.get(metric_aal)),
                    "standard_deviation": _money(metric.get(metric_std)),
                    "amounts": [
                        _money(by_entity[name].get(period)) if period else ""
                        for period in slots
                    ],
                })
            return table_rows

        return {
            "run_id": int(run[0]),
            "loss_level": run[1],
            "perspective": run[2],
            "entity_count": run[3],
            "aal": run[4],
            "standard_deviation": run[5],
            "periods": periods,
            "headers": [
                {"slot": index + 1, "period": period}
                for index, period in enumerate(slots)
            ],
            "return_period": return_period,
            "portfolio": portfolio,
            "portfolio_by_period": portfolio_by_period,
            "rows": rows,
            "chart_limit": chart_limit,
            "result_rows": _result_rows("EXCL") + _result_rows("DIFF"),
            "exposure": exposure_rows,
            "exposure_total": exposure_total,
            "exposure_limit": _EXPOSURE_LIST_LIMIT,
        }
    finally:
        conn.close()


def _analysis_user_log(analysis_id):
    conn = _open_dpo()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT LoggedAt, AnalysisRunID, ElapsedSeconds,
                   LocationsCompleted, LocationsTotal, Message
            FROM dbo.tAnalysisUserLog
            WHERE AnalysisID = ?
            ORDER BY AnalysisUserLogID
            """,
            analysis_id)
        rows = []
        for logged_at, run_id, elapsed, completed, total, message in cur.fetchall():
            rows.append({
                "logged_at": logged_at.strftime("%Y-%m-%d %H:%M") if logged_at else "",
                "run_id": run_id,
                "elapsed": None if elapsed is None else float(elapsed),
                "completed": completed,
                "total": total,
                "message": message,
            })
        return rows
    finally:
        conn.close()


_PIE_COLORS = ("#f08700", "#30d880", "#5eb1ff", "#e8f0f8", "#ef4444", "#c084fc", "#fbbf24", "#22d3ee")


def _pie_slices(pairs):
    total = sum(count for _label, count in pairs) or 1
    start = 0.0
    slices = []
    for index, (label, count) in enumerate(pairs):
        share = count / total * 100.0
        slices.append({
            "label": label,
            "count": count,
            "share": share,
            "start": start,
            "end": start + share,
            "color": _PIE_COLORS[index % len(_PIE_COLORS)],
        })
        start += share
    return slices


def _range_number(value):
    value = float(value)
    if abs(value - round(value)) < 0.00005:
        return f"{value:,.0f}"
    return f"{value:,.2f}"


def _input_number(value):
    text = f"{float(value):.4f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _range_fields(bands):
    rows = [
        {"start": _input_number(start), "end": _input_number(end)}
        for start, end in list(bands)[:5]
    ]
    while len(rows) < 5:
        rows.append({"start": "", "end": ""})
    return rows


def _year_built_fields(bands):
    rows = []
    for label, start, end in list(bands)[:5]:
        rows.append({
            "label": label,
            "start": "" if start is None else str(int(start)),
            "end": "" if end is None else str(int(end)),
        })
    while len(rows) < 5:
        rows.append({"label": "", "start": "", "end": ""})
    return rows


def _optional_year(raw):
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError as exc:
        raise ValueError("Year built ranges must be whole years.") from exc


def _parse_year_built_form(form):
    labels = form.getlist("range_label")
    starts = form.getlist("range_from")
    ends = form.getlist("range_to")
    if len(labels) != 5 or len(starts) != 5 or len(ends) != 5:
        raise ValueError("Enter five year built ranges.")
    bands = []
    for index, (raw_label, raw_start, raw_end) in enumerate(zip(labels, starts, ends), start=1):
        label = (raw_label or "").strip()
        if not label:
            raise ValueError(f"Range {index} needs a name.")
        if label.lower() == "unknown":
            raise ValueError("Unknown is reserved for a year built of 0.")
        start = _optional_year(raw_start)
        end = _optional_year(raw_end)
        if start is None and end is None:
            raise ValueError(f"Range {index} needs a from year, a to year, or both.")
        if start == 0 or end == 0:
            raise ValueError("Year 0 is Unknown and cannot be part of a range.")
        if start is not None and end is not None and start > end:
            raise ValueError(f"Range {index} starts above its end.")
        if start is None and bands:
            raise ValueError("Only the first range can omit From.")
        if end is None and index != 5:
            raise ValueError("Only the last range can omit To.")
        if bands and bands[-1][2] is None:
            raise ValueError("Only the last range can omit To.")
        if bands and start is not None and bands[-1][2] is not None and start <= bands[-1][2]:
            raise ValueError("Ranges must be in order and must not overlap.")
        bands.append((label, start, end))
    return bands


def _band_text(start, end):
    return f"{_range_number(start)} – {_range_number(end)}"


def _even_ranges(low, high, count=5):
    """Split the EDM minimum and maximum into equal bands."""
    if low is None or high is None:
        return []
    low = float(low)
    high = float(high)
    if high < low:
        low, high = high, low
    edges = [round(low + (high - low) * i / count, 4) for i in range(count)]
    edges.append(round(high, 4))
    edges[0] = round(low, 4)
    for index in range(1, len(edges)):
        if edges[index] < edges[index - 1]:
            edges[index] = edges[index - 1]
    return list(zip(edges[:-1], edges[1:]))


def _modifier_bounds():
    """Min and max distance to coast and elevation across the analyses' EDMs."""
    conn = _open_dpo()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT DISTINCT EdmName FROM dbo.tAnalysis "
            "WHERE EdmName IS NOT NULL AND LTRIM(RTRIM(EdmName)) <> ''")
        names = [row[0] for row in cur.fetchall()]
    finally:
        conn.close()
    coast_min = coast_max = elev_min = elev_max = None
    for name in names:
        database = _resolve_edm_database(name)
        edm = _open_moodys(database)
        try:
            cur = edm.cursor()
            cur.execute(
                """
                SELECT
                    MIN(DISTCOAST),
                    MAX(DISTCOAST),
                    MIN(CASE WHEN ELEVATION > -900 THEN ELEVATION END),
                    MAX(CASE WHEN ELEVATION > -900 THEN ELEVATION END)
                FROM dbo.hudet
                """)
            row = cur.fetchone()
        finally:
            edm.close()
        if not row:
            continue
        coast_min = _combine_bound(coast_min, row[0], min)
        coast_max = _combine_bound(coast_max, row[1], max)
        elev_min = _combine_bound(elev_min, row[2], min)
        elev_max = _combine_bound(elev_max, row[3], max)
    return {
        "dtc_min": coast_min, "dtc_max": coast_max,
        "elev_min": elev_min, "elev_max": elev_max,
    }


def _combine_bound(current, value, pick):
    if value is None:
        return current
    value = float(value)
    return value if current is None else pick(current, value)


_DEFAULT_RANGES = None


def _default_ranges():
    """Five equal bands from the EDM minimum and maximum. Cached after the first read."""
    global _DEFAULT_RANGES
    if _DEFAULT_RANGES is None:
        bounds = _modifier_bounds()
        _DEFAULT_RANGES = {
            "dtc": _even_ranges(bounds["dtc_min"], bounds["dtc_max"]),
            "elevation": _even_ranges(bounds["elev_min"], bounds["elev_max"]),
        }
    return _DEFAULT_RANGES


_YEAR_BUILT_DEFAULTS = (
    ("Pre-1937", None, 1936),
    ("1937-1973", 1937, 1973),
    ("1974-1988", 1974, 1988),
    ("1989-2000", 1989, 2000),
    ("Post-2000", 2001, None),
)


def _display_ranges():
    saved = rdm_settings.load_ranges()
    defaults = None
    ranges = {
        "year_built": saved.get("year_built") or list(_YEAR_BUILT_DEFAULTS),
    }
    for key in ("dtc", "elevation"):
        if saved.get(key):
            ranges[key] = saved[key]
            continue
        if defaults is None:
            defaults = _default_ranges()
        ranges[key] = defaults[key]
    return ranges


def _year_built_label(year, bands):
    """Year 0 and a missing year are Unknown. Other years use the saved bands."""
    if year is None or year == "":
        return "Unknown"
    try:
        year = int(year)
    except (TypeError, ValueError):
        return "Unknown"
    if year <= 0:
        return "Unknown"
    for label, start, end in bands:
        if start is not None and year < start:
            continue
        if end is not None and year > end:
            continue
        return label
    return "Other"


def _year_built_counts(years, bands):
    counts = Counter(_year_built_label(year, bands) for year in years)
    pairs = []
    if counts.get("Unknown"):
        pairs.append(("Unknown", counts["Unknown"]))
    for label, _start, _end in bands:
        if counts.get(label):
            pairs.append((label, counts[label]))
    if counts.get("Other"):
        pairs.append(("Other", counts["Other"]))
    return pairs


def _band_label(value, bands):
    if value is None or not bands:
        return "Unknown"
    value = float(value)
    if value < bands[0][0]:
        return f"Under {_range_number(bands[0][0])}"
    for index, (start, end) in enumerate(bands):
        last = index == len(bands) - 1
        if start <= value < end or (last and start <= value <= end):
            return _band_text(start, end)
    return f"Over {_range_number(bands[-1][1])}"


def _band_counts(values, bands):
    counts = Counter(_band_label(value, bands) for value in values)
    pairs = []
    if bands:
        under = f"Under {_range_number(bands[0][0])}"
        over = f"Over {_range_number(bands[-1][1])}"
        if counts.get(under):
            pairs.append((under, counts[under]))
    else:
        under = over = None
    for start, end in bands:
        label = _band_text(start, end)
        pairs.append((label, counts.get(label, 0)))
    if over and counts.get(over):
        pairs.append((over, counts[over]))
    if counts.get("Unknown"):
        pairs.append(("Unknown", counts["Unknown"]))
    return pairs


def _parse_range_form(form):
    starts = form.getlist("range_from")
    ends = form.getlist("range_to")
    if len(starts) != 5 or len(ends) != 5:
        raise ValueError("Enter five ranges.")
    bands = []
    for index, (raw_start, raw_end) in enumerate(zip(starts, ends), start=1):
        try:
            start = float(raw_start)
            end = float(raw_end)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Range {index} must be a number.") from exc
        if start > end:
            raise ValueError(f"Range {index} starts above its end.")
        if bands and start < bands[-1][1]:
            raise ValueError("Ranges must be in order and must not overlap.")
        bands.append((start, end))
    return bands


def _pml_without_locations(analysis_id, return_period, entity_ids, original_pml):
    """Occurrence PML of the portfolio after every selected location is removed."""
    if not entity_ids:
        return float(original_pml or 0)
    analysis = _analysis_header(analysis_id)
    settings = _analysis_settings_rows(analysis_id)
    if not analysis or not settings:
        return float(original_pml or 0)
    row = settings[0]
    rdm = _open_moodys(analysis["rdm_name"])
    try:
        cur = rdm.cursor()
        event_ids, mean, std, exposure, rate = _portfolio_elt(
            cur, analysis["rdm_analysis_id"], row["loss_perspective"], analysis["portfolio_id"])
        if row["loss_level"] in ("State", "County"):
            by_entity = _entity_event_losses(
                cur, row["loss_level"], analysis["rdm_analysis_id"], row["loss_perspective"])
            wanted = {str(entity_id) for entity_id in entity_ids}
            by_entity = {key: value for key, value in by_entity.items() if key in wanted}
        else:
            by_entity = _entity_event_losses_for_ids(
                cur, row["loss_level"], analysis["rdm_analysis_id"],
                row["loss_perspective"], entity_ids)
    finally:
        rdm.close()
    removed = np.zeros(mean.shape, dtype=float)
    index = {int(event_id): slot for slot, event_id in enumerate(event_ids)}
    for losses in by_entity.values():
        for event_id, loss in losses.items():
            slot = index.get(int(event_id))
            if slot is not None:
                removed[slot] += float(loss or 0)
    remaining = rdm_analysis.subtract_elt(mean, removed)
    scale = np.ones(mean.shape, dtype=float)
    positive = mean > 0
    scale[positive] = remaining[positive] / mean[positive]
    scale[~positive] = 0.0
    values = rdm_analysis.pml_from_elt(
        remaining, std * scale, exposure * scale, rate, [return_period])
    return float(values[0])


def _pml_impact(analysis_id, return_period, target, premium_target=None, premiums=None, name="Option 1", refresh_ep=False):
    """Rank locations by marginal DIFF until the PML drop or the premium limit is reached.

    The summary uses the stored DIFF file. The excluding portfolio curve is
    calculated only when refresh_ep is set.
    """
    conn = _open_dpo()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT TOP 1 r.AnalysisRunID
            FROM dbo.tAnalysisRun AS r
            INNER JOIN dbo.tAnalysisSettings AS s
                ON s.AnalysisSettingsID = r.AnalysisSettingsID
            WHERE s.AnalysisID = ? AND r.RunStatus = 'Complete'
            ORDER BY r.AnalysisRunID DESC
            """,
            analysis_id)
        run = cur.fetchone()
        if not run:
            return None
        run_id = int(run[0])
        cur.execute(
            """
            SELECT ExposureLevelID, Construction, Occupancy, NumberOfStories, YearBuilt,
                   FloorArea, InceptDate, ExpireDate, Admin1Code, Admin2Name,
                   Latitude, Longitude, LocationValue, DistCoast, Elevation, StreetAddress
            FROM dbo.tLocationExposure
            WHERE AnalysisID = ?
            """,
            analysis_id)
        exposure = {str(row[0]): row for row in cur.fetchall()}
    finally:
        conn.close()
    stored, _periods = _read_result_curves(analysis_id)
    original_pml = stored["EXCL"].get("PORTFOLIO", {}).get(int(return_period), 0.0)
    ranked_ids = [
        (entity_id, by_period.get(int(return_period), 0.0))
        for entity_id, by_period in stored["DIFF"].items()
        if entity_id != "PORTFOLIO"
    ]
    ranked_ids.sort(key=lambda item: item[1], reverse=True)
    ranked = []
    cumulative = 0.0
    for entity_id, marginal in ranked_ids:
        row = exposure.get(entity_id)
        cumulative += marginal
        ranked.append({
            "entity_id": entity_id,
            "diff": marginal,
            "cumulative": cumulative,
            "construction": (row[1] if row else None) or "Unknown",
            "occupancy": (row[2] if row else None) or "Unknown",
            "stories": "Unknown" if row is None or row[3] is None else str(int(row[3])),
            "year_built": None if row is None or row[4] is None else int(row[4]),
            "floor_area": None if row is None or row[5] is None else float(row[5]),
            "incept": None if row is None else row[6],
            "expire": None if row is None else row[7],
            "state": (row[8] if row else None) or "Unknown",
            "county": (row[9] if row else None) or "Unknown",
            "latitude": None if row is None or row[10] is None else float(row[10]),
            "longitude": None if row is None or row[11] is None else float(row[11]),
            "tiv": float((row[12] if row else 0) or 0),
            "premium": (premiums or {}).get(entity_id, 0.0),
            "dist_coast": None if row is None or row[13] is None else float(row[13]),
            "elevation": None if row is None or row[14] is None else float(row[14]),
            "street": "" if row is None or row[15] is None else str(row[15]).strip(),
        })
    if not ranked:
        return None
    chosen = []
    pml_so_far = 0.0
    premium_so_far = 0.0
    tiv_so_far = 0.0
    for item in ranked:
        if not target and not premium_target:
            break
        chosen.append(item)
        pml_so_far += item["diff"]
        premium_so_far += item["premium"]
        tiv_so_far += item["tiv"]
        pml_hit = bool(target) and pml_so_far >= target
        premium_hit = bool(premium_target) and premium_so_far >= premium_target
        if pml_hit or premium_hit:
            break
    reached = (not target or pml_so_far >= target) and (not premium_target or premium_so_far >= premium_target)
    ranges = _display_ranges()
    for item in ranked:
        item["dtc"] = _band_label(item["dist_coast"], ranges["dtc"])
        item["elevation_band"] = _band_label(item["elevation"], ranges["elevation"])

    def _area_band(area):
        if area is None:
            return "Unknown"
        if area <= 0:
            return "0"
        if area <= 1000:
            return "1–1,000"
        if area <= 2000:
            return "1,001–2,000"
        if area <= 3000:
            return "2,001–3,000"
        return "3,001+"

    lats = [item["latitude"] for item in chosen if item["latitude"] is not None]
    lons = [item["longitude"] for item in chosen if item["longitude"] is not None]
    lat_span = (max(lats) - min(lats)) if lats else 0
    lon_span = (max(lons) - min(lons)) if lons else 0
    points = []
    for item in chosen:
        if item["latitude"] is None or item["longitude"] is None:
            continue
        x = 50.0 if lon_span == 0 else (item["longitude"] - min(lons)) / lon_span * 100.0
        y = 50.0 if lat_span == 0 else (max(lats) - item["latitude"]) / lat_span * 100.0
        incept = item["incept"].strftime("%Y-%m-%d") if item["incept"] else ""
        expire = item["expire"].strftime("%Y-%m-%d") if item["expire"] else ""
        points.append({
            "entity_id": item["entity_id"],
            "state": item["state"],
            "county": item["county"],
            "latitude": item["latitude"],
            "longitude": item["longitude"],
            "x": x,
            "y": y,
            "construction": item["construction"],
            "occupancy": item["occupancy"],
            "stories": item["stories"],
            "yearBuilt": item["year_built"],
            "floorArea": item["floor_area"],
            "incept": incept,
            "expire": expire,
            "locationValue": item["tiv"],
            "diff": item["diff"],
            "street": item["street"],
        })
    state_counts = Counter(item["state"] for item in chosen)
    county_counts = Counter(f"{item['county']}, {item['state']}" for item in chosen)
    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    expire_counts = Counter(item["expire"].month for item in chosen if item["expire"])
    peak = max(list(expire_counts.values()) + [1])
    return {
        "name": name or "Option 1",
        "return_period": int(return_period),
        "target": float(target or 0),
        "premium_target": premium_target,
        "tiv": tiv_so_far,
        "premium": premium_so_far,
        "original_pml": original_pml,
        "ep_refreshed": bool(refresh_ep and chosen),
        "new_pml": _pml_without_locations(
            analysis_id, int(return_period),
            [item["entity_id"] for item in chosen], original_pml)
            if refresh_ep and chosen else None,
        "count": len(chosen),
        "available": len(ranked),
        "reached": reached,
        "cumulative": pml_so_far if chosen else 0.0,
        "locations": chosen,
        "points": points,
        "states": state_counts.most_common(),
        "counties": county_counts.most_common(),
        "pies": {
            "Construction": _pie_slices(Counter(item["construction"] for item in chosen).most_common()),
            "Occupancy": _pie_slices(Counter(item["occupancy"] for item in chosen).most_common()),
            "Stories": _pie_slices(Counter(item["stories"] for item in chosen).most_common()),
            "Year built": _pie_slices(_year_built_counts(
                (item["year_built"] for item in chosen), ranges["year_built"])),
            "Floor area": _pie_slices(Counter(_area_band(item["floor_area"]) for item in chosen).most_common()),
        },
        "modifiers": {
            "DTC": _band_counts((item["dist_coast"] for item in chosen), ranges["dtc"]),
            "Elevation": _band_counts((item["elevation"] for item in chosen), ranges["elevation"]),
        },
        "timeline": [
            {
                "month": label,
                "expire": expire_counts[index],
                "expire_width": expire_counts[index] / peak * 100.0,
            }
            for index, label in enumerate(month_names, start=1)
        ],
    }


def _location_premiums(analysis):
    """Location premium from policy.BLANPREAMT, shared by each location's TIV.

    Premium for every risk level is the policy blanket amount. A location
    reaches that policy through property, account, and policy on ACCGRPID.
    Policy and account runs will use the same amount in a later version.
    """
    if not analysis or not analysis.get("portfolio_id"):
        return {}
    try:
        peril_id = _peril_id(analysis.get("peril"))
        database = _resolve_edm_database(analysis.get("edm_name"))
    except Exception:
        return {}
    edm = _open_moodys(database)
    try:
        cur = edm.cursor()
        cur.execute(
            """
            SELECT CONVERT(varchar(40), p.LOCID), g.ACCGRPID, c.LocationValue, prem.AccountPremium
            FROM dbo.portacct AS pa
            INNER JOIN dbo.accgrp AS g ON g.ACCGRPID = pa.ACCGRPID
            INNER JOIN dbo.Property AS p ON p.ACCGRPID = g.ACCGRPID
            INNER JOIN (
                SELECT LOCID, SUM(VALUEAMT) AS LocationValue
                FROM dbo.loccvg
                WHERE PERIL = ?
                GROUP BY LOCID
            ) AS c ON c.LOCID = p.LOCID
            LEFT JOIN (
                SELECT pol.ACCGRPID, SUM(pol.BLANPREAMT) AS AccountPremium
                FROM dbo.policy AS pol
                GROUP BY pol.ACCGRPID
            ) AS prem ON prem.ACCGRPID = g.ACCGRPID
            WHERE pa.PORTINFOID = ?
            """,
            peril_id, int(analysis["portfolio_id"]))
        rows = cur.fetchall()
    finally:
        edm.close()
    accounts = {}
    for loc_id, account_id, tiv, premium in rows:
        bucket = accounts.setdefault(int(account_id), {"premium": float(premium or 0), "tiv": 0.0, "locs": []})
        value = float(tiv or 0)
        bucket["tiv"] += value
        bucket["locs"].append((str(loc_id), value))
    allocated = {}
    for bucket in accounts.values():
        for loc_id, value in bucket["locs"]:
            share = (value / bucket["tiv"]) if bucket["tiv"] else 0.0
            allocated[loc_id] = bucket["premium"] * share
    return allocated


def _scenario_summary(analysis_id, return_period, pml_target, premium_target, premiums):
    """Remove highest-DIFF locations until the PML drop or the premium limit is reached."""
    conn = _open_dpo()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT TOP 1 r.AnalysisRunID
            FROM dbo.tAnalysisRun AS r
            INNER JOIN dbo.tAnalysisSettings AS s
                ON s.AnalysisSettingsID = r.AnalysisSettingsID
            WHERE s.AnalysisID = ? AND r.RunStatus = 'Complete'
            ORDER BY r.AnalysisRunID DESC
            """,
            analysis_id)
        run = cur.fetchone()
        if not run:
            return None
        run_id = int(run[0])
        cur.execute(
            """
            SELECT ExposureLevelID, LocationValue
            FROM dbo.tLocationExposure
            WHERE AnalysisID = ?
            """,
            analysis_id)
        exposure = {str(row[0]): float(row[1] or 0) for row in cur.fetchall()}
    finally:
        conn.close()
    stored, _periods = _read_result_curves(analysis_id)
    original = stored["EXCL"].get("PORTFOLIO", {}).get(int(return_period), 0.0)
    ranked = sorted(
        (
            (entity_id, by_period.get(int(return_period), 0.0), exposure.get(entity_id, 0.0))
            for entity_id, by_period in stored["DIFF"].items()
            if entity_id != "PORTFOLIO"
        ),
        key=lambda item: item[1],
        reverse=True,
    )
    chosen_pml = 0.0
    chosen_premium = 0.0
    chosen_tiv = 0.0
    count = 0
    for entity_id, diff, tiv in ranked:
        count += 1
        chosen_pml += float(diff or 0)
        chosen_tiv += float(tiv or 0)
        chosen_premium += premiums.get(str(entity_id), 0.0)
        pml_hit = pml_target and chosen_pml >= pml_target
        premium_hit = premium_target and chosen_premium >= premium_target
        if pml_target and premium_target:
            if pml_hit or premium_hit:
                break
        elif pml_hit or premium_hit:
            break
    return {
        "return_period": int(return_period),
        "pml_target": pml_target,
        "premium_target": premium_target,
        "locations": count,
        "tiv": chosen_tiv,
        "pml": chosen_pml,
        "premium": chosen_premium,
        "original_pml": original,
        "new_pml": max(original - chosen_pml, 0.0),
    }


def _coord_ok(lat, lon):
    if lat is None or lon is None:
        return False
    lat = float(lat)
    lon = float(lon)
    if lat == 0 or lon == 0:
        return False
    return -90 <= lat <= 90 and -180 <= lon <= 180


def _location_map_points(analysis_id, return_period, target):
    """Exposure points for the map. Invalid coordinates are counted and left out."""
    _ensure_location_exposure(analysis_id)
    conn = _open_dpo()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT TOP 1 r.AnalysisRunID
            FROM dbo.tAnalysisRun AS r
            INNER JOIN dbo.tAnalysisSettings AS s
                ON s.AnalysisSettingsID = r.AnalysisSettingsID
            WHERE s.AnalysisID = ? AND r.RunStatus = 'Complete'
            ORDER BY r.AnalysisRunID DESC
            """,
            analysis_id)
        run = cur.fetchone()
        run_id = int(run[0]) if run else None
        if run_id is None:
            rows = []
        else:
            cur.execute(
                """
                SELECT ExposureLevelID, Latitude, Longitude, Construction, Occupancy,
                       NumberOfStories, YearBuilt, FloorArea, InceptDate, ExpireDate,
                       Admin1Code, Admin2Name, PostalCode, LocationValue, StreetAddress
                FROM dbo.tLocationExposure
                WHERE AnalysisID = ?
                """,
                analysis_id)
            exposure_rows = cur.fetchall()
    finally:
        conn.close()
    if run_id is None:
        rows = []
    else:
        stored, _periods = _read_result_curves(analysis_id)
        diffs = {
            entity_id: by_period.get(int(return_period), 0.0)
            for entity_id, by_period in stored["DIFF"].items()
            if entity_id != "PORTFOLIO"
        }
        rows = sorted(
            exposure_rows,
            key=lambda row: (str(row[0]) not in diffs, -(diffs.get(str(row[0]), 0.0))),
        )
        rows = [row + (diffs.get(str(row[0])),) for row in rows]
    chosen = []
    cumulative = 0.0
    for row in rows:
        marginal = float(row[15] or 0)
        cumulative += marginal
        chosen.append(row)
        if target and cumulative >= float(target):
            break
    if not target:
        chosen = rows
    points = []
    skipped = 0
    for row in chosen:
        lat, lon = row[1], row[2]
        if not _coord_ok(lat, lon):
            skipped += 1
            continue
        incept = row[8].strftime("%Y-%m-%d") if row[8] else ""
        expire = row[9].strftime("%Y-%m-%d") if row[9] else ""
        points.append({
            "id": str(row[0]),
            "lat": float(lat),
            "lon": float(lon),
            "construction": row[3] or "",
            "occupancy": row[4] or "",
            "stories": None if row[5] is None else int(row[5]),
            "yearBuilt": None if row[6] is None else int(row[6]),
            "floorArea": None if row[7] is None else float(row[7]),
            "incept": incept,
            "expire": expire,
            "state": row[10] or "",
            "county": row[11] or "",
            "postalCode": row[12] or "",
            "locationValue": None if row[13] is None else float(row[13]),
            "street": row[14] or "",
            "diff": None if row[15] is None else float(row[15]),
        })
    app.logger.warning(
        "Location map analysis %s plotted %s and skipped %s invalid coordinates",
        analysis_id, len(points), skipped)
    return points, skipped


def _map_view_points(analysis_id, premiums=None):
    """Every located exposure, with marginal DIFF and allocated premium.

    Premium to PML Ratio is premium divided by marginal DIFF. The map uses the
    100-year DIFF as the baseline when the risk has premium.
    """
    _ensure_location_exposure(analysis_id)
    conn = _open_dpo()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT ExposureLevelID, Latitude, Longitude, Construction, Occupancy,
                   NumberOfStories, YearBuilt, FloorArea, InceptDate, ExpireDate,
                   Admin1Code, Admin2Name, PostalCode, LocationValue, StreetAddress,
                   DistCoast, Elevation
            FROM dbo.tLocationExposure
            WHERE AnalysisID = ?
            """,
            analysis_id)
        rows = cur.fetchall()
    finally:
        conn.close()
    stored, periods = _read_result_curves(analysis_id)
    if premiums is None:
        premiums = _location_premiums(_analysis_header(analysis_id))
    ranges = _display_ranges()
    points = []
    skipped = 0
    for row in rows:
        lat, lon = row[1], row[2]
        if not _coord_ok(lat, lon):
            skipped += 1
            continue
        entity_id = str(row[0])
        by_period = stored["DIFF"].get(entity_id, {})
        points.append({
            "id": entity_id,
            "lat": float(lat),
            "lon": float(lon),
            "construction": row[3] or "",
            "occupancy": row[4] or "",
            "stories": None if row[5] is None else int(row[5]),
            "yearBuilt": None if row[6] is None else int(row[6]),
            "floorArea": None if row[7] is None else float(row[7]),
            "incept": row[8].strftime("%Y-%m-%d") if row[8] else "",
            "expire": row[9].strftime("%Y-%m-%d") if row[9] else "",
            "state": row[10] or "",
            "county": row[11] or "",
            "postalCode": row[12] or "",
            "locationValue": None if row[13] is None else float(row[13]),
            "street": "" if row[14] is None else str(row[14]).strip(),
            "dtc": None if row[15] is None else float(row[15]),
            "elevation": None if row[16] is None else float(row[16]),
            "premium": None if entity_id not in premiums else float(premiums.get(entity_id) or 0),
            "diffs": {str(period): float(by_period.get(period, 0.0)) for period in periods},
        })
    def _range_payload(bands):
        return [{"start": float(start), "end": float(end)} for start, end in bands]

    return {
        "periods": periods,
        "locations": points,
        "skipped": skipped,
        "ranges": {
            "dtc": _range_payload(ranges["dtc"]),
            "elevation": _range_payload(ranges["elevation"]),
        },
    }


def _money(value):
    if value is None:
        return "—"
    return f"{float(value):,.0f}"


@app.route("/reports")
def reports():
    selected = request.args.get("analysis_id", type=int)
    return_period = request.args.get("return_period", type=int)
    chart_limit = request.args.get("chart_limit", type=int)
    view = request.args.get("view") or "impact"
    error = None
    choices = []
    dashboard = None
    impact = None
    scenarios = []
    impacts = []
    map_view = None
    exposure_summary = None
    try:
        choices = _analysis_choices()
        if selected:
            dashboard = _dashboard_for(selected, return_period, chart_limit or 10)
            if dashboard:
                periods = dashboard["periods"]
                default_period = 100 if 100 in periods else periods[len(periods) // 2]
                posted_periods = request.args.getlist("scenario_period")
                posted_pml = request.args.getlist("scenario_pml")
                posted_premium = request.args.getlist("scenario_premium")
                posted_names = request.args.getlist("scenario_name")
                if posted_periods:
                    for index, raw_period in enumerate(posted_periods):
                        try:
                            period = int(raw_period)
                        except (TypeError, ValueError):
                            period = default_period
                        if period not in periods:
                            period = default_period
                        pml_raw = posted_pml[index] if index < len(posted_pml) else ""
                        prem_raw = posted_premium[index] if index < len(posted_premium) else ""
                        try:
                            pml_target = float(pml_raw) if str(pml_raw).strip() else None
                        except ValueError:
                            pml_target = None
                        try:
                            premium_target = float(prem_raw) if str(prem_raw).strip() else None
                        except ValueError:
                            premium_target = None
                        name = posted_names[index].strip() if index < len(posted_names) and posted_names[index].strip() else f"Option {index + 1}"
                        scenarios.append({
                            "name": name,
                            "period": period,
                            "pml": "" if pml_target is None else int(pml_target) if pml_target == int(pml_target) else pml_target,
                            "premium": "" if premium_target is None else int(premium_target) if premium_target == int(premium_target) else premium_target,
                            "pml_target": pml_target if pml_target and pml_target > 0 else None,
                            "premium_target": premium_target if premium_target and premium_target > 0 else None,
                        })
                else:
                    portfolio_pml = float(
                        (dashboard.get("portfolio_by_period") or {}).get(default_period) or 0)
                    drop = int(round(portfolio_pml * 0.10)) if portfolio_pml > 0 else None
                    scenarios = [{
                        "name": "Option 1",
                        "period": default_period,
                        "pml": "" if drop is None else drop,
                        "premium": "",
                        "pml_target": drop,
                        "premium_target": None,
                    }]
                premiums = _location_premiums(_analysis_header(selected))
                refresh_ep = request.args.get("refresh_ep") == "1"
                impacts = []
                for option in scenarios:
                    built = _pml_impact(
                        selected, option["period"], option["pml_target"],
                        option["premium_target"], premiums, option["name"],
                        refresh_ep=refresh_ep)
                    if built:
                        built["pml_input"] = option["pml"]
                        built["premium_input"] = option["premium"]
                        impacts.append(built)
                map_view = _map_view_points(selected, premiums)
                if dashboard.get("loss_level") == "Location":
                    exposure_summary = _exposure_summary(selected)
    except Exception as exc:
        error = f"Could not load the dashboard: {exc}"
    return render_template(
        "reports.html",
        choices=choices,
        selected=selected,
        dashboard=dashboard,
        impact=impact,
        scenarios=scenarios,
        impacts=impacts,
        map_view=map_view,
        exposure_summary=exposure_summary,
        view=view if view in ("results", "impact", "map") else "impact",
        error=error,
        chart_limits=_CHART_LIMITS,
        money=_money)


@app.route("/reports/<int:analysis_id>/exposures")
def location_exposures(analysis_id):
    rows, _total = _location_exposure(analysis_id)
    return jsonify(rows)


@app.route("/reports/<int:analysis_id>/map-points")
def location_map_points(analysis_id):
    return_period = request.args.get("impact_period", type=int) or 100
    target = request.args.get("impact_target", type=float)
    points, skipped = _location_map_points(analysis_id, return_period, target)
    response = jsonify(points)
    response.headers["X-Locations-Skipped"] = str(skipped)
    return response


@app.route("/reports/<int:analysis_id>/log")
def analysis_time_log(analysis_id):
    analysis = None
    user_log = []
    error = None
    try:
        analysis = _analysis_header(analysis_id)
        if not analysis:
            error = "Analysis was not found."
        else:
            user_log = _analysis_user_log(analysis_id)
    except Exception as exc:
        error = f"Could not load the analysis time log: {exc}"
    return render_template(
        "reports_log.html",
        analysis=analysis,
        user_log=user_log,
        error=error)


@app.route("/new")
def new_analysis():
    clients = []
    error = request.args.get("error")
    try:
        clients = _load_clients()
    except Exception as exc:
        error = error or f"Could not read clients from BMS_CMS: {exc}"
    return render_template("connect.html", clients=clients, error=error)


def _moodys_database_names(fragment):
    """Database names on the Moody's server whose name contains fragment."""
    conn = _open_saved(_moodys_login(), "master", False)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT name FROM sys.databases WHERE name LIKE ? ORDER BY name",
            f"%{fragment}%")
        return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


@app.route("/api/rdm-databases")
def api_rdm_databases():
    try:
        names = _moodys_database_names("RDM")
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"databases": names})


@app.route("/api/edm-databases")
def api_edm_databases():
    try:
        names = _moodys_database_names("EDM")
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"databases": names})


@app.route("/api/rdm-client")
def api_rdm_client():
    database = request.args.get("database", "").strip()
    try:
        conn = _open_cms()
        try:
            match = client_match.match_client(conn, database)
        finally:
            conn.close()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(match or {})


@app.route("/api/rdm-analyses")
def api_rdm_analyses():
    database = request.args.get("database", "").strip()
    try:
        conn = _open_moodys(database)
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT [ID], [NAME], [PERIL], [EXPOSUREID], [SRCEDM],
                       RTRIM([EngineVersion])
                FROM dbo.rdm_analysis
                WHERE ISGROUP = 0
                ORDER BY [NAME]
                """)
            rows = [{
                "id": int(r[0]),
                "name": r[1] or "",
                "peril": r[2] or "",
                "portfolioId": r[3],
                "edmName": r[4] or "",
                "engineVersion": (r[5] or "").strip(),
            } for r in cur.fetchall()]
        finally:
            conn.close()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"analyses": rows})


@app.route("/api/rdm-portstats")
def api_rdm_portstats():
    database = request.args.get("database", "").strip()
    try:
        analysis_id = int(request.args.get("analysis", "").strip())
    except ValueError:
        return jsonify({"error": "Analysis id is required."}), 400
    try:
        conn = _open_moodys(database)
        try:
            cur = conn.cursor()
            cur.execute(
                """
                WITH ranked AS (
                    SELECT
                        RTRIM(PERSPCODE) AS PerspCode,
                        PUREPREMIUM,
                        TOTALSTDDEV,
                        ROW_NUMBER() OVER (
                            PARTITION BY RTRIM(PERSPCODE)
                            ORDER BY ID
                        ) AS rn
                    FROM dbo.rdm_portstats
                    WHERE ANLSID = ?
                )
                SELECT PerspCode, PUREPREMIUM, TOTALSTDDEV
                FROM ranked
                WHERE rn = 1
                """,
                analysis_id)
            stats = [{
                "perspective": (r[0] or "").strip(),
                "aal": None if r[1] is None or r[1] < 0 else float(r[1]),
                "stdDev": None if r[2] is None or r[2] < 0 else float(r[2]),
            } for r in cur.fetchall()]
        finally:
            conn.close()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"stats": stats})


@app.route("/new", methods=["POST"])
def create_analysis():
    name = request.form.get("analysis_name", "").strip()
    description = request.form.get("analysis_description", "").strip() or None
    client_id = request.form.get("client_id", "").strip()
    edm_name = request.form.get("edm_name", "").strip() or None
    portfolio_raw = request.form.get("portfolio_id", "").strip()
    rdm_name = request.form.get("rdm_name", "").strip()
    rdm_analysis_id = request.form.get("rdm_analysis_id", "").strip()
    rdm_analysis_name = request.form.get("rdm_analysis_name", "").strip()
    if not name or not client_id or not rdm_name or not rdm_analysis_id or not rdm_analysis_name:
        return redirect(url_for("new_analysis", error="Analysis name, client, and an RDM analysis are required."))
    try:
        portfolio_id = int(portfolio_raw) if portfolio_raw else None
        rdm_id = int(rdm_analysis_id)
        client = int(client_id)
    except ValueError:
        return redirect(url_for("new_analysis", error="Client, portfolio, and RDM analysis id must be numbers."))
    try:
        peril = _ungrouped_peril(rdm_name, rdm_id)
    except ValueError as exc:
        return redirect(url_for("new_analysis", error=str(exc)))
    try:
        conn = _open_dpo()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                DECLARE @AnalysisID INT;
                EXEC dbo.usp_Analysis_Create
                    @AnalysisName=?, @AnalysisDescription=?, @ClientID=?,
                    @EdmName=?, @PortfolioID=?, @RdmName=?, @RdmAnalysisID=?,
                    @RdmAnalysisName=?, @Peril=?, @AnalysisID=@AnalysisID OUTPUT;
                SELECT @AnalysisID;
                """,
                name, description, client, edm_name, portfolio_id, rdm_name,
                rdm_id, rdm_analysis_name, peril)
            analysis_id = _first_value(cur)
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        return redirect(url_for("new_analysis", error=f"Could not create the analysis: {exc}"))
    return redirect(url_for("analysis_settings", analysis_id=analysis_id))


_PERSPECTIVE_LABELS = {
    "GU": "Ground up",
    "GR": "Gross",
    "RL": "Net",
}


def _client_folder(client_id):
    """Client name used as the results folder. The database still stores ClientID."""
    conn = _open_cms()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT ClientName FROM dbo.tClient WHERE ClientID = ?",
            int(client_id))
        row = cur.fetchone()
    finally:
        conn.close()
    if row and (row[0] or "").strip():
        return row[0].strip()
    return str(client_id)


def _result_parts(analysis):
    return (
        _client_folder(analysis["client_id"]),
        analysis["id"],
        analysis["name"],
        analysis["peril"],
    )


def _read_result_curves(analysis_id):
    analysis = _analysis_header(analysis_id)
    if not analysis:
        raise FileNotFoundError(f"Analysis {analysis_id} was not found.")
    return results_store.read_curves(*_result_parts(analysis))


def _analysis_header(analysis_id):
    conn = _open_dpo()
    try:
        cur = conn.cursor()
        cur.execute("EXEC dbo.usp_Analysis_Get @AnalysisID=?", analysis_id)
        row = cur.fetchone()
        if not row:
            return None
        return {
            "id": int(row[0]),
            "name": row[1],
            "description": row[2] or "",
            "client_id": int(row[3]),
            "edm_name": row[4] or "",
            "portfolio_id": row[5],
            "rdm_name": row[6],
            "rdm_analysis_id": int(row[7]),
            "rdm_analysis_name": row[8],
            "peril": row[9] or "",
        }
    finally:
        conn.close()


def _analysis_ep_points(analysis_id):
    """Return (return periods, custom, defaults).

    Custom is true when this analysis already has its own points.
    Defaults are the active catalog points.
    """
    conn = _open_dpo()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT DISTINCT e.ReturnPeriod
            FROM dbo.tAnalysisEpPoint AS a
            INNER JOIN dbo.tEpPoint AS e
                ON e.EpPointID = a.EpPointID
            INNER JOIN dbo.tAnalysisSettings AS s
                ON s.AnalysisSettingsID = a.AnalysisSettingsID
            WHERE s.AnalysisID = ?
            ORDER BY e.ReturnPeriod
            """,
            analysis_id)
        custom = [int(r[0]) for r in cur.fetchall()]
        cur.execute("EXEC dbo.usp_EpPoint_List")
        defaults = [int(r[1]) for r in cur.fetchall()]
        if custom:
            return custom, True, defaults
        return defaults, False, defaults
    finally:
        conn.close()


def _analysis_settings_rows(analysis_id):
    conn = _open_dpo()
    try:
        cur = conn.cursor()
        cur.execute("EXEC dbo.usp_AnalysisSettings_List @AnalysisID=?", analysis_id)
        rows = []
        for r in cur.fetchall():
            created = r[4]
            rows.append({
                "id": int(r[0]),
                "analysis_id": int(r[1]),
                "loss_level": r[2],
                "loss_perspective": r[3],
                "created_at": created.strftime("%Y-%m-%d %H:%M") if created else "",
            })
        return rows
    finally:
        conn.close()


@app.route("/analysis/<int:analysis_id>/settings")
def analysis_settings(analysis_id):
    try:
        analysis = _analysis_header(analysis_id)
        levels, perspectives = _dpo_form_lists()
        settings = _analysis_settings_rows(analysis_id)
    except Exception as exc:
        return redirect(url_for("new_analysis", error=f"Could not open analysis settings: {exc}"))
    if not analysis:
        return redirect(url_for("new_analysis", error="Analysis was not found."))
    if not levels:
        levels = ["Account", "Policy", "Location", "Lob", "County", "State", "Other"]
    if not perspectives:
        perspectives = ["GU", "GR", "RL"]
    perspective_choices = [
        {"code": code, "label": _PERSPECTIVE_LABELS.get(code, code)}
        for code in perspectives
    ]
    try:
        ep_points, ep_custom, default_ep_points = _analysis_ep_points(analysis_id)
    except Exception:
        ep_points, ep_custom, default_ep_points = [], False, []
    try:
        can_resume = bool((_analysis_progress().get(analysis_id) or {}).get("can_resume"))
    except Exception:
        can_resume = False
    return render_template(
        "analysis_settings.html",
        analysis=analysis,
        settings=settings,
        loss_levels=levels,
        perspectives=perspective_choices,
        ep_points=_ep_slots(ep_points),
        ep_custom=ep_custom,
        default_ep_points=_ep_slots(default_ep_points),
        error=request.args.get("error"),
        saved=request.args.get("saved"),
        open_ep=request.args.get("ep") == "1",
        can_resume=can_resume,
    )


@app.route("/analysis/<int:analysis_id>/ep-points", methods=["POST"])
def save_analysis_ep_points(analysis_id):
    raw = request.form.getlist("return_period")
    periods = []
    for value in raw:
        text = value.strip()
        if not text:
            continue
        try:
            period = int(text)
        except ValueError:
            return redirect(url_for(
                "analysis_settings", analysis_id=analysis_id, ep=1,
                error="EP points must be whole numbers."))
        if period < 0:
            return redirect(url_for(
                "analysis_settings", analysis_id=analysis_id, ep=1,
                error="EP points must be zero or greater."))
        if period == 0:
            continue
        if period not in periods:
            periods.append(period)
    if not periods:
        return redirect(url_for(
            "analysis_settings", analysis_id=analysis_id, ep=1,
            error="Enter at least one EP point."))
    if len(periods) > 15:
        return redirect(url_for(
            "analysis_settings", analysis_id=analysis_id, ep=1,
            error="The application only supports up to 15 points."))
    try:
        conn = _open_dpo()
        try:
            cur = conn.cursor()
            cur.execute(
                "EXEC dbo.usp_AnalysisEpPoint_Replace @AnalysisID=?, @ReturnPeriods=?",
                analysis_id, ",".join(str(p) for p in periods))
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        message = str(exc)
        if "50007" in message:
            message = "Save a loss level and perspective before saving EP points."
        else:
            message = f"Could not save EP points: {exc}"
        return redirect(url_for("analysis_settings", analysis_id=analysis_id, ep=1, error=message))
    return redirect(url_for("analysis_settings", analysis_id=analysis_id, saved="ep"))


@app.route("/analysis/<int:analysis_id>/settings", methods=["POST"])
def save_analysis_settings(analysis_id):
    loss_level = request.form.get("loss_level", "").strip()
    perspective = request.form.get("loss_perspective", "").strip()
    if loss_level not in ("Account", "Policy", "Location", "Lob", "County", "State", "Other"):
        return redirect(url_for(
            "analysis_settings", analysis_id=analysis_id,
            error="Loss level is not recognized."))
    if perspective not in ("GU", "GR", "RL"):
        return redirect(url_for(
            "analysis_settings", analysis_id=analysis_id,
            error="Loss perspective is not recognized."))
    try:
        conn = _open_dpo()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                DECLARE @AnalysisSettingsID INT;
                EXEC dbo.usp_AnalysisSettings_Create
                    @AnalysisID=?, @LossLevelName=?, @LossPerspectiveCode=?,
                    @AnalysisSettingsID=@AnalysisSettingsID OUTPUT;
                SELECT @AnalysisSettingsID;
                """,
                analysis_id, loss_level, perspective)
            _first_value(cur)
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        message = str(exc)
        if "UQ_tAnalysisSettings" in message or "duplicate" in message.lower():
            message = "That loss level and loss perspective are already saved for this analysis."
        else:
            message = f"Could not save analysis settings: {exc}"
        return redirect(url_for("analysis_settings", analysis_id=analysis_id, error=message))
    return redirect(url_for("analysis_settings", analysis_id=analysis_id, saved="1"))


_LOSS_LEVEL_TABLE = {
    "Account": "dbo.rdm_account",
    "Policy": "dbo.rdm_policy",
    "Location": "dbo.rdm_loc",
    "State": "dbo.rdm_geoidlob",
    "County": "dbo.rdm_geoidlob",
}
_STATS_TABLE = {
    "Account": "dbo.rdm_accountstats",
    "Policy": "dbo.rdm_policystats",
    "Location": "dbo.rdm_locstats",
}


def _entity_count(rdm_cur, loss_level, rdm_analysis_id, perspective):
    """Unique entity ids for this RMS analysis and perspective."""
    table = _LOSS_LEVEL_TABLE.get(loss_level)
    if table is None:
        raise RuntimeError(f"Loss level {loss_level} does not have an RDM loss table.")
    if loss_level == "State":
        sql = f"""
            SELECT COUNT(DISTINCT STATE)
            FROM {table}
            WHERE ANLSID = ?
              AND RTRIM(PERSPCODE) = ?
              AND STATE IS NOT NULL
              AND RTRIM(STATE) <> ''
        """
    elif loss_level == "County":
        sql = f"""
            SELECT COUNT(*)
            FROM (
                SELECT DISTINCT STATE, COUNTYNUM
                FROM {table}
                WHERE ANLSID = ?
                  AND RTRIM(PERSPCODE) = ?
                  AND COUNTYNUM IS NOT NULL
            ) AS counties
        """
    else:
        sql = f"""
            SELECT COUNT(DISTINCT ID)
            FROM {table}
            WHERE ANLSID = ?
              AND RTRIM(PERSPCODE) = ?
        """
    rdm_cur.execute(sql, rdm_analysis_id, perspective)
    row = rdm_cur.fetchone()
    return int(row[0] or 0)


def _present_metric(value):
    """RMS uses -1 when a portfolio metric was not populated."""
    if value is None:
        return None
    number = float(value)
    if number < 0:
        return None
    return number


def _portfolio_curve(rdm_cur, rdm_analysis_id, perspective, portfolio_id):
    """Return-period points from the admin EP type, plus portfolio AAL and standard deviation.

    The admin EP type selects rdm_portep only. AAL and standard deviation come
    from rdm_portstats for this analysis and perspective, regardless of EPTYPE.
    When that row was not published, both are calculated from the full event set.
    """
    portfolio = int(portfolio_id) if portfolio_id is not None else None
    if portfolio is None:
        rdm_cur.execute(
            """
            SELECT TOP 1 ID
            FROM dbo.rdm_port
            WHERE ANLSID = ? AND RTRIM(PERSPCODE) = ?
            ORDER BY ID
            """,
            rdm_analysis_id, perspective)
        found = rdm_cur.fetchone()
        if not found:
            raise RuntimeError(f"No rdm_port event losses for perspective {perspective}.")
        portfolio = int(found[0])
    ep_type = rdm_settings.load_ep_type()
    rdm_cur.execute(
        """
        SELECT EP, PERSPVALUE
        FROM dbo.rdm_portep
        WHERE ANLSID = ?
          AND RTRIM(PERSPCODE) = ?
          AND EPTYPE = ?
          AND ID = ?
        """,
        rdm_analysis_id, perspective, ep_type, portfolio)
    rows = rdm_cur.fetchall()
    if not rows:
        curve = "OEP" if ep_type == 1 else "AEP"
        raise RuntimeError(f"No {curve} curve in rdm_portep for perspective {perspective}.")
    rdm_cur.execute(
        """
        SELECT TOP 1 PUREPREMIUM, TOTALSTDDEV
        FROM dbo.rdm_portstats
        WHERE ANLSID = ?
          AND RTRIM(PERSPCODE) = ?
        ORDER BY ID
        """,
        rdm_analysis_id, perspective)
    stats = rdm_cur.fetchone()
    aal = _present_metric(stats[0]) if stats else None
    standard_deviation = _present_metric(stats[1]) if stats else None
    if aal is None or standard_deviation is None:
        _event_ids, mean, event_std, _exposure, rate = _portfolio_elt(
            rdm_cur, rdm_analysis_id, perspective, portfolio)
        calculated_aal, calculated_std = rdm_analysis.annual_metrics(mean, event_std, rate)
        aal = calculated_aal if aal is None else aal
        standard_deviation = calculated_std if standard_deviation is None else standard_deviation
    return [float(r[0]) for r in rows], [float(r[1]) for r in rows], aal, standard_deviation


def _portfolio_elt(rdm_cur, rdm_analysis_id, perspective, portfolio_id):
    """Portfolio event losses, uncertainty, and annual rates."""
    rdm_cur.execute(
        """
        SELECT p.EVENTID, p.PERSPVALUE, p.STDDEVI, p.STDDEVC, p.EXPVALUE, ISNULL(e.RATE, 0)
        FROM dbo.rdm_port p
        LEFT JOIN dbo.rdm_anlsevent e
          ON e.ANLSID = p.ANLSID AND e.EVENTID = p.EVENTID
        WHERE p.ANLSID = ?
          AND RTRIM(p.PERSPCODE) = ?
          AND (? IS NULL OR p.ID = ?)
        """,
        rdm_analysis_id, perspective, portfolio_id, portfolio_id)
    rows = rdm_cur.fetchall()
    if not rows:
        raise RuntimeError(f"No rdm_port event losses for perspective {perspective}.")
    event_ids = np.array([int(r[0]) for r in rows], dtype=np.int64)
    mean = np.array([float(r[1]) for r in rows], dtype=float)
    # RMS fits the event beta with the sum of the independent and correlated
    # standard deviations. Quadrature understates the occurrence curve.
    std = (
        np.array([float(r[2] or 0) for r in rows], dtype=float)
        + np.array([float(r[3] or 0) for r in rows], dtype=float))
    exposure = np.array([float(r[4] or 0) for r in rows], dtype=float)
    rate = np.array([float(r[5] or 0) for r in rows], dtype=float)
    return event_ids, mean, std, exposure, rate


def _level_stats(rdm_cur, loss_level, rdm_analysis_id, perspective):
    """Pure premium and total standard deviation by loss-level id.

    The full-event-set row (EPTYPE 0) is used when that analysis published one.
    Otherwise the published row for the id is used. A negative RMS placeholder
    is left unset so the caller can calculate from the event losses.
    """
    table = _STATS_TABLE.get(loss_level)
    if table is None:
        return {}
    rdm_cur.execute(
        f"""
        SELECT CONVERT(varchar(40), ID), EPTYPE, PUREPREMIUM, TOTALSTDDEV
        FROM {table}
        WHERE ANLSID = ? AND RTRIM(PERSPCODE) = ?
        """,
        rdm_analysis_id, perspective)
    chosen = {}
    chosen_type = {}
    for entity_id, ep_type, pure_premium, total_stddev in rdm_cur.fetchall():
        key = str(entity_id).strip()
        ep_type = int(ep_type or 0)
        if key in chosen and not (chosen_type[key] != 0 and ep_type == 0):
            continue
        chosen[key] = (_present_metric(pure_premium), _present_metric(total_stddev))
        chosen_type[key] = ep_type
    return chosen


def _portfolio_stats(rdm_cur, rdm_analysis_id, perspective, _portfolio_id):
    """Published portfolio pure premium and total standard deviation.

    Matched on analysis and perspective only. EPTYPE is not required.
    """
    rdm_cur.execute(
        """
        SELECT TOP 1 PUREPREMIUM, TOTALSTDDEV
        FROM dbo.rdm_portstats
        WHERE ANLSID = ?
          AND RTRIM(PERSPCODE) = ?
        ORDER BY ID
        """,
        rdm_analysis_id, perspective)
    stats = rdm_cur.fetchone()
    if not stats:
        return None, None
    return _present_metric(stats[0]), _present_metric(stats[1])


def _annual_from_losses(rate_by_event, losses):
    """AAL and standard deviation from event losses and annual rates."""
    if not losses:
        return 0.0, 0.0
    event_ids = list(losses)
    mean = np.array([float(losses[event_id] or 0) for event_id in event_ids], dtype=float)
    rate = np.array([float(rate_by_event.get(int(event_id), 0.0)) for event_id in event_ids], dtype=float)
    return rdm_analysis.annual_metrics(mean, np.zeros(mean.shape, dtype=float), rate)


def _removed_losses(event_ids, entity_losses):
    removed = np.zeros(len(event_ids), dtype=float)
    index = {int(event_id): slot for slot, event_id in enumerate(event_ids)}
    for event_id, loss in entity_losses.items():
        slot = index.get(int(event_id))
        if slot is not None:
            removed[slot] = float(loss or 0)
    return removed


def _run_loss_context(run_id):
    conn = _open_dpo()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT ll.LossLevelName, lp.LossPerspectiveCode
            FROM dbo.tAnalysisRun AS r
            INNER JOIN dbo.tAnalysisSettings AS s
                ON s.AnalysisSettingsID = r.AnalysisSettingsID
            INNER JOIN dbo.tLossLevel AS ll ON ll.LossLevelID = s.LossLevelID
            INNER JOIN dbo.tLossPerspective AS lp
                ON lp.LossPerspectiveID = s.LossPerspectiveID
            WHERE r.AnalysisRunID = ?
            """,
            run_id)
        row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        raise RuntimeError("The analysis run has no loss level.")
    return row[0], row[1]


def _result_metrics(analysis, run_id):
    """EXCL metrics are the portfolio without the risk. DIFF metrics are the risk."""
    loss_level, perspective = _run_loss_context(run_id)
    rdm = _open_moodys(analysis["rdm_name"])
    try:
        rdm_cur = rdm.cursor()
        event_ids, mean, std, _exposure, rate = _portfolio_elt(
            rdm_cur, analysis["rdm_analysis_id"], perspective, analysis["portfolio_id"])
        rate_by_event = {
            int(event_id): float(value) for event_id, value in zip(event_ids, rate)
        }
        portfolio_aal, portfolio_std = _portfolio_stats(
            rdm_cur, analysis["rdm_analysis_id"], perspective, analysis["portfolio_id"])
        if portfolio_aal is None or portfolio_std is None:
            calculated_aal, calculated_std = rdm_analysis.annual_metrics(mean, std, rate)
            portfolio_aal = calculated_aal if portfolio_aal is None else portfolio_aal
            portfolio_std = calculated_std if portfolio_std is None else portfolio_std
        stats = _level_stats(
            rdm_cur, loss_level, analysis["rdm_analysis_id"], perspective)
        by_entity = _entity_event_losses(
            rdm_cur, loss_level, analysis["rdm_analysis_id"], perspective)
    finally:
        rdm.close()
    metrics = {
        "PORTFOLIO": {
            "excl_aal": portfolio_aal,
            "excl_stddev": portfolio_std,
            "diff_aal": 0.0,
            "diff_stddev": 0.0,
        }
    }
    for entity_id, entity_losses in by_entity.items():
        published = stats.get(entity_id)
        if published is not None and published[0] is not None and published[1] is not None:
            diff_aal, diff_std = published
        else:
            diff_aal, diff_std = _annual_from_losses(rate_by_event, entity_losses)
        excl_aal, excl_std = rdm_analysis.excluding_annual(
            mean, std, rate, _removed_losses(event_ids, entity_losses))
        metrics[str(entity_id)] = {
            "excl_aal": excl_aal,
            "excl_stddev": excl_std,
            "diff_aal": diff_aal,
            "diff_stddev": diff_std,
        }
    return metrics


def _stage_finished_ids(rdm_cur, finished_ids):
    """Put completed numeric risk ids in a temp table on this RDM connection."""
    numbers = []
    for entity_id in finished_ids or ():
        try:
            numbers.append(int(entity_id))
        except (TypeError, ValueError):
            continue
    if not numbers:
        return False
    rdm_cur.execute("IF OBJECT_ID('tempdb..#FinishedRisk') IS NOT NULL DROP TABLE #FinishedRisk")
    rdm_cur.execute("CREATE TABLE #FinishedRisk (ID BIGINT NOT NULL PRIMARY KEY)")
    for offset in range(0, len(numbers), 800):
        chunk = numbers[offset:offset + 800]
        values = ",".join(f"({number})" for number in chunk)
        rdm_cur.execute(f"INSERT INTO #FinishedRisk (ID) VALUES {values}")
    return True


def _iter_risk_event_losses(rdm_cur, loss_level, rdm_analysis_id, perspective, finished_ids=None):
    """Yield one risk's event losses at a time, ordered by risk id.

    Account rows are ordered by ACCGRPID, policy rows by policy id, and
    location rows by location id. The caller keeps only the portfolio ELT
    and the risk that was just yielded. finished_ids are not read again.
    """
    table = _LOSS_LEVEL_TABLE.get(loss_level)
    if table is None:
        raise RuntimeError(f"Loss level {loss_level} does not have an RDM loss table.")
    finished_ids = {str(entity_id).strip() for entity_id in (finished_ids or ())}
    skip_sql = _stage_finished_ids(rdm_cur, finished_ids) and loss_level not in ("State", "County")
    if loss_level == "State":
        sql = f"""
            SELECT RTRIM(STATE), EVENTID, SUM(PERSPVALUE)
            FROM {table}
            WHERE ANLSID = ? AND RTRIM(PERSPCODE) = ?
              AND STATE IS NOT NULL AND RTRIM(STATE) <> ''
            GROUP BY RTRIM(STATE), EVENTID
            ORDER BY RTRIM(STATE), EVENTID
        """
    elif loss_level == "County":
        sql = f"""
            SELECT RTRIM(STATE) + '-' + CONVERT(varchar(20), COUNTYNUM), EVENTID, SUM(PERSPVALUE)
            FROM {table}
            WHERE ANLSID = ? AND RTRIM(PERSPCODE) = ? AND COUNTYNUM IS NOT NULL
            GROUP BY RTRIM(STATE), COUNTYNUM, EVENTID
            ORDER BY RTRIM(STATE), COUNTYNUM, EVENTID
        """
    else:
        sql = f"""
            SELECT CONVERT(varchar(40), ID), EVENTID, SUM(PERSPVALUE)
            FROM {table}
            WHERE ANLSID = ? AND RTRIM(PERSPCODE) = ?
        """
        if skip_sql:
            sql += f" AND NOT EXISTS (SELECT 1 FROM #FinishedRisk AS f WHERE f.ID = {table}.ID)"
        sql += " GROUP BY ID, EVENTID ORDER BY ID, EVENTID"
    rdm_cur.execute(sql, rdm_analysis_id, perspective)
    current_id = None
    losses = {}
    row = rdm_cur.fetchone()
    while row is not None:
        entity_id = str(row[0]).strip()
        if current_id is None:
            current_id = entity_id
        elif entity_id != current_id:
            if current_id not in finished_ids:
                yield current_id, losses
            losses = {}
            current_id = entity_id
        losses[int(row[1])] = float(row[2] or 0)
        row = rdm_cur.fetchone()
    if current_id is not None and current_id not in finished_ids:
        yield current_id, losses


def _entity_event_losses(rdm_cur, loss_level, rdm_analysis_id, perspective, limit=None):
    """Event losses for each loss-level id, keyed by entity id.

    limit keeps the ids with the largest total loss. Used for a short review run.
    """
    table = _LOSS_LEVEL_TABLE.get(loss_level)
    if table is None:
        raise RuntimeError(f"Loss level {loss_level} does not have an RDM loss table.")
    if limit and loss_level not in ("State", "County"):
        sql = f"""
            WITH top_ids AS (
                SELECT TOP ({int(limit)}) ID
                FROM {table}
                WHERE ANLSID = ? AND RTRIM(PERSPCODE) = ?
                GROUP BY ID
                ORDER BY SUM(PERSPVALUE) DESC
            )
            SELECT CONVERT(varchar(40), t.ID), t.EVENTID, SUM(t.PERSPVALUE)
            FROM {table} AS t
            INNER JOIN top_ids AS i ON i.ID = t.ID
            WHERE t.ANLSID = ? AND RTRIM(t.PERSPCODE) = ?
            GROUP BY t.ID, t.EVENTID
        """
        rdm_cur.execute(sql, rdm_analysis_id, perspective, rdm_analysis_id, perspective)
        by_entity = {}
        for entity_id, event_id, loss in rdm_cur.fetchall():
            by_entity.setdefault(str(entity_id), {})[int(event_id)] = float(loss or 0)
        return by_entity
    if loss_level == "State":
        sql = f"""
            SELECT RTRIM(STATE), EVENTID, SUM(PERSPVALUE)
            FROM {table}
            WHERE ANLSID = ? AND RTRIM(PERSPCODE) = ?
              AND STATE IS NOT NULL AND RTRIM(STATE) <> ''
            GROUP BY RTRIM(STATE), EVENTID
        """
    elif loss_level == "County":
        sql = f"""
            SELECT RTRIM(STATE) + '-' + CONVERT(varchar(20), COUNTYNUM), EVENTID, SUM(PERSPVALUE)
            FROM {table}
            WHERE ANLSID = ? AND RTRIM(PERSPCODE) = ? AND COUNTYNUM IS NOT NULL
            GROUP BY RTRIM(STATE), COUNTYNUM, EVENTID
        """
    else:
        sql = f"""
            SELECT CONVERT(varchar(40), ID), EVENTID, SUM(PERSPVALUE)
            FROM {table}
            WHERE ANLSID = ? AND RTRIM(PERSPCODE) = ?
            GROUP BY ID, EVENTID
        """
    rdm_cur.execute(sql, rdm_analysis_id, perspective)
    by_entity = {}
    for entity_id, event_id, loss in rdm_cur.fetchall():
        by_entity.setdefault(str(entity_id), {})[int(event_id)] = float(loss or 0)
    return by_entity


def _check_message(label, actual, expected, actual_name, expected_name):
    """Say whether a location total matches the portfolio figure."""
    gap = actual - expected
    if abs(expected) <= 1.0:
        ok = abs(gap) <= 1.0
        share = ""
    else:
        ok = abs(gap) <= max(1.0, abs(expected) * 1e-4)
        share = f" ({gap / expected * 100:.2f}%)"
    status = "Pass" if ok else "Fail"
    return (
        f"Check {label}: sum of {actual_name} {actual:,.2f} vs {expected_name} "
        f"{expected:,.2f}. Difference {gap:,.2f}{share}. {status}."
    )


def _result_check_messages(stored, periods):
    """Compare location DIFF totals with the portfolio PML and AAL."""
    portfolio = stored["EXCL"].get("PORTFOLIO") or stored["DIFF"].get("PORTFOLIO") or {}
    metrics = stored.get("METRICS") or {}
    portfolio_aal = float((metrics.get("PORTFOLIO") or {}).get("excl_aal") or 0.0)
    locations = [entity for entity in stored["DIFF"] if entity != "PORTFOLIO"]
    aal_sum = 0.0
    for entity_id in locations:
        value = (metrics.get(entity_id) or {}).get("diff_aal")
        if value is not None:
            aal_sum += float(value)
    messages = [_check_message("AAL", aal_sum, portfolio_aal, "location DIFF AAL", "portfolio AAL")]
    for period in periods:
        diff_sum = sum(float(stored["DIFF"][entity].get(period, 0.0)) for entity in locations)
        portfolio_loss = float(portfolio.get(period, 0.0))
        messages.append(_check_message(
            f"1/{period}", diff_sum, portfolio_loss, "location DIFF", "portfolio PML"))
    return messages


def _log_result_checks(analysis_id, run_id):
    """Write the DIFF and AAL reconciliation to the analysis log."""
    stored, periods = _read_result_curves(analysis_id)
    locations = sum(1 for entity in stored["DIFF"] if entity != "PORTFOLIO")
    if not locations:
        return
    for message in _result_check_messages(stored, periods):
        _log_analysis_time(analysis_id, run_id, None, locations, locations, message)


def _record_progress(cur, analysis_id, run_id, completed, total, message):
    """Store how many risks are done so the analysis list can draw the pie."""
    cur.execute(
        """
        EXEC dbo.usp_AnalysisUserLog_Add
            @AnalysisID=?, @AnalysisRunID=?, @ElapsedSeconds=?,
            @LocationsCompleted=?, @LocationsTotal=?, @Message=?
        """,
        analysis_id, run_id, None, completed, total, (message or "")[:500])


def _mark_run_failed(run_id, exc):
    if not run_id:
        return
    try:
        conn = _open_dpo()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                EXEC dbo.usp_AnalysisRun_SetStatus
                    @AnalysisRunID=?, @RunStatus=?, @ErrorMessage=?
                """,
                int(run_id), "Failed", str(exc)[:4000])
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _log_analysis_time(analysis_id, run_id, elapsed_seconds, completed, total, message):
    """Append a timing row that can be read back by AnalysisID."""
    conn = _open_dpo()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            EXEC dbo.usp_AnalysisUserLog_Add
                @AnalysisID=?, @AnalysisRunID=?, @ElapsedSeconds=?,
                @LocationsCompleted=?, @LocationsTotal=?, @Message=?
            """,
            analysis_id, run_id, elapsed_seconds, completed, total, message[:500])
        conn.commit()
    finally:
        conn.close()


def _entity_event_losses_for_ids(rdm_cur, loss_level, rdm_analysis_id, perspective, entity_ids):
    """Event losses for an explicit set of loss-level ids."""
    table = _LOSS_LEVEL_TABLE.get(loss_level)
    if table is None or loss_level in ("State", "County"):
        raise RuntimeError(f"Loss level {loss_level} cannot be loaded by id list.")
    numbers = [int(entity_id) for entity_id in entity_ids]
    if not numbers:
        return {}
    id_list = ",".join(str(number) for number in numbers)
    rdm_cur.execute(
        f"""
        SELECT CONVERT(varchar(40), ID), EVENTID, SUM(PERSPVALUE)
        FROM {table}
        WHERE ANLSID = ? AND RTRIM(PERSPCODE) = ? AND ID IN ({id_list})
        GROUP BY ID, EVENTID
        """,
        rdm_analysis_id, perspective)
    by_entity = {}
    for entity_id, event_id, loss in rdm_cur.fetchall():
        by_entity.setdefault(str(entity_id), {})[int(event_id)] = float(loss or 0)
    return by_entity


def _complete_remaining_locations(analysis, batch_size=25):
    """Write excluding curves for loss-level ids missing from the latest run."""
    started = time.perf_counter()
    _ensure_location_exposure(analysis["id"])
    points, _custom, _defaults = _analysis_ep_points(analysis["id"])
    points = [point for point in points if point > 0][:15]
    settings = _analysis_settings_rows(analysis["id"])
    if not settings:
        raise RuntimeError("Save a loss level and perspective before writing portfolio results.")
    row = settings[0]
    dpo = _open_dpo()
    try:
        cur = dpo.cursor()
        cur.execute(
            """
            SELECT TOP 1 r.AnalysisRunID
            FROM dbo.tAnalysisRun AS r
            INNER JOIN dbo.tAnalysisSettings AS s
                ON s.AnalysisSettingsID = r.AnalysisSettingsID
            WHERE s.AnalysisID = ? AND r.RunStatus = 'Complete'
            ORDER BY r.AnalysisRunID DESC
            """,
            analysis["id"])
        found = cur.fetchone()
        if not found:
            raise RuntimeError("No completed run exists for this analysis.")
        run_id = int(found[0])
        cur.execute(
            """
            SELECT ReturnPeriod, PmlValue
            FROM dbo.tPmlResult
            WHERE AnalysisRunID = ? AND ResultType = 'EXCL' AND EntityID = N'PORTFOLIO'
            ORDER BY ReturnPeriod
            """,
            run_id)
        portfolio = {int(period): float(value) for period, value in cur.fetchall()}
        points = [point for point in points if point in portfolio]
        cur.execute(
            """
            SELECT DISTINCT EntityID
            FROM dbo.tPmlResult
            WHERE AnalysisRunID = ? AND EntityID <> N'PORTFOLIO'
            """,
            run_id)
        done_ids = {str(item[0]) for item in cur.fetchall()}
    finally:
        dpo.close()

    rdm = _open_moodys(analysis["rdm_name"])
    try:
        rdm_cur = rdm.cursor()
        total = _entity_count(
            rdm_cur, row["loss_level"], analysis["rdm_analysis_id"], row["loss_perspective"])
        event_ids, elt_mean, elt_std, elt_exposure, elt_rate = _portfolio_elt(
            rdm_cur, analysis["rdm_analysis_id"], row["loss_perspective"], analysis["portfolio_id"])
        ceilings = rdm_analysis.pml_from_elt(
            elt_mean, elt_std, elt_exposure, elt_rate, points)
        table = _LOSS_LEVEL_TABLE[row["loss_level"]]
        rdm_cur.execute(
            f"""
            SELECT CONVERT(varchar(40), ID)
            FROM {table}
            WHERE ANLSID = ? AND RTRIM(PERSPCODE) = ?
            GROUP BY ID
            ORDER BY ID
            """,
            analysis["rdm_analysis_id"], row["loss_perspective"])
        remaining = [str(item[0]) for item in rdm_cur.fetchall() if str(item[0]) not in done_ids]
        _log_analysis_time(
            analysis["id"], run_id, 0, len(done_ids), total,
            f"Started the remaining {len(remaining)} of {total} locations.")
        dpo = _open_dpo()
        try:
            dpo_cur = dpo.cursor()
            _ensure_excl_metric_table(dpo_cur)
            dpo.commit()
            completed_now = 0
            for offset in range(0, len(remaining), batch_size):
                batch = remaining[offset:offset + batch_size]
                by_entity = _entity_event_losses_for_ids(
                    rdm_cur, row["loss_level"], analysis["rdm_analysis_id"],
                    row["loss_perspective"], batch)
                for entity_id in batch:
                    excl_values, excl_aal, excl_stddev = _excluding_curve(
                        event_ids, elt_mean, elt_std, elt_exposure, elt_rate,
                        by_entity.get(entity_id, {}), points, ceilings)
                    _insert_excl_metric(dpo_cur, run_id, entity_id, excl_aal, excl_stddev)
                    for period, excl_value in zip(points, excl_values):
                        if excl_value != excl_value:
                            raise RuntimeError(
                                f"No excluding loss at return period {period} for {entity_id}.")
                        portfolio_value = portfolio[int(period)]
                        dpo_cur.execute(
                            """
                            EXEC dbo.usp_PmlResult_Insert
                                @AnalysisRunID=?, @ResultType=?, @EntityID=?,
                                @ReturnPeriod=?, @PmlValue=?
                            """,
                            run_id, "EXCL", entity_id, int(period), float(excl_value))
                        dpo_cur.execute(
                            """
                            EXEC dbo.usp_PmlResult_Insert
                                @AnalysisRunID=?, @ResultType=?, @EntityID=?,
                                @ReturnPeriod=?, @PmlValue=?
                            """,
                            run_id, "DIFF", entity_id, int(period),
                            max(portfolio_value - float(excl_value), 0.0))
                    completed_now += 1
                dpo.commit()
                elapsed = time.perf_counter() - started
                _log_analysis_time(
                    analysis["id"], run_id, round(elapsed, 1),
                    len(done_ids) + completed_now, total,
                    f"Wrote {completed_now} remaining locations.")
                print(
                    f"progress {len(done_ids) + completed_now}/{total} "
                    f"elapsed {elapsed:.1f}",
                    flush=True)
        finally:
            dpo.close()
        elapsed = time.perf_counter() - started
        each = elapsed / completed_now if completed_now else 0
        _log_analysis_time(
            analysis["id"], run_id, round(elapsed, 1),
            len(done_ids) + completed_now, total,
            f"Completed {completed_now} remaining locations in {elapsed:.1f} seconds "
            f"({each:.1f} seconds each).")
        if row["loss_level"] == "Location":
            _save_location_exposure(analysis)
        _publish_result_file(analysis["id"], run_id)
        print(f"DONE {elapsed:.1f}", flush=True)
        return elapsed, completed_now, total
    finally:
        rdm.close()


def _excluding_curve(event_ids, mean, std, exposure, rate, entity_losses, points, ceilings=None):
    """Subtract one entity's event losses from the portfolio ELT and rebuild the curve.

    Also returns the excluding AAL and standard deviation. AAL is the event
    rate times the remaining perspvalue.
    """
    return rdm_analysis.excluding_pml(
        event_ids, mean, std, exposure, rate, entity_losses, points, ceilings)


# RDM peril code -> (loccvg PERIL, EDM detail table). The physical tables are
# eqdet and hudet. Grouped analyses are peril YY and have no detail table.
_PERIL_DETAIL = {
    "EQ": (1, "eqdet"),
    "FF": (1, "eqdet"),
    "WS": (2, "hudet"),
    "HU": (2, "hudet"),
    "TO": (3, "todet"),
    "CS": (3, "todet"),
    "WT": (3, "todet"),
    "FL": (4, "fldet"),
    "FR": (5, "frdet"),
    "TR": (6, "trdet"),
}
_CLASS_BY_TABLE = {
    "eqdet": (("RMS", "RMSCLASS"), ("ATC", "ATCCLASS"), ("ISO", "ISOCLASS"), ("FIRE", "FIRECLASS"), ("USER", "USERCLASS")),
    "hudet": (("RMS", "RMSCLASS"), ("FIRE", "FIRECLASS"), ("USER", "USERCLASS")),
    "todet": (("RMS", "RMSCLASS"), ("FIRE", "FIRECLASS"), ("USER", "USERCLASS")),
}
_OCC_BY_TABLE = {
    "eqdet": (("ATC", "ATCOCC"), ("SIC", "SICOCC"), ("ISO", "ISOOCC"), ("IBC", "IBCOCC"), ("USER", "USEROCC")),
    "hudet": (("ATC", "ATCOCC"), ("SIC", "SICOCC"), ("ISO", "ISOOCC"), ("IBC", "IBCOCC"), ("USER", "USEROCC")),
    "todet": (("ATC", "ATCOCC"), ("ISO", "ISOOCC"), ("USER", "USEROCC")),
}


def _peril_detail(code):
    found = _PERIL_DETAIL.get((code or "").strip().upper())
    if found is None:
        raise RuntimeError(
            f"Peril {code} does not have a detail table. Grouped analyses (YY) are not supported.")
    return found


def _peril_id(code):
    return _peril_detail(code)[0]


# Peril -> (construction descriptions, occupancy descriptions) in RMS_VULNERABILITY_vXX.
_VULN_BY_PERIL = {
    "EQ": ("eqvccus", "eqvoccus"),
    "FF": ("eqvccus", "eqvoccus"),
    "WS": ("huvccus", "huvoccus"),
    "HU": ("huvccus", "huvoccus"),
    "TO": ("tovccus", "tovoccus"),
    "CS": ("csvccus", "csvoccus"),
    "WT": ("wtvccus", "wtvoccus"),
}


def _vulnerability_database(engine_version):
    """RMS_VULNERABILITY_v25 for an engine version of 25 or 25.0."""
    text = str(engine_version or "").strip()
    if text[:1].lower() == "v":
        text = text[1:]
    major = text.split(".", 1)[0].strip()
    if not major.isdigit():
        raise RuntimeError(
            f"Engine version {engine_version} does not identify a vulnerability database.")
    return f"RMS_VULNERABILITY_v{int(major)}"


def _vuln_token(value):
    """Scheme and code key. Whole numbers match whether stored as 1 or 1.0."""
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    try:
        number = float(text)
    except ValueError:
        return text.upper()
    if number == int(number):
        return str(int(number))
    return text.upper()


def _vuln_desc_lookup(database, table, scheme_col, code_col, desc_col):
    conn = _open_moodys(database)
    try:
        cur = conn.cursor()
        cur.execute(
            f"SELECT [{scheme_col}], [{code_col}], [{desc_col}] FROM dbo.[{table}]")
        labels = {}
        for scheme, code, desc in cur.fetchall():
            key = (_vuln_token(scheme), _vuln_token(code))
            label = str(desc or "").strip()
            if key[0] and key[1] and label and key not in labels:
                labels[key] = label
        return labels
    finally:
        conn.close()


def _vulnerability_labels(engine_version, peril):
    """Construction and occupancy descriptions for one engine version and peril."""
    pair = _VULN_BY_PERIL.get((peril or "").strip().upper())
    if pair is None:
        return {}, {}
    database = _vulnerability_database(engine_version)
    return (
        _vuln_desc_lookup(database, pair[0], "C_CLASSIF", "C_CLASS", "C_DESC"),
        _vuln_desc_lookup(database, pair[1], "O_CLASSIF", "O_CLASS", "O_DESC"),
    )


def _describe_code(labels, scheme, code):
    label = labels.get((_vuln_token(scheme), _vuln_token(code)))
    if label:
        return label
    return "" if code is None else str(code).strip()


def _scheme_value(scheme_column, pairs, fallback):
    """Peril-detail class or occupancy, using the location scheme, then Property."""
    if not pairs:
        return fallback
    whens = " ".join(
        f"WHEN '{scheme}' THEN NULLIF(RTRIM(d.{column}), '')"
        for scheme, column in pairs)
    return f"COALESCE(CASE {scheme_column} {whens} END, {fallback})"


def _engine_version(rdm_name, rdm_analysis_id):
    """EngineVersion from rdm_analysis. Blank when the analysis cannot be read."""
    if not rdm_name or rdm_analysis_id is None:
        return ""
    conn = _open_moodys(rdm_name)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT RTRIM(EngineVersion) FROM dbo.rdm_analysis WHERE ID = ?",
            int(rdm_analysis_id))
        row = cur.fetchone()
    finally:
        conn.close()
    if not row or row[0] is None:
        return ""
    return str(row[0]).strip()


def _ungrouped_peril(rdm_name, rdm_analysis_id):
    """Peril stored on tAnalysis. Grouped RDM analyses are rejected."""
    conn = _open_moodys(rdm_name)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT RTRIM(PERIL), ISGROUP FROM dbo.rdm_analysis WHERE ID = ?",
            int(rdm_analysis_id))
        row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        raise ValueError("That RDM analysis was not found.")
    if row[1]:
        raise ValueError("Grouped analyses are not supported. Peril YY has no detail table.")
    peril = (row[0] or "").strip()
    try:
        _peril_detail(peril)
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc
    return peril


def _resolve_edm_database(edm_name):
    """SRCEDM is the logical name. The server database may carry a suffix."""
    name = (edm_name or "").strip()
    if not name:
        raise RuntimeError("This analysis does not have an EDM name.")
    conn = _open_moodys("master")
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT name
            FROM sys.databases
            WHERE name = ? OR name LIKE ? + '[_]%'
            ORDER BY CASE WHEN name = ? THEN 0 ELSE 1 END, name
            """,
            name, name, name)
        rows = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()
    if not rows:
        raise RuntimeError(f"EDM database {name} was not found.")
    return rows[0]


def _save_location_exposure(analysis):
    """One exposure row per location. The analysis peril picks the detail table."""
    if not analysis.get("portfolio_id"):
        raise RuntimeError("This analysis does not have a portfolio id.")
    peril_id, detail_table = _peril_detail(analysis.get("peril"))
    construction = _scheme_value(
        "p.BLDGSCHEME", _CLASS_BY_TABLE.get(detail_table, ()),
        "NULLIF(RTRIM(p.BLDGCLASS), '')")
    occupancy = _scheme_value(
        "p.OCCSCHEME", _OCC_BY_TABLE.get(detail_table, ()),
        "CONVERT(nvarchar(32), p.OCCTYPE)")
    coast_sql = "d.DISTCOAST" if detail_table == "hudet" else "hu.DISTCOAST"
    elev_sql = "d.ELEVATION" if detail_table == "hudet" else "hu.ELEVATION"
    coast_join = "" if detail_table == "hudet" else "LEFT JOIN dbo.hudet AS hu ON hu.LOCID = p.LOCID"
    database = _resolve_edm_database(analysis.get("edm_name"))
    edm = _open_moodys(database)
    try:
        cur = edm.cursor()
        cur.execute(
            f"""
            SELECT
                CONVERT(varchar(40), p.LOCID),
                {construction},
                {occupancy},
                p.NUMSTORIES,
                YEAR(p.YEARBUILT),
                p.FLOORAREA,
                p.INCEPTDATE,
                p.EXPIREDATE,
                a.Admin1Code,
                a.Admin2Name,
                a.PostalCode,
                a.Latitude,
                a.Longitude,
                c.LocationValue,
                {coast_sql},
                CASE WHEN {elev_sql} <= -900 THEN NULL ELSE {elev_sql} END,
                RTRIM(p.BLDGSCHEME),
                RTRIM(p.OCCSCHEME),
                RTRIM(a.StreetAddress)
            FROM dbo.portacct AS pa
            INNER JOIN dbo.accgrp AS g
                ON g.ACCGRPID = pa.ACCGRPID
            INNER JOIN dbo.Property AS p
                ON p.ACCGRPID = g.ACCGRPID
            INNER JOIN dbo.{detail_table} AS d
                ON d.LOCID = p.LOCID
            {coast_join}
            LEFT JOIN dbo.Address AS a
                ON a.AddressID = p.AddressID
            INNER JOIN (
                SELECT LOCID, SUM(VALUEAMT) AS LocationValue
                FROM dbo.loccvg
                WHERE PERIL = ?
                GROUP BY LOCID
            ) AS c
                ON c.LOCID = p.LOCID
            WHERE pa.PORTINFOID = ?
            """,
            peril_id, int(analysis["portfolio_id"]))
        rows = list({str(row[0]): row for row in cur.fetchall()}.values())
    finally:
        edm.close()
    class_labels, occ_labels = _vulnerability_labels(
        _engine_version(analysis["rdm_name"], analysis["rdm_analysis_id"]),
        analysis.get("peril"))
    rows = [
        (
            row[0],
            _describe_code(class_labels, row[16], row[1]),
            _describe_code(occ_labels, row[17], row[2]),
            row[3], row[4], row[5], row[6], row[7],
            row[8], row[9],
            None if row[18] is None else str(row[18]).strip(),
            row[10], row[11], row[12], row[13], row[14], row[15],
        )
        for row in rows
    ]
    dpo = _open_dpo()
    try:
        cur = dpo.cursor()
        cur.execute("EXEC dbo.usp_LocationExposure_Replace @AnalysisID=?", analysis["id"])
        cur.fast_executemany = True
        cur.executemany(
            """
            INSERT INTO dbo.tLocationExposure (
                AnalysisID, ExposureLevelID, Construction, Occupancy,
                NumberOfStories, YearBuilt, FloorArea, InceptDate, ExpireDate,
                Admin1Code, Admin2Name, StreetAddress, PostalCode, Latitude, Longitude,
                LocationValue, DistCoast, Elevation)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [(analysis["id"],) + tuple(row) for row in rows])
        dpo.commit()
    finally:
        dpo.close()
    return len(rows)


def _ensure_location_exposure(analysis_id):
    """Store location ids and coordinates when a location analysis has none.

    Policy and account placement is left for a later version. A location run
    also stores coordinates when it starts and again when it finishes.
    """
    if not any(row["loss_level"] == "Location" for row in _analysis_settings_rows(analysis_id)):
        return 0
    conn = _open_dpo()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM dbo.tLocationExposure WHERE AnalysisID = ?",
            analysis_id)
        stored = int(cur.fetchone()[0])
    finally:
        conn.close()
    if stored:
        return stored
    analysis = _analysis_header(analysis_id)
    if not analysis:
        return 0
    return _save_location_exposure(analysis)


_EXPOSURE_BUCKETS = 5
_EXPOSURE_TOP_LIMITS = (5, 10, 25)
_EXPOSURE_TOP_DEFAULT = 10


def _exposure_text(value):
    """Blank and 0 are Unknown on a category chart."""
    if value is None:
        return "Unknown"
    text = str(value).strip()
    if text in ("", "0"):
        return "Unknown"
    return text


def _exposure_number(value, allow_negative=False, unknown_at=None):
    """Zero is Unknown. Missing values are Unknown. Negatives stay only for elevation."""
    if value is None or value == "":
        return None
    number = float(value)
    if number == 0 or (number < 0 and not allow_negative):
        return None
    if unknown_at is not None and number >= unknown_at:
        return None
    return number


def _whole_number(value):
    return abs(value - round(value)) < 0.00005


def _exposure_bins(numbers):
    """Bars for the positive values on Stories, Year built, Floor area, DTC, and Elevation.

    Five or fewer whole steps from the minimum to the maximum keep a bar for
    each value, so stories 1 through 5 are five bars. A wider span is split
    into five ranges. Whole numbers from 1 to 10 become 1–2, 3–4, 5–6, 7–8,
    and 9–10. A value on an interior boundary belongs to the higher range,
    and the last range includes the maximum.
    """
    if not numbers:
        return []
    low = min(numbers)
    high = max(numbers)
    if _whole_number(low) and _whole_number(high) and all(_whole_number(number) for number in numbers):
        low_i = int(round(low))
        high_i = int(round(high))
        count = high_i - low_i + 1
        if count <= _EXPOSURE_BUCKETS:
            return [(float(step), float(step)) for step in range(low_i, high_i + 1)]
        step = math.ceil(count / _EXPOSURE_BUCKETS)
        bins = []
        start = low_i
        while start <= high_i and len(bins) < _EXPOSURE_BUCKETS:
            end = high_i if len(bins) == _EXPOSURE_BUCKETS - 1 else min(high_i, start + step - 1)
            bins.append((float(start), float(end)))
            start = int(end) + 1
        return bins
    distinct = sorted(set(numbers))
    if len(distinct) <= _EXPOSURE_BUCKETS:
        return [(number, number) for number in distinct]
    if high == low:
        return [(low, high)]
    width = (high - low) / _EXPOSURE_BUCKETS
    bins = []
    for index in range(_EXPOSURE_BUCKETS):
        start = low + index * width
        end = high if index == _EXPOSURE_BUCKETS - 1 else low + (index + 1) * width
        bins.append((start, end))
    return bins


def _exposure_bin_index(value, bins):
    if not bins:
        return None
    contiguous = len(bins) > 1 and all(
        abs(bins[index][1] - bins[index + 1][0]) < 0.00005
        for index in range(len(bins) - 1))
    last = len(bins) - 1
    for index, (start, end) in enumerate(bins):
        if abs(start - end) < 0.00005:
            same = abs(value - start) < 0.00005
            if not same and _whole_number(value) and _whole_number(start):
                same = int(round(value)) == int(round(start))
            if same:
                return index
            continue
        if contiguous:
            if index == last and start <= value <= end:
                return index
            if index < last and start <= value < end:
                return index
        elif start <= value <= end:
            return index
    return None


def _exposure_bin_label(start, end, unit):
    if abs(start - end) < 0.00005:
        label = _range_number(start)
    else:
        label = f"{_range_number(start)}–{_range_number(end)}"
    if unit:
        label = f"{label} {unit}"
    return label


def _compact_money(value):
    number = float(value or 0)
    sign = "-" if number < 0 else ""
    amount = abs(number)
    for scale, suffix in ((1000000000, "B"), (1000000, "M"), (1000, "K")):
        if amount >= scale:
            return f"{sign}${amount / scale:.1f}{suffix}"
    return f"{sign}${amount:,.0f}"


def _exposure_share(part, whole):
    if not whole:
        return "0.0%"
    return f"{part / whole * 100:.1f}%"


def _exposure_average(value, count):
    if not count:
        return "—"
    return f"{value / count:,.1f}"


def _exposure_table_row(label, count, value, total_count, total_value, rank=None):
    row = {
        "label": label,
        "count": int(count),
        "value": float(value),
        "risks": f"{int(count):,}",
        "risk_share": _exposure_share(count, total_count),
        "tiv": f"{float(value):,.0f}",
        "tiv_share": _exposure_share(value, total_value),
        "average": _exposure_average(value, count),
    }
    if rank is not None:
        row["rank"] = rank
    return row


def _exposure_exhibit(title, groups, mode="all"):
    """Risks, share of risks, TIV, share of TIV, and average TIV per risk.

    groups is every location in display order. Top profiles keep the first 25
    names and fold the rest into Other. The total is the whole book.
    """
    total_count = sum(count for _label, count, _value in groups)
    total_value = sum(value for _label, _count, value in groups)
    other = None
    tail_count = 0
    tail_value = 0.0
    if mode == "top":
        limit = max(_EXPOSURE_TOP_LIMITS)
        named = groups[:limit]
        tail = groups[limit:]
        rows = [
            _exposure_table_row(label, count, value, total_count, total_value, rank=index)
            for index, (label, count, value) in enumerate(named, start=1)
        ]
        rest = groups[_EXPOSURE_TOP_DEFAULT:]
        other = _exposure_table_row(
            "Other",
            sum(count for _label, count, _value in rest),
            sum(value for _label, _count, value in rest),
            total_count,
            total_value)
        tail_count = sum(count for _label, count, _value in tail)
        tail_value = sum(value for _label, _count, value in tail)
    else:
        rows = [
            _exposure_table_row(label, count, value, total_count, total_value)
            for label, count, value in groups
        ]
    return {
        "title": title,
        "mode": mode,
        "kind": "table",
        "rows": rows,
        "other": other,
        "tail_count": tail_count,
        "tail_value": tail_value,
        "total": _exposure_table_row("Total", total_count, total_value, total_count, total_value),
    }


def _exposure_category_chart(title, rows, index, mode="all"):
    groups = {}
    for row in rows:
        label = _exposure_text(row[index])
        if label not in groups:
            groups[label] = {"count": 0, "value": 0.0}
        groups[label]["count"] += 1
        groups[label]["value"] += float(row[-1] or 0)
    ranked = sorted(groups.items(), key=lambda item: (-item[1]["value"], -item[1]["count"], item[0]))
    ordered = [(label, item["count"], item["value"]) for label, item in ranked]
    return _exposure_exhibit(title, ordered, mode)


def _exposure_range_chart(title, rows, index, unit="", allow_negative=False, unknown_at=None):
    unknown = {"count": 0, "value": 0.0}
    measured = []
    for row in rows:
        amount = float(row[-1] or 0)
        number = _exposure_number(row[index], allow_negative=allow_negative, unknown_at=unknown_at)
        if number is None:
            unknown["count"] += 1
            unknown["value"] += amount
        else:
            measured.append((number, amount))
    bins = _exposure_bins([number for number, _amount in measured])
    groups = [{"count": 0, "value": 0.0} for _bin in bins]
    for number, amount in measured:
        slot = _exposure_bin_index(number, bins)
        if slot is None:
            unknown["count"] += 1
            unknown["value"] += amount
            continue
        groups[slot]["count"] += 1
        groups[slot]["value"] += amount
    ordered = [("Unknown", unknown["count"], unknown["value"])]
    for (start, end), item in zip(bins, groups):
        ordered.append((_exposure_bin_label(start, end, unit), item["count"], item["value"]))
    return _exposure_exhibit(title, ordered)


def _stories_exhibit(rows, index):
    """Number of stories in the fixed low-rise through tall bands."""
    bands = (
        "Unknown",
        "1 to 3 stories—Low-rise",
        "4 to 7 stories—Mid-rise",
        "8 to 14 stories—High-rise",
        "15 or more stories—Tall",
    )
    groups = [{"count": 0, "value": 0.0} for _band in bands]
    for row in rows:
        amount = float(row[-1] or 0)
        number = _exposure_number(row[index])
        if number is None:
            slot = 0
        else:
            stories = int(round(number))
            if stories <= 3:
                slot = 1
            elif stories <= 7:
                slot = 2
            elif stories <= 14:
                slot = 3
            else:
                slot = 4
        groups[slot]["count"] += 1
        groups[slot]["value"] += amount
    ordered = [(label, item["count"], item["value"]) for label, item in zip(bands, groups)]
    return _exposure_exhibit("Number of stories", ordered)


def _year_built_exhibit(rows, index):
    """Year built in the fixed Pre-1937 through Post-2000 bands. 9999 is Unknown."""
    bands = (
        "Unknown",
        "Pre-1937",
        "1937-1973",
        "1974-1988",
        "1989-2000",
        "Post-2000",
    )
    groups = [{"count": 0, "value": 0.0} for _band in bands]
    for row in rows:
        amount = float(row[-1] or 0)
        number = _exposure_number(row[index], unknown_at=9999)
        if number is None:
            slot = 0
        else:
            year = int(round(number))
            if year < 1937:
                slot = 1
            elif year <= 1973:
                slot = 2
            elif year <= 1988:
                slot = 3
            elif year <= 2000:
                slot = 4
            else:
                slot = 5
        groups[slot]["count"] += 1
        groups[slot]["value"] += amount
    ordered = [(label, item["count"], item["value"]) for label, item in zip(bands, groups)]
    return _exposure_exhibit("Year built", ordered)


def _exposure_summary(analysis_id):
    """Count and location value by the exposure attributes shown on the map."""
    _ensure_location_exposure(analysis_id)
    conn = _open_dpo()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT Construction, Occupancy, NumberOfStories, YearBuilt, FloorArea,
                   DistCoast, Elevation, Admin1Code, Admin2Name, PostalCode, LocationValue
            FROM dbo.tLocationExposure
            WHERE AnalysisID = ?
            """,
            analysis_id)
        rows = cur.fetchall()
    finally:
        conn.close()
    location_value = sum(float(row[-1] or 0) for row in rows)
    charts = [
        _exposure_category_chart("Construction", rows, 0),
        _exposure_category_chart("Occupancy", rows, 1),
        _stories_exhibit(rows, 2),
        _year_built_exhibit(rows, 3),
        _exposure_range_chart("Floor area", rows, 4),
        _exposure_range_chart("Distance to coast", rows, 5, unit="mi"),
        _exposure_range_chart("Elevation", rows, 6, unit="ft", allow_negative=True),
        _exposure_category_chart("State", rows, 7, mode="top"),
        _exposure_category_chart("County", rows, 8, mode="top"),
        _exposure_category_chart("Postal code", rows, 9, mode="top"),
    ]
    return {
        "locations": len(rows),
        "location_value": location_value,
        "top_default": _EXPOSURE_TOP_DEFAULT,
        "top_choices": list(_EXPOSURE_TOP_LIMITS),
        "charts": charts,
    }


_EXPOSURE_LIST_LIMIT = 100


def _location_exposure(analysis_id, limit=None):
    """Location rows for the exposure list. limit keeps the page to the first ids."""
    conn = _open_dpo()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM dbo.tLocationExposure WHERE AnalysisID = ?",
            analysis_id)
        total = int(cur.fetchone()[0])
        top_sql = ""
        order_sql = ""
        if limit:
            top_sql = f"TOP ({max(1, int(limit))}) "
            order_sql = " ORDER BY TRY_CONVERT(bigint, ExposureLevelID), ExposureLevelID"
        cur.execute(
            f"""
            SELECT {top_sql}ExposureLevelID, Construction, Occupancy, NumberOfStories, YearBuilt,
                   FloorArea, InceptDate, ExpireDate, Admin1Code, Admin2Name,
                   PostalCode, Latitude, Longitude, LocationValue, DistCoast, Elevation,
                   StreetAddress
            FROM dbo.tLocationExposure
            WHERE AnalysisID = ?{order_sql}
            """,
            analysis_id)
        ranges = _display_ranges()
        found = {}
        for row in cur.fetchall():
            incept = row[6].strftime("%Y-%m-%d") if row[6] else ""
            expire = row[7].strftime("%Y-%m-%d") if row[7] else ""
            found[str(row[0])] = {
                "construction": row[1] or "",
                "occupancy": row[2] or "",
                "stories": "" if row[3] is None else int(row[3]),
                "year_built": "" if row[4] is None else int(row[4]),
                "floor_area": "" if row[5] is None else f"{float(row[5]):,.0f}",
                "incept_date": incept,
                "expire_date": expire,
                "admin1": row[8] or "",
                "admin2": row[9] or "",
                "postal_code": row[10] or "",
                "latitude": "" if row[11] is None else f"{float(row[11]):.5f}",
                "longitude": "" if row[12] is None else f"{float(row[12]):.5f}",
                "location_value": _money(row[13]),
                "dtc": _band_label(row[14], ranges["dtc"]),
                "elevation": _band_label(row[15], ranges["elevation"]),
                "street_address": "" if row[16] is None else str(row[16]).strip(),
            }
        return found, total
    finally:
        conn.close()


def _result_file_metrics(_analysis, run_id):
    """AAL and standard deviation stored with each excluding and difference curve.

    Excluding AAL and standard deviation were calculated from the remaining
    event losses while that risk's curve was built. Every difference metric,
    including AAL and standard deviation, is the portfolio value minus the
    excluding value.
    """
    metrics = _stored_portfolio_metrics(run_id)
    portfolio = metrics["PORTFOLIO"]
    portfolio_aal = float(portfolio["excl_aal"])
    portfolio_stddev = float(portfolio["excl_stddev"])
    for entity_id, (aal, standard_deviation) in _stored_excl_metrics(run_id).items():
        if entity_id == "PORTFOLIO":
            continue
        metrics[entity_id] = {
            "excl_aal": float(aal),
            "excl_stddev": float(standard_deviation),
            "diff_aal": portfolio_aal - float(aal),
            "diff_stddev": portfolio_stddev - float(standard_deviation),
        }
    return metrics


def _stored_portfolio_metrics(run_id):
    """Portfolio AAL and standard deviation already saved on the run."""
    conn = _open_dpo()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT PortfolioAal, PortfolioStandardDeviation
            FROM dbo.tAnalysisRun
            WHERE AnalysisRunID = ?
            """,
            run_id)
        row = cur.fetchone()
    finally:
        conn.close()
    aal = 0.0 if row is None or row[0] is None else float(row[0])
    standard_deviation = 0.0 if row is None or row[1] is None else float(row[1])
    return {
        "PORTFOLIO": {
            "excl_aal": aal,
            "excl_stddev": standard_deviation,
            "diff_aal": 0.0,
            "diff_stddev": 0.0,
        }
    }


def _publish_result_file(analysis_id, run_id):
    """Write the wide EXCL and DIFF curves for a run under App_Data."""
    conn = _open_dpo()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT EntityID, ResultType, ReturnPeriod, PmlValue
            FROM dbo.tPmlResult
            WHERE AnalysisRunID = ?
            """,
            run_id)
        rows = cur.fetchall()
    finally:
        conn.close()
    if rows:
        analysis = _analysis_header(analysis_id)
        results_store.write_results(
            *_result_parts(analysis),
            results_store.wide_frame(rows, _result_file_metrics(analysis, run_id)))
        _log_result_checks(analysis_id, run_id)


def _resumable_run(cur, settings_id):
    """Latest unfinished run for these settings, if risks can be continued."""
    cur.execute(
        """
        SELECT TOP 1 AnalysisRunID, RunStatus, EntityCount
        FROM dbo.tAnalysisRun
        WHERE AnalysisSettingsID = ? AND RunStatus IN (N'Running', N'Failed')
        ORDER BY AnalysisRunID DESC
        """,
        settings_id)
    found = cur.fetchone()
    if not found:
        return None
    return int(found[0]), (found[1] or "").strip(), int(found[2] or 0)


def _stored_portfolio_points(cur, run_id, points):
    """Portfolio losses already stored for this run, aligned to the EP points."""
    cur.execute(
        """
        SELECT ReturnPeriod, PmlValue
        FROM dbo.tPmlResult
        WHERE AnalysisRunID = ? AND ResultType = N'EXCL' AND EntityID = N'PORTFOLIO'
        """,
        run_id)
    stored = {int(period): float(value) for period, value in cur.fetchall()}
    if any(point not in stored for point in points):
        return None
    return [stored[point] for point in points]


def _finished_and_partial(cur, run_id, point_count):
    """Risks with a full excluding and difference curve, and risks left incomplete."""
    cur.execute(
        """
        SELECT EntityID, ResultType, COUNT(*)
        FROM dbo.tPmlResult
        WHERE AnalysisRunID = ? AND EntityID <> N'PORTFOLIO'
        GROUP BY EntityID, ResultType
        """,
        run_id)
    counts = {}
    for entity_id, result_type, count in cur.fetchall():
        counts.setdefault(str(entity_id).strip(), {})[str(result_type).strip()] = int(count)
    finished = set()
    partial = []
    for entity_id, by_type in counts.items():
        if by_type.get("EXCL") == point_count and by_type.get("DIFF") == point_count:
            finished.add(entity_id)
        else:
            partial.append(entity_id)
    return finished, partial


def _ensure_excl_metric_table(cur):
    """Per-risk excluding AAL and standard deviation, written as each curve is saved."""
    cur.execute(
        """
        IF OBJECT_ID(N'dbo.tExclMetric', N'U') IS NULL
        CREATE TABLE dbo.tExclMetric (
            AnalysisRunID       INT            NOT NULL,
            EntityID            NVARCHAR(255)  NOT NULL,
            Aal                 FLOAT          NOT NULL,
            StandardDeviation   FLOAT          NOT NULL,
            CONSTRAINT PK_tExclMetric PRIMARY KEY CLUSTERED (AnalysisRunID, EntityID),
            CONSTRAINT FK_tExclMetric_tAnalysisRun
                FOREIGN KEY (AnalysisRunID) REFERENCES dbo.tAnalysisRun (AnalysisRunID)
        )
        """)


def _insert_excl_metric(cur, run_id, entity_id, aal, stddev):
    cur.execute(
        """
        INSERT INTO dbo.tExclMetric (AnalysisRunID, EntityID, Aal, StandardDeviation)
        VALUES (?, ?, ?, ?)
        """,
        int(run_id), str(entity_id), float(aal), float(stddev))


def _stored_excl_metrics(run_id):
    """Excluding AAL and standard deviation saved while each risk was calculated."""
    conn = _open_dpo()
    try:
        cur = conn.cursor()
        cur.execute("SELECT OBJECT_ID(N'dbo.tExclMetric', N'U')")
        if cur.fetchone()[0] is None:
            return {}
        cur.execute(
            """
            SELECT EntityID, Aal, StandardDeviation
            FROM dbo.tExclMetric
            WHERE AnalysisRunID = ?
            """,
            int(run_id))
        return {
            str(entity_id).strip(): (float(aal), float(stddev))
            for entity_id, aal, stddev in cur.fetchall()
        }
    finally:
        conn.close()


def _delete_partial_results(cur, run_id, entity_ids):
    _ensure_excl_metric_table(cur)
    for entity_id in entity_ids:
        cur.execute(
            """
            DELETE FROM dbo.tPmlResult
            WHERE AnalysisRunID = ? AND EntityID = ?
            """,
            run_id, entity_id)
        cur.execute(
            """
            DELETE FROM dbo.tExclMetric
            WHERE AnalysisRunID = ? AND EntityID = ?
            """,
            run_id, entity_id)


def _write_portfolio_results(analysis, _entity_limit=None):
    points, _custom, _defaults = _analysis_ep_points(analysis["id"])
    points = [point for point in points if point > 0][:15]
    if not points:
        raise RuntimeError("Save at least one EP point before writing portfolio results.")
    _ensure_location_exposure(analysis["id"])
    settings = _analysis_settings_rows(analysis["id"])
    if not settings:
        raise RuntimeError("Save a loss level and perspective before writing portfolio results.")
    rdm = _open_moodys(analysis["rdm_name"])
    dpo = None
    published = []
    run_id = None
    run_open = False
    try:
        dpo = _open_dpo()
        rdm_cur = rdm.cursor()
        dpo_cur = dpo.cursor()
        _ensure_excl_metric_table(dpo_cur)
        dpo.commit()
        for row in settings:
            existing = _resumable_run(dpo_cur, row["id"])
            if existing:
                run_id, _status, entity_count = existing
                run_open = True
                dpo_cur.execute(
                    "EXEC dbo.usp_AnalysisRun_SetStatus @AnalysisRunID=?, @RunStatus=?",
                    run_id, "Running")
                dpo.commit()
            else:
                dpo_cur.execute(
                    """
                    DECLARE @AnalysisRunID INT;
                    EXEC dbo.usp_AnalysisRun_Create
                        @AnalysisSettingsID=?, @AnalysisRunID=@AnalysisRunID OUTPUT;
                    SELECT @AnalysisRunID;
                    """,
                    row["id"])
                run_id = _first_value(dpo_cur)
                run_open = True
                entity_count = 0
                dpo_cur.execute(
                    "EXEC dbo.usp_AnalysisRun_SetStatus @AnalysisRunID=?, @RunStatus=?",
                    run_id, "Running")
                dpo.commit()
            exceedance, losses, aal, standard_deviation = _portfolio_curve(
                rdm_cur, analysis["rdm_analysis_id"], row["loss_perspective"], analysis["portfolio_id"])
            if not entity_count:
                entity_count = _entity_count(
                    rdm_cur, row["loss_level"], analysis["rdm_analysis_id"], row["loss_perspective"])
            values = _stored_portfolio_points(dpo_cur, run_id, points)
            if values is None:
                values = rdm_analysis.portfolio_pml_from_portep(exceedance, losses, points)
                dpo_cur.execute(
                    """
                    EXEC dbo.usp_AnalysisRun_SetStatus
                        @AnalysisRunID=?, @RunStatus=?, @EntityCount=?
                    """,
                    run_id, "Running", entity_count)
                _record_progress(
                    dpo_cur, analysis["id"], run_id, 0, entity_count,
                    f"Saving the portfolio curve for {entity_count} risks.")
                dpo.commit()
                for result_type in ("EXCL", "DIFF"):
                    for period, value in zip(points, values):
                        if value != value:  # NaN
                            raise RuntimeError(f"No portfolio loss at return period {period}.")
                        dpo_cur.execute(
                            """
                            EXEC dbo.usp_PmlResult_Insert
                                @AnalysisRunID=?, @ResultType=?, @EntityID=?,
                                @ReturnPeriod=?, @PmlValue=?
                            """,
                            run_id, result_type, "PORTFOLIO", int(period), float(value))
                dpo.commit()
            else:
                dpo_cur.execute(
                    """
                    EXEC dbo.usp_AnalysisRun_SetStatus
                        @AnalysisRunID=?, @RunStatus=?, @EntityCount=?
                    """,
                    run_id, "Running", entity_count)
            finished, partial = _finished_and_partial(dpo_cur, run_id, len(points))
            if partial:
                _delete_partial_results(dpo_cur, run_id, partial)
            completed = len(finished)
            _record_progress(
                dpo_cur, analysis["id"], run_id, completed, entity_count,
                f"Completed {completed} of {entity_count} risks.")
            dpo.commit()
            event_ids, elt_mean, elt_std, elt_exposure, elt_rate = _portfolio_elt(
                rdm_cur, analysis["rdm_analysis_id"], row["loss_perspective"], analysis["portfolio_id"])
            ceilings = rdm_analysis.pml_from_elt(
                elt_mean, elt_std, elt_exposure, elt_rate, points)
            _record_progress(
                dpo_cur, analysis["id"], run_id, completed, entity_count,
                f"Processing locations on {os.cpu_count() or 1} cores.")
            dpo.commit()
            logged_at = time.perf_counter()
            workers = os.cpu_count() or 1

            def _store_curve(entity_id, excl_values, excl_aal, excl_stddev):
                nonlocal completed, logged_at
                _insert_excl_metric(dpo_cur, run_id, entity_id, excl_aal, excl_stddev)
                for period, excl_value, portfolio_value in zip(points, excl_values, values):
                    if excl_value != excl_value:
                        raise RuntimeError(f"No excluding loss at return period {period} for {entity_id}.")
                    dpo_cur.execute(
                        """
                        EXEC dbo.usp_PmlResult_Insert
                            @AnalysisRunID=?, @ResultType=?, @EntityID=?,
                            @ReturnPeriod=?, @PmlValue=?
                        """,
                        run_id, "EXCL", entity_id, int(period), float(excl_value))
                    diff_value = max(float(portfolio_value) - float(excl_value), 0.0)
                    dpo_cur.execute(
                        """
                        EXEC dbo.usp_PmlResult_Insert
                            @AnalysisRunID=?, @ResultType=?, @EntityID=?,
                            @ReturnPeriod=?, @PmlValue=?
                        """,
                        run_id, "DIFF", entity_id, int(period), diff_value)
                completed += 1
                if time.perf_counter() - logged_at >= 5:
                    _record_progress(
                        dpo_cur, analysis["id"], run_id, completed, entity_count,
                        f"Completed {completed} of {entity_count} risks.")
                    dpo.commit()
                    logged_at = time.perf_counter()

            pending = set()
            with ProcessPoolExecutor(
                    max_workers=workers,
                    initializer=exclude_pool.init_worker,
                    initargs=(event_ids, elt_mean, elt_std, elt_exposure, elt_rate, points, ceilings)) as pool:
                for entity_id, risk_elt in _iter_risk_event_losses(
                        rdm_cur, row["loss_level"], analysis["rdm_analysis_id"],
                        row["loss_perspective"], finished):
                    pending.add(pool.submit(exclude_pool.exclude_risk, entity_id, risk_elt))
                    del risk_elt
                    if len(pending) >= workers:
                        done, pending = wait(pending, return_when=FIRST_COMPLETED)
                        for future in done:
                            finished_id, excl_values, excl_aal, excl_stddev = future.result()
                            _store_curve(finished_id, excl_values, excl_aal, excl_stddev)
                if pending:
                    done, _pending = wait(pending)
                    for future in done:
                        finished_id, excl_values, excl_aal, excl_stddev = future.result()
                        _store_curve(finished_id, excl_values, excl_aal, excl_stddev)
            dpo_cur.execute(
                """
                EXEC dbo.usp_AnalysisRun_SetStatus
                    @AnalysisRunID=?, @RunStatus=?, @EntityCount=?,
                    @PortfolioAal=?, @PortfolioStandardDeviation=?
                """,
                run_id, "Complete", entity_count, aal, standard_deviation)
            _record_progress(
                dpo_cur, analysis["id"], run_id, completed, entity_count,
                f"Completed {completed} of {entity_count} risks.")
            dpo.commit()
            published.append(run_id)
            run_open = False
        for finished_run in published:
            _publish_result_file(analysis["id"], finished_run)
        if any(row["loss_level"] == "Location" for row in settings):
            _save_location_exposure(analysis)
    except Exception as exc:
        if dpo is not None:
            try:
                dpo.rollback()
            except Exception:
                pass
        if run_open:
            _mark_run_failed(run_id, exc)
        raise
    finally:
        rdm.close()
        if dpo is not None:
            dpo.close()


def _run_analysis_job(analysis):
    try:
        _write_portfolio_results(analysis)
    except Exception as exc:
        print(f"Analysis {analysis['id']} stopped: {exc}", flush=True)
    finally:
        with _RUN_LOCK:
            _RUNNING_ANALYSES.discard(analysis["id"])
            _RUN_STARTED.pop(analysis["id"], None)


@app.route("/analysis/<int:analysis_id>/portfolio-results", methods=["POST"])
def write_portfolio_results(analysis_id):
    try:
        analysis = _analysis_header(analysis_id)
        if not analysis:
            return redirect(url_for("new_analysis", error="Analysis was not found."))
        points, _custom, _defaults = _analysis_ep_points(analysis_id)
        if not [point for point in points if point > 0]:
            return redirect(url_for(
                "analysis_settings", analysis_id=analysis_id,
                error="Save at least one EP point before writing portfolio results."))
        if not _analysis_settings_rows(analysis_id):
            return redirect(url_for(
                "analysis_settings", analysis_id=analysis_id,
                error="Save a loss level and perspective before writing portfolio results."))
        with _RUN_LOCK:
            already = analysis_id in _RUNNING_ANALYSES
            if not already:
                _RUNNING_ANALYSES.add(analysis_id)
                _RUN_STARTED[analysis_id] = time.time()
        if not already:
            threading.Thread(
                target=_run_analysis_job, args=(analysis,),
                name=f"analysis-{analysis_id}", daemon=False).start()
    except Exception as exc:
        with _RUN_LOCK:
            _RUNNING_ANALYSES.discard(analysis_id)
            _RUN_STARTED.pop(analysis_id, None)
        return redirect(url_for(
            "analysis_settings", analysis_id=analysis_id,
            error=f"Could not write portfolio EP points: {exc}"))
    return redirect(url_for("analysis_list"))


def _analysis_form_values():
    name = request.form.get("analysis_name", "").strip()
    description = request.form.get("analysis_description", "").strip() or None
    client_id = request.form.get("client_id", "").strip()
    edm_name = request.form.get("edm_name", "").strip() or None
    portfolio_raw = request.form.get("portfolio_id", "").strip()
    rdm_name = request.form.get("rdm_name", "").strip()
    rdm_analysis_id = request.form.get("rdm_analysis_id", "").strip()
    rdm_analysis_name = request.form.get("rdm_analysis_name", "").strip()
    if not name or not client_id or not rdm_name or not rdm_analysis_id or not rdm_analysis_name:
        raise ValueError("Analysis name, client, and an RDM analysis are required.")
    try:
        portfolio_id = int(portfolio_raw) if portfolio_raw else None
        rdm_id = int(rdm_analysis_id)
        client = int(client_id)
    except ValueError as exc:
        raise ValueError("Client, portfolio, and RDM analysis id must be numbers.") from exc
    peril = _ungrouped_peril(rdm_name, rdm_id)
    return (name, description, client, edm_name, portfolio_id, rdm_name,
            rdm_id, rdm_analysis_name, peril)


def _format_remaining(seconds):
    """Short remaining-time label from a processing-rate estimate."""
    seconds = int(round(seconds))
    if seconds < 60:
        return "est. under 1 min"
    minutes = (seconds + 30) // 60
    if minutes < 60:
        return f"est. {minutes} min"
    hours, mins = divmod(minutes, 60)
    if mins == 0:
        return f"est. {hours} hr"
    return f"est. {hours} hr {mins} min"


def _eta_text(completed, total, latest_at, processing_started_at, processing_started_count,
              first_progress_at, first_progress_count):
    """Remaining time from location processing only, after 10% are done.

    The clock starts at the latest "Processing locations" log. Risks already
    finished before that log are not part of the rate, so a resumed run does
    not count time the application was stopped.
    """
    completed = int(completed or 0)
    total = int(total or 0)
    if total <= 0 or completed <= 0 or latest_at is None or completed >= total:
        return ""
    if completed / total < 0.10:
        return ""
    if processing_started_at is not None:
        elapsed = (latest_at - processing_started_at).total_seconds()
        done = completed - int(processing_started_count or 0)
    elif first_progress_at is not None and completed > int(first_progress_count or 0):
        elapsed = (latest_at - first_progress_at).total_seconds()
        done = completed - int(first_progress_count or 0)
    else:
        return ""
    if elapsed <= 0 or done <= 0:
        return ""
    return _format_remaining((total - completed) * elapsed / done)


def _progress_view(status, completed, total, eta=""):
    """Pie inputs for one analysis job. Progress is a percent from 0 to 100."""
    status = status or ""
    if status == "Complete" and completed is None:
        completed = total
    completed = int(completed or 0)
    total = int(total or 0)
    stats = f"{completed:,} / {total:,}" if total > 0 else ""
    if status == "Complete" and total <= 0:
        progress = 100
        label = "Complete"
    elif status in ("Running", "Queued"):
        actual = int(round(min(100.0, completed / total * 100.0))) if total > 0 else 0
        progress = max(3, actual)
        label = "running"
    elif total <= 0 and status == "Failed":
        progress = 0
        label = "Failed"
    elif total <= 0:
        progress = 0
        label = "Not started"
    else:
        progress = int(round(min(100.0, completed / total * 100.0)))
        label = stats
        if status == "Failed":
            label = f"Failed {label}"
    return {
        "run_status": status,
        "completed": completed,
        "total": total,
        "progress": progress,
        "progress_label": label,
        "progress_stats": stats if status in ("Running", "Queued") else "",
        "eta": eta if status in ("Running", "Queued") else "",
        "running": status in ("Running", "Queued"),
    }


def _analysis_progress():
    """Latest run and its newest completed-risk count, keyed by analysis id."""
    conn = _open_dpo()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            WITH latest_run AS (
                SELECT
                    s.AnalysisID,
                    r.AnalysisRunID,
                    r.RunStatus,
                    r.EntityCount,
                    r.CreatedAt,
                    ROW_NUMBER() OVER (
                        PARTITION BY s.AnalysisID
                        ORDER BY r.AnalysisRunID DESC
                    ) AS rn
                FROM dbo.tAnalysisRun AS r
                INNER JOIN dbo.tAnalysisSettings AS s
                    ON s.AnalysisSettingsID = r.AnalysisSettingsID
            ),
            latest_log AS (
                SELECT
                    AnalysisRunID,
                    LocationsCompleted,
                    LocationsTotal,
                    LoggedAt,
                    ROW_NUMBER() OVER (
                        PARTITION BY AnalysisRunID
                        ORDER BY AnalysisUserLogID DESC
                    ) AS rn
                FROM dbo.tAnalysisUserLog
                WHERE AnalysisRunID IS NOT NULL
                  AND LocationsTotal IS NOT NULL
            ),
            processing_start AS (
                SELECT
                    AnalysisRunID,
                    LoggedAt,
                    LocationsCompleted,
                    ROW_NUMBER() OVER (
                        PARTITION BY AnalysisRunID
                        ORDER BY AnalysisUserLogID DESC
                    ) AS rn
                FROM dbo.tAnalysisUserLog
                WHERE Message LIKE N'Processing locations%'
            ),
            first_progress AS (
                SELECT
                    AnalysisRunID,
                    LoggedAt,
                    LocationsCompleted,
                    ROW_NUMBER() OVER (
                        PARTITION BY AnalysisRunID
                        ORDER BY AnalysisUserLogID
                    ) AS rn
                FROM dbo.tAnalysisUserLog
                WHERE LocationsCompleted > 0
            )
            SELECT
                lr.AnalysisID,
                lr.RunStatus,
                lr.EntityCount,
                lr.CreatedAt,
                ll.LocationsCompleted,
                ll.LocationsTotal,
                ll.LoggedAt,
                ps.LoggedAt,
                ps.LocationsCompleted,
                fp.LoggedAt,
                fp.LocationsCompleted
            FROM latest_run AS lr
            LEFT JOIN latest_log AS ll
                ON ll.AnalysisRunID = lr.AnalysisRunID AND ll.rn = 1
            LEFT JOIN processing_start AS ps
                ON ps.AnalysisRunID = lr.AnalysisRunID AND ps.rn = 1
            LEFT JOIN first_progress AS fp
                ON fp.AnalysisRunID = lr.AnalysisRunID AND fp.rn = 1
            WHERE lr.rn = 1
            """)
        jobs = {}
        for (analysis_id, status, entity_count, created_at, completed, total,
             latest_at, processing_started_at, processing_started_count,
             first_progress_at, first_progress_count) in cur.fetchall():
            status = (status or "").strip()
            if total is None and status == "Complete":
                total = entity_count
            elif total is None:
                total = entity_count
            if completed is None and status == "Complete":
                completed = total
            elif completed is None:
                completed = 0
            eta = _eta_text(
                completed, total, latest_at, processing_started_at, processing_started_count,
                first_progress_at, first_progress_count)
            jobs[int(analysis_id)] = _progress_view(status, completed, total, eta)
            jobs[int(analysis_id)]["_created_at"] = created_at
        with _RUN_LOCK:
            started = dict(_RUN_STARTED)
        for analysis_id, started_at in started.items():
            current = jobs.get(analysis_id)
            created_at = current.get("_created_at") if current else None
            created_ts = None
            if created_at is not None:
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
                created_ts = created_at.timestamp()
            newer_run = created_ts is not None and created_ts >= started_at
            if current is None or (current["run_status"] != "Running" and not newer_run):
                jobs[analysis_id] = _progress_view("Running", 0, 0)
        for job in jobs.values():
            job.pop("_created_at", None)
        with _RUN_LOCK:
            active = set(_RUNNING_ANALYSES)
        for analysis_id, job in jobs.items():
            total = int(job.get("total") or 0)
            completed = int(job.get("completed") or 0)
            unfinished = job.get("run_status") in ("Running", "Failed") and (total == 0 or completed < total)
            job["can_resume"] = unfinished and analysis_id not in active
        return jobs
    finally:
        conn.close()


def _list_analyses():
    conn = _open_dpo()
    try:
        cur = conn.cursor()
        cur.execute("EXEC dbo.usp_Analysis_List")
        rows = []
        for r in cur.fetchall():
            created = r[10]
            rows.append({
                "id": int(r[0]),
                "name": r[1],
                "description": r[2] or "",
                "client_id": int(r[3]),
                "edm_name": r[4] or "",
                "portfolio_id": r[5] if r[5] is not None else "",
                "rdm_name": r[6],
                "rdm_analysis_name": r[8],
                "peril": r[9] or "",
                "created_at": created.strftime("%Y-%m-%d %H:%M") if created else "",
                "has_results": False,
            })
        cur.execute(
            """
            SELECT DISTINCT s.AnalysisID
            FROM dbo.tAnalysisRun AS r
            INNER JOIN dbo.tAnalysisSettings AS s
                ON s.AnalysisSettingsID = r.AnalysisSettingsID
            WHERE r.RunStatus = 'Complete'
            """)
        performed = {int(r[0]) for r in cur.fetchall()}
        progress = _analysis_progress()
        for row in rows:
            row["has_results"] = row["id"] in performed
            row.update(progress.get(row["id"]) or _progress_view("", None, None))
            row.setdefault("can_resume", False)
        return rows
    finally:
        conn.close()


@app.route("/analysis-list")
def analysis_list():
    error = request.args.get("error")
    rows = []
    clients = {}
    try:
        rows = _list_analyses()
        clients = {c["id"]: c["name"] for c in _load_clients()}
    except Exception as exc:
        error = error or str(exc)
    for row in rows:
        row["client_name"] = clients.get(row["client_id"], str(row["client_id"]))
    return render_template(
        "analysis_list.html", rows=rows, error=error,
        saved=request.args.get("saved"),
        deleted=request.args.get("deleted"))


@app.route("/api/analysis-progress")
def api_analysis_progress():
    try:
        jobs = [
            {"id": analysis_id, **job}
            for analysis_id, job in _analysis_progress().items()
        ]
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"jobs": jobs})


@app.route("/analysis/<int:analysis_id>/delete", methods=["POST"])
def delete_analysis(analysis_id):
    try:
        existing = _analysis_header(analysis_id)
        conn = _open_dpo()
        try:
            cur = conn.cursor()
            cur.execute("EXEC dbo.usp_Analysis_Delete @AnalysisID=?", analysis_id)
            conn.commit()
        finally:
            conn.close()
        if existing:
            results_store.remove_results(*_result_parts(existing))
    except Exception as exc:
        return redirect(url_for("analysis_list", error=f"Could not delete the analysis: {exc}"))
    return redirect(url_for("analysis_list", deleted="1"))


@app.route("/analysis/<int:analysis_id>/edit")
def edit_analysis(analysis_id):
    try:
        analysis = _analysis_header(analysis_id)
        clients = _load_clients()
    except Exception as exc:
        return redirect(url_for("analysis_list", error=str(exc)))
    if not analysis:
        return redirect(url_for("analysis_list", error="Analysis was not found."))
    try:
        engine_version = _engine_version(analysis["rdm_name"], analysis["rdm_analysis_id"])
    except Exception:
        engine_version = ""
    return render_template(
        "edit_analysis.html", analysis=analysis, clients=clients,
        engine_version=engine_version,
        error=request.args.get("error"), saved=request.args.get("saved"))


@app.route("/analysis/<int:analysis_id>/edit", methods=["POST"])
def update_analysis(analysis_id):
    try:
        values = _analysis_form_values()
    except ValueError as exc:
        return redirect(url_for("edit_analysis", analysis_id=analysis_id, error=str(exc)))
    try:
        existing = _analysis_header(analysis_id)
        conn = _open_dpo()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                EXEC dbo.usp_Analysis_Update
                    @AnalysisID=?, @AnalysisName=?, @AnalysisDescription=?, @ClientID=?,
                    @EdmName=?, @PortfolioID=?, @RdmName=?, @RdmAnalysisID=?,
                    @RdmAnalysisName=?, @Peril=?;
                """,
                analysis_id, *values)
            conn.commit()
        finally:
            conn.close()
        if existing:
            updated = _analysis_header(analysis_id)
            results_store.move_results(*_result_parts(existing), *_result_parts(updated))
    except Exception as exc:
        return redirect(url_for(
            "edit_analysis", analysis_id=analysis_id,
            error=f"Could not update the analysis: {exc}"))
    return redirect(url_for("edit_analysis", analysis_id=analysis_id, saved="1"))


@app.route("/analysis/<int:analysis_id>/settings/<int:settings_id>", methods=["POST"])
def update_analysis_settings(analysis_id, settings_id):
    loss_level = request.form.get("loss_level", "").strip()
    perspective = request.form.get("loss_perspective", "").strip()
    if loss_level not in ("Account", "Policy", "Location", "Lob", "County", "State", "Other"):
        return redirect(url_for(
            "analysis_settings", analysis_id=analysis_id,
            error="Loss level is not recognized."))
    if perspective not in ("GU", "GR", "RL"):
        return redirect(url_for(
            "analysis_settings", analysis_id=analysis_id,
            error="Loss perspective is not recognized."))
    try:
        conn = _open_dpo()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                EXEC dbo.usp_AnalysisSettings_Update
                    @AnalysisSettingsID=?, @LossLevelName=?, @LossPerspectiveCode=?;
                """,
                settings_id, loss_level, perspective)
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        message = str(exc)
        if "50006" in message or "already saved" in message.lower() or "UQ_tAnalysisSettings" in message:
            message = "That loss level and loss perspective are already saved for this analysis."
        else:
            message = f"Could not update analysis settings: {exc}"
        return redirect(url_for("analysis_settings", analysis_id=analysis_id, error=message))
    return redirect(url_for("analysis_settings", analysis_id=analysis_id, saved="1"))


def _ep_slots(points):
    """Fifteen display slots. Unused slots are 0 and are not stored."""
    slots = [int(point) for point in list(points)[:15]]
    slots.extend([0] * (15 - len(slots)))
    return slots


def _catalog_ep_points():
    """Return (15 default slots, every catalog return period)."""
    conn = _open_dpo()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT ReturnPeriod, SortOrder, IsActive FROM dbo.tEpPoint ORDER BY ReturnPeriod")
        rows = [(int(r[0]), int(r[1]), bool(r[2])) for r in cur.fetchall()]
    finally:
        conn.close()
    catalog = [period for period, _sort, _active in rows]
    active = [(period, sort) for period, sort, active in rows if active]
    slots = [0] * 15
    if active and all(1 <= sort <= 15 for _period, sort in active) and len({sort for _period, sort in active}) == len(active):
        for period, sort in active:
            slots[sort - 1] = period
    else:
        for index, (period, _sort) in enumerate(sorted(active)):
            if index < 15:
                slots[index] = period
    return slots, catalog


@app.route("/admin")
def admin_page():
    saved = rdm_settings.load()
    moodys = rdm_settings.load_moodys()
    view = {k: v for k, v in saved.items() if k != "password"}
    moodys_view = {k: v for k, v in moodys.items() if k != "password"}
    try:
        ep_points, ep_catalog = _catalog_ep_points()
        ep_error = None
    except Exception as exc:
        ep_points, ep_catalog = _ep_slots([]), []
        ep_error = f"Could not load default EP points: {exc}"
    try:
        display_ranges = _display_ranges()
        range_error = None
    except Exception as exc:
        display_ranges = {"dtc": [], "elevation": [], "year_built": list(_YEAR_BUILT_DEFAULTS)}
        range_error = f"Could not read EDM ranges: {exc}"
    return render_template(
        "admin.html", settings=view, moodys=moodys_view,
        servers=rdm_settings.ALLOWED_SERVERS,
        ep_points=ep_points, ep_catalog=ep_catalog, ep_error=ep_error,
        ep_type=rdm_settings.load_ep_type(),
        dtc_ranges=_range_fields(display_ranges["dtc"]),
        elevation_ranges=_range_fields(display_ranges["elevation"]),
        year_built_ranges=_year_built_fields(display_ranges.get("year_built") or _YEAR_BUILT_DEFAULTS),
        ranges_saved=rdm_settings.load_ranges(),
        range_error=range_error,
        flash=request.args.get("flash"), flash_kind=request.args.get("kind", "success"))


def _admin_settings_or_redirect():
    try:
        return rdm_settings.from_form(request.form, rdm_settings.load()), None
    except ValueError as exc:
        return None, redirect(url_for("admin_page", flash=str(exc), kind="error", panel="dpo"))


def _dpo_from_moodys(database):
    moodys = _moodys_login()
    return {
        "server": moodys["server"],
        "database": (database or "BMS_DPO").strip() or "BMS_DPO",
        "username": moodys.get("username", ""),
        "password": moodys.get("password", ""),
        "driver": moodys.get("driver") or APP_CFG["odbc_driver"],
        "use_integrated_security": False,
    }


@app.route("/admin/server/test", methods=["POST"])
def admin_server_test():
    database = request.form.get("database", "").strip() or "BMS_DPO"
    try:
        settings = _dpo_from_moodys(database)
        _test_sql(settings, driver=settings["driver"], use_windows_auth=False)
    except Exception as exc:
        return redirect(url_for("admin_page", flash=f"Connection failed: {exc}", kind="error", panel="dpo"))
    return redirect(url_for(
        "admin_page", kind="success", panel="dpo",
        flash=f"Connected to {settings['server']}/{settings['database']}."))


@app.route("/admin/server/save", methods=["POST"])
def admin_server_save():
    database = request.form.get("database", "").strip() or "BMS_DPO"
    try:
        settings = _dpo_from_moodys(database)
        _test_sql(settings, driver=settings["driver"], use_windows_auth=False)
        rdm_settings.save(settings)
    except Exception as exc:
        return redirect(url_for("admin_page", flash=f"Connection failed: {exc}", kind="error", panel="dpo"))
    return redirect(url_for(
        "admin_page", kind="success", panel="dpo",
        flash="DPO database settings were tested and saved on the Moody's server."))


def _moodys_or_redirect():
    try:
        return rdm_settings.from_moodys_form(request.form, rdm_settings.load_moodys()), None
    except ValueError as exc:
        return None, redirect(url_for("admin_page", flash=str(exc), kind="error", panel="moodys"))


@app.route("/admin/ep-type", methods=["POST"])
def admin_ep_type_save():
    try:
        rdm_settings.save_ep_type(request.form.get("ep_type", "1"))
    except (TypeError, ValueError) as exc:
        return redirect(url_for("admin_page", flash=str(exc), kind="error", panel="curve"))
    choice = "OEP" if rdm_settings.load_ep_type() == 1 else "AEP"
    return redirect(url_for(
        "admin_page", kind="success", panel="curve",
        flash=f"Moody's modeling curve is {choice}."))


@app.route("/admin/ranges", methods=["POST"])
def admin_ranges_save():
    kind = request.form.get("kind", "").strip()
    panel = {"dtc": "dtc", "elevation": "elevation", "year_built": "year"}.get(kind, "dtc")
    try:
        bands = _parse_year_built_form(request.form) if kind == "year_built" else _parse_range_form(request.form)
        rdm_settings.save_ranges(kind, bands)
    except ValueError as exc:
        return redirect(url_for("admin_page", flash=str(exc), kind="error", panel="range", range=panel))
    label = {"dtc": "DTC", "elevation": "Elevation", "year_built": "Year built"}.get(kind, "Range")
    return redirect(url_for(
        "admin_page", kind="success", panel="range", range=panel,
        flash=f"{label} ranges saved. Exhibits use these bands."))


@app.route("/admin/moodys/test", methods=["POST"])
def admin_moodys_test():
    settings, redir = _moodys_or_redirect()
    if redir:
        return redir
    try:
        _test_sql(settings, use_windows_auth=False)
    except Exception as exc:
        return redirect(url_for("admin_page", flash=f"Connection failed: {exc}", kind="error", panel="moodys"))
    return redirect(url_for(
        "admin_page", kind="success", panel="moodys",
        flash=f"Connected to {settings['server']}."))


@app.route("/admin/moodys/save", methods=["POST"])
def admin_moodys_save():
    settings, redir = _moodys_or_redirect()
    if redir:
        return redir
    try:
        _test_sql(settings, use_windows_auth=False)
        rdm_settings.save_moodys(settings)
        rdm_settings.save({
            "server": settings["server"],
            "database": _dpo_database(),
            "username": settings.get("username", ""),
            "password": settings.get("password", ""),
            "use_integrated_security": False,
        })
    except Exception as exc:
        return redirect(url_for("admin_page", flash=f"Connection failed: {exc}", kind="error", panel="moodys"))
    return redirect(url_for(
        "admin_page", kind="success", panel="moodys",
        flash="Moody's SQL login was tested, encrypted, and saved."))


@app.route("/admin/ep-points/catalog", methods=["POST"])
def admin_ep_catalog_add():
    text = request.form.get("return_period", "").strip()
    try:
        period = int(text)
    except ValueError:
        return redirect(url_for(
            "admin_page", panel="ep", kind="error",
            flash="EP points must be whole numbers."))
    if period <= 0:
        return redirect(url_for(
            "admin_page", panel="ep", kind="error",
            flash="A list point must be greater than zero."))
    try:
        conn = _open_dpo()
        try:
            cur = conn.cursor()
            cur.execute("EXEC dbo.usp_EpPoint_CatalogAdd @ReturnPeriod=?", period)
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        message = str(exc)
        if "50010" in message:
            message = "That point is already in the list."
        else:
            message = f"Could not add the EP point: {exc}"
        return redirect(url_for("admin_page", panel="ep", kind="error", flash=message))
    return redirect(url_for(
        "admin_page", panel="ep", kind="success",
        flash=f"{period} was added to the list."))


@app.route("/admin/ep-points", methods=["POST"])
def admin_ep_points_save():
    raw = request.form.getlist("return_period")
    periods = []
    for value in raw:
        text = value.strip()
        if not text:
            continue
        try:
            period = int(text)
        except ValueError:
            return redirect(url_for(
                "admin_page", panel="ep", kind="error",
                flash="EP points must be whole numbers."))
        if period < 0:
            return redirect(url_for(
                "admin_page", panel="ep", kind="error",
                flash="EP points must be zero or greater."))
        periods.append(period)
    periods = (periods + [0] * 15)[:15]
    stored = [period for period in periods if period > 0]
    if not stored:
        return redirect(url_for(
            "admin_page", panel="ep", kind="error",
            flash="Drag at least one point onto a default slot."))
    if len(periods) > 15:
        return redirect(url_for(
            "admin_page", panel="ep", kind="error",
            flash="Default EP points are limited to 15."))
    try:
        conn = _open_dpo()
        try:
            cur = conn.cursor()
            cur.execute(
                "EXEC dbo.usp_EpPoint_Replace @ReturnPeriods=?",
                ",".join(str(p) for p in periods))
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        message = str(exc)
        if "50009" in message:
            message = "Default EP points are limited to 15."
        else:
            message = f"Could not save default EP points: {exc}"
        return redirect(url_for("admin_page", panel="ep", kind="error", flash=message))
    return redirect(url_for(
        "admin_page", panel="ep", kind="success",
        flash="Default EP points were saved."))


@app.route("/connect", methods=["POST"])
def connect():
    moodys = rdm_settings.load_moodys()
    server = (moodys.get("server") or "").strip()
    database = request.form.get("database", "").strip()
    driver = (moodys.get("driver") or "").strip() or APP_CFG["odbc_driver"]
    username = (moodys.get("username") or "").strip()
    password = moodys.get("password") or ""
    if not server or not username:
        return redirect(url_for(
            "new_analysis",
            error="Set the Moody's SQL login under Admin, Server, Moody's Server."))
    if not database:
        return redirect(url_for("new_analysis", error="Database is required."))
    params = {"server": server, "database": database, "driver": driver,
              "use_windows_auth": False, "username": username,
              "password": password}
    try:
        conn = rdm_db.connect(
            server=server, database=database, driver=driver,
            use_windows_auth=False, username=username, password=password)
        conn.close()
    except Exception as exc:
        return redirect(url_for("new_analysis",
                                error=f"Connection failed: {exc}"))
    tok = secrets.token_hex(16)
    SESSIONS[tok] = {"conn": params}
    resp = redirect(url_for("analyses"))
    resp.set_cookie("pml_session", tok, httponly=True, samesite="Lax")
    return resp


@app.route("/disconnect")
def disconnect():
    tok = request.cookies.get("pml_session")
    SESSIONS.pop(tok, None)
    resp = redirect(url_for("index"))
    resp.delete_cookie("pml_session")
    return resp


@app.route("/analyses")
def analyses():
    sess, redir = _require_session()
    if redir:
        return redir
    problems = _schema_problems()
    rows, error = [], None
    try:
        conn = _open(sess)
        try:
            id_col = SCHEMA["analyses_id_column"]
            name_col = SCHEMA["analyses_name_column"]
            extra = [c for c in SCHEMA["analyses_display_columns"]
                     if c not in (id_col, name_col)]
            cols = [f"[{id_col}] AS _id", f"[{name_col}] AS _name"] + \
                   [f"[{c}] AS _x{i}" for i, c in enumerate(extra)]
            df = rdm_db.read_df(
                conn, f"SELECT {', '.join(cols)} FROM {SCHEMA['analyses_table']}")
            for _, r in df.iterrows():
                aid = r["_id"]
                probes = {g: rdm_db.probe_granularity(conn, SCHEMA, g, aid)
                          for g in rdm_schema.GRANULARITIES}
                rows.append({
                    "id": str(aid), "name": str(r["_name"]),
                    "extra": [str(r[f"_x{i}"]) for i in range(len(extra))],
                    "extra_headers": extra,
                    "probes": probes,
                })
        finally:
            conn.close()
    except Exception as exc:
        error = f"Could not read {SCHEMA['analyses_table']}: {exc}"
    return render_template("analyses.html", rows=rows, error=error,
                           problems=problems,
                           granularities=rdm_schema.GRANULARITIES,
                           conn=sess["conn"])


@app.route("/setup")
def setup():
    sess, redir = _require_session()
    if redir:
        return redir
    analysis_id = request.args.get("analysis_id", "")
    analysis_name = request.args.get("analysis_name", analysis_id)
    probes, error = {}, None
    try:
        conn = _open(sess)
        try:
            probes = {g: rdm_db.probe_granularity(conn, SCHEMA, g, analysis_id)
                      for g in rdm_schema.GRANULARITIES}
        finally:
            conn.close()
    except Exception as exc:
        error = str(exc)
    return render_template(
        "setup.html", analysis_id=analysis_id, analysis_name=analysis_name,
        probes=probes, error=error,
        granularities=rdm_schema.GRANULARITIES,
        perspectives=rdm_schema.PERSPECTIVES)


@app.route("/run", methods=["POST"])
def run():
    sess, redir = _require_session()
    if redir:
        return redir
    analysis_id = request.form.get("analysis_id", "")
    granularity = request.form.get("granularity", "")
    perspective = request.form.get("perspective", "")
    if granularity not in rdm_schema.GRANULARITIES or \
            perspective not in rdm_schema.PERSPECTIVES:
        return redirect(url_for("setup", analysis_id=analysis_id,
                                error="Invalid granularity or perspective."))
    started = time.time()
    try:
        conn = _open(sess)
        try:
            mat = rdm_db.fetch_loss_matrix(conn, SCHEMA, granularity,
                                           perspective, analysis_id)
            if mat.shape[1] == 0:
                raise ValueError("No entities found for this analysis.")
            excl_df, diff_df, portfolio_pml, portfolio_curve = \
                rdm_analysis.leave_one_out_pml(mat)
            run_id = time.strftime("%Y%m%d_%H%M%S")
            schema_prefix = SCHEMA["results_schema"]
            prefix = SCHEMA["results_table_prefix"]
            meta = {"analysis_id": analysis_id,
                    "granularity": granularity,
                    "perspective": perspective}
            excl_table = f"{schema_prefix}.{prefix}EXCL_{run_id}"
            diff_table = f"{schema_prefix}.{prefix}DIFF_{run_id}"
            rdm_db.write_results_table(conn, excl_df, excl_table, meta)
            rdm_db.write_results_table(conn, diff_df, diff_table, meta)
        finally:
            conn.close()
    except Exception as exc:
        return render_template("error.html",
                               message=f"Run failed: {exc}"), 500

    RUNS[run_id] = {
        "analysis_id": analysis_id, "granularity": granularity,
        "perspective": perspective, "excl": excl_df, "diff": diff_df,
        "portfolio_pml": portfolio_pml,
        "portfolio_curve": portfolio_curve,
        "loss_matrix": mat,  # for on-demand entity curves
        "excl_table": excl_table, "diff_table": diff_table,
        "elapsed": round(time.time() - started, 1),
        "n_events": int(mat.shape[0]), "n_entities": int(mat.shape[1]),
        "points": rdm_analysis.TARGET_POINTS,
    }
    return redirect(url_for("results", run_id=run_id))


@app.route("/results/<run_id>")
def results(run_id):
    sess, redir = _require_session()
    if redir:
        return redir
    run = RUNS.get(run_id)
    if not run:
        return render_template("error.html",
                               message="Unknown or expired run id."), 404
    t, l = rdm_analysis.downsample_curve(*run["portfolio_curve"])
    chart = {"t": [float(x) for x in t], "l": [float(x) for x in l]}
    return render_template("results.html", run=run, run_id=run_id,
                           chart=chart)


@app.route("/api/curve/<run_id>")
def api_curve(run_id):
    if not _token():
        return jsonify({"error": "not connected"}), 401
    run = RUNS.get(run_id)
    if not run:
        return jsonify({"error": "unknown run"}), 404
    entity = request.args.get("entity")
    mat = run["loss_matrix"]
    portfolio = mat.to_numpy(dtype=float).sum(axis=1)
    if entity and entity in mat.columns.astype(str):
        col = mat.columns[mat.columns.astype(str) == entity][0]
        vec = portfolio - mat[col].to_numpy(dtype=float)
        vec = vec.clip(min=0.0)
        label = f"Excl {entity}"
    else:
        vec = portfolio
        label = "Portfolio"
    t, l = rdm_analysis.downsample_curve(*rdm_analysis.ep_curve(vec))
    return jsonify({"label": label,
                    "t": [float(x) for x in t],
                    "l": [float(x) for x in l]})


@app.route("/download/<run_id>/<which>")
def download(run_id, which):
    if not _token():
        return redirect(url_for("index"))
    run = RUNS.get(run_id)
    if not run or which not in ("excl", "diff"):
        return render_template("error.html",
                               message="Unknown run or file."), 404
    df = run[which].copy()
    df.insert(0, "entity", df.index)
    csv = df.to_csv(index=False)
    name = (f"{run['analysis_id']}_{run['granularity']}_"
            f"{which.upper()}_{run_id}.csv")
    return Response(csv, mimetype="text/csv",
                    headers={"Content-Disposition":
                             f"attachment; filename={name}"})


if __name__ == "__main__":
    platform_port = os.environ.get("HTTP_PLATFORM_PORT")
    if platform_port:
        app.run(host="127.0.0.1", port=int(platform_port), debug=False, threaded=True)
    else:
        app.run(host="0.0.0.0", port=int(APP_CFG.get("flask_port", 5000)),
                debug=False, threaded=True)
