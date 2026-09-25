"""Entry point: start the local API, then open the desktop window.

The window is Edge WebView2 on Windows (already present on Windows 10/11), via
pywebview. If pywebview or WebView2 is missing, AEGIS falls back to opening
your default browser rather than refusing to start.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import socket
import sys
import threading
import time

from .config import (APP_NAME, DEFAULT_PORT, HOST, LOG_DIR, VERSION,
                     ensure_dirs, settings)


def bind_streams() -> None:
    """Give the process real stdout/stderr before anything else runs.

    Launched from the desktop shortcut, AEGIS runs under pythonw.exe, which
    gives the process no console: sys.stdout and sys.stderr are None. Any
    library that writes to them then raises AttributeError - uvicorn's logging
    handlers above all, inside a worker thread - and the app disappears with no
    window and no message. From the outside the icon simply does nothing.

    So: point them at a file first. Nothing else in the app can be trusted to
    run until this has happened.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        ensure_dirs()
        stream = open(LOG_DIR / "console.log", "a", encoding="utf-8",
                      buffering=1, errors="replace")
    except OSError:
        stream = io.StringIO()          # swallow it, but never leave None
    for name in ("stdout", "stderr", "__stdout__", "__stderr__"):
        if getattr(sys, name, None) is None:
            setattr(sys, name, stream)


def use_utf8_console() -> None:
    """Windows consoles are cp1252 and raise on anything outside it.

    A log line containing an em dash or a check mark would otherwise take the
    whole process down with UnicodeEncodeError - which looks like a crash, not
    like a punctuation problem.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def log_startup(message: str) -> None:
    """Write to a file as well as stdout.

    Launched from the desktop shortcut AEGIS runs under pythonw.exe, which has
    no console at all - without this, a startup failure would be completely
    silent and the icon would just appear to do nothing.
    """
    print(f"[{APP_NAME}] {message}")
    try:
        ensure_dirs()
        with open(LOG_DIR / "startup.log", "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {message}\n")
    except OSError:
        pass


def use_proactor_loop() -> None:
    """MCP stdio servers are subprocesses, and on Windows only the Proactor
    loop can spawn those. Set it before uvicorn builds its loop, or every
    stdio MCP server fails with NotImplementedError."""
    if sys.platform == "win32":
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        except AttributeError:
            pass


def port_is_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind((HOST, port))
            return True
        except OSError:
            return False


def pick_port(preferred: int) -> int:
    if port_is_free(preferred):
        return preferred
    for offset in range(1, 20):
        if port_is_free(preferred + offset):
            return preferred + offset
    with socket.socket() as s:
        s.bind((HOST, 0))
        return int(s.getsockname()[1])


def serve(port: int) -> threading.Thread:
    import uvicorn
    from .server import app

    config = uvicorn.Config(app, host=HOST, port=port, log_level="warning",
                            access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True, name="aegis-api")
    thread.start()

    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            with socket.create_connection((HOST, port), timeout=0.4):
                return thread
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("The AEGIS API did not start within 20 seconds.")


def open_window(url: str) -> bool:
    try:
        import webview  # type: ignore[import-not-found]
    except ImportError:
        return False
    try:
        webview.create_window(f"{APP_NAME}", url, width=1280, height=860,
                              min_size=(960, 640))
        webview.start()
        return True
    except Exception as exc:  # WebView2 runtime missing, headless session, etc.
        log_startup(f"Desktop window unavailable ({exc}); falling back to browser.")
        return False


def run_scheduled_task(key: str) -> int:
    """Headless single-task run, for Windows Task Scheduler.

    No server, no window: bring up only what a run needs, do it, exit. Whatever
    happens goes into the startup log, because nobody is watching a console
    that Task Scheduler never shows.
    """
    ensure_dirs()
    use_proactor_loop()

    async def go() -> dict:
        from . import files, memory, scheduler, store
        from .mcp.registry import registry
        from .tools.approval import gate

        store.connect()
        memory.connect()
        files.connect()
        await registry.connect_enabled()
        try:
            return await scheduler.run_once(key, reason="windows-scheduler")
        finally:
            gate.cancel_all()
            await registry.shutdown()
            store.close()
            memory.close()
            files.close()

    log_startup(f"scheduled task '{key}' starting")
    try:
        result = asyncio.run(go())
    except Exception as exc:
        import traceback
        log_startup(f"scheduled task '{key}' FAILED:\n{traceback.format_exc()}")
        print(f"[{APP_NAME}] task {key} failed: {exc}")
        return 1

    if not result.get("ok"):
        log_startup(f"scheduled task '{key}' did not run: {result.get('error')}")
        print(f"[{APP_NAME}] {result.get('error')}")
        return 1

    log_startup(f"scheduled task '{key}' finished in {result['seconds']}s "
                f"-> {result.get('file') or 'no file'}")
    print(f"[{APP_NAME}] {key}: {result['status']} in {result['seconds']}s")
    return 0


def main(argv: list[str] | None = None) -> int:
    bind_streams()          # must be first: see the docstring above
    use_utf8_console()
    try:
        return _main(argv)
    except Exception:
        import traceback
        log_startup("FAILED TO START:\n" + traceback.format_exc())
        raise


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aegis", description=f"{APP_NAME} {VERSION}")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-window", action="store_true",
                        help="Run the API only; open the UI in a browser yourself.")
    parser.add_argument("--browser", action="store_true",
                        help="Use the default browser instead of a desktop window.")
    parser.add_argument("--run-task", metavar="KEY", default="",
                        help="Run one scheduled task headless and exit. This is "
                             "what Windows Task Scheduler calls.")
    args = parser.parse_args(argv)

    if args.run_task:
        return run_scheduled_task(args.run_task)

    ensure_dirs()
    use_proactor_loop()
    port = pick_port(args.port)
    if port != args.port:
        log_startup(f"Port {args.port} was busy, using {port}. "
                    f"OAuth redirect URIs must match this port.")
    settings.set("port", port)

    url = f"http://{HOST}:{port}/"
    serve(port)
    log_startup(f"{VERSION} running at {url}")

    if args.no_window:
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return 0

    if args.browser or not open_window(url):
        import webbrowser
        webbrowser.open(url)
        log_startup("Opened in the default browser.")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
