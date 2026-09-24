"""Schema mapping: translate config.yaml into the table/column names the app queries.

Every RDM database names things slightly differently, so NOTHING about the
physical schema is hard-coded here except the analyses table name the user
confirmed (RDM_analysis). Copy config.example.yaml to config.yaml and fill
in the mapping for your database before first use. validate() reports any
mapping the app needs but you have not supplied yet.
"""

import os

import yaml

GRANULARITIES = ("account", "policy", "location")
PERSPECTIVES = ("ground up", "gross", "net before tax")


def _default_schema():
    g = {}
    for gran in GRANULARITIES:
        g[gran] = {
            "loss_table": "",            # e.g. dbo.RDM_ELT_Account
            "analysis_id_column": "",    # column holding the analysis key
            "event_id_column": "",       # column holding the event id
            "entity_id_column": "",      # account / policy / location number
            "loss_columns": {           # loss column per perspective
                "ground up": "",
                "gross": "",
                "net before tax": "",
            },
        }
    return {
        "analyses_table": "RDM_analysis",
        "analyses_id_column": "analysis_id",
        "analyses_name_column": "analysis_name",
        # extra columns from RDM_analysis to show in the summary list
        "analyses_display_columns": [],
        "granularities": g,
        # where result tables are written, e.g. "dbo"
        "results_schema": "dbo",
        "results_table_prefix": "PML_",
    }


def _deep_merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path="config.yaml"):
    """Load config.yaml over the defaults. Returns (app_cfg, schema)."""
    schema = _default_schema()
    app_cfg = {
        "server": "",
        "database": "",
        "odbc_driver": "ODBC Driver 18 for SQL Server",
        "use_windows_auth": True,
        "username": "",
        "port": 1433,
        "host": "127.0.0.1",
        "flask_port": 5000,
    }
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        app_cfg = _deep_merge(app_cfg, raw.get("connection", {}))
        schema = _deep_merge(schema, raw.get("schema", {}))
    return app_cfg, schema


def validate(schema):
    """Return a list of human-readable problems with the schema mapping."""
    problems = []
    if not schema.get("analyses_table"):
        problems.append("schema.analyses_table is not set.")
    if not schema.get("analyses_id_column"):
        problems.append("schema.analyses_id_column is not set.")
    for gran in GRANULARITIES:
        g = schema["granularities"][gran]
        missing = [k for k in ("loss_table", "analysis_id_column",
                               "event_id_column", "entity_id_column")
                   if not g.get(k)]
        for p in PERSPECTIVES:
            if not g.get("loss_columns", {}).get(p):
                missing.append(f"loss_columns[{p}]")
        if missing:
            problems.append(
                f"granularity '{gran}' is missing: {', '.join(missing)} "
                f"(app will report this granularity as unavailable)."
            )
    return problems


def granularity_available(schema, granularity):
    """True when the mapping for a granularity is complete enough to query."""
    g = schema["granularities"][granularity]
    cols = [g.get("loss_table"), g.get("analysis_id_column"),
            g.get("event_id_column"), g.get("entity_id_column")]
    if not all(cols):
        return False
    return all(g.get("loss_columns", {}).get(p) for p in PERSPECTIVES)
