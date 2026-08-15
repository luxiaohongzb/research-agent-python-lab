from __future__ import annotations

import asyncio
import importlib
import json
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, TypeAdapter, ValidationError

from research_agent.domain import Paper, SearchTask
from research_agent.providers import normalize_doi, stable_paper_id


class McpConfigurationError(ValueError):
    pass


class McpToolError(RuntimeError):
    pass


JsonScalar = str | int | float | bool | None


class McpPaperServerConfig(BaseModel):
    """Explicit mapping from one MCP search tool to the Paper domain contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_.-]+$")
    url: HttpUrl
    search_tool: str = Field(min_length=1, max_length=200)
    bearer_token_env: str | None = Field(
        default=None,
        pattern=r"^[A-Z_][A-Z0-9_]*$",
    )
    query_argument: str = Field(default="query", min_length=1, max_length=100)
    limit_argument: str = Field(default="limit", min_length=1, max_length=100)
    year_from_argument: str | None = Field(default=None, min_length=1, max_length=100)
    year_to_argument: str | None = Field(default=None, min_length=1, max_length=100)
    static_arguments: dict[str, JsonScalar] = Field(default_factory=dict)
    max_results: int = Field(default=50, ge=1, le=100)
    trust_env: bool = False


class McpServerStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    url: str
    connected: bool
    protocol_version: str | None = None
    server_name: str | None = None
    tools: tuple[str, ...] = ()
    configured_tool_available: bool = False
    error_type: str | None = None


class McpToolGateway(Protocol):
    async def call_tool(
        self, tool_name: str, arguments: Mapping[str, JsonScalar]
    ) -> dict[str, Any] | list[Any]: ...


class OfficialMcpToolGateway:
    """Stateless MCP 2.x client using Streamable HTTP and structured tool results."""

    def __init__(
        self,
        config: McpPaperServerConfig,
        *,
        timeout_seconds: float = 45.0,
        target: Any = None,
    ) -> None:
        self._config = config
        self._timeout_seconds = timeout_seconds
        self._target = target
        self._verified = False
        self._verification_lock = asyncio.Lock()

    async def call_tool(
        self, tool_name: str, arguments: Mapping[str, JsonScalar]
    ) -> dict[str, Any] | list[Any]:
        if tool_name != self._config.search_tool:
            raise McpToolError(f"MCP tool is not allow-listed: {tool_name}")
        validation_error: McpToolError | McpConfigurationError | None = None
        result: Any = None
        async with asyncio.timeout(self._timeout_seconds):
            async with self._connect() as client:
                validation_error = await self._verify_tool(client, tool_name)
                if validation_error is None:
                    result = await client.call_tool(tool_name, dict(arguments))
        if validation_error is not None:
            raise validation_error
        if result is None:
            raise McpToolError("MCP tool returned no result")
        if bool(result.is_error):
            raise McpToolError(_safe_tool_error(result.content))
        return _structured_result(result)

    async def probe(self) -> McpServerStatus:
        try:
            async with asyncio.timeout(self._timeout_seconds):
                async with self._connect() as client:
                    listed = await client.list_tools()
                    tools = tuple(sorted(tool.name for tool in listed.tools))
                    server_info = client.server_info
                    return McpServerStatus(
                        name=self._config.name,
                        url=str(self._config.url),
                        connected=True,
                        protocol_version=str(client.protocol_version),
                        server_name=(str(server_info.name) if server_info else None),
                        tools=tools,
                        configured_tool_available=self._config.search_tool in tools,
                    )
        except Exception as exc:
            return McpServerStatus(
                name=self._config.name,
                url=str(self._config.url),
                connected=False,
                error_type=type(exc).__name__,
            )

    async def _verify_tool(
        self, client: Any, tool_name: str
    ) -> McpToolError | McpConfigurationError | None:
        if self._verified:
            return None
        async with self._verification_lock:
            if self._verified:
                return None
            listed = await client.list_tools()
            tool = next((item for item in listed.tools if item.name == tool_name), None)
            if tool is None:
                return McpToolError(
                    f"configured MCP tool {tool_name!r} was not advertised by "
                    f"server {self._config.name!r}"
                )
            validation_error = _tool_schema_error(self._config, tool)
            if validation_error is not None:
                return validation_error
            self._verified = True
            return None

    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[Any]:
        try:
            mcp = importlib.import_module("mcp")
        except ImportError as exc:
            raise RuntimeError('MCP mode requires: python -m pip install -e ".[mcp]"') from exc
        if self._target is not None:
            async with mcp.Client(self._target) as client:
                yield client
            return
        token = _bearer_token(self._config)
        try:
            httpx2 = importlib.import_module("httpx2")
            transport_module = importlib.import_module("mcp.client.streamable_http")
        except ImportError as exc:
            raise RuntimeError("MCP HTTP authentication dependencies are unavailable") from exc
        timeout = httpx2.Timeout(self._timeout_seconds, read=self._timeout_seconds)
        headers = {"Authorization": f"Bearer {token}"} if token is not None else None
        async with httpx2.AsyncClient(
            headers=headers,
            timeout=timeout,
            follow_redirects=False,
            trust_env=self._config.trust_env,
        ) as http_client:
            transport = transport_module.streamable_http_client(
                str(self._config.url),
                http_client=http_client,
            )
            async with mcp.Client(transport) as client:
                yield client


class McpPaperProvider:
    def __init__(
        self,
        config: McpPaperServerConfig,
        gateway: McpToolGateway,
    ) -> None:
        self._config = config
        self._gateway = gateway
        self.name = f"mcp:{config.name}"

    async def search(self, task: SearchTask, limit: int) -> list[Paper]:
        bounded_limit = min(limit, self._config.max_results)
        arguments: dict[str, JsonScalar] = {
            **self._config.static_arguments,
            self._config.query_argument: task.query,
            self._config.limit_argument: bounded_limit,
        }
        if self._config.year_from_argument and task.year_from is not None:
            arguments[self._config.year_from_argument] = task.year_from
        if self._config.year_to_argument and task.year_to is not None:
            arguments[self._config.year_to_argument] = task.year_to
        payload = await self._gateway.call_tool(self._config.search_tool, arguments)
        records = _paper_records(payload)
        papers = [
            paper
            for item in records[:bounded_limit]
            if (paper := _to_paper(item, source=self.name)) is not None
        ]
        return papers


def parse_mcp_paper_servers(value: str) -> tuple[McpPaperServerConfig, ...]:
    try:
        raw = json.loads(value)
        configs = TypeAdapter(list[McpPaperServerConfig]).validate_python(raw)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise McpConfigurationError(f"invalid MCP paper server configuration: {exc}") from exc
    names = [item.name for item in configs]
    if len(names) != len(set(names)):
        raise McpConfigurationError("MCP paper server names must be unique")
    return tuple(configs)


def build_mcp_paper_providers(
    configs: tuple[McpPaperServerConfig, ...],
    *,
    timeout_seconds: float,
) -> tuple[McpPaperProvider, ...]:
    return tuple(
        McpPaperProvider(
            config,
            OfficialMcpToolGateway(config, timeout_seconds=timeout_seconds),
        )
        for config in configs
    )


async def probe_mcp_paper_servers(
    configs: tuple[McpPaperServerConfig, ...],
    *,
    timeout_seconds: float,
) -> tuple[McpServerStatus, ...]:
    gateways = [
        OfficialMcpToolGateway(config, timeout_seconds=timeout_seconds) for config in configs
    ]
    return tuple(await asyncio.gather(*(gateway.probe() for gateway in gateways)))


def _bearer_token(config: McpPaperServerConfig) -> str | None:
    if config.bearer_token_env is None:
        return None
    value = os.getenv(config.bearer_token_env)
    if not value:
        raise McpConfigurationError(
            f"MCP server {config.name!r} requires environment variable {config.bearer_token_env!r}"
        )
    return value


def _tool_schema_error(config: McpPaperServerConfig, tool: Any) -> McpConfigurationError | None:
    schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None) or {}
    if not isinstance(schema, dict):
        return None
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return None
    supplied = {
        *config.static_arguments,
        config.query_argument,
        config.limit_argument,
        *([config.year_from_argument] if config.year_from_argument else []),
        *([config.year_to_argument] if config.year_to_argument else []),
    }
    missing_mappings = {config.query_argument, config.limit_argument} - properties.keys()
    if missing_mappings:
        return McpConfigurationError(
            f"MCP tool {config.search_tool!r} does not declare configured arguments: "
            f"{', '.join(sorted(missing_mappings))}"
        )
    required = schema.get("required")
    if isinstance(required, list):
        missing_required = {str(item) for item in required} - supplied
        if missing_required:
            return McpConfigurationError(
                f"MCP tool {config.search_tool!r} has required arguments without mappings: "
                f"{', '.join(sorted(missing_required))}"
            )
    return None


def _structured_result(result: Any) -> dict[str, Any] | list[Any]:
    if isinstance(result.structured_content, (dict, list)):
        return result.structured_content
    texts = [
        str(block.text)
        for block in result.content
        if getattr(block, "type", None) == "text" and hasattr(block, "text")
    ]
    if not texts:
        raise McpToolError("MCP tool returned no structured or textual JSON content")
    try:
        payload = json.loads("\n".join(texts))
    except json.JSONDecodeError as exc:
        raise McpToolError("MCP tool text output is not valid JSON") from exc
    if not isinstance(payload, (dict, list)):
        raise McpToolError("MCP tool output must be a JSON object or array")
    return payload


def _safe_tool_error(content: list[Any]) -> str:
    text = next(
        (
            str(block.text)
            for block in content
            if getattr(block, "type", None) == "text" and hasattr(block, "text")
        ),
        "MCP tool execution failed",
    )
    return text[:500]


def _paper_records(payload: dict[str, Any] | list[Any]) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        values = payload
    else:
        values = next(
            (
                payload[key]
                for key in ("papers", "results", "items")
                if isinstance(payload.get(key), list)
            ),
            [payload] if "title" in payload else [],
        )
    return [dict(item) for item in values if isinstance(item, dict)]


def _to_paper(item: dict[str, Any], *, source: str) -> Paper | None:
    title = str(item.get("title") or "").strip()
    if not title:
        return None
    doi = normalize_doi(str(item["doi"])) if item.get("doi") else None
    authors = _authors(item.get("authors"))
    year = _year(item.get("year"))
    external_ids = item.get("external_ids")
    if not isinstance(external_ids, dict):
        external_ids = {}
    try:
        return Paper(
            paper_id=stable_paper_id(doi=doi, title=title),
            title=title,
            abstract=str(item.get("abstract") or ""),
            authors=authors,
            year=year,
            doi=doi,
            url=item.get("url"),
            source=source,
            external_ids={str(key): str(value) for key, value in external_ids.items()},
            score=max(0.0, float(item.get("score") or 0.0)),
        )
    except (TypeError, ValueError, ValidationError):
        return None


def _authors(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    names: list[str] = []
    for item in value[:50]:
        if isinstance(item, str):
            name = item.strip()
        elif isinstance(item, dict):
            name = str(item.get("name") or item.get("display_name") or "").strip()
        else:
            name = ""
        if name:
            names.append(name)
    return tuple(names)


def _year(value: Any) -> int | None:
    if value is None:
        return None
    try:
        year = int(value)
    except (TypeError, ValueError):
        return None
    return year if 1900 <= year <= 2100 else None
