"""MCP Streamable HTTP application (tech design §5, §10).

Uses the official MCP Python SDK low-level Server: one small set of
task-oriented tools, JSON schemas derived from the compiled operation
catalog.  Business errors are returned through the SDK's tool-error
mechanism (``is_error`` payloads), never as synthetic HTTP statuses.

Resources expose low-frequency, non-sensitive information:
``drawbridge://environments``, ``drawbridge://apps``.
"""

from __future__ import annotations

import json
from typing import Any

from mcp import types
from mcp.server import Server

from drawbridge.errors import DrawbridgeError
from drawbridge.gateway.service import GatewayService

#: Fixed tool inventory; every tool follows the same validation and
#: execution path as ops_operation_run / the workflow engine (spec §7).
TOOL_NAMES = (
    "ops_catalog",
    "ops_status",
    "ops_logs",
    "ops_history",
    "ops_release_plan",
    "ops_release_apply",
    "ops_release_status",
    "ops_test",
    "ops_service_restart",
    "ops_release_rollback",
    "ops_operation_run",
)


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _str_schema(description: str, max_length: int | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "string", "description": description}
    if max_length:
        schema["maxLength"] = max_length
    return schema


def build_tool_definitions() -> list[types.Tool]:
    tools = [
        types.Tool(
            name="ops_catalog",
            description="List registered operations, workflows and apps with parameter schemas.",
            input_schema=_schema(
                {"app": _str_schema("optional app id", 64)}, []
            ),
        ),
        types.Tool(
            name="ops_status",
            description="Aggregated host metrics, current release and active job for one target.",
            input_schema=_schema(
                {
                    "app": _str_schema("app id", 64),
                    "environment": _str_schema("environment, e.g. staging", 64),
                },
                ["app", "environment"],
            ),
        ),
        types.Tool(
            name="ops_logs",
            description="Bounded, server-filtered service logs with cursor pagination.",
            input_schema=_schema(
                {
                    "app": _str_schema("app id", 64),
                    "environment": _str_schema("environment", 64),
                    "service": _str_schema("registered service name", 64),
                    "query": _str_schema("literal substring filter (no regex)", 128),
                    "cursor": _str_schema("opaque pagination cursor", 512),
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                    "tail": {"type": "integer", "minimum": 1, "maximum": 1000},
                    "since_seconds": {"type": "integer", "minimum": 1, "maximum": 86400},
                },
                ["app", "environment"],
            ),
        ),
        types.Tool(
            name="ops_history",
            description=(
                "Bounded target history: releases (with rollback eligibility), "
                "recent jobs, or audit events. Use before rollback to pick a "
                "release id."
            ),
            input_schema=_schema(
                {
                    "app": _str_schema("app id", 64),
                    "environment": _str_schema("environment, e.g. staging", 64),
                    "what": {
                        "type": "string",
                        "enum": ["releases", "jobs", "events"],
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                    "cursor": _str_schema("opaque pagination cursor", 64),
                },
                ["app", "environment"],
            ),
        ),
        types.Tool(
            name="ops_release_plan",
            description=(
                "Resolve a registered git ref to a frozen commit for one target "
                "and produce a plan."
            ),
            input_schema=_schema(
                {
                    "app": _str_schema("app id", 64),
                    "environment": _str_schema("environment", 64),
                    "source_mode": {"type": "string", "enum": ["fetch", "local"]},
                    "git_ref": _str_schema("refs/heads/..., refs/tags/... or 40-hex SHA", 200),
                    "workflow": _str_schema("registered workflow (deploy_verify)", 64),
                    "agent_id": _str_schema("optional tracing id", 128),
                    "parent_task_id": _str_schema("optional tracing id", 128),
                },
                ["app", "environment", "source_mode", "git_ref"],
            ),
        ),
        types.Tool(
            name="ops_release_apply",
            description="Execute a plan through the frozen workflow; returns an async job_id.",
            input_schema=_schema(
                {
                    "plan_id": _str_schema("plan id from ops_release_plan", 36),
                    "idempotency_key": _str_schema("unique key for this apply", 128),
                    "agent_id": _str_schema("optional tracing id", 128),
                    "parent_task_id": _str_schema("optional tracing id", 128),
                },
                ["plan_id", "idempotency_key"],
            ),
        ),
        types.Tool(
            name="ops_release_status",
            description="Query job or release status, steps, health results and recovery info.",
            input_schema=_schema(
                {
                    "job_id": _str_schema("job id", 36),
                    "release_id": _str_schema("release id", 36),
                },
                [],
            ),
        ),
        types.Tool(
            name="ops_test",
            description="Run a registered test suite against the current release.",
            input_schema=_schema(
                {
                    "release_id": _str_schema("current release id", 36),
                    "suite": _str_schema("registered suite name", 64),
                    "idempotency_key": _str_schema("unique key", 128),
                    "agent_id": _str_schema("optional tracing id", 128),
                    "parent_task_id": _str_schema("optional tracing id", 128),
                },
                ["release_id", "suite", "idempotency_key"],
            ),
        ),
        types.Tool(
            name="ops_service_restart",
            description="Restart a registered service and run its health gate.",
            input_schema=_schema(
                {
                    "app": _str_schema("app id", 64),
                    "environment": _str_schema("environment", 64),
                    "service": _str_schema("registered restartable service", 64),
                    "reason": _str_schema("audit reason, no line breaks", 256),
                    "idempotency_key": _str_schema("unique key", 128),
                    "agent_id": _str_schema("optional tracing id", 128),
                    "parent_task_id": _str_schema("optional tracing id", 128),
                },
                ["app", "environment", "service", "reason", "idempotency_key"],
            ),
        ),
        types.Tool(
            name="ops_release_rollback",
            description=(
                "Roll back to a historical successful release; creates a new "
                "release record."
            ),
            input_schema=_schema(
                {
                    "app": _str_schema("app id", 64),
                    "environment": _str_schema("environment", 64),
                    "release_id": _str_schema("historical release id", 36),
                    "reason": _str_schema("audit reason", 256),
                    "idempotency_key": _str_schema("unique key", 128),
                    "agent_id": _str_schema("optional tracing id", 128),
                    "parent_task_id": _str_schema("optional tracing id", 128),
                },
                ["app", "environment", "release_id", "reason", "idempotency_key"],
            ),
        ),
        types.Tool(
            name="ops_operation_run",
            description="Run one registered public operation with validated parameters.",
            input_schema=_schema(
                {
                    "operation": _str_schema("registered operation name", 64),
                    "app": _str_schema("app id", 64),
                    "environment": _str_schema("environment", 64),
                    "parameters": {"type": "object", "additionalProperties": True},
                    "idempotency_key": _str_schema("required for write operations", 128),
                    "agent_id": _str_schema("optional tracing id", 128),
                    "parent_task_id": _str_schema("optional tracing id", 128),
                },
                ["operation", "app", "environment"],
            ),
        ),
    ]
    assert [t.name for t in tools] == list(TOOL_NAMES)
    return tools


class MCPAppFactory:
    """Binds a GatewayService into the MCP protocol application."""

    def __init__(self, service: GatewayService, *, stateless: bool = True) -> None:
        self.service = service
        self.server: Server = Server(
            "drawbridge",
            version="0.1.0",
            title="Drawbridge",
            instructions=(
                "Drawbridge exposes audited deployment operations for registered "
                "apps only. Start with ops_catalog, plan before applying, and "
                "poll ops_release_status for async jobs."
            ),
            on_list_tools=self._list_tools,
            on_call_tool=self._call_tool,
        )
        self.stateless = stateless

    async def _list_tools(self, _ctx: Any, _params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(tools=build_tool_definitions())

    async def _call_tool(self, _ctx: Any, params: Any) -> types.CallToolResult:
        name = params.name
        arguments: dict[str, Any] = dict(params.arguments or {})
        try:
            result = await self._dispatch(name, arguments)
        except DrawbridgeError as exc:
            payload = exc.to_dict()
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text", text=json.dumps(payload, ensure_ascii=True)
                    )
                ],
                structured_content=payload,
                is_error=True,
            )
        return types.CallToolResult(
            content=[
                types.TextContent(
                    type="text",
                    text=json.dumps(result, ensure_ascii=True, default=str),
                )
            ],
            structured_content=result,
        )

    async def _dispatch(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        svc = self.service
        if name == "ops_catalog":
            return await svc.ops_catalog(app_id=args.get("app"))
        if name == "ops_status":
            return await svc.ops_status(args["app"], args["environment"])
        if name == "ops_logs":
            return await svc.ops_logs(
                app=args["app"],
                environment=args["environment"],
                service=args.get("service"),
                query=args.get("query", ""),
                cursor=args.get("cursor"),
                # Raw values: strict validation in the service layer rejects
                # bools/strings — no coercion at the protocol boundary.
                limit=args.get("limit", 100),
                tail=args.get("tail", 200),
                since_seconds=args.get("since_seconds", 300),
            )
        if name == "ops_history":
            return await svc.ops_history(
                app=args["app"],
                environment=args["environment"],
                what=args.get("what", "releases"),
                limit=args.get("limit", 50),
                cursor=args.get("cursor"),
            )
        if name == "ops_release_plan":
            return await svc.ops_release_plan(
                app=args["app"],
                environment=args["environment"],
                source_mode=args["source_mode"],
                git_ref=args["git_ref"],
                workflow=args.get("workflow", "deploy_verify"),
                agent_id=args.get("agent_id"),
                parent_task_id=args.get("parent_task_id"),
            )
        if name == "ops_release_apply":
            return await svc.ops_release_apply(
                plan_id=args["plan_id"],
                idempotency_key=args["idempotency_key"],
                agent_id=args.get("agent_id"),
                parent_task_id=args.get("parent_task_id"),
            )
        if name == "ops_release_status":
            return await svc.ops_release_status(
                job_id=args.get("job_id"), release_id=args.get("release_id")
            )
        if name == "ops_test":
            return await svc.ops_test(
                release_id=args["release_id"],
                suite=args["suite"],
                idempotency_key=args["idempotency_key"],
                agent_id=args.get("agent_id"),
                parent_task_id=args.get("parent_task_id"),
            )
        if name == "ops_service_restart":
            return await svc.ops_service_restart(
                app=args["app"],
                environment=args["environment"],
                service=args["service"],
                reason=args["reason"],
                idempotency_key=args["idempotency_key"],
                agent_id=args.get("agent_id"),
                parent_task_id=args.get("parent_task_id"),
            )
        if name == "ops_release_rollback":
            return await svc.ops_release_rollback(
                app=args["app"],
                environment=args["environment"],
                release_id=args["release_id"],
                reason=args["reason"],
                idempotency_key=args["idempotency_key"],
                agent_id=args.get("agent_id"),
                parent_task_id=args.get("parent_task_id"),
            )
        if name == "ops_operation_run":
            return await svc.ops_operation_run(
                operation=args["operation"],
                app=args["app"],
                environment=args["environment"],
                parameters=args.get("parameters"),
                idempotency_key=args.get("idempotency_key"),
                agent_id=args.get("agent_id"),
                parent_task_id=args.get("parent_task_id"),
            )
        raise DrawbridgeError(f"unknown tool {name!r}", code="UNKNOWN_OPERATION")

    def build_asgi_app(self) -> Any:
        """Streamable HTTP ASGI app mounted under the configured base path."""
        return self.server.streamable_http_app(
            json_response=True,
            stateless_http=True,
        )
