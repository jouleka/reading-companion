"""Private object-storage directory permissions, including supported Windows Python versions."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from functools import lru_cache
import os
from pathlib import Path


class _Acl(ctypes.Structure):
    _fields_ = [("revision", wintypes.BYTE), ("reserved", wintypes.BYTE),
                ("size", wintypes.WORD), ("count", wintypes.WORD), ("reserved2", wintypes.WORD)]


class _AllowedAce(ctypes.Structure):
    _fields_ = [("kind", wintypes.BYTE), ("flags", wintypes.BYTE), ("size", wintypes.WORD),
                ("mask", wintypes.DWORD), ("sid_start", wintypes.DWORD)]


class _WindowsSecurity:
    def __init__(self):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.security = ctypes.WinDLL("advapi32", use_last_error=True)
        pointer = ctypes.c_void_p
        out_pointer = ctypes.POINTER(pointer)
        self._bind(self.kernel, "GetCurrentProcess", wintypes.HANDLE, [])
        self._bind(self.kernel, "CloseHandle", wintypes.BOOL, [wintypes.HANDLE])
        self._bind(self.kernel, "LocalFree", pointer, [pointer])
        self._bind(self.security, "OpenProcessToken", wintypes.BOOL,
                   [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)])
        self._bind(self.security, "GetTokenInformation", wintypes.BOOL,
                   [wintypes.HANDLE, ctypes.c_int, pointer, wintypes.DWORD,
                    ctypes.POINTER(wintypes.DWORD)])
        self._bind(self.security, "ConvertSidToStringSidW", wintypes.BOOL,
                   [pointer, ctypes.POINTER(wintypes.LPWSTR)])
        self._bind(self.security, "ConvertStringSecurityDescriptorToSecurityDescriptorW", wintypes.BOOL,
                   [wintypes.LPCWSTR, wintypes.DWORD, out_pointer,
                    ctypes.POINTER(wintypes.DWORD)])
        self._bind(self.security, "GetSecurityDescriptorDacl", wintypes.BOOL,
                   [pointer, ctypes.POINTER(wintypes.BOOL), out_pointer,
                    ctypes.POINTER(wintypes.BOOL)])
        self._bind(self.security, "GetSecurityDescriptorControl", wintypes.BOOL,
                   [pointer, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD)])
        self._bind(self.security, "GetNamedSecurityInfoW", wintypes.DWORD,
                   [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, out_pointer, out_pointer,
                    out_pointer, out_pointer, out_pointer])
        self._bind(self.security, "SetNamedSecurityInfoW", wintypes.DWORD,
                   [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
                    pointer, pointer, pointer, pointer])
        self._bind(self.security, "GetAce", wintypes.BOOL,
                   [pointer, wintypes.DWORD, out_pointer])

    @staticmethod
    def _bind(library, name, result, arguments):
        function = getattr(library, name)
        function.restype, function.argtypes = result, arguments

    @staticmethod
    def _require(success):
        if not success:
            raise ctypes.WinError(ctypes.get_last_error())

    def _sid(self, pointer):
        text = wintypes.LPWSTR()
        self._require(self.security.ConvertSidToStringSidW(pointer, ctypes.byref(text)))
        try:
            return text.value
        finally:
            self.kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))

    def _process_sid(self):
        token, size = wintypes.HANDLE(), wintypes.DWORD()
        self._require(self.security.OpenProcessToken(
            self.kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)))
        try:
            self.security.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
            if not size.value:
                raise OSError("Windows token identity is unavailable")
            information = ctypes.create_string_buffer(size.value)
            self._require(self.security.GetTokenInformation(
                token, 1, information, size, ctypes.byref(size)))
            return self._sid(ctypes.c_void_p.from_buffer(information))
        finally:
            self.kernel.CloseHandle(token)

    def _permissions(self, path):
        owner, dacl, descriptor = (ctypes.c_void_p() for _ in range(3))
        error = self.security.GetNamedSecurityInfoW(
            path, 1, 0x00000001 | 0x00000004, ctypes.byref(owner), None,
            ctypes.byref(dacl), None, ctypes.byref(descriptor))
        if error:
            raise ctypes.WinError(error)
        try:
            control, revision = wintypes.WORD(), wintypes.DWORD()
            self._require(self.security.GetSecurityDescriptorControl(
                descriptor, ctypes.byref(control), ctypes.byref(revision)))
            if not dacl.value:
                return self._sid(owner), False, []
            acl = ctypes.cast(dacl, ctypes.POINTER(_Acl)).contents
            entries = []
            for index in range(acl.count):
                ace = ctypes.c_void_p()
                self._require(self.security.GetAce(dacl, index, ctypes.byref(ace)))
                header = ctypes.cast(ace, ctypes.POINTER(_AllowedAce)).contents
                sid = self._sid(ace.value + _AllowedAce.sid_start.offset) if header.kind == 0 else None
                entries.append((header.kind, header.flags, header.mask, sid))
            return self._sid(owner), bool(control.value & 0x1000), entries
        finally:
            self.kernel.LocalFree(descriptor)

    def secure(self, path):
        current_user = self._process_sid()
        trusted = {current_user, "S-1-5-18", "S-1-5-32-544"}
        expected = {(0, 0x03, 0x001F01FF, sid) for sid in trusted}
        owner, protected, entries = self._permissions(path)
        if owner not in trusted:
            raise OSError("object storage directory has an untrusted filesystem owner")
        if protected and len(entries) == len(expected) and set(entries) == expected:
            return  # Do not rewrite/propagate an already-correct ACL on every object read.
        sddl = "D:P" + "".join(f"(A;OICI;FA;;;{sid})" for sid in sorted(trusted))
        descriptor, dacl = ctypes.c_void_p(), ctypes.c_void_p()
        self._require(self.security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(descriptor), None))
        try:
            present, defaulted = wintypes.BOOL(), wintypes.BOOL()
            self._require(self.security.GetSecurityDescriptorDacl(
                descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)))
            error = self.security.SetNamedSecurityInfoW(
                path, 1, 0x00000004 | 0x80000000, None, None, dacl, None)
            if error:
                raise ctypes.WinError(error)
        finally:
            self.kernel.LocalFree(descriptor)
        owner, protected, entries = self._permissions(path)
        if owner not in trusted or not protected or len(entries) != len(expected) or set(entries) != expected:
            raise OSError("private object storage directory ACL verification failed")


@lru_cache(maxsize=1)
def _windows_security():
    return _WindowsSecurity()


def secure_directory(path: str | Path) -> None:
    """Harden only this existing directory; never alter ancestor or general workspace ACLs."""
    if os.name != "nt":
        os.chmod(path, 0o700)
        return
    path = Path(path)
    attributes = path.lstat().st_file_attributes
    if attributes & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT: do not follow owner-folder junctions.
        raise OSError("object storage directories cannot be reparse points")
    _windows_security().secure(str(path))
