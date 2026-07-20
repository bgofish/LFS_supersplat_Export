"""Best-effort secure credential storage; never falls back to plain text."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from pathlib import Path

from .storage import default_cache_root

SERVICE = "lfs_supersplat"
ACCOUNT = "api-key"


def load_token() -> str:
    try:
        import keyring  # type: ignore[import-not-found]

        value = keyring.get_password(SERVICE, ACCOUNT)
        if value:
            return value.strip()
    except Exception:
        pass
    if os.name != "nt":
        return ""
    try:
        return _dpapi_unprotect(_credential_path().read_bytes())
    except Exception:
        return ""


def save_token(token: str) -> bool:
    token = token.strip()
    if not token:
        clear_token()
        return True
    try:
        import keyring  # type: ignore[import-not-found]

        keyring.set_password(SERVICE, ACCOUNT, token)
        return True
    except Exception:
        pass
    if os.name != "nt":
        return False
    try:
        path = _credential_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_dpapi_protect(token))
        return True
    except Exception:
        return False


def clear_token() -> None:
    try:
        import keyring  # type: ignore[import-not-found]

        keyring.delete_password(SERVICE, ACCOUNT)
    except Exception:
        pass
    try:
        _credential_path().unlink(missing_ok=True)
    except OSError:
        pass


def _credential_path() -> Path:
    return default_cache_root() / "credentials.dpapi"


if os.name == "nt":
    class _DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _dpapi_protect(value: str) -> bytes:
    if os.name != "nt":
        raise OSError("Windows DPAPI is unavailable")
    raw = value.encode("utf-8")
    source = (ctypes.c_byte * len(raw)).from_buffer_copy(raw)
    input_blob = _DataBlob(len(raw), ctypes.cast(source, ctypes.POINTER(ctypes.c_byte)))
    output_blob = _DataBlob()
    crypt = ctypes.windll.crypt32.CryptProtectData
    local_free = ctypes.windll.kernel32.LocalFree
    if not crypt(ctypes.byref(input_blob), "LichtFeld SuperSplat", None, None, None, 0, ctypes.byref(output_blob)):
        raise OSError("Windows DPAPI encryption failed")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        local_free(output_blob.pbData)


def _dpapi_unprotect(value: bytes) -> str:
    if os.name != "nt":
        raise OSError("Windows DPAPI is unavailable")
    source = (ctypes.c_byte * len(value)).from_buffer_copy(value)
    input_blob = _DataBlob(len(value), ctypes.cast(source, ctypes.POINTER(ctypes.c_byte)))
    output_blob = _DataBlob()
    crypt = ctypes.windll.crypt32.CryptUnprotectData
    local_free = ctypes.windll.kernel32.LocalFree
    if not crypt(ctypes.byref(input_blob), None, None, None, None, 0, ctypes.byref(output_blob)):
        raise OSError("Windows DPAPI decryption failed")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData).decode("utf-8").strip()
    finally:
        local_free(output_blob.pbData)
