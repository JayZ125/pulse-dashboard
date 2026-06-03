#!/usr/bin/env python3
"""scan_web.py — Thin wrapper that runs scan.main() in web mode for GitHub Pages.

Sets PULSE_WEB=1 so scan.main() writes index.html instead of local paths.
In CI (GitHub Actions), scan.py is in the same directory.
Locally, falls back to ~/.claude/skills/us-tech-pulse/scan.py.
"""
import os
import sys


def find_scan_py():
    """Locate scan.py — either local (this repo) or in the user's skill dir."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "scan.py"),
        os.path.expanduser("~/.claude/skills/us-tech-pulse/scan.py"),
    ]
    for path in candidates:
        if os.path.exists(path):
            return os.path.dirname(path)
    return None


def main():
    scan_dir = find_scan_py()
    if not scan_dir:
        print("ERROR: scan.py not found in repo or ~/.claude/skills/us-tech-pulse/",
              file=sys.stderr)
        sys.exit(1)

    # Add the scan.py directory to path
    sys.path.insert(0, scan_dir)

    # Force web mode
    os.environ["PULSE_WEB"] = "1"

    # Import and run
    import scan
    sys.exit(scan.main())


if __name__ == "__main__":
    main()
