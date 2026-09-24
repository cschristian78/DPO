"""Encrypted SQL connection settings, matching the CMS Admin Server tab.

Server, database, username, and password are stored with Windows DPAPI under
App_Data and are never written in clear text.
"""

import base64
import ctypes
import json
import os
from ctypes import wintypes

ALLOWED_SERVERS = (
    "vmus-sql-099",
    "VMUS-DPS-01",
    "MoodysDB",
    "VeriskCurrent",
    "VeriskPrevious",
)

_APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SETTINGS_PATH = os.path.join(_APP_ROOT, "App_Data", "connectionsettings.protected.json")

_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


_crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_crypt32.CryptProtectData.argtypes = [
    ctypes.POINTER(_DATA_BLOB), wintypes.LPCWSTR, ctypes.POINTER(_DATA_BLOB),
    ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DATA_BLOB),
]
_crypt32.CryptProtectData.restype = wintypes.BOOL
_crypt32.CryptUnprotectData.argtypes = [
    ctypes.POINTER(_DATA_BLOB), ctypes.POINTER(wintypes.LPWSTR),
    ctypes.POINTER(_DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p,
    wintypes.DWORD, ctypes.POINTER(_DATA_BLOB),
]
_crypt32.CryptUnprotectData.restype = wintypes.BOOL
_kernel32.LocalFree.argtypes = [ctypes.c_void_p]
_kernel32.LocalFree.restype = ctypes.c_void_p


def _defaults():
    return {
        "server": ALLOWED_SERVERS[0],
        "database": "BMS_DPO",
        "username": "",
        "password": "",
        "use_integrated_security": True,
    }


def _protect(value):
    raw = value.encode("utf-8")
    buf = ctypes.create_string_buffer(raw, len(raw))
    blob_in = _DATA_BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DATA_BLOB()
    ok = _crypt32.CryptProtectData(
        ctypes.byref(blob_in), "DPO connection settings",
        None, None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
    if not ok:
        raise OSError(ctypes.get_last_error(), "Could not encrypt connection settings.")
    try:
        protected = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        _kernel32.LocalFree(blob_out.pbData)
    return base64.b64encode(protected).decode("ascii")


def _unprotect(value, default=""):
    if not value:
        return default
    try:
        raw = base64.b64decode(value)
        buf = ctypes.create_string_buffer(raw, len(raw))
        blob_in = _DATA_BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        blob_out = _DATA_BLOB()
        ok = _crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None,
            _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
        if not ok:
            return default
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData).decode("utf-8")
        finally:
            _kernel32.LocalFree(blob_out.pbData)
    except Exception:
        return default


def _canonical_server(server):
    match = next((s for s in ALLOWED_SERVERS if s.lower() == (server or "").strip().lower()), None)
    if not match:
        raise ValueError("The selected SQL Server is not approved for this application.")
    return match


def _read_file():
    if not os.path.exists(_SETTINGS_PATH):
        return {}
    try:
        with open(_SETTINGS_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh) or {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_file(raw):
    os.makedirs(os.path.dirname(_SETTINGS_PATH), exist_ok=True)
    with open(_SETTINGS_PATH, "w", encoding="utf-8") as fh:
        json.dump(raw, fh, indent=2)


def _moodys_defaults():
    return {
        "server": "",
        "driver": "ODBC Driver 18 for SQL Server",
        "username": "",
        "password": "",
    }


def load():
    settings = _defaults()
    raw = _read_file()
    dpo = raw.get("Dpo") or raw
    settings["server"] = _unprotect(dpo.get("Server"), settings["server"])
    settings["database"] = _unprotect(dpo.get("Database"), "BMS_DPO") or "BMS_DPO"
    settings["username"] = _unprotect(dpo.get("UserName"), "")
    settings["password"] = _unprotect(dpo.get("Password"), "")
    settings["use_integrated_security"] = bool(dpo.get("UseIntegratedSecurity", True))
    if settings["server"] not in ALLOWED_SERVERS:
        settings["server"] = ALLOWED_SERVERS[0]
    return settings


def load_moodys():
    settings = _moodys_defaults()
    moodys = _read_file().get("Moodys") or {}
    settings["server"] = _unprotect(moodys.get("Server"), "")
    settings["driver"] = _unprotect(moodys.get("Driver"), settings["driver"]) or settings["driver"]
    settings["username"] = _unprotect(moodys.get("UserName"), "")
    settings["password"] = _unprotect(moodys.get("Password"), "")
    return settings


def save(settings):
    raw = _read_file()
    for key in ("Server", "Database", "UserName", "Password", "UseIntegratedSecurity"):
        raw.pop(key, None)
    raw["Dpo"] = {
        "Server": _protect(settings["server"]) if settings["server"] else None,
        "Database": _protect(settings["database"]) if settings["database"] else None,
        "UserName": _protect(settings["username"]) if settings["username"] else None,
        "Password": _protect(settings["password"]) if settings["password"] else None,
        "UseIntegratedSecurity": bool(settings["use_integrated_security"]),
    }
    _write_file(raw)


def save_moodys(settings):
    raw = _read_file()
    raw["Moodys"] = {
        "Server": _protect(settings["server"]) if settings["server"] else None,
        "Driver": _protect(settings["driver"]) if settings["driver"] else None,
        "UserName": _protect(settings["username"]) if settings["username"] else None,
        "Password": _protect(settings["password"]) if settings["password"] else None,
    }
    _write_file(raw)


def from_moodys_form(form, previous=None):
    """SQL login moved from New Analysis. Blank password keeps the saved one."""
    previous = previous or _moodys_defaults()
    server = (form.get("server") or "").strip()
    if not server:
        raise ValueError("Server is required.")
    username = (form.get("username") or "").strip()
    if not username:
        raise ValueError("SQL login requires a username.")
    password = form.get("password") or ""
    if not password:
        password = previous.get("password") or ""
    driver = (form.get("driver") or "").strip() or previous.get("driver") or _moodys_defaults()["driver"]
    return {
        "server": server,
        "driver": driver,
        "username": username,
        "password": password,
    }


def from_form(form, previous=None):
    """Build settings from the Admin Server form. Blank password keeps the saved one."""
    previous = previous or _defaults()
    server = _canonical_server(form.get("server", ""))
    database = (form.get("database") or "").strip() or "BMS_DPO"
    username = (form.get("username") or "").strip()
    password = form.get("password") or ""
    if not password:
        password = previous.get("password") or ""
    integrated = form.get("use_integrated_security") == "on"
    if integrated or not (username or password):
        integrated = True
        username = ""
        password = ""
    elif not username:
        raise ValueError("SQL login requires a username.")
    return {
        "server": server,
        "database": database,
        "username": username,
        "password": password,
        "use_integrated_security": integrated,
    }
