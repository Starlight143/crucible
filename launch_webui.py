#!/usr/bin/env python3
"""
Crucible WebUI Launcher
- Auto-installs Flask if missing
- Finds a free localhost port (8080-9000)
- Adds crucible.com -> 127.0.0.1 to hosts file (if admin)
- Starts Flask server and opens browser
"""
from __future__ import annotations

import os
import sys
import socket
import subprocess
import threading
import time
import webbrowser
from pathlib import Path

DOMAIN = "crucible.com"
HOSTS_FILE = r"C:\Windows\System32\drivers\etc\hosts"
PORT_START = 8080
PORT_END = 9000
PROJECT_ROOT = Path(__file__).parent.resolve()


# ---------------------------------------------------------------------------
# Dependency bootstrap
# ---------------------------------------------------------------------------

def ensure_flask() -> None:
    try:
        import flask  # noqa: F401
        return
    except ImportError:
        pass
    print("[INFO] Flask not found. Installing...")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "flask", "--quiet"],
            capture_output=True, text=True, timeout=180,
        )
    except subprocess.TimeoutExpired:
        # Without a timeout, a hanging pip call (corporate proxy / dead PyPI
        # mirror / network drop) leaves the launcher stuck with no diagnostic.
        # 180 s is generous for a cold install; fail loud past that.
        print("[ERROR] pip install flask timed out after 180s.")
        print("        Check network / proxy / PyPI mirror, then run:")
        print(f"        {sys.executable} -m pip install flask")
        sys.exit(1)
    if result.returncode != 0:
        print("[ERROR] Failed to install Flask:")
        print(result.stderr)
        sys.exit(1)
    print("[OK] Flask installed.")


# ---------------------------------------------------------------------------
# Port detection
# ---------------------------------------------------------------------------

def find_free_port() -> int:
    # Inclusive range — range(PORT_START, PORT_END) would exclude the upper
    # bound and fail with "no free port" when 8080..8999 are taken but 9000
    # itself is available.  range(..., PORT_END + 1) restores the docstring's
    # promised "8080-9000" contract.
    for port in range(PORT_START, PORT_END + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No free port found in range {PORT_START}-{PORT_END}")


# ---------------------------------------------------------------------------
# Hosts file
# ---------------------------------------------------------------------------

def ensure_hosts_entry() -> bool:
    """
    Appends '127.0.0.1 crucible.com' to hosts if not already present.
    Returns True if domain is mapped (already existed or just added).
    Returns False silently on permission error — caller falls back to localhost.
    """
    try:
        content = Path(HOSTS_FILE).read_text(encoding="utf-8", errors="replace")
        if DOMAIN in content:
            return True
        with open(HOSTS_FILE, "a", encoding="utf-8") as fh:
            fh.write(f"\n# Crucible WebUI\n127.0.0.1 {DOMAIN}\n")
        print(f"[OK] Hosts entry added: 127.0.0.1 {DOMAIN}")
        return True
    except PermissionError:
        # No admin rights — silently fall back, no UAC prompt needed
        return False
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Browser opener
# ---------------------------------------------------------------------------

def open_browser(url: str, delay: float = 2.5) -> None:
    def _open() -> None:
        time.sleep(delay)
        webbrowser.open(url)
    threading.Thread(target=_open, daemon=True).start()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    # 1. Ensure dependencies
    ensure_flask()

    # 2. Find port
    port = find_free_port()
    print(f"[OK] Port: {port}")

    # 3. Hosts file
    hosts_ok = ensure_hosts_entry()
    url = f"http://{DOMAIN}:{port}" if hosts_ok else f"http://localhost:{port}"

    print(f"\n  Crucible WebUI")
    print(f"  URL  : {url}")
    print(f"  Root : {PROJECT_ROOT}")
    print()

    # 4. Propagate config to Flask app
    os.environ["WEBUI_PORT"] = str(port)
    os.environ["WEBUI_URL"] = url
    os.environ["PROJECT_ROOT"] = str(PROJECT_ROOT)

    # 5. Open browser after delay
    open_browser(url)

    # 6. Import and start Flask
    sys.path.insert(0, str(PROJECT_ROOT))
    try:
        from webui.app import app  # noqa: PLC0415
    except ImportError as exc:
        print(f"[ERROR] Cannot import webui.app: {exc}")
        print("  Make sure the 'webui/' folder exists next to this file.")
        sys.exit(1)

    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
