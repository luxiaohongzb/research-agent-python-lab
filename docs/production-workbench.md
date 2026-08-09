# 生产工作台指南

Iteration 5 让研究运行具备稳定身份、可恢复状态、实时进度、人工复核和可观测性；
Iteration 6 再把执行从 API 进程拆到独立 Worker。默认内存模式适合学习和单元测试，
Compose 模式验证 PostgreSQL、Redis 和 MinIO 组成的分布式运行边界。

## 运行模型

```mermaid
sequenceDiagram
    participant C as Client
    participant A as FastAPI
    participant R as Run Store
    participant Q as Redis Streams
    participant W as Worker
    participant G as LangGraph
    participant P as Checkpoint Store
    participant S as MinIO
    C->>A: POST /runs + Idempotency-Key
    A->>R: create snapshot (unique key)
    A->>Q: enqueue run_id
    A-->>C: 202 PENDING + run_id
    W->>Q: XREADGROUP + acquire lease
    W->>G: execute with thread_id=run_id
    G->>P: persist node checkpoints
    G->>S: put run-scoped Artifact
    G-->>Q: node progress
    A->>Q: XREAD after sequence
    A-->>C: SSE sequence + event
    W->>R: save result or failure
    W->>Q: XACK + release lease
    C->>A: DELETE /runs/{id}
    A->>Q: set cancellation token
    C->>A: POST /runs/{id}/resume
    A->>Q: clear token + enqueue resume
```

运行快照和 checkpoint 不能合并为一个概念：快照是面向 API 的聚合，包含请求、状态、
结果与审阅；checkpoint 是 LangGraph 的内部执行状态。恢复时以 `run_id` 作为稳定的
`thread_id`，没有 checkpoint 才从头执行。

## 启动模式

仅学习和离线测试：

```bash
python -m pip install -e ".[dev]"
research-agent-server --reload
```

完整分布式模式：

```bash
docker compose up --build
```

宿主机分别运行 API 与 Worker 时安装全部适配器：

```powershell
python -m pip install -e ".[dev,postgres,observability,distributed]"
$env:RESEARCH_AGENT_INDEX_MODE="postgres"
$env:RESEARCH_AGENT_CHECKPOINT_MODE="postgres"
$env:RESEARCH_AGENT_RUN_STORE_MODE="postgres"
$env:RESEARCH_AGENT_EVENT_BROKER_MODE="redis"
$env:RESEARCH_AGENT_DISPATCH_MODE="redis"
$env:RESEARCH_AGENT_CANCELLATION_MODE="redis"
$env:RESEARCH_AGENT_ARTIFACT_STORE_MODE="s3"
$env:RESEARCH_AGENT_DATABASE_URL="postgresql://research:research@127.0.0.1:5432/research_agent"
research-agent-server
research-agent-worker
```

macOS/Linux 使用同名 `export`。Windows 原生运行 PostgreSQL checkpoint 时，API 和 Worker
入口都会选择 psycopg 支持的 Selector event loop。

## API 演示

创建任务时使用业务请求唯一键，网络重试不会产生第二个任务：

```bash
curl -X POST http://127.0.0.1:8000/v1/research/runs \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: demo-2026-001" \
  -d '{"question":"Compare agentic RAG and claim verification","max_workers":2}'
```

订阅进度；断线重连时把最后收到的 SSE `id` 作为 `after_sequence`：

```bash
curl -N "http://127.0.0.1:8000/v1/research/runs/{run_id}/events?after_sequence=0"
```

取消与恢复：

```bash
curl -X DELETE http://127.0.0.1:8000/v1/research/runs/{run_id}
curl -X POST http://127.0.0.1:8000/v1/research/runs/{run_id}/resume
```

审阅可绑定 run、Claim 或 Evidence；后端会拒绝不存在的 ID：

```bash
curl -X POST http://127.0.0.1:8000/v1/research/runs/{run_id}/reviews \
  -H "Content-Type: application/json" \
  -d '{"reviewer":"reviewer@example.com","decision":"APPROVE","claim_id":"claim-...","comment":"Evidence supports the claim."}'
```

导出和指标：

```text
GET /v1/research/runs/{run_id}/export/bibtex
GET /v1/research/runs/{run_id}/export/csl-json
GET /v1/metrics/summary
GET /metrics/
```

设置 `RESEARCH_AGENT_OTEL_ENABLED=true` 并配置标准 OTLP 环境变量后，运行会产生
`research.run` span。Prometheus 指标包括运行状态、活跃任务、耗时、估算费用和 Worker
状态；`/v1/metrics/summary` 是便于前端演示的 API 进程聚合，不替代长期时序数据库。
分布式 Worker 在容器内 `9100` 暴露自己的 Prometheus 指标，生产 Prometheus 应同时抓取
API `/metrics/` 和每个 Worker `:9100/metrics`。

## 一致性与边界

- PostgreSQL 唯一索引保证幂等，不依赖进程锁。
- Redis consumer group 是至少一次投递；终态检查和执行租约让重复消息收敛。
- 取消令牌跨进程共享，但只在 LangGraph 节点边界生效，不会粗暴终止正在写外部系统的调用。
- SSE 历史通过 Redis Stream `MAXLEN` 有界保留，sequence 使用原子递增。
- Artifact 使用 S3 JSON schema，不使用不安全的 pickle；run ID 同时进入 key 和读取校验。
- OpenTelemetry 默认关闭，避免本地测试隐式访问外部 collector。
- 审阅记录目前是领域审计数据，不包含身份认证或审批状态机。

下一阶段仍需 OIDC/RBAC、租户级资源配额、失败队列、自动 Artifact 生命周期任务和审批
状态机。对于付款、发信等不可逆工具，仍需 outbox/inbox 或下游幂等键；Redis 租约本身
不能提供严格 exactly-once。
