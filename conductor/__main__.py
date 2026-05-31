"""Entry point for ``python3 -m conductor``."""  # noqa: N999 — repo dir 'ci-wrapper' has a hyphen

from __future__ import annotations

import sys

from conductor.cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
