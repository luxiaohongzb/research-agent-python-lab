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

全文检索和自适应多 Agent 已形成基础链路。下一步将内存 checkpoint/Artifact Store 换成持久化实现，加入领域 embedding 评测、幂等、取消、限流、熔断、OpenTelemetry 和人工审批。
