"""Tests for scripts.write_devlog_entry — jsonl → DEVLOG.md skeleton."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from scripts import write_devlog_entry as wde

if TYPE_CHECKING:
    from pathlib import Path


def _gates(**ms: int) -> dict[str, dict[str, Any]]:
    return {name: {"status": "PASS", "ms": val} for name, val in ms.items()}


def _entry(sha: str, msg: str, gates: dict[str, Any] | None, token: str | None, *, consumed: bool = False,
           ts: str = "2026-05-31T12:00:00Z", ins: int = 10, dels: int = 5) -> dict[str, Any]:
    return {
        "sha": sha, "short_sha": sha[:7], "token": token, "branch": "v1.1",
        "message": msg, "author": "local-dev", "files_changed": 1,
        "insertions": ins, "deletions": dels, "gates": gates,
        "total_ms": sum(g["ms"] for g in gates.values()) if gates else None,
        "consumed": consumed, "timestamp": ts,
    }


def _write_log(log: Path, entries: list[dict[str, Any]]) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


@pytest.fixture()
def wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    log = tmp_path / ".cdmad" / "commit_log.jsonl"
    devlog = tmp_path / "DEVLOG.md"
    monkeypatch.setattr(wde, "LOG_PATH", log)
    monkeypatch.setattr(wde, "DEVLOG_PATH", devlog)
    return log, devlog


_FULL_GATES = dict(lint=10, typecheck=20, security=30, memory=40, test=50, stress=60, build=70)


class TestList:
    def test_prints_unconsumed_writes_nothing(
        self, wired: tuple[Path, Path], capsys: pytest.CaptureFixture[str],
    ) -> None:
        log, devlog = wired
        _write_log(log, [_entry("aaaaaaa0", "feat: x", _gates(**_FULL_GATES), "tokA")])
        before = log.read_text()
        rc = wde.main(["--list"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "Unconsumed commits: 1" in out
        assert "feat: x" in out
        assert not devlog.exists()          # nothing written
        assert log.read_text() == before     # not marked consumed


class TestDryRun:
    def test_prints_skeleton_writes_nothing(self, wired: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
        log, devlog = wired
        _write_log(log, [_entry("bbbbbbb0", "fix: y", _gates(**_FULL_GATES), "tokB")])
        before = log.read_text()
        rc = wde.main(["--dry-run"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "## " in out and "{title placeholder}" in out
        assert "### Commits This Session" in out
        assert not devlog.exists()
        assert log.read_text() == before     # consumed flag untouched
        assert json.loads(log.read_text().splitlines()[0])["consumed"] is False


class TestNormalRun:
    def test_appends_and_marks_consumed(self, wired: tuple[Path, Path]) -> None:
        log, devlog = wired
        _write_log(log, [
            _entry("ccccccc0", "feat: a", _gates(**_FULL_GATES), "tokC"),
            _entry("ddddddd0", "feat: b", _gates(**_FULL_GATES), "tokD"),
        ])
        rc = wde.main([])
        assert rc == 0
        text = devlog.read_text()
        assert "### Commits This Session" in text
        assert "feat: a" in text and "feat: b" in text
        # both entries marked consumed in the jsonl
        consumed = [json.loads(line)["consumed"] for line in log.read_text().splitlines()]
        assert consumed == [True, True]

    def test_appends_to_existing_devlog(self, wired: tuple[Path, Path]) -> None:
        log, devlog = wired
        devlog.write_text("# DEVLOG\n\n## Old Entry\n\ncontent\n")
        _write_log(log, [_entry("eeeeeee0", "feat: c", _gates(**_FULL_GATES), "tokE")])
        wde.main([])
        text = devlog.read_text()
        assert "## Old Entry" in text          # existing preserved
        assert "feat: c" in text               # new appended
        assert text.index("Old Entry") < text.index("feat: c")


class TestNoUnconsumed:
    def test_clean_exit_no_write(self, wired: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
        log, devlog = wired
        _write_log(log, [_entry("fffffff0", "feat: done", _gates(**_FULL_GATES), "tokF", consumed=True)])
        rc = wde.main([])
        assert rc == 0
        assert "Nothing to write" in capsys.readouterr().out
        assert not devlog.exists()


class TestGateSummary:
    def test_fastest_slowest_runs(self, wired: tuple[Path, Path]) -> None:
        log, _ = wired
        g1 = _gates(lint=10, typecheck=20, security=30, memory=40, test=50, stress=60, build=70)
        g2 = _gates(lint=5, typecheck=25, security=15, memory=45, test=55, stress=65, build=75)
        entries = [_entry("1111111", "c1", g1, "t1"), _entry("2222222", "c2", g2, "t2")]
        summary = wde.gate_summary(entries)
        assert summary["lint"] == (5, 10, 2)        # fastest, slowest, runs
        assert summary["build"] == (70, 75, 2)

    def test_skipped_when_gates_null(self, wired: tuple[Path, Path]) -> None:
        entries = [_entry("3333333", "c3", None, None)]
        assert wde.gate_summary(entries) == {}


class TestFormat:
    def test_skeleton_matches_devlog_format(self, wired: tuple[Path, Path]) -> None:
        entries = [_entry("4444444", "feat: fmt", _gates(**_FULL_GATES), "tok4")]
        skeleton = wde.build_skeleton(entries, "May 31, 2026")
        # H2 header at the right level, and the per-entry epigraph closer
        assert skeleton.startswith("## May 31, 2026 — ")
        assert skeleton.rstrip().endswith('*"Generation is optional. Verification is not."*')
        assert skeleton.count("---") >= 3   # section separators
