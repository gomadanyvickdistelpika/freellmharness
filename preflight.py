"""Pre-flight check: does AEGIS actually start on this machine?

Run it with python.exe (a visible console) when the desktop icon appears to do
nothing. It starts the real server, calls it, and prints a plain verdict - then
tells you exactly which piece failed rather than leaving you guessing.

    .venv\\Scripts\\python.exe preflight.py
"""

from __future__ import annotations

import socket
import sys
import threading
import time
import traceback
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Windows consoles are cp1252 by default and raise on anything outside it.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

LINE = "=" * 62
problems: list[str] = []


def say(label: str, value: str = "", ok: bool | None = None) -> None:
    mark = "     " if ok is None else ("  ok " if ok else " FAIL")
    print(f"[{mark}] {label}{(': ' + value) if value else ''}")


def main() -> int:
    print(LINE)
    print("  AEGIS pre-flight")
    print(LINE)

    say("Python", sys.version.split()[0])
    say("Executable", sys.executable)
    say("Console attached", "no (pythonw)" if sys.stdout is None else "yes")

    # 1. Imports -----------------------------------------------------------
    try:
        from aegis import VERSION
        from aegis.main import bind_streams, pick_port, serve
        bind_streams()
        say("AEGIS imports", VERSION, True)
    except Exception:
        say("AEGIS imports", "", False)
        print(traceback.format_exc())
        problems.append("The aegis package could not be imported. Dependencies "
                        "are probably missing - re-run AEGIS-SETUP.bat.")
        return report()

    # 2. Dependencies ------------------------------------------------------
    for name, why in (("fastapi", "the API"), ("uvicorn", "the web server"),
                      ("httpx", "talking to models"), ("psutil", "hardware probe"),
                      ("cryptography", "the key vault")):
        try:
            __import__(name)
            say(f"{name}", why, True)
        except Exception as exc:
            say(f"{name}", str(exc), False)
            problems.append(f"{name} is missing - re-run AEGIS-SETUP.bat.")

    # 3. Optional extras ----------------------------------------------------
    try:
        import webview            # noqa: F401
        say("pywebview", "desktop window available", True)
    except Exception as exc:
        say("pywebview", f"{exc} - will fall back to your browser", None)

    try:
        import playwright         # noqa: F401
        say("playwright", "browser use available", True)
    except Exception:
        say("playwright", "not installed - browser tools will be hidden", None)

    try:
        import pyautogui          # noqa: F401
        say("pyautogui", "computer use available", True)
    except Exception:
        say("pyautogui", "not installed - computer-use tools will be hidden", None)

    # 4. The actual server --------------------------------------------------
    print("-" * 62)
    try:
        port = pick_port(8817)
        say("Port chosen", str(port))
        serve(port)
        say("Server thread started", "", True)
    except Exception:
        say("Server thread started", "", False)
        print(traceback.format_exc())
        problems.append("The web server would not start. The traceback above "
                        "says why.")
        return report()

    url = f"http://127.0.0.1:{port}/api/settings"
    for attempt in range(12):
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                if response.status == 200:
                    say("API responds", url, True)
                    break
        except Exception as exc:
            if attempt == 11:
                say("API responds", str(exc), False)
                problems.append("The server started but never answered. "
                                "Something may be blocking 127.0.0.1 - check "
                                "your firewall or antivirus.")
            time.sleep(1)

    try:
        from aegis import skills
        from aegis.hardware import probe
        hw = probe().to_dict()
        say("Hardware probe",
            f"{hw['ram_total_gb']} GB RAM, {hw['accelerator']}", True)
        say("Skills loaded", str(len(skills.loadable())), True)
    except Exception as exc:
        say("Hardware / skills", str(exc), False)

    return report()


def report() -> int:
    print(LINE)
    if problems:
        print("  RESULT: AEGIS cannot start. Reasons:\n")
        for item in problems:
            print(f"    - {item}")
    else:
        print("  RESULT: AEGIS starts correctly on this machine.")
        print("  If the desktop icon still does nothing, the log is at")
        print("    %LOCALAPPDATA%\\Aegis\\logs\\startup.log")
    print(LINE)
    return 1 if problems else 0


if __name__ == "__main__":
    code = main()
    print()
    input("Press Enter to close...")
    sys.exit(code)
