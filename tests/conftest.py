"""Lets `pytest` run without installing the package first."""
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
for p in (root / "src", root / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
