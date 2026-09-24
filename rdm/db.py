"""SQL Server access via pyodbc.

Credentials are passed in at runtime (UI or environment) and are only ever
held in process memory - they are never written to disk or logs.
"""

import pandas as pd


def build_connection_string(server, database, driver, use_windows_auth=True,
                             username="", password="", timeout=15):
    parts = [
        f"DRIVER={{{driver}}}",
        f"SERVER={server}",
        f"DATABASE={database}",
        "Encrypt=no",
        f"Connection Timeout={timeout}",
    ]
    if use_windows_auth:
        parts.append("Trusted_Connection=yes")
    else:
        parts.append(f"UID={username}")
        parts.append(f"PWD={password}")
    return ";".join(parts) + ";"


def connect(server, database, driver, use_windows_auth=True,
            username="", password=""):
    import pyodbc  # imported lazily so config validation works without it
    cs = build_connection_string(server, database, driver, use_windows_auth,
                                 username, password)
    return pyodbc.connect(cs)


def read_df(conn, sql, params=()):
    """Run a SELECT and return a pandas DataFrame."""
    return pd.read_sql(sql, conn, params=params)


def probe_granularity(conn, schema, granularity, analysis_id):
    """Check whether loss rows exist for an analysis at a granularity.

    Returns dict(available=bool, events=int, entities=int, note=str).
    """
    from .schema import granularity_available
    if not granularity_available(schema, granularity):
        return {"available": False, "events": 0, "entities": 0,
                "note": "schema mapping incomplete - see config.yaml"}
    g = schema["granularities"][granularity]
    sql = (
        f"SELECT COUNT(*) AS n_rows, "
        f"COUNT(DISTINCT [{g['entity_id_column']}]) AS n_entities, "
        f"COUNT(DISTINCT [{g['event_id_column']}]) AS n_events "
        f"FROM {g['loss_table']} "
        f"WHERE [{g['analysis_id_column']}] = ?"
    )
    try:
        df = read_df(conn, sql, [analysis_id])
    except Exception as exc:  # e.g. table/column name wrong in mapping
        return {"available": False, "events": 0, "entities": 0,
                "note": f"query failed: {exc}"}
    row = df.iloc[0]
    ok = int(row["n_rows"]) > 0
    return {
        "available": ok,
        "events": int(row["n_events"]),
        "entities": int(row["n_entities"]),
        "note": "" if ok else "no loss rows for this analysis",
    }


def fetch_loss_matrix(conn, schema, granularity, perspective, analysis_id):
    """Return event x entity loss DataFrame for one analysis/granularity/perspective."""
    g = schema["granularities"][granularity]
    loss_col = g["loss_columns"][perspective]
    sql = (
        f"SELECT [{g['event_id_column']}] AS event_id, "
        f"[{g['entity_id_column']}] AS entity_id, "
        f"[{loss_col}] AS loss "
        f"FROM {g['loss_table']} "
        f"WHERE [{g['analysis_id_column']}] = ?"
    )
    df = read_df(conn, sql, [analysis_id])
    df["entity_id"] = df["entity_id"].astype(str)
    mat = df.pivot_table(index="event_id", columns="entity_id",
                         values="loss", aggfunc="sum", fill_value=0.0)
    return mat


def write_results_table(conn, df, table_name, meta):
    """Write an EXCL/DIFF result DataFrame to a SQL Server table.

    meta: dict with analysis_id, granularity, perspective keys - stored as
    leading columns on every row.
    """
    cols = list(df.columns)
    col_defs = (
        "[analysis_id] NVARCHAR(255), [granularity] NVARCHAR(32), "
        "[perspective] NVARCHAR(32), [entity_id] NVARCHAR(255), "
        + ", ".join(f"[{c}] FLOAT" for c in cols)
    )
    cur = conn.cursor()
    # drop + recreate so re-runs are clean
    cur.execute(f"IF OBJECT_ID(N'{table_name}', N'U') IS NOT NULL "
                f"DROP TABLE {table_name}")
    cur.execute(f"CREATE TABLE {table_name} ({col_defs})")
    placeholders = ", ".join(["?"] * (4 + len(cols)))
    rows = [
        (str(meta["analysis_id"]), meta["granularity"], meta["perspective"],
         str(idx), *[float(v) for v in df.loc[idx, cols]])
        for idx in df.index
    ]
    cur.executemany(
        f"INSERT INTO {table_name} "
        f"([analysis_id],[granularity],[perspective],[entity_id],"
        + ",".join(f"[{c}]" for c in cols) + f") VALUES ({placeholders})",
        rows,
    )
    conn.commit()
    return table_name
