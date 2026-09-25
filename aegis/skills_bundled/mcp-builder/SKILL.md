---
name: mcp-builder
description: >
  Use when the user wants to connect a new service or tool to AEGIS — "add an MCP server", "connect
  GitHub/Gmail/Drive", "make a tool for…", "build an MCP server", "create an agent that…". Covers
  installing from the catalogue, writing a custom MCP server in Python, and creating agents.
---

# MCP & agent builder

## 1. Try the catalogue first
`mcp_admin__catalog` lists ready-made servers (filesystem, fetch, git, memory graph, sequential
thinking, time, Playwright, GitHub, Google Workspace, Brave Search, SQLite). Install with
`mcp_admin__install(key, values)` — ask the user for any token/folder the entry needs. If Node or uv is
missing, give the one install command and stop.

## 2. Write a custom MCP server (Python, FastMCP)
When no server exists for the job, build one in the project workspace:
1. `code__run`: `pip install "mcp[cli]"`
2. `files__write` `servers/<name>_server.py`:
```python
from mcp.server.fastmcp import FastMCP
mcp = FastMCP("<name>")

@mcp.tool()
def get_something(query: str) -> str:
    """One-line description the model will read."""
    return "result"

if __name__ == "__main__":
    mcp.run()          # stdio
```
3. Test: `code__python` importing the module and calling the function directly.
4. Register: `mcp_admin__register(name, command="python", args=["<full path to server.py>"])`,
   then `mcp_admin__connect(name)`. Tools appear as `<name>__<tool>`.
Rules: tool names start with a verb (get/list/search = read-only, create/send/delete = asks);
never hard-code secrets — read them from environment variables passed in `env`.

## 3. Create an agent
`agent__create(name, description, instructions, skills, tools, boost)` saves a reusable agent.
Write instructions as a numbered way of working, pick only the tool families it needs, and
suggest a local route for anything involving private family or health details.
