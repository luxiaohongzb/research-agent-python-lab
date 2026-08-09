# 架构说明

## 目标

第一版优先证明三个能力：研究循环可以被预算约束、报告结论可以追溯到精确证据、生成结果可以被独立质量门禁拦截。

```mermaid
flowchart TD
    A[ResearchRequest] --> B[Planner and Complexity Router]
    B -->|simple| C[Single Research Graph]
    B -->|deep| Q[Supervisor]
    Q -->|LangGraph Send| R[Parallel Research Workers]
    R --> S[Artifact IDs]
    S --> T[Supervisor Merge]
    C --> D[Keyword and Vector Search]
    T --> D
    C --> U[Metadata and Citation Search]
    T --> U
    U --> E[RRF and Diversity Rerank]
    D --> E
    E --> N[DOI and Title Dedup]
    N --> O[Full-text Passage or Abstract Fallback]
    O --> P[EvidenceCard]
    P --> F{Coverage Judge}
    F -->|gap and budget remains| G[Refine Query]
    G --> C
    F -->|enough or budget exhausted| H[Report Synthesis]
    H --> I[Atomic Claim Split]
    I --> J[Independent Verifier]
    J --> K{Quality Gate}
    K -->|all supported| L[COMPLETED]
    K -->|unsupported or no evidence| M[NEEDS_REVIEW]
```

## 分层

- `domain.py`：稳定的科研数据契约，不依赖框架。
- `providers.py`：外部学术检索端口和适配器，失败隔离、DOI/标题归并。
- `ingestion.py`：GROBID multipart 客户端和 TEI → Passage/引用边转换。
- `retrieval.py`：关键词、向量、元数据、引用图召回契约，RRF 与 reranker。
- `postgres_index.py`：PostgreSQL `tsvector`、pgvector HNSW 和持久化引用边。
- `semantic_scholar.py`：Semantic Scholar 搜索与一跳引用邻域。
- `reasoner.py`：规划、证据抽取、综合和验证端口。默认确定性实现保证离线复现。
- `llm_reasoner.py`：原生 JSON Schema 输出、语义后校验、调用指标和阶段级 fallback。
- `prompts.py`：四个职责隔离且有版本号的系统提示词。
- `workflow.py`：LangGraph 节点、条件边、checkpoint、预算和门禁。
- `application.py`：同步/异步用例和运行状态。
- `api.py`：HTTP 边界，不承载业务规则。
- `artifacts.py`：并行 Worker 的 run-scoped 大对象交接，图状态只保存引用。
- `run_store.py`：运行快照持久化和数据库级幂等约束。
- `events.py`：SSE 事件的有界回放与实时 fan-out。
- `distributed.py`：Redis Streams 任务队列、取消令牌与执行租约。
- `worker.py`：consumer-group Worker、消息确认和租约心跳。
- `observability.py`：Prometheus 运行指标、质量摘要和可选 OTLP trace。
- `citations.py`：从规范化 Paper 生成 BibTeX 和 CSL JSON。
- `evaluation.py`：独立于运行链路的质量指标。

## 关键决策

### 为什么不直接使用预制 ReAct Agent

科研任务的 DOI 归一化、预算扣减、引用 ID 和质量门禁是确定性规则。状态图把这些规则固定为代码，只把查询规划、证据理解和综合等开放问题交给 reasoner。

### 为什么默认使用离线 reasoner

项目首先需要可重复的架构基线。离线实现使 CI 不依赖密钥、模型版本和远端限流。接入 LLM 后必须继续通过同一组领域契约和评测门槛。

### 结构化模型边界

四个模型阶段分别使用 `PlanOutput`、`EvidenceOutput`、`SynthesisOutput` 和 `VerificationOutput`，不能互相越权。Provider 原生 schema 只保证 JSON 形状，代码还会检查：

- 查询数量、空值和研究范围；
- EvidenceCard 的 passage ID 由系统注入；
- Claim 引用的 evidence ID 必须属于输入白名单；
- Verifier 只能看到 Claim 绑定的 Passage；
- 置信度范围、空 Claim、未知引用等语义不变量。

每次调用产生 `ModelInvocation`，保存 stage、provider、model、prompt version、latency、token、费用估算和错误类型。价格由运行配置提供，避免在代码中冻结易变信息。结构化或语义验证连续失败两次后，只回退当前阶段，并把失败记录带入最终结果。

