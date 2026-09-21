"""Wispbyte-compatible launcher for NERP - Case Management.

Wispbyte's Python server selector runs one file from the repository root.  The
application itself remains in ``src/nerp_forms_bot`` so it can still be used as
an installable Python package on other hosts.
"""

from __future__ import annotations

import sys
from importlib import import_module
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parent / "src"
sys.path.insert(0, str(SOURCE_ROOT))

run = import_module("nerp_forms_bot.main").run


if __name__ == "__main__":
    run()
