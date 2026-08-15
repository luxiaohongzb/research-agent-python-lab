# Atlas Research v1.3 产品交付手册

## React 产品入口

- `/workbench`：创建研究任务、查看实时执行轨迹、报告、Claim 核验和论文来源；
- `/library`：拖放上传 PDF，查看 GROBID 解析进度与当前标签页的最近入库记录；
- `/admin`：管理员查看运行指标、状态分布、MCP Server、审计日志和失败死信；
- `/docs`：FastAPI 接口文档。

前端源代码位于 `frontend/`。Docker 和 CI 会自动执行生产构建；宿主机开发可在该目录运行 `npm install` 和 `npm run dev`，请求会代理到本机 `8000` 端口的 API。

## 交付范围

Atlas Research 是一个证据优先的智能科研助理。它把复杂问题拆成研究计划，执行多源检索和并行子任务，生成原子声明，逐项映射证据并独立核验，最后输出可导出的研究报告。

本版本可直接演示和二次开发，包含：

- 响应式中文科研工作台：任务创建、预算设置、实时事件、取消/恢复、报告和声明核验；
- FastAPI API 与 OpenAPI 文档，异步任务使用幂等键和 SSE 事件流；
- API Key 认证、`ADMIN / RESEARCHER / REVIEWER` RBAC 和租户数据隔离；
- 租户级并发、Worker、Token 和费用配额，越限返回 `429`；
- 追加式审计日志，服务端写入真实操作者，不信任客户端 reviewer 字段；
- PostgreSQL/pgvector、Redis Streams、独立 Worker、MinIO Artifact Store；
- 执行租约、协作取消、失败死信流和 Prometheus/OpenTelemetry 可观测性；
- 离线确定性模式、真实数据源模式、结构化 LLM 模式和 24 条 golden 质量门禁。

## 一键启动

要求 Docker Engine 和 Docker Compose：

```bash
cp .env.example .env
docker compose up --build -d
docker compose ps
```

打开：

- 产品工作台：<http://127.0.0.1:8000/workbench>
- API 文档：<http://127.0.0.1:8000/docs>
- 健康与就绪探针：`/health`、`/ready`
- Prometheus 指标：`/metrics/`

首次构建会拉取 GROBID 镜像，耗时取决于网络。默认使用离线可复现语料，不需要模型密钥即可完成端到端演示。

## 安全模式

生成一个高熵随机密钥，把 `.env` 切换为：

```dotenv
RESEARCH_AGENT_AUTH_MODE=api_key
RESEARCH_AGENT_API_KEYS_JSON={"YOUR_RANDOM_KEY":{"subject":"admin@example.com","tenant_id":"demo","roles":["ADMIN","RESEARCHER","REVIEWER"]}}
```

然后重建 API 和 Worker：

```bash
docker compose up -d --build --force-recreate api worker
```

在工作台右上角“访问密钥”中录入密钥。浏览器只把它放在当前标签页的 `sessionStorage`。API 调用使用 `Authorization: Bearer YOUR_RANDOM_KEY`。

生产环境还应在反向代理启用 TLS、请求速率限制和安全响应头，使用外部 Secret Manager 注入密钥，并限制 PostgreSQL、Redis、MinIO 和 GROBID 端口仅在私有网络访问。内置 API Key 适合项目交付与内部系统；面向公众的 SaaS 应在网关接 OIDC，并将验证后的主体映射为本项目的 `Principal`。

## 角色与隔离

| 能力 | ADMIN | RESEARCHER | REVIEWER |
|---|---:|---:|---:|
| 创建、取消、恢复研究 | ✓ | ✓ | — |
| 查看报告与导出 | ✓ | ✓ | ✓ |
| 人工审核 | ✓ | — | ✓ |
| 审计、运行指标、死信流 | ✓ | — | — |

所有持久化运行都带 `tenant_id`。查询、事件、审核和导出统一按服务端身份中的租户过滤；跨租户访问返回 `404`，避免泄露资源是否存在。幂等键的唯一范围也是租户级。

## 验收清单

```bash
python -m pip install -e ".[dev,openai,postgres,distributed]"
ruff check .
ruff format --check .
mypy src
pytest
research-agent-eval datasets/golden.jsonl --min-pass-rate 1.0 --summary-only
docker compose config --quiet
```

人工验收：

1. 打开工作台，提交示例问题；
2. 观察 `queued → started → progress → completed` 事件；
3. 确认报告、论文数、证据数、原子声明和支持率呈现；
4. 下载 BibTeX 与 CSL JSON；
5. 在 API Key 模式下验证无密钥 `401`、错误角色 `403`、跨租户 `404`；
6. 用 `GET /v1/audit/events` 验证创建、取消、恢复和审核留痕。

## 面试演示主线

推荐用五分钟讲清楚四层：领域层解决“证据和声明如何建模”；LangGraph 解决“有界循环和动态并行如何编排”；Redis/PostgreSQL 解决“进程失败后如何恢复并收敛重复投递”；认证、租户和审计解决“Demo 如何进入真实组织”。最后明确边界：这是至少一次投递，不虚构 exactly-once；离线 embedding 是工程基线，不声称代表 SOTA 语义效果；自动生成结果仍应接受领域专家复核。
