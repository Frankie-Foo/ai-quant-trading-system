"""Packaged Loop reviews must identify the exact deployed source."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from operations.loop_integration import review_builder


def test_release_identity_matches_packaged_source_and_detects_tamper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        review_builder.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=128, stdout=""),
    )
    files = ("data_plane/providers/alpaca.py", "kernel/catalysts.py")
    for name in files:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"a\nb\n" if name == files[0] else name.encode("ascii"))
    identity = "a" * 40 + "+" + review_builder._release_code_digest(tmp_path)
    (tmp_path / "release-code-id.txt").write_text(identity, encoding="ascii")
    assert review_builder._git_commit(tmp_path) == identity
    (tmp_path / files[0]).write_bytes(b"a\r\nb\r\n")
    assert review_builder._git_commit(tmp_path) == identity
    monkeypatch.setattr(
        review_builder.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="b" * 40),
    )
    assert review_builder._git_commit(tmp_path) == identity
    (tmp_path / files[1]).write_bytes(b"changed")
    with pytest.raises(RuntimeError, match="does not match"):
        review_builder._git_commit(tmp_path)


def test_git_identity_without_release_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        review_builder.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="b" * 40),
    )
    assert review_builder._git_commit(tmp_path) == "b" * 40
