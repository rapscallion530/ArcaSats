# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rapscallion
"""Release-hygiene gate for the public repo.

Fails (exit 1) if any of these are true, so it can run in CI before a tag/publish:

  1. A file that must NEVER be tracked is in the git index — real financial data
     (`data/`, `*.sqlite`), secrets (`secret.key`, `.env`), caches (`__pycache__`,
     `.pytest_cache`), real fixtures (`fixtures/real/`), or local-only launchers.
  2. A primary doc's "N tests" claim disagrees with the actual collected test count
     (the exact drift the 2026-06 audit caught: README 120 / code-review 108 / status 83).
  3. A vendored frontend asset that local mode (BTT_ASSETS=local, the default) depends on
     is missing or empty — i.e. an ordinary launch would have to phone a CDN.

Usage:  python scripts/release_check.py
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Docs that describe the CURRENT state and must track the live test count. Historical files
# (RELEASE_NOTES_*, CHANGELOG.md) are intentionally excluded — they pin a past release's number.
LIVING_DOCS = ["README.md", "docs/code-review.md", "docs/requirements-and-status.md"]

# Vendored assets a default (local) launch loads instead of a CDN.
VENDORED_ASSETS = ["app/static/tailwind.css", "app/static/vendor/htmx.min.js"]


def _tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True)
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def _is_forbidden(path: str) -> str | None:
    """Return a human reason if `path` must not be tracked, else None."""
    p = path.replace("\\", "/")
    name = p.rsplit("/", 1)[-1]
    if p == "data" or p.startswith("data/"):
        return "real financial data dir"
    if name == "secret.key" or name.startswith("secrets."):
        return "secret material"
    if p.endswith((".sqlite", ".sqlite3", ".db")):
        return "SQLite database"
    if "__pycache__/" in p + "/" or p.endswith((".pyc", ".pyo")):
        return "Python bytecode cache"
    if ".pytest_cache/" in p + "/":
        return "pytest cache"
    if name == ".env" or p.endswith(".env"):
        return "environment/secrets file"
    if "fixtures/real/" in p:
        return "real (non-synthetic) fixture"
    if name in ("open-claude-here.bat",) or name.endswith(".local.ps1"):
        return "local-only launcher/tooling"
    return None


def check_no_forbidden_tracked() -> list[str]:
    problems = []
    for path in _tracked_files():
        reason = _is_forbidden(path)
        if reason:
            problems.append(f"tracked file should not be committed ({reason}): {path}")
    return problems


def collected_test_count() -> int:
    """Count `def test_*` across tests/ — matches what pytest collects, without importing it."""
    pat = re.compile(r"^\s*def\s+test_\w*\s*\(", re.MULTILINE)
    total = 0
    for f in (ROOT / "tests").glob("*.py"):
        total += len(pat.findall(f.read_text(encoding="utf-8")))
    return total


def check_doc_test_counts(expected: int) -> list[str]:
    """Every "<N> tests" / "<N>-test" claim in a living doc must equal the collected count."""
    # Leading \b so a digit glued mid-word ("BIP84 test vectors", "BIP78") isn't read as a count.
    claim = re.compile(r"\b(\d+)[\s-]+tests?\b", re.IGNORECASE)
    problems = []
    for rel in LIVING_DOCS:
        f = ROOT / rel
        if not f.exists():
            continue
        for m in claim.finditer(f.read_text(encoding="utf-8")):
            if int(m.group(1)) != expected:
                problems.append(
                    f"{rel}: claims '{m.group(0).strip()}' but {expected} tests are collected")
    return problems


def check_vendored_assets() -> list[str]:
    problems = []
    for rel in VENDORED_ASSETS:
        f = ROOT / rel
        if not f.exists():
            problems.append(f"vendored asset missing (local mode would need a CDN): {rel}")
        elif f.stat().st_size == 0:
            problems.append(f"vendored asset is empty: {rel}")
    return problems


def main() -> int:
    count = collected_test_count()
    checks = [
        ("No forbidden files tracked", check_no_forbidden_tracked()),
        (f"Doc test counts match collected ({count})", check_doc_test_counts(count)),
        ("Vendored assets present", check_vendored_assets()),
    ]
    failed = False
    for title, problems in checks:
        if problems:
            failed = True
            print(f"FAIL  {title}")
            for p in problems:
                print(f"      - {p}")
        else:
            print(f"ok    {title}")
    if failed:
        print("\nRelease check FAILED — fix the above before publishing.")
        return 1
    print("\nRelease check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
