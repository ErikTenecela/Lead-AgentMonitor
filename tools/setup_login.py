"""
setup_login.py
One-time setup: opens browser windows for you to manually log in
to Facebook and Nextdoor, then saves the sessions for future headless use.

Usage:
    python setup_login.py facebook
    python setup_login.py nextdoor
    python setup_login.py all
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from scrape_facebook import login as fb_login, SESSION_PATH as FB_SESSION
from scrape_nextdoor import login as nd_login, SESSION_PATH as ND_SESSION


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "all"

    if target in ("facebook", "all"):
        print("=== Facebook Login ===")
        fb_login(FB_SESSION)
        print()

    if target in ("nextdoor", "all"):
        print("=== Nextdoor Login ===")
        nd_login(ND_SESSION)
        print()

    print("Done. Sessions saved. You can now run: python orchestrator.py")
