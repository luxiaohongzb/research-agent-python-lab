# MCP Client 科研连接器

## 定位

本项目作为 MCP Client 连接外部科研工具。MCP 只替换或扩展数据获取层，不能绕过现有的预算、去重、EvidenceCard、原子 Claim、独立 Verifier 和质量门禁。

当前实现使用官方 MCP Python SDK 2.x，通过 Streamable HTTP 连接一个或多个 MCP Server。每个 Server 显式配置一个只读论文搜索工具；模型不能任意选择或调用未列入配置的工具。

```text
MCP Server search tool
        ↓ structured JSON
McpPaperProvider
        ↓ Paper domain validation
CompositePaperProvider
        ↓
Hybrid RAG → Evidence → Claims → Verification
```

## MCP 工具输出契约

搜索工具至少接收 `query` 和 `limit`，返回结构化 JSON。推荐输出：

```json
{
  "papers": [
    {
      "title": "Required paper title",
      "abstract": "Optional abstract",
      "authors": ["Ada Researcher"],
      "year": 2026,
      "doi": "10.5555/example",
      "url": "https://example.org/paper",
      "external_ids": {"zotero": "ITEM-1"},
      "score": 0.91
    }
  ]
}
```

也接受顶层数组，以及 `results` 或 `items` 数组。`title` 缺失、URL 非 HTTP(S)、字段类型错误的记录会被拒绝。服务端最好声明 output schema 并返回 `structured_content`；兼容旧服务时可以返回包含同一 JSON 的单个文本块。

## 配置

安装依赖：

```bash
python -m pip install -e ".[mcp]"
```

本地 Streamable HTTP 示例：

```dotenv
RESEARCH_AGENT_MCP_ENABLED=true
RESEARCH_AGENT_MCP_TIMEOUT_SECONDS=45
RESEARCH_AGENT_MCP_PAPER_SERVERS_JSON=[{"name":"zotero","url":"http://127.0.0.1:3000/mcp","search_tool":"search_papers","bearer_token_env":"MCP_BEARER_TOKEN"}]
MCP_BEARER_TOKEN=replace-with-real-token
```

Docker 中访问宿主机 MCP Server 时使用：

```dotenv
RESEARCH_AGENT_MCP_PAPER_SERVERS_JSON=[{"name":"zotero","url":"http://host.docker.internal:3000/mcp","search_tool":"search_papers","bearer_token_env":"MCP_BEARER_TOKEN"}]
```

启动或重建：

```bash
docker compose up -d --build --force-recreate api worker
```

不需要认证的 MCP Server 可以省略 `bearer_token_env`。密钥只通过被点名的环境变量读取，不写入 Server JSON、运行结果或审计详情。

## 参数映射

不同 MCP Server 的参数名可能不同，可显式映射：

```json
[
  {
    "name": "library",
    "url": "https://mcp.example.com/mcp",
    "search_tool": "library_search",
    "bearer_token_env": "MCP_BEARER_TOKEN",
    "trust_env": false,
    "query_argument": "text",
    "limit_argument": "page_size",
    "year_from_argument": "published_after",
    "year_to_argument": "published_before",
    "static_arguments": {"collection": "research"},
    "max_results": 30
  }
]
```

`trust_env` 默认关闭，避免本机或容器内的 MCP 流量被系统代理意外转发。只有明确需要通过企业 HTTP 代理访问 MCP Server 时才应开启。

首次工具调用会读取 Server 的工具列表并校验：工具名必须存在；query/limit 参数必须出现在 input schema；所有必填参数必须由动态映射或 `static_arguments` 提供。配置错误会失败，而不是让模型猜测参数。

## 诊断

管理员调用：

```bash
curl http://127.0.0.1:8000/v1/integrations/mcp \
  -H "Authorization: Bearer YOUR_RESEARCH_AGENT_KEY"
```

返回连接状态、协商协议版本、Server 名、工具清单和配置工具是否存在。错误只暴露异常类型，不返回 Token 或远端响应正文。

研究结果中的 `retrieval_errors`/warnings 会标记 `mcp:<server-name>` 失败。MCP Provider 与其他 Provider 并行且失败隔离，单个 MCP Server 不可用不会丢失其他检索来源。

## 安全边界

- 仅调用配置中的 `search_tool`，不把完整工具列表交给模型自由选择；
- MCP 返回值视为不可信数据，必须经过 JSON、Pydantic 和领域模型校验；
- 远端文本不能作为系统指令，仍受项目 Prompt Injection 边界约束；
- 每个 Server 有超时和最大结果数，调用也计入研究工具预算；
- 当前只接入只读论文搜索；写 Zotero、发消息、执行代码等副作用工具不在本阶段范围；
- 生产环境使用 HTTPS、OAuth 或短期 Token，并由 Secret Manager 注入环境变量。

## 当前边界

当前版本支持 Streamable HTTP，不启动本地 stdio 子进程。原因是 API/Worker 运行在容器和多副本环境中，远程 HTTP Server 的生命周期、鉴权与可观测性更明确。后续如需桌面版 Zotero stdio Server，可在同一个 `McpToolGateway` 协议下增加 transport 配置，不需要修改科研工作流。
