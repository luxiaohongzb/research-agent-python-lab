# 迭代路线

## 已完成：MVP-1 可核验研究链路

- LangGraph 条件循环与内存 checkpoint
- 研究预算、离线语料、OpenAlex/Crossref adapters
- Paper、Passage、EvidenceCard、AtomicClaim 数据链
- 独立 Claim verifier 和质量门禁
- FastAPI、CLI、Trace、离线指标与 CI

## 已完成：Iteration 2 结构化 LLM 与评测集

- 基于 `with_structured_output` 实现 Planner、Extractor、Synthesizer、Verifier
- 每个结构化输出增加语义校验和重试边界
- 20–30 条中英双语 golden cases
- citation precision、coverage、unsupported claim CI 门禁
- 模型、prompt、token、费用和延迟版本记录

额外交付：阶段级确定性 fallback、中文 bigram 检索、域外问题拒答门禁、Mypy strict CI。

## 已完成：Iteration 3 全文和混合检索

- GROBID sidecar：TEI 转结构化 Passage
- PostgreSQL、pgvector HNSW 和全文索引
- BM25、embedding、metadata、citation graph 四路召回与 RRF
- Cross-encoder rerank 和来源多样性约束
- Semantic Scholar 引用图与相似论文

额外交付：PDF 上传 API、解析与内容版本留痕、内存降级索引、真实 pgvector 容器集成测试、可选 cross-encoder 依赖隔离。当前 Semantic Scholar 实现覆盖搜索、引用和参考文献邻域；官方 recommendation endpoint 可在相关性评测集准备完成后接入。

## 已完成：Iteration 4 自适应多 Agent

- Complexity Router：simple 使用单研究图，deep 才启用 Supervisor
- 使用 LangGraph `Send` 并行 3–5 个独立 Research Worker
- Artifact Store 交接 ID，不复制整段上下文
- 每次运行设置 token、费用、时间、查询、论文与 Worker 数量上限

额外交付：Worker 超时与失败隔离、运行级 Artifact 引用鉴权、并发峰值回归测试、Worker 结果与多维预算进入 API 输出和 Trace。

## 已完成：Iteration 5 生产工作台

- PostgreSQL checkpoint、恢复、取消和幂等
- SSE 进度、证据/Claim 审阅与人工审批记录
- OpenTelemetry、Prometheus、成本和质量看板
- BibTeX、CSL JSON 导出，便于导入 Zotero

额外交付：PostgreSQL 运行快照、SSE 有界事件回放、Windows Selector 事件循环启动器、真实 PostgreSQL checkpoint CI。当前取消、事件流和 Artifact Store 仍以单实例为边界。

## Iteration 6：多租户与分布式执行

- Redis/NATS 事件总线、Worker 队列、租约与跨副本协作取消
- S3/MinIO Artifact Store、生命周期与引用计数清理
- OIDC、RBAC、租户隔离、审计日志与审批 UI
- Zotero API、只读 MCP 连接器和领域 embedding/reranker 评测
