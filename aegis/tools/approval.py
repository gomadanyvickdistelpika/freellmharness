"""The approval gate.

Policy (set in Settings, default `auto_read_ask_write`):

    auto_read_ask_write   reads run immediately, writes ask
    ask_always            everything asks
    auto_all              nothing asks
    per_server            each server carries its own policy

A decision can be remembered per tool ("always allow this one"), which is what
makes an agent loop bearable without handing over blanket permission.

Remembered decisions live in settings, so they survive a restart and can be
inspected and revoked in the UI. Nothing here can be set by a model - the
policy is read, never written, by the agent loop.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from ..config import settings
from .base import READ, WRITE

POLICIES = ("auto_read_ask_write", "ask_always", "auto_all", "per_server")
APPROVAL_TIMEOUT = 300.0     # five minutes, then treated as denied


@dataclass
class Pending:
    id: str
    tool: str
    server: str
    risk: str
    arguments: dict[str, Any]
    created: float = field(default_factory=time.time)
    future: asyncio.Future = field(default_factory=asyncio.Future, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "tool": self.tool, "server": self.server,
                "risk": self.risk, "arguments": self.arguments,
                "age": round(time.time() - self.created, 1)}


class ApprovalGate:
    def __init__(self) -> None:
        self.pending: dict[str, Pending] = {}

    # -- policy -----------------------------------------------------------

    @staticmethod
    def policy(server: str = "") -> str:
        base = settings.get("tool_policy", "auto_read_ask_write")
        if base == "per_server" and server:
            per = settings.get("server_policies") or {}
            return per.get(server, "auto_read_ask_write")
        return base if base in POLICIES else "auto_read_ask_write"

    @staticmethod
    def remembered(tool: str) -> str | None:
        return (settings.get("tool_decisions") or {}).get(tool)

    @staticmethod
    def remember(tool: str, decision: str) -> None:
        decisions = dict(settings.get("tool_decisions") or {})
        if decision in ("allow", "deny"):
            decisions[tool] = decision
        else:
            decisions.pop(tool, None)
        settings.set("tool_decisions", decisions)

    @staticmethod
    def forget_all() -> None:
        settings.set("tool_decisions", {})

    # -- decisions --------------------------------------------------------

    def decide_without_asking(self, tool: str, server: str, risk: str) -> bool | None:
        """True allow, False deny, None means we must ask."""
        remembered = self.remembered(tool)
        if remembered == "allow":
            return True
        if remembered == "deny":
            return False

        policy = self.policy(server)
        if policy == "auto_all":
            return True
        if policy == "ask_always":
            return None
        # auto_read_ask_write, and per-server resolving to it
        return True if risk == READ else None

    async def request(self, tool: str, server: str, risk: str,
                      arguments: dict[str, Any]) -> Pending:
        item = Pending(id=uuid.uuid4().hex[:10], tool=tool, server=server,
                       risk=risk, arguments=arguments)
        self.pending[item.id] = item
        return item

    async def wait(self, item: Pending) -> tuple[bool, str]:
        try:
            return await asyncio.wait_for(item.future, APPROVAL_TIMEOUT)
        except asyncio.TimeoutError:
            return False, "No answer within five minutes, so it was not run."
        except asyncio.CancelledError:
            return False, "Cancelled."
        finally:
            self.pending.pop(item.id, None)

    def resolve(self, approval_id: str, approved: bool,
                remember: bool = False) -> bool:
        item = self.pending.get(approval_id)
        if not item or item.future.done():
            return False
        if remember:
            self.remember(item.tool, "allow" if approved else "deny")
        item.future.set_result(
            (approved, "Approved." if approved else "You declined this call."))
        return True

    def cancel_all(self) -> None:
        for item in list(self.pending.values()):
            if not item.future.done():
                item.future.set_result((False, "Run stopped."))
        self.pending.clear()

    def list(self) -> list[dict[str, Any]]:
        return [p.to_dict() for p in self.pending.values()]


gate = ApprovalGate()
