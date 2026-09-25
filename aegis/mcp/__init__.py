"""Model Context Protocol support: discovery, client, registry."""

from .discovery import scan, scan_report
from .protocol import MCPClient, MCPError, MCPTool, ToolCallResult, build_client
from .registry import registry

__all__ = ["scan", "scan_report", "MCPClient", "MCPError", "MCPTool",
           "ToolCallResult", "build_client", "registry"]
