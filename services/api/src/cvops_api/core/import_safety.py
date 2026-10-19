"""Safety checks for dataset import: which server folders may be read, and what a zip may hold.

Used by the import API route (fast feedback) and by the ``import_dataset`` step
(authoritative: workflows can reach the step without going through the route).
"""

from __future__ import annotations

import zipfile
from pathlib import Path, PureWindowsPath

# Only judge the compression ratio of files bigger than this; tiny files compress oddly.
_RATIO_MIN_SIZE = 1024 * 1024


def import_roots(raw: str) -> list[Path]:
    """Parse ``IMPORT_ROOTS`` (comma-separated) into resolved absolute directories."""
    return [Path(part.strip()).resolve() for part in raw.split(",") if part.strip()]


def resolve_import_folder(raw: str, roots: list[Path], *, check_exists: bool = True) -> Path:
    """Return the real path of ``raw`` if it lies inside one of ``roots``, else raise ``ValueError``.

    ``Path.resolve()`` follows symlinks and ``..``, so neither can be used to climb out of
    a root. Containment is checked before existence so a caller cannot probe which paths
    exist outside the roots. The API passes ``check_exists=False`` because the worker, not
    the API, owns the filesystem the folder lives on.
    """
    if not roots:
        raise ValueError("Server-path import is disabled (IMPORT_ROOTS is not set)")
    path = Path(raw)
    if not path.is_absolute():
        raise ValueError("folder_path must be an absolute path")
    resolved = path.resolve()
    if not any(resolved.is_relative_to(root) for root in roots):
        raise ValueError("folder_path is outside the allowed import folders (IMPORT_ROOTS)")
    if check_exists and not resolved.is_dir():
        raise ValueError("folder_path is not a directory")
    return resolved


def _is_unsafe_name(name: str) -> bool:
    """True for absolute paths, drive letters, or any ``..`` path component."""
    normalized = name.replace("\\", "/")
    return (
        normalized.startswith("/")
        or bool(PureWindowsPath(name).drive)
        or ".." in normalized.split("/")
    )


def check_zip(zf: zipfile.ZipFile, *, max_bytes: int, max_entries: int, max_ratio: int) -> None:
    """Raise ``ValueError`` if the archive is too big, too crowded, or has unsafe member names.

    Reads only the archive's directory, so nothing is unpacked yet. The sizes are the ones
    the archive declares; ``zipfile`` stops reading a member at its declared size.
    """
    infos = zf.infolist()
    if len(infos) > max_entries:
        raise ValueError(f"Zip has too many entries ({len(infos)}; limit {max_entries})")

    total = 0
    for info in infos:
        if _is_unsafe_name(info.filename):
            raise ValueError(f"Unsafe path in zip archive: {info.filename}")
        total += info.file_size
        if total > max_bytes:
            raise ValueError(f"Zip is too large when unpacked (limit {max_bytes // 1024**2} MiB)")
        if info.compress_size > 0 and info.file_size > _RATIO_MIN_SIZE:
            ratio = info.file_size / info.compress_size
            if ratio > max_ratio:
                raise ValueError(f"Suspicious compression ratio for {info.filename}: {ratio:.0f}")
