"""Read-only effective write-access checks for Windows managed paths."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from pathlib import Path

_GENERIC_WRITE = 0x40000000
_DELETE = 0x00010000
_DACL_SECURITY_INFORMATION = 0x00000004
_TOKEN_QUERY = 0x0008
_TOKEN_DUPLICATE = 0x0002
_SECURITY_IMPERSONATION = 2
_FILE_ADD_FILE = 0x0002
_FILE_ADD_SUBDIRECTORY = 0x0004
_FILE_WRITE_ATTRIBUTES = 0x0100
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _GenericMapping(ctypes.Structure):
    _fields_ = (
        ("GenericRead", wintypes.DWORD),
        ("GenericWrite", wintypes.DWORD),
        ("GenericExecute", wintypes.DWORD),
        ("GenericAll", wintypes.DWORD),
    )


_FILE_MAPPING = _GenericMapping(0x00120089, 0x00120116, 0x001200A0, 0x001F01FF)


def _existing_target(path: Path) -> Path | None:
    candidate = path
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            return None
        candidate = parent
    return candidate


def _open_for_effective_write_access(target: Path) -> bool | None:
    """Ask NTFS to authorize the current token without creating or changing a file."""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        desired = (
            _FILE_ADD_FILE | _FILE_ADD_SUBDIRECTORY | _FILE_WRITE_ATTRIBUTES
            if target.is_dir()
            else _GENERIC_WRITE | _DELETE
        )
        handle = kernel32.CreateFileW(
            str(target),
            desired,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
            None,
            _OPEN_EXISTING,
            _FILE_FLAG_BACKUP_SEMANTICS if target.is_dir() else 0,
            None,
        )
        if handle == _INVALID_HANDLE_VALUE:
            return False
        kernel32.CloseHandle(handle)
        return True
    except (AttributeError, OSError):
        return None


def effective_write_access(path: Path) -> bool | None:
    """Return effective token write access, or ``None`` when it cannot be queried.

    This intentionally performs no create/open-for-write probe.  Windows
    ``os.access`` is not an ACL-effective authorization check and can disagree
    with the access token that the application will actually use.
    """
    target = _existing_target(path)
    if target is None:
        return None
    if os.name != "nt":
        try:
            return os.access(target, os.W_OK | os.X_OK)
        except OSError:
            return None
    opened = _open_for_effective_write_access(target)
    if opened is not None:
        return opened
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32.GetCurrentProcess.argtypes = ()
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        advapi32.OpenProcessToken.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        )
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.DuplicateToken.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.POINTER(wintypes.HANDLE),
        )
        advapi32.DuplicateToken.restype = wintypes.BOOL
        advapi32.GetFileSecurityW.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        )
        advapi32.GetFileSecurityW.restype = wintypes.BOOL
        advapi32.AccessCheck.argtypes = (
            ctypes.c_void_p,
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(_GenericMapping),
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.BOOL),
        )
        advapi32.AccessCheck.restype = wintypes.BOOL
        advapi32.MapGenericMask.argtypes = (
            ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(_GenericMapping)
        )
        advapi32.MapGenericMask.restype = None
        token = wintypes.HANDLE()
        duplicate = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(), _TOKEN_QUERY | _TOKEN_DUPLICATE, ctypes.byref(token)
        ):
            return None
        try:
            if not advapi32.DuplicateToken(
                token, _SECURITY_IMPERSONATION, ctypes.byref(duplicate)
            ):
                return None
            try:
                needed = wintypes.DWORD()
                # The expected insufficient-buffer failure is the query result.
                advapi32.GetFileSecurityW(
                    str(target), _DACL_SECURITY_INFORMATION, None, 0, ctypes.byref(needed)
                )
                if not needed.value:
                    return None
                descriptor = ctypes.create_string_buffer(needed.value)
                if not advapi32.GetFileSecurityW(
                    str(target),
                    _DACL_SECURITY_INFORMATION,
                    descriptor,
                    needed.value,
                    ctypes.byref(needed),
                ):
                    return None
                desired = wintypes.DWORD(
                    _GENERIC_WRITE | (0 if target.is_dir() else _DELETE)
                )
                advapi32.MapGenericMask(ctypes.byref(desired), ctypes.byref(_FILE_MAPPING))
                privilege_size = wintypes.DWORD(1024)
                privileges = ctypes.create_string_buffer(privilege_size.value)
                granted = wintypes.DWORD()
                status = wintypes.BOOL()
                if not advapi32.AccessCheck(
                    ctypes.cast(descriptor, ctypes.c_void_p),
                    duplicate,
                    desired,
                    ctypes.byref(_FILE_MAPPING),
                    ctypes.cast(privileges, ctypes.c_void_p),
                    ctypes.byref(privilege_size),
                    ctypes.byref(granted),
                    ctypes.byref(status),
                ):
                    return None
                return bool(status.value)
            finally:
                kernel32.CloseHandle(duplicate)
        finally:
            kernel32.CloseHandle(token)
    except (AttributeError, OSError):
        return None
