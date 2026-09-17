"""Drawbridge — MCP bridge for server operations and deployment.

Drawbridge connects AI coding agents (Codex, Claude Code, ...) to a target
server through a strict, task-oriented MCP interface.  It never exposes
arbitrary shell, Docker or Git commands: every executable action is a
registered operation or a frozen workflow template, validated by compiled
configuration and executed only by the Runner principal.
"""

__version__ = "0.1.0"
