"""Tests for hooks.pre_commit — git hook integration."""

import subprocess
from unittest.mock import MagicMock, patch


class TestGetCurrentSha:
    @patch("subprocess.check_output")
    def test_returns_sha_from_rev_parse(self, mock_output: MagicMock) -> None:
        mock_output.return_value = b"abc123def456\n"
        import hooks.pre_commit as hook

        sha = hook.get_current_sha()
        assert sha == "abc123def456"

    @patch("subprocess.check_output")
    def test_falls_back_to_write_tree_on_initial_commit(self, mock_output: MagicMock) -> None:
        tree_hash = "d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3"
        mock_output.side_effect = [
            subprocess.CalledProcessError(128, "git rev-parse"),
            tree_hash.encode() + b"\n",
        ]
        import hooks.pre_commit as hook

        sha = hook.get_current_sha()
        assert sha == tree_hash


class TestGetCurrentBranch:
    @patch("subprocess.check_output")
    def test_returns_branch_name(self, mock_output: MagicMock) -> None:
        mock_output.return_value = b"feature/my-branch\n"
        import hooks.pre_commit as hook

        branch = hook.get_current_branch()
        assert branch == "feature/my-branch"

    @patch("subprocess.check_output", side_effect=Exception("not a git repo"))
    def test_fallback_on_error(self, mock_output: MagicMock) -> None:
        import hooks.pre_commit as hook

        branch = hook.get_current_branch()
        assert branch == "unknown"
