"""Unit tests for dataset-import safety checks (no database needed)."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from cvops_api.core.import_safety import check_zip, import_roots, resolve_import_folder

LIMITS = dict(max_bytes=10 * 1024 * 1024, max_entries=100, max_ratio=100)


def _zip(members: dict[str, bytes], compression: int = zipfile.ZIP_STORED) -> zipfile.ZipFile:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return zipfile.ZipFile(io.BytesIO(buf.getvalue()))


def _size(zf: zipfile.ZipFile) -> int:
    """Length of the archive bytes behind a ``_zip`` result (what the step passes in)."""
    assert isinstance(zf.fp, io.BytesIO)
    return zf.fp.getbuffer().nbytes


# ── server folder paths ────────────────────────────────────────────────────


def test_roots_parsing_ignores_blanks(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    assert import_roots(f" {a} , ,{b},") == [a.resolve(), b.resolve()]
    assert import_roots("") == []


@pytest.mark.parametrize("entry", ["data/imports", "./data", "imports", "~/data"])
def test_relative_root_is_refused(tmp_path: Path, entry: str) -> None:
    # Relative to whose cwd? The API and the worker differ, so refuse outright.
    with pytest.raises(ValueError, match="absolute"):
        import_roots(f"{tmp_path},{entry}")


@pytest.mark.parametrize("entry", ["/", "//", "/.", "/..", "/tmp/.."])
def test_filesystem_root_is_refused(tmp_path: Path, entry: str) -> None:
    # Checked after resolving, so spellings of "/" are caught too.
    with pytest.raises(ValueError, match="filesystem root"):
        import_roots(f"{tmp_path},{entry}")


def test_root_that_symlinks_to_slash_is_refused(tmp_path: Path) -> None:
    link = tmp_path / "everything"
    link.symlink_to("/", target_is_directory=True)
    with pytest.raises(ValueError, match="filesystem root"):
        import_roots(str(link))


def test_folder_import_is_disabled_without_roots(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="disabled"):
        resolve_import_folder(str(tmp_path), [])


def test_folder_inside_root_is_accepted(tmp_path: Path) -> None:
    ds = tmp_path / "datasets" / "cars"
    ds.mkdir(parents=True)
    root = tmp_path / "datasets"
    assert resolve_import_folder(str(ds), [root.resolve()]) == ds.resolve()


def test_root_itself_is_accepted(tmp_path: Path) -> None:
    assert resolve_import_folder(str(tmp_path), [tmp_path.resolve()]) == tmp_path.resolve()


def test_folder_outside_root_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "datasets"
    other = tmp_path / "secrets"
    root.mkdir()
    other.mkdir()
    with pytest.raises(ValueError, match="outside"):
        resolve_import_folder(str(other), [root.resolve()])


def test_sibling_with_same_prefix_is_rejected(tmp_path: Path) -> None:
    # "/x/datasets-private" starts with the text "/x/datasets" but is not inside it.
    root = tmp_path / "datasets"
    sibling = tmp_path / "datasets-private"
    root.mkdir()
    sibling.mkdir()
    with pytest.raises(ValueError, match="outside"):
        resolve_import_folder(str(sibling), [root.resolve()])


def test_dotdot_escape_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "datasets"
    (tmp_path / "secrets").mkdir()
    root.mkdir()
    with pytest.raises(ValueError, match="outside"):
        resolve_import_folder(str(root / ".." / "secrets"), [root.resolve()])


def test_symlink_escape_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "datasets"
    secret = tmp_path / "secrets"
    root.mkdir()
    secret.mkdir()
    (root / "link").symlink_to(secret, target_is_directory=True)
    with pytest.raises(ValueError, match="outside"):
        resolve_import_folder(str(root / "link"), [root.resolve()])


def test_relative_path_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        resolve_import_folder("datasets/cars", [tmp_path.resolve()])


def test_missing_folder_inside_root_is_rejected_only_when_existence_is_checked(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "nope"
    with pytest.raises(ValueError, match="not a directory"):
        resolve_import_folder(str(missing), [tmp_path.resolve()])
    # The API cannot see the worker's disk, so it skips the existence check.
    assert resolve_import_folder(str(missing), [tmp_path.resolve()], check_exists=False)


def test_existence_of_paths_outside_roots_is_not_revealed(tmp_path: Path) -> None:
    root = tmp_path / "datasets"
    root.mkdir()
    existing, missing = tmp_path, tmp_path / "does-not-exist"
    messages = set()
    for p in (existing, missing):
        with pytest.raises(ValueError) as exc:
            resolve_import_folder(str(p), [root.resolve()])
        messages.add(str(exc.value))
    assert len(messages) == 1  # same answer either way


# ── zip archives ───────────────────────────────────────────────────────────


def test_normal_zip_is_accepted() -> None:
    zf = _zip({"images/a.jpg": b"x" * 1000, "labels/a.txt": b"0 0.5 0.5 0.1 0.1"})
    check_zip(zf, archive_size=_size(zf), **LIMITS)


def test_too_many_entries_is_rejected() -> None:
    zf = _zip({f"f{i}.txt": b"x" for i in range(5)})
    with pytest.raises(ValueError, match="too many entries"):
        check_zip(zf, archive_size=_size(zf), **{**LIMITS, "max_entries": 4})


def test_oversized_zip_is_rejected() -> None:
    zf = _zip({"a.bin": b"\0" * 3000, "b.bin": b"\0" * 3000})
    with pytest.raises(ValueError, match="too large"):
        check_zip(zf, archive_size=_size(zf), **{**LIMITS, "max_bytes": 5000})


def test_zip_bomb_ratio_is_rejected() -> None:
    # 5 MiB of zeros deflates to a few KiB: far over a 100x ratio.
    zf = _zip({"bomb.bin": b"\0" * (5 * 1024 * 1024)}, zipfile.ZIP_DEFLATED)
    with pytest.raises(ValueError, match="compression ratio"):
        check_zip(zf, archive_size=_size(zf), **LIMITS)


def test_bomb_split_into_many_small_files_is_rejected() -> None:
    # 20 files of exactly 1 MiB each: none is over the per-file floor, so only the
    # archive-wide ratio (20 MiB unpacked from a few tens of KiB) can catch it.
    members = {f"part{i}.bin": b"\0" * (1024 * 1024) for i in range(20)}
    zf = _zip(members, zipfile.ZIP_DEFLATED)
    with pytest.raises(ValueError, match="whole zip"):
        check_zip(zf, archive_size=_size(zf), **{**LIMITS, "max_bytes": 100 * 1024 * 1024})


def test_overall_ratio_uses_the_real_archive_size() -> None:
    # Declared sizes alone look fine (stored, ratio 1), but if the archive on disk is far
    # smaller than what it unpacks to (e.g. overlapping entries), the whole-zip check fires.
    zf = _zip({"a.bin": b"x" * (2 * 1024 * 1024)})
    check_zip(zf, archive_size=_size(zf), **LIMITS)
    with pytest.raises(ValueError, match="whole zip"):
        check_zip(zf, archive_size=10_000, **LIMITS)


def test_small_zip_is_not_judged_on_overall_ratio() -> None:
    # Many tiny, very compressible text files: high overall ratio, but under the floor.
    zf = _zip(
        {f"labels/{i}.txt": b"0 0.5 0.5 0.1 0.1\n" * 50 for i in range(80)}, zipfile.ZIP_DEFLATED
    )
    check_zip(zf, archive_size=_size(zf), **{**LIMITS, "max_ratio": 2})


def test_small_highly_compressible_file_is_not_flagged() -> None:
    # Under 1 MiB the ratio is not judged (tiny text files compress extremely well).
    zf = _zip({"a.txt": b"a" * 100_000}, zipfile.ZIP_DEFLATED)
    check_zip(zf, archive_size=_size(zf), **LIMITS)


@pytest.mark.parametrize(
    "name",
    [
        "../evil.txt",
        "a/../../evil.txt",
        "/etc/passwd",
        "..\\evil.txt",
        "C:\\evil.txt",
        "C:/evil.txt",
    ],
)
def test_unsafe_member_names_are_rejected(name: str) -> None:
    zf = _zip({name: b"x"})
    with pytest.raises(ValueError, match="Unsafe path"):
        check_zip(zf, archive_size=_size(zf), **LIMITS)


def test_dots_inside_a_name_are_not_unsafe() -> None:
    # The naive `".." in name` test would wrongly reject these legitimate names.
    zf = _zip({"images/photo..final.jpg": b"x", "labels/a..b.txt": b"x"})
    check_zip(zf, archive_size=_size(zf), **LIMITS)
