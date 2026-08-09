# 生产工作台指南

Iteration 5 的目标不是把后台任务包装成几个接口，而是让一次研究运行具备稳定身份、
可恢复状态、实时进度、人工复核和可观测性。默认内存模式适合学习和面试演示；
PostgreSQL 模式用于验证接近生产的持久化边界。

## 运行模型

```mermaid
sequenceDiagram
    participant C as Client
    participant A as FastAPI
    participant R as Run Store
    participant G as LangGraph
    participant P as Checkpoint Store
    C->>A: POST /runs + Idempotency-Key
    A->>R: create snapshot (unique key)
    A-->>C: 202 PENDING + run_id
    A->>G: execute with thread_id=run_id
    G->>P: persist node checkpoints
    G-->>A: node progress
    A-->>C: SSE sequence + event
    A->>R: save result or failure
    C->>A: DELETE /runs/{id}
    A->>G: cancel local task
    C->>A: POST /runs/{id}/resume
    A->>G: resume same thread_id
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

完整 PostgreSQL 模式：

```bash
docker compose up -d postgres
python -m pip install -e ".[dev,postgres,observability]"
```

PowerShell：

```powershell
$env:RESEARCH_AGENT_INDEX_MODE="postgres"
$env:RESEARCH_AGENT_CHECKPOINT_MODE="postgres"
$env:RESEARCH_AGENT_RUN_STORE_MODE="postgres"
$env:RESEARCH_AGENT_DATABASE_URL="postgresql://research:research@127.0.0.1:5432/research_agent"
research-agent-server
```

macOS/Linux 使用同名 `export`。也可以直接 `docker compose up --build` 启动 API、
PostgreSQL/pgvector 和 GROBID。Windows 原生运行 PostgreSQL checkpoint 时必须使用项目
启动器，以便 psycopg 使用 Selector event loop。

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
状态；`/v1/metrics/summary` 是便于前端演示的进程内聚合，不替代长期时序数据库。

## 一致性与边界

- PostgreSQL 唯一索引保证幂等，不依赖进程锁。
- 取消是单实例 task cancellation；checkpoint 让同一 run 可以继续。
- SSE 历史有容量上限，慢消费者队列满时淘汰最旧事件，防止内存无界增长。
- 事件、活跃 task 和 Artifact 仍是进程内状态，当前不应水平扩容。
- OpenTelemetry 默认关闭，避免本地测试隐式访问外部 collector。
- 审阅记录目前是领域审计数据，不包含身份认证或审批状态机。

多副本演进应引入共享事件总线、持久化 Artifact Store、带租约的 Worker 队列和数据库
取消令牌，再加 OIDC/RBAC 与租户级资源配额。这个边界在面试中要主动说明：当前实现验证
了恢复和运维契约，但没有把单实例能力包装成“分布式完成”。
