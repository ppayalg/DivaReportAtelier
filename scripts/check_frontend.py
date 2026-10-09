#!/usr/bin/env python3
"""Syntax-check the dashboard's inline JavaScript (needs Node.js on PATH)."""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

html = (Path(__file__).resolve().parents[1] / "frontend" / "index.html").read_text(encoding="utf-8")
scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
if not scripts:
    sys.exit("no inline <script> found")
if not shutil.which("node"):
    sys.exit("node not found; skipping frontend syntax check")
with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
    fh.write("\n".join(scripts))
sys.exit(subprocess.call(["node", "--check", fh.name]))
