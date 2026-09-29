"""Wide EXCL and DIFF curves, one Parquet file per analysis.

Files live under App_Data/results/<client>/<analysis id>_<name>_<peril>.parquet.
A later run of the same analysis replaces that file.
"""

import os

import pandas as pd

_APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_INVALID = '<>:"/\\|?*'


def path_segment(value):
    """Keep a client or analysis name readable and legal as a Windows path part."""
    text = "".join(
        "_" if char in _INVALID or ord(char) < 32 else char
        for char in str(value or "").strip())
    return text.strip(" .") or "unknown"


def result_path(client, analysis_id, analysis_name, peril):
    """Path under App_Data for one analysis."""
    folder = os.path.join(_APP_ROOT, "App_Data", "results", path_segment(client))
    filename = (
        f"{int(analysis_id)}_{path_segment(analysis_name)}_{path_segment(peril)}.parquet")
    return os.path.join(folder, filename)


def wide_frame(rows, metrics=None):
    """Turn entity / result type / return period / value rows into one row per entity.

    metrics maps an entity id to excl_aal, excl_stddev, diff_aal, and diff_stddev.
    """
    entities = {}
    periods = set()
    for entity_id, result_type, period, value in rows:
        kind = str(result_type).strip()
        if kind not in ("EXCL", "DIFF"):
            continue
        period = int(period)
        periods.add(period)
        entities.setdefault(str(entity_id), {"EXCL": {}, "DIFF": {}})
        entities[str(entity_id)][kind][period] = float(value or 0)
    periods = sorted(periods)
    names = sorted(entities, key=lambda name: (name != "PORTFOLIO", name))
    metrics = metrics or {}
    records = []
    for name in names:
        extra = metrics.get(name, {})
        record = {"EntityID": name}
        for period in periods:
            record[f"EXCL_{period}"] = entities[name]["EXCL"].get(period, 0.0)
            record[f"DIFF_{period}"] = entities[name]["DIFF"].get(period, 0.0)
        record["EXCL_AAL"] = float(extra.get("excl_aal") or 0)
        record["EXCL_STDDEV"] = float(extra.get("excl_stddev") or 0)
        record["DIFF_AAL"] = float(extra.get("diff_aal") or 0)
        record["DIFF_STDDEV"] = float(extra.get("diff_stddev") or 0)
        records.append(record)
    return pd.DataFrame.from_records(records)


def write_results(client, analysis_id, analysis_name, peril, frame):
    path = result_path(client, analysis_id, analysis_name, peril)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    frame.to_parquet(path, engine="pyarrow", compression="zstd", index=False)
    return path


def move_results(old_client, analysis_id, old_name, old_peril, client, analysis_name, peril):
    """Follow an analysis when its client, name, or peril changes."""
    source = result_path(old_client, analysis_id, old_name, old_peril)
    target = result_path(client, analysis_id, analysis_name, peril)
    if os.path.normcase(source) == os.path.normcase(target) or not os.path.isfile(source):
        return target
    os.makedirs(os.path.dirname(target), exist_ok=True)
    os.replace(source, target)
    _remove_empty_folder(os.path.dirname(source))
    return target


def remove_results(client, analysis_id, analysis_name, peril):
    path = result_path(client, analysis_id, analysis_name, peril)
    if os.path.isfile(path):
        os.remove(path)
    _remove_empty_folder(os.path.dirname(path))


def _remove_empty_folder(folder):
    if os.path.isdir(folder) and not os.listdir(folder):
        os.rmdir(folder)


def read_curves(client, analysis_id, analysis_name, peril):
    """Return ({EXCL, DIFF} -> entity -> period -> loss, sorted return periods)."""
    path = result_path(client, analysis_id, analysis_name, peril)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"No Parquet results at {path}.")
    frame = pd.read_parquet(path, engine="pyarrow")
    excl_cols = _period_columns(frame.columns, "EXCL_")
    diff_cols = _period_columns(frame.columns, "DIFF_")
    periods = sorted(int(column.split("_", 1)[1]) for column in excl_cols)
    stored = {"EXCL": {}, "DIFF": {}, "METRICS": {}}
    for record in frame.to_dict("records"):
        entity_id = str(record["EntityID"])
        stored["EXCL"][entity_id] = {
            int(column.split("_", 1)[1]): float(record[column] or 0)
            for column in excl_cols
        }
        stored["DIFF"][entity_id] = {
            int(column.split("_", 1)[1]): float(record[column] or 0)
            for column in diff_cols
        }
        stored["METRICS"][entity_id] = {
            "excl_aal": _metric_value(record, "EXCL_AAL"),
            "excl_stddev": _metric_value(record, "EXCL_STDDEV"),
            "diff_aal": _metric_value(record, "DIFF_AAL"),
            "diff_stddev": _metric_value(record, "DIFF_STDDEV"),
        }
    return stored, periods


def _period_columns(columns, prefix):
    """EXCL_100 is a return period. EXCL_AAL is not."""
    found = []
    for column in columns:
        if not str(column).startswith(prefix):
            continue
        suffix = str(column).split("_", 1)[1]
        if suffix.isdigit():
            found.append(str(column))
    return found


def _metric_value(record, column):
    if column not in record:
        return None
    value = record[column]
    if value is None or value != value:
        return None
    return float(value)