### 状态与 Artifact

默认开发模式将序列化后的领域对象放入 LangGraph state，并使用 `InMemorySaver`。
设置 `CHECKPOINT_MODE=postgres` 后，工作流切换到官方 `AsyncPostgresSaver`，由
LangGraph 管理 checkpoint 表；`RUN_STORE_MODE=postgres` 则把任务状态、结果、审阅和
幂等键保存为应用自己的 JSONB 快照。两者职责不同：checkpoint 用于恢复图执行，run
store 用于稳定的 HTTP 查询契约。

分布式模式下，Worker 的 `RetrievalBatch` 进入 S3/MinIO，checkpoint 只保存
`ArtifactRef`。对象 key 同时包含 run ID 和 artifact ID，读取时再次校验 run ID，避免
跨运行越权。Store 已提供分页统计和按 run 批量删除原语；自动生命周期调度留到
Iteration 7。

下一步存储演进：

- 论文全文和解析文件进入对象存储；
- Passage、EvidenceCard 和 Claim 进入 Artifact Store；
- graph state 只保存 ID 和短摘要，避免上下文膨胀。

### 运行控制与事件

`Idempotency-Key` 在 PostgreSQL 中有唯一约束，重复提交返回原 run。分布式模式下，API
只写入 Redis Stream，独立 Worker 使用 consumer group 消费。Worker 获得 run 级 Redis
租约后才执行，并以租期三分之一为周期续约；进程崩溃后，其他 Worker 使用
`XAUTOCLAIM` 回收超过租期的 pending 消息。终态 run 会直接确认重复消息。

取消接口立即写 PostgreSQL `CANCELLED` 快照和 Redis 取消令牌。远端 Worker 在 LangGraph
节点进度安全点读取令牌，不再依赖 API 进程持有 asyncio task；恢复会清除令牌，并使用
相同 `thread_id/run_id` 继续 checkpoint。节点更新进入按 run 隔离的 Redis Stream，事件
sequence 使用 Redis 原子递增，SSE 可通过 `after_sequence` 跨 API 副本重放。

该链路是至少一次投递，不是 exactly-once。数据库幂等键、consumer group、run 终态检查
和执行租约共同使重复投递收敛；若要处理外部不可逆工具，还必须使用 outbox/inbox 或工具
自身的幂等键。

### 为什么使用 RRF

BM25/`ts_rank_cd`、余弦相似度、元数据相关性和引用图信号的数值范围不同，直接加权会把校准问题隐藏在常数中。当前实现先在各 lane 内排序，再以 `1 / (k + rank)` 融合；cross-encoder 只对候选集重排，不承担全库召回。每个 `RetrievalHit` 保留 lane rank、lane score、融合分数和最终名次，便于离线评测与线上解释。

### 全文退化策略

PDF 上传依次经过文件头/体积校验、GROBID 全文接口、TEI 解析和索引写入。研究查询优先使用命中的全文 Passage；没有全文命中时才从摘要生成 Passage。因此 GROBID 或数据库不可用不会破坏默认离线演示，但生产部署应为 GROBID 503 增加队列、熔断和异步重试。

### 自适应多 Agent

Planner 输出 `SIMPLE/DEEP`。简单请求继续进入原单图，只有可拆成多个独立搜索方向的深度请求才进入 Supervisor。Supervisor 在派发前同时计算查询、工具调用、Worker 和剩余墙钟时间容量，然后用 LangGraph `Send` 创建并行 Worker。

Worker 不直接更新共享 `papers/passages`，而是把 `RetrievalBatch` 写入 Artifact Store，只返回 `ResearchWorkerResult`。该对象包含 Worker/Task ID、ArtifactRef、状态、耗时和错误类型。汇总节点串行读取同一 run 的 Artifact、统一扣减预算并执行去重，规避并行 state channel 冲突和上下文复制。

### 安全边界

OpenAlex/Crossref 地址在代码中固定，来源内容永远按数据处理。远端源失败只产生 warning。未来加入网页/PDF 下载时，必须增加域名、重定向、内网 IP、文件类型和体积校验，并让解析器运行在受限容器。
