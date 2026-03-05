"""Tests for scripts.start_gates — service lifecycle management."""

import json
import os
from unittest.mock import patch

import pytest


class TestStartGatesModule:
    def test_gate_registry_import(self) -> None:
        """Verify start_gates can access GATE_REGISTRY."""
        from gates.gates import GATE_REGISTRY

        assert len(GATE_REGISTRY) > 0

    def test_pid_file_path(self) -> None:
        from scripts import start_gates

        assert start_gates.PID_FILE.endswith(".gate_pids.json")

    def test_root_resolves_to_project(self) -> None:
        from scripts import start_gates

        assert os.path.isdir(start_gates.ROOT)


class TestStopAll:
    def test_stop_no_pid_file(self, capsys: pytest.CaptureFixture[str]) -> None:
        """stop_all gracefully handles missing PID file."""
        from scripts.start_gates import stop_all

        with patch("scripts.start_gates.PID_FILE", "/tmp/nonexistent_pids.json"):  # nosec B108
            stop_all()
        captured = capsys.readouterr()
        assert "No running" in captured.out

    def test_stop_with_dead_processes(self, tmp_path: object) -> None:
        """stop_all handles already-terminated PIDs."""
        from scripts.start_gates import stop_all

        pid_file = os.path.join(str(tmp_path), "pids.json")
        with open(pid_file, "w") as f:
            json.dump({"lint": 999999999}, f)

        with patch("scripts.start_gates.PID_FILE", pid_file):
            stop_all()  # should not raise

        assert not os.path.exists(pid_file)
