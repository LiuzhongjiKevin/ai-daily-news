from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "check_secrets.py"


def load_module() -> object:
    spec = importlib.util.spec_from_file_location("check_secrets", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_scan_finds_raw_secret_in_binary_path_and_redacts_value(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Would catch binary tracked files leaking a configured secret undetected or echoed."""
    module = load_module()
    monkeypatch.setattr(module, "tracked_paths", lambda: [Path("含 空格.bin")])
    monkeypatch.setattr(module, "read_tracked_bytes", lambda _: b"prefix\\x00actual-secret\\xff")
    monkeypatch.setattr(module.os, "environ", {"MS_TOKEN_KEY": "actual-secret"})

    assert module.main() == 1
    output = capsys.readouterr().out
    assert "MS_TOKEN_KEY" in output
    assert "含 空格.bin" in output
    assert "actual-secret" not in output


def test_scan_skips_empty_values_and_reports_git_failure(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Would catch empty environment values matching every file or a failed tracked-file query passing."""
    module = load_module()
    monkeypatch.setattr(module.os, "environ", {"MAIL_TO": ""})
    monkeypatch.setattr(module, "tracked_paths", lambda: [Path("plain.txt")])
    monkeypatch.setattr(module, "read_tracked_bytes", lambda _: b"ordinary content")
    assert module.main() == 0

    def failed_paths() -> list[Path]:
        raise module.SecretScanError("git failed")

    monkeypatch.setattr(module, "tracked_paths", failed_paths)
    assert module.main() == 2
    assert "git failed" not in capsys.readouterr().err


def test_scan_allows_nonmatching_nonempty_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Would catch a safe tracked file being rejected merely because a secret is configured."""
    module = load_module()
    monkeypatch.setattr(module, "tracked_paths", lambda: [Path("ordinary file.txt")])
    monkeypatch.setattr(module, "read_tracked_bytes", lambda _: b"no credentials here")
    monkeypatch.setattr(module.os, "environ", {"DEEPSEEK_API_KEY": "different-value"})

    assert module.main() == 0


def test_tracked_paths_uses_git_nul_delimited_output_without_losing_whitespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Would catch newline parsing that splits a legitimate tracked filename."""
    module = load_module()
    completed = subprocess.CompletedProcess(
        ["git", "ls-files", "-z"],
        0,
        stdout="normal.txt\0含 空格.bin\0line\nbreak.txt\0".encode(),
    )
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: completed)

    assert module.tracked_paths() == [Path("normal.txt"), Path("含 空格.bin"), Path("line\nbreak.txt")]


def test_read_tracked_bytes_preserves_binary_nul_data(tmp_path: Path) -> None:
    """Would catch text decoding that makes a binary secret occurrence invisible to the scanner."""
    module = load_module()
    path = tmp_path / "binary.bin"
    path.write_bytes(b"before\x00secret\xffafter")

    assert module.read_tracked_bytes(path) == b"before\x00secret\xffafter"
