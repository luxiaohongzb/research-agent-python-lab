# 评测指南

## 目标

评测不是给最终报告打一个模糊总分，而是判断系统是否在正确边界内完成任务。当前数据集包含 24 条中英双语案例：14 条可回答任务和 10 条域外或无答案任务。

## 运行

```bash
research-agent-eval datasets/golden.jsonl --min-pass-rate 1.0
```

CI 在 Python 3.11 和 3.13 上执行相同门禁。失败命令返回非零退出码。

## 检索消融基准

仓库内置了一个小型、确定性的中英双语检索基准，用来防止关键词、向量或 RRF
融合排序在重构中静默退化：

```bash
research-agent-retrieval-bench \
  datasets/retrieval_corpus.jsonl \
  datasets/retrieval_cases.jsonl \
  --k 2 --summary-only --min-hit-rate 1.0
```

PowerShell 可将续行符换成反引号，或直接在一行执行。报告分别输出 `keyword`、
`vector`、`hybrid_rrf` 的 Hit Rate、Recall@K、MRR 和 nDCG@K。语料文件每行包含
`paper_id`、`title`、`text`；标注文件每行包含 `case_id`、`query` 和
`relevant_paper_ids`。

这组四篇文档的基准只承担快速回归职责，不代表生产检索质量。生产评测应使用冻结的
真实语料快照和人工 relevance judgment，并扩大难负例、跨语言和多相关文档案例。

## Case 契约

每行是一个 JSON 对象，包含：

- `case_id`：稳定且唯一的案例 ID；
- `language`：`en` 或 `zh`；
- `question`：研究问题；
- `expected_status`：`COMPLETED` 或 `NEEDS_REVIEW`；
- `min_papers`、`min_claims`：最低覆盖要求；
- `min_citation_precision`：Claim 引用的 EvidenceCard 是否真实存在；
- `min_citation_coverage`：有引用的 Claim 比例；
- `min_supported_claim_rate`：Verifier 判为 `SUPPORTED` 的比例。

## 当前门禁

可回答案例要求至少两篇论文、两个 Claim，并达到 100% citation precision、citation coverage 和 supported claim rate。无答案案例要求最终状态为 `NEEDS_REVIEW`；允许检索阶段出现弱相关候选，但覆盖度门禁不能让单一来源升级为完成报告。

## 使用边界

离线数据集验证的是确定性管线、不变量和回归稳定性，不代表真实科研质量。接入全文检索后，应加入人工标注的 Recall@K、nDCG、passage entailment、conflict recall 和 limitation capture；接入真实模型后应固定 model、prompt version 和数据快照，并将模型评审与人工抽审结合。

Iteration 4 额外输出 `worker_utilization`，即 `used_workers / max_workers`。它不是越高越好：简单问题应为 0，只有 DEEP 问题才应消耗 Worker 预算。并发回归测试还会测量 provider 的峰值并发，避免“代码看起来 fan-out，实际串行执行”。
