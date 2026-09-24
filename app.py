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

import os
import secrets
import time

import pandas as pd
from flask import (Flask, Response, jsonify, redirect, render_template,
                   request, url_for)

from rdm import analysis as rdm_analysis
from rdm import db as rdm_db
from rdm import schema as rdm_schema
from rdm import client_match
from rdm import settings_store as rdm_settings

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)

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


def _open_dpo():
    saved = rdm_settings.load()
    if not (saved.get("server") or "").strip():
        raise RuntimeError("Set the DPO server under Admin, Server, DPO Server.")
    return _open_saved(saved, saved.get("database") or "BMS_DPO", saved["use_integrated_security"])


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
    """Active CMS clients and the loss level / perspective lookups."""
    conn = _open_dpo()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT ClientID, ClientName FROM BMS_CMS.dbo.tClient "
            "WHERE ClientStatus = 'Active' ORDER BY ClientName")
        clients = [{"id": int(r[0]), "name": r[1]} for r in cur.fetchall()]
        cur.execute("SELECT LossLevelName FROM dbo.tLossLevel ORDER BY LossLevelID")
        levels = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT LossPerspectiveCode FROM dbo.tLossPerspective ORDER BY LossPerspectiveID")
        perspectives = [r[0] for r in cur.fetchall()]
        return clients, levels, perspectives
    finally:
        conn.close()


# ---------------------------------------------------------------- routes

@app.route("/")
def index():
    return render_template("home.html")


@app.route("/new")
def new_analysis():
    clients, levels, perspectives = [], [], []
    error = request.args.get("error")
    try:
        clients, levels, perspectives = _dpo_form_lists()
    except Exception as exc:
        error = error or str(exc)
    if not levels:
        levels = ["Account", "Policy", "Location", "Lob", "Other"]
    if not perspectives:
        perspectives = ["GU", "GR", "RL"]
    created = request.args.get("created")
    return render_template(
        "connect.html", clients=clients, loss_levels=levels,
        perspectives=perspectives, error=error, created=created)


@app.route("/api/rdm-databases")
def api_rdm_databases():
    try:
        conn = _open_saved(_moodys_login(), "master", False)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT name FROM sys.databases WHERE name LIKE '%RDM%' ORDER BY name")
            names = [r[0] for r in cur.fetchall()]
        finally:
            conn.close()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"databases": names})


@app.route("/api/rdm-client")
def api_rdm_client():
    database = request.args.get("database", "").strip()
    try:
        conn = _open_dpo()
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
                "SELECT [ID], [NAME], [PERIL], [EXPOSUREID], [SRCEDM] "
                "FROM dbo.rdm_analysis ORDER BY [NAME]")
            rows = [{
                "id": int(r[0]),
                "name": r[1] or "",
                "peril": r[2] or "",
                "portfolioId": r[3],
                "edmName": r[4] or "",
            } for r in cur.fetchall()]
        finally:
            conn.close()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"analyses": rows})


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
    peril = request.form.get("peril", "").strip() or None
    loss_level = request.form.get("loss_level", "").strip()
    perspective = request.form.get("loss_perspective", "").strip()
    if not name or not client_id or not rdm_name or not rdm_analysis_id or not rdm_analysis_name:
        return redirect(url_for("new_analysis", error="Analysis name, client, and an RDM analysis are required."))
    if loss_level not in ("Account", "Policy", "Location", "Lob", "Other"):
        return redirect(url_for("new_analysis", error="Loss level is not recognized."))
    if perspective not in ("GU", "GR", "RL"):
        return redirect(url_for("new_analysis", error="Loss perspective is not recognized."))
    try:
        portfolio_id = int(portfolio_raw) if portfolio_raw else None
        rdm_id = int(rdm_analysis_id)
        client = int(client_id)
    except ValueError:
        return redirect(url_for("new_analysis", error="Client, portfolio, and RDM analysis id must be numbers."))
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
        return redirect(url_for("new_analysis", error=f"Could not create the analysis: {exc}"))
    return redirect(url_for("new_analysis", created=analysis_id))


@app.route("/admin")
def admin_page():
    saved = rdm_settings.load()
    moodys = rdm_settings.load_moodys()
    view = {k: v for k, v in saved.items() if k != "password"}
    moodys_view = {k: v for k, v in moodys.items() if k != "password"}
    return render_template(
        "admin.html", settings=view, moodys=moodys_view,
        servers=rdm_settings.ALLOWED_SERVERS,
        flash=request.args.get("flash"), flash_kind=request.args.get("kind", "success"))


def _admin_settings_or_redirect():
    try:
        return rdm_settings.from_form(request.form, rdm_settings.load()), None
    except ValueError as exc:
        return None, redirect(url_for("admin_page", flash=str(exc), kind="error", panel="dpo"))


@app.route("/admin/server/test", methods=["POST"])
def admin_server_test():
    settings, redir = _admin_settings_or_redirect()
    if redir:
        return redir
    try:
        _test_sql(settings)
    except Exception as exc:
        return redirect(url_for("admin_page", flash=f"Connection failed: {exc}", kind="error", panel="dpo"))
    return redirect(url_for(
        "admin_page", kind="success", panel="dpo",
        flash=f"Connected to {settings['server']}/{settings['database']}."))


@app.route("/admin/server/save", methods=["POST"])
def admin_server_save():
    settings, redir = _admin_settings_or_redirect()
    if redir:
        return redir
    try:
        _test_sql(settings)
        rdm_settings.save(settings)
    except Exception as exc:
        return redirect(url_for("admin_page", flash=f"Connection failed: {exc}", kind="error", panel="dpo"))
    return redirect(url_for(
        "admin_page", kind="success", panel="dpo",
        flash="Server settings were tested, encrypted, and saved."))


def _moodys_or_redirect():
    try:
        return rdm_settings.from_moodys_form(request.form, rdm_settings.load_moodys()), None
    except ValueError as exc:
        return None, redirect(url_for("admin_page", flash=str(exc), kind="error", panel="moodys"))


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
    except Exception as exc:
        return redirect(url_for("admin_page", flash=f"Connection failed: {exc}", kind="error", panel="moodys"))
    return redirect(url_for(
        "admin_page", kind="success", panel="moodys",
        flash="Moody's SQL login was tested, encrypted, and saved."))


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
        app.run(host="127.0.0.1", port=int(platform_port), debug=False)
    else:
        app.run(host="0.0.0.0", port=int(APP_CFG.get("flask_port", 5000)),
                debug=False)
