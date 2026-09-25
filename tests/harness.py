"""Shared pass/fail reporting for the AEGIS test suites.

Windows consoles default to cp1252, which cannot encode the characters that
turn up routinely in test output - the check marks the markdown exporter uses,
em dashes, ellipses. Printing one raises UnicodeEncodeError and takes the whole
run down mid-suite, which reads like a hang rather than a bug. So: ask for UTF-8
first, and if the console refuses, replace what it cannot render rather than
dying over a tick.
"""

from __future__ import annotations

import sys

PASS: list[str] = []
FAIL: list[str] = []


def _use_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


_use_utf8()


def _safe(text: str) -> str:
    """Last resort if reconfigure did not take: drop what the console cannot show."""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(encoding, errors="strict")
        return text
    except (UnicodeEncodeError, LookupError):
        return text.encode(encoding, errors="replace").decode(encoding, "replace")


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    mark = "  ok  " if condition else " FAIL "
    line = f"[{mark}] {name}{(' - ' + detail) if detail else ''}"
    print(_safe(line))


def section(title: str) -> None:
    print(_safe(f"\n--- {title} ---"))


def report() -> int:
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("Failures:")
        for name in FAIL:
            print(_safe(f"  - {name}"))
    return 1 if FAIL else 0
