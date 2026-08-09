# 面试指南

## 一分钟介绍

这是一个 evidence-first 的智能科研助理。我没有让模型一次性搜索并生成答案，而是用 LangGraph 把过程拆成规划、并行检索、去重、证据卡片、覆盖度判断、报告生成、原子 Claim 核验和质量门禁。所有循环都有 `ResearchBudget`，每个 Claim 都必须经过 `EvidenceCard` 回到 `Passage`。没有证据或存在不支持结论时，状态是 `NEEDS_REVIEW`，而不是继续幻觉。

## 能体现工程能力的点

1. 选择状态图而非完全自由 Agent：控制成本、恢复、测试和状态迁移。
2. 领域模型不依赖 LangGraph：未来可以替换编排器和模型供应商。
3. provider 失败隔离：远端源并行，一个失败不拖垮整个任务。
4. 生成与核验分离：Verifier 只依据绑定 Passage，不依据模型常识。
5. 评测分层：分别测引用精度、覆盖率、支持率和预算利用率。

## 常见追问

### 为什么不是所有问题都启动多 Agent？

多 Agent 适合能拆成独立方向的广度任务，但协调和 token 成本高。项目先用复杂度路由，简单问题走单图，深度问题才 fan-out，并限制 Worker 数和轮数。

### 当前 verifier 可靠吗？

当前词法 verifier 是可复现基线，只证明数据链和质量门禁。下一步用结构化 entailment 模型增强，并保留确定性 citation ID 检查、人工抽审与 conflict/partial 标签。

### 如何进入生产？

将内存 checkpoint 换 PostgreSQL，全文解析交给 GROBID，检索升级为 BM25 + pgvector + citation graph + reranker，并加入幂等、限流、熔断、OpenTelemetry 和人工审批。
