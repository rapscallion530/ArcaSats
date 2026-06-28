# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rapscallion
"""Update (or first-time install) the bundled Tor for ArcaSats — CVE hygiene for headless installs.

    python scripts/update_tor.py            # install/update to the latest verified Tor
    python scripts/update_tor.py --check    # just report installed vs latest

Downloads the official Tor Expert Bundle over HTTPS and verifies its sha256 (see
app/services/tor_service.py). The desktop app also checks for updates on launch and exposes a
button in Settings; this CLI is for servers/StartOS or scripted updates.
"""
import sys

from app.services import tor_service


def main() -> int:
    if "--check" in sys.argv:
        info = tor_service.check_update()
        print(f"installed: {info['installed']}\nlatest:    {info['latest']}\n"
              f"update available: {info['update_available']}")
        return 0
    print(f"installed: {tor_service.installed_version()}; fetching latest…")
    result = tor_service.update()
    if not result.get("ok"):
        print(f"FAILED: {result.get('error')}")
        return 1
    if result.get("updated"):
        print(f"updated to v{result['version']}")
    else:
        print("already up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
