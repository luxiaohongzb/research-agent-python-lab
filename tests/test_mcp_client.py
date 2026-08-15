import asyncio
import socket
from typing import Any

import pytest
import uvicorn
from mcp.server import MCPServer

from research_agent.config import Settings
from research_agent.domain import SearchTask
from research_agent.mcp_client import (
    McpConfigurationError,
    McpPaperProvider,
    McpPaperServerConfig,
    McpToolError,
    OfficialMcpToolGateway,
    parse_mcp_paper_servers,
)
from research_agent.workflow import build_default_workflow


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@pytest.mark.asyncio
async def test_mcp_paper_provider_uses_real_protocol_and_validates_output() -> None:
    server = MCPServer("paper-catalog")
    calls: list[dict[str, Any]] = []

    @server.tool()
    def search_papers(query: str, limit: int, year_from: int | None = None) -> dict[str, Any]:
        calls.append({"query": query, "limit": limit, "year_from": year_from})
        return {
            "papers": [
                {
                    "title": "MCP for Evidence-Grounded Research",
                    "abstract": f"Structured MCP evidence for {query}.",
                    "authors": ["Ada Researcher", {"name": "Lin Scientist"}],
                    "year": 2026,
                    "doi": "https://doi.org/10.5555/mcp-research",
                    "url": "https://example.org/mcp-research",
                    "external_ids": {"catalog": "MCP-1"},
                    "score": 0.9,
                },
                {"abstract": "missing title is rejected"},
            ]
        }

    config = McpPaperServerConfig(
        name="catalog",
        url="http://127.0.0.1:9999/mcp",
        search_tool="search_papers",
        year_from_argument="year_from",
    )
    provider = McpPaperProvider(
        config,
        OfficialMcpToolGateway(config, target=server),
    )
    task = SearchTask(
        sub_question="MCP evidence",
        query="scientific MCP",
        purpose="find evidence",
        year_from=2024,
    )

    papers = await provider.search(task, limit=3)

    assert calls == [{"query": "scientific MCP", "limit": 3, "year_from": 2024}]
    assert len(papers) == 1
    assert papers[0].doi == "10.5555/mcp-research"
    assert papers[0].authors == ("Ada Researcher", "Lin Scientist")
    assert papers[0].source == "mcp:catalog"


@pytest.mark.asyncio
async def test_mcp_gateway_connects_over_streamable_http() -> None:
    mcp_server = MCPServer("remote-paper-catalog")

    @mcp_server.tool()
    def search_papers(query: str, limit: int) -> dict[str, Any]:
        return {"papers": [{"title": f"Remote result for {query}", "year": 2026}][:limit]}

    port = _free_port()
    uvicorn_server = uvicorn.Server(
        uvicorn.Config(
            mcp_server.streamable_http_app(stateless_http=True, json_response=True),
            host="127.0.0.1",
            port=port,
            log_level="warning",
        )
    )
    server_task = asyncio.create_task(uvicorn_server.serve())
    try:
        for _ in range(100):
            if uvicorn_server.started:
                break
            await asyncio.sleep(0.01)
        assert uvicorn_server.started
        config = McpPaperServerConfig(
            name="remote",
            url=f"http://127.0.0.1:{port}/mcp",
            search_tool="search_papers",
        )
        provider = McpPaperProvider(config, OfficialMcpToolGateway(config))

        papers = await provider.search(
            SearchTask(
                sub_question="remote MCP",
                query="protocol transport",
                purpose="test Streamable HTTP",
            ),
            limit=2,
        )

        assert papers[0].title == "Remote result for protocol transport"
        assert papers[0].source == "mcp:remote"
    finally:
        uvicorn_server.should_exit = True
        await asyncio.wait_for(server_task, timeout=5)


@pytest.mark.asyncio
async def test_mcp_gateway_rejects_tools_outside_configured_allowlist() -> None:
    config = McpPaperServerConfig(
        name="catalog",
        url="http://127.0.0.1:9999/mcp",
        search_tool="search_papers",
    )
    gateway = OfficialMcpToolGateway(config, target=MCPServer("empty"))

    with pytest.raises(McpToolError, match="not allow-listed"):
        await gateway.call_tool("delete_library", {})


@pytest.mark.asyncio
async def test_mcp_gateway_rejects_unmapped_required_tool_arguments() -> None:
    server = MCPServer("paper-catalog")

    @server.tool()
    def search_papers(query: str, limit: int, collection: str) -> dict[str, Any]:
        return {"papers": [], "collection": collection, "query": query, "limit": limit}

    config = McpPaperServerConfig(
        name="catalog",
        url="http://127.0.0.1:9999/mcp",
        search_tool="search_papers",
    )
    gateway = OfficialMcpToolGateway(config, target=server)

    with pytest.raises(McpConfigurationError, match="collection"):
        await gateway.call_tool("search_papers", {"query": "evidence", "limit": 3})


def test_mcp_configuration_is_strict_and_requires_unique_server_names() -> None:
    with pytest.raises(McpConfigurationError, match="unique"):
        parse_mcp_paper_servers(
            """
            [
              {"name":"papers","url":"http://one.example/mcp","search_tool":"search"},
              {"name":"papers","url":"http://two.example/mcp","search_tool":"search"}
            ]
            """
        )
    with pytest.raises(McpConfigurationError, match="invalid"):
        parse_mcp_paper_servers(
            '[{"name":"papers","url":"file:///tmp/socket","search_tool":"search"}]'
        )


def test_enabling_mcp_without_servers_fails_at_startup() -> None:
    with pytest.raises(RuntimeError, match="MCP is enabled"):
        build_default_workflow(Settings(mcp_enabled=True, mcp_paper_servers_json="[]"))
