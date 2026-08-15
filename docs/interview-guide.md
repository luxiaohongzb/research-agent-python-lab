# 面试指南

## 一分钟介绍

这是一个 evidence-first 的智能科研助理。我没有让模型一次性搜索并生成答案，而是用 LangGraph 把过程拆成规划、并行检索、去重、证据卡片、覆盖度判断、报告生成、原子 Claim 核验和质量门禁。所有循环都有 `ResearchBudget`，每个 Claim 都必须经过 `EvidenceCard` 回到 `Passage`。没有证据或存在不支持结论时，状态是 `NEEDS_REVIEW`，而不是继续幻觉。

## 能体现工程能力的点

1. 选择状态图而非完全自由 Agent：控制成本、恢复、测试和状态迁移。
2. 领域模型不依赖 LangGraph：未来可以替换编排器和模型供应商。
3. provider 失败隔离：远端源并行，一个失败不拖垮整个任务。
4. 生成与核验分离：Verifier 只依据绑定 Passage，不依据模型常识。
5. 评测分层：分别测引用精度、覆盖率、支持率和预算利用率。
6. 结构化输出不是终点：Provider 保证 JSON schema，Java/Python 领域规则继续校验引用白名单、置信度和来源绑定。
7. 可观测 fallback：单阶段模型失败不会丢掉整个 run，失败次数、错误类型、token、延迟和 prompt 版本会保留。
8. 混合检索不直接相加原始分数：四路召回先各自排序，再用 RRF 融合，候选集最后做 cross-encoder/来源多样性重排。
9. 全文引用不是字符串拼接：GROBID TEI 被解析为带章节、页码、坐标、版本和哈希的 Passage，证据可回到 PDF 位置。
10. 自适应 fan-out：只有 `DEEP` 问题才用 `Send` 并行派发 Worker，简单问题不支付协调成本。
11. Worker 传 Artifact ID 而非全文，Supervisor 是唯一归并点；超时或失败 Worker 不影响其他分支。
12. 运行快照和 checkpoint 分层：前者服务 HTTP 查询与幂等，后者服务 LangGraph 断点恢复。
13. SSE 使用 sequence 支持断线重放；Prometheus 看运行趋势，Trace 看单次调用路径。
14. 人工审阅绑定具体 Claim/Evidence ID，BibTeX/CSL 导出复用规范化文献模型。
15. API 和执行 Worker 分离：Redis Streams 至少一次投递，终态检查与 run 租约抑制重复执行。
16. checkpoint、run snapshot、event stream 和 S3 Artifact 各自承担不同恢复语义，不混成一个“状态库”。
17. MCP 作为受控的数据源适配层：只允许配置的只读工具，先校验工具 Schema 和结构化结果，再转换成 Paper 领域对象；不会把外部工具列表直接交给模型自由执行。

## 常见追问

### 为什么不是所有问题都启动多 Agent？

多 Agent 适合能拆成独立方向的广度任务，但协调和 token 成本高。项目先用复杂度路由，简单问题走单图，深度问题才 fan-out，并限制 Worker 数和轮数。

### 并行 Worker 如何避免状态冲突？

Worker 不写共享论文数组。每个 Worker 把检索批次放进 Artifact Store，通过带 run ID 的引用交给 Supervisor；只有 Supervisor Merge 节点读取并归并。LangGraph state 中的 WorkerResult 使用 reducer 聚合，因此并发完成顺序不会覆盖其他分支。

### 当前 verifier 可靠吗？

默认词法 verifier 是可复现基线，只证明数据链和质量门禁。OpenAI 模式会切换到结构化语义核验，同时保留确定性 citation ID 检查；进入生产后仍需增加人工抽审和 conflict/partial 专项数据集。

### 为什么使用结构化输出后还要业务校验？

JSON Schema 只能保证字段和类型正确，不能保证 evidence ID 真正存在，也不能保证 Claim 忠于输入。项目先用 Provider 原生 schema 约束形状，再由代码验证引用白名单和领域不变量；连续失败后只回退当前阶段。

### 如何进入生产？

分布式运行链路已经具备 PostgreSQL checkpoint/运行快照、Redis Streams 队列与事件、
独立 Worker、执行租约、协作取消、MinIO Artifact、Prometheus 和 OpenTelemetry 接入点。
下一步重点不再是“加一个队列”，而是 OIDC/RBAC、多租户隔离、配额限流、失败队列、
Artifact 生命周期任务和审批 UI。系统明确采用至少一次投递；不可逆工具还需要 outbox
或下游幂等键，不能把 Redis 租约描述成 exactly-once。

### 为什么 MCP 不直接接到 ReAct Agent？

科研系统对来源、预算和引用有确定性约束。当前把 MCP 搜索工具适配为 PaperProvider，外部结果必须经过领域校验、去重、RAG 和 Claim Verifier。这样既获得协议级可插拔性，又不会让任意 MCP 工具绕过权限、预算和证据链。副作用工具需要单独的审批与幂等设计，不能因为 MCP 提供了统一协议就默认可信。
