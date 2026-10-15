"""Native Windows ACL regressions; no configured or ancestor directory is used as a fixture."""
import ctypes
from ctypes import wintypes
import hashlib
import os
import uuid

import pytest

from app.hosted.filesystem_permissions import _windows_security
from app.hosted.storage import (
    EPUB_MEDIA_TYPE, EncryptedFilesystemStorage, ObjectStorageUnavailableError,
    StorageConfigurationError, new_source_ref,
)
from app.hosted.tenant.models import OwnerId


def _storage(path):
    return EncryptedFilesystemStorage(root=path, encryption_key=b"k" * 32, max_object_bytes=100)


def _broaden_fixture_directory(path):
    security = _windows_security()
    descriptor, dacl = ctypes.c_void_p(), ctypes.c_void_p()
    security._require(security.security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        "D:P(A;OICI;FA;;;WD)", 1, ctypes.byref(descriptor), None))
    try:
        present, defaulted = wintypes.BOOL(), wintypes.BOOL()
        security._require(security.security.GetSecurityDescriptorDacl(
            descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)))
        error = security.security.SetNamedSecurityInfoW(
            str(path), 1, 0x00000004 | 0x80000000, None, None, dacl, None)
        assert error == 0
    finally:
        security.kernel.LocalFree(descriptor)
    assert security._permissions(str(path))[2] == [(0, 3, 0x001F01FF, "S-1-1-0")]


@pytest.mark.skipif(os.name != "nt", reason="native Windows DACL contract")
def test_existing_broad_root_and_owner_are_repaired_without_touching_ancestors(tmp_path):
    security = _windows_security()
    before_ancestor = security._permissions(str(tmp_path))
    root = tmp_path / "objects"
    ref = new_source_ref(OwnerId(uuid.uuid4()))
    owner = root / ref.owner_id.value.hex
    owner.mkdir(parents=True)
    _broaden_fixture_directory(root)
    _broaden_fixture_directory(owner)
    storage = _storage(root)
    payload = b"synthetic permission audit"
    storage.put(ref, payload, media_type=EPUB_MEDIA_TYPE,
                expected_sha256=hashlib.sha256(payload).hexdigest())
    expected = {security._process_sid(), "S-1-5-18", "S-1-5-32-544"}
    for directory in (root, owner):
        filesystem_owner, protected, entries = security._permissions(str(directory))
        assert filesystem_owner in expected and protected
        assert set(entries) == {(0, 3, 0x001F01FF, sid) for sid in expected}
    file = next(owner.glob("*.epub.enc"))
    _filesystem_owner, _protected, entries = security._permissions(str(file))
    assert {entry[3] for entry in entries} == expected
    assert all(entry[:3] == (0, 0x10, 0x001F01FF) for entry in entries)
    assert storage.get(ref).data == payload
    assert security._permissions(str(tmp_path)) == before_ancestor


@pytest.mark.skipif(os.name != "nt", reason="native Windows DACL contract")
def test_read_repairs_preexisting_inherited_broad_owner_permissions(tmp_path):
    storage = _storage(tmp_path / "objects")
    ref = new_source_ref(OwnerId(uuid.uuid4()))
    payload = b"synthetic old object"
    storage.put(ref, payload, media_type=EPUB_MEDIA_TYPE,
                expected_sha256=hashlib.sha256(payload).hexdigest())
    owner = tmp_path / "objects" / ref.owner_id.value.hex
    file = next(owner.glob("*.epub.enc"))
    _broaden_fixture_directory(owner)
    security = _windows_security()
    assert {entry[3] for entry in security._permissions(str(file))[2]} == {"S-1-1-0"}
    assert storage.get(ref).data == payload
    assert {entry[3] for entry in security._permissions(str(file))[2]} == {
        security._process_sid(), "S-1-5-18", "S-1-5-32-544",
    }


def test_root_acl_failure_prevents_storage_startup(tmp_path, monkeypatch):
    def fail(_path):
        raise OSError("synthetic access denied")
    monkeypatch.setattr("app.hosted.storage.secure_directory", fail)
    with pytest.raises(StorageConfigurationError, match="permissions could not be secured"):
        _storage(tmp_path / "objects")


def test_owner_acl_failure_prevents_object_publication(tmp_path, monkeypatch):
    storage = _storage(tmp_path / "objects")
    def fail(_path):
        raise OSError("synthetic access denied")
    monkeypatch.setattr("app.hosted.storage.secure_directory", fail)
    ref = new_source_ref(OwnerId(uuid.uuid4()))
    payload = b"synthetic permission audit"
    with pytest.raises(ObjectStorageUnavailableError, match="directory permissions failed"):
        storage.put(ref, payload, media_type=EPUB_MEDIA_TYPE,
                    expected_sha256=hashlib.sha256(payload).hexdigest())
    assert not list((tmp_path / "objects").rglob("*.epub.enc"))
