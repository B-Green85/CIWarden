"""Tests for scripts.start_gates.load_env — .env file loading."""
from __future__ import annotations

import os
from typing import TYPE_CHECKING
from unittest.mock import patch

if TYPE_CHECKING:
    from pathlib import Path

from scripts.start_gates import load_env


class TestLoadEnv:
    def test_loads_simple_key_value(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("FOO=bar\n")

        with patch.dict(os.environ, {}, clear=True):
            loaded = load_env(str(env_file))

        assert loaded == {"FOO": "bar"}

    def test_sets_os_environ(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("MY_VAR=hello\n")

        with patch.dict(os.environ, {}, clear=True):
            load_env(str(env_file))
            assert os.environ["MY_VAR"] == "hello"

    def test_skips_blank_lines(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("A=1\n\n\nB=2\n")

        with patch.dict(os.environ, {}, clear=True):
            loaded = load_env(str(env_file))

        assert loaded == {"A": "1", "B": "2"}

    def test_skips_comments(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("# This is a comment\nKEY=value\n# Another comment\n")

        with patch.dict(os.environ, {}, clear=True):
            loaded = load_env(str(env_file))

        assert loaded == {"KEY": "value"}

    def test_skips_lines_without_equals(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("VALID=yes\nno_equals_here\nALSO_VALID=yep\n")

        with patch.dict(os.environ, {}, clear=True):
            loaded = load_env(str(env_file))

        assert loaded == {"VALID": "yes", "ALSO_VALID": "yep"}

    def test_strips_double_quotes(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text('KEY="quoted value"\n')

        with patch.dict(os.environ, {}, clear=True):
            loaded = load_env(str(env_file))

        assert loaded == {"KEY": "quoted value"}

    def test_strips_single_quotes(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("KEY='single quoted'\n")

        with patch.dict(os.environ, {}, clear=True):
            loaded = load_env(str(env_file))

        assert loaded == {"KEY": "single quoted"}

    def test_preserves_unmatched_quotes(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("KEY=\"mixed'\n")

        with patch.dict(os.environ, {}, clear=True):
            loaded = load_env(str(env_file))

        assert loaded == {"KEY": "\"mixed'"}

    def test_handles_equals_in_value(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("KEY=base64==\n")

        with patch.dict(os.environ, {}, clear=True):
            loaded = load_env(str(env_file))

        assert loaded == {"KEY": "base64=="}

    def test_strips_whitespace_around_key_and_value(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("  KEY  =  value  \n")

        with patch.dict(os.environ, {}, clear=True):
            loaded = load_env(str(env_file))

        assert loaded == {"KEY": "value"}

    def test_returns_empty_dict_when_file_missing(self, tmp_path: Path) -> None:
        loaded = load_env(str(tmp_path / "nonexistent"))
        assert loaded == {}

    def test_returns_empty_dict_for_empty_file(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("")

        loaded = load_env(str(env_file))
        assert loaded == {}

    def test_loads_example_env_format(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text(
            "ANTHROPIC_API_KEY=sk-ant-test123\n"
            "CDMAD_LOCAL_DEV=1\n"
            "CDMAD_DRIFT_THRESHOLD=0.5\n"
        )

        with patch.dict(os.environ, {}, clear=True):
            loaded = load_env(str(env_file))

        assert loaded["ANTHROPIC_API_KEY"] == "sk-ant-test123"
        assert loaded["CDMAD_LOCAL_DEV"] == "1"
        assert loaded["CDMAD_DRIFT_THRESHOLD"] == "0.5"
