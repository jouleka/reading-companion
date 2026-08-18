"""Backup, portable export, verified restore, and data-directory lifecycle controls (LIT-24)."""

from .archive import (
    BackupResult,
    DataDirLock,
    DataDirLocked,
    LifecycleError,
    RestoreResult,
    VerificationReport,
    backup_book,
    restore_book,
    verify_archive,
)

__all__ = [
    "BackupResult",
    "DataDirLock",
    "DataDirLocked",
    "LifecycleError",
    "RestoreResult",
    "VerificationReport",
    "backup_book",
    "restore_book",
    "verify_archive",
]
