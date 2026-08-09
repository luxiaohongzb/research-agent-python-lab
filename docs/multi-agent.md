# 自适应多 Agent 最佳实践

## 执行模型

```text
Planner → Complexity Router
             ├─ SIMPLE → 原单图检索
             └─ DEEP → Supervisor → Send(worker × N)
                                      │
                                      ├─ RetrievalBatch → Artifact Store
                                      └─ WorkerResult → Artifact ID only
                                  Supervisor Merge → Evidence/Verify
```

多 Agent 不是默认模式。只有问题能拆成独立证据方向，并且剩余预算允许时才 fan-out。当前确定性 Router 使用比较、综述等意图词和问题词项数量；结构化 LLM Planner 也必须输出同一 `Complexity` 契约。

## 为什么 Worker 不直接返回论文全文

并行节点如果同时写 `papers/passages`，既容易触发 state channel 冲突，也会把相同内容复制到 checkpoint 和后续模型上下文。当前 Worker 把 `RetrievalBatch` 写到 run-scoped Artifact Store，只返回小型引用：

```json
{
  "worker_id": "worker-1-...",
  "task_id": "search-...",
  "status": "COMPLETED",
  "artifact_ref": {"artifact_id": "artifact-...", "run_id": "..."},
  "elapsed_ms": 53
}
```

Supervisor Merge 是唯一能把 Worker 结果合并进主状态的节点。Artifact Store 校验 `run_id`，防止跨运行读取。

## 预算语义

每次请求可设置：

- `max_workers`：本次运行最多派发的 Worker 总数，范围 1–5；
- `max_total_tokens`：所有结构化模型阶段累计 token；
- `max_cost_usd`：按配置模型价格累计的估算费用；
- `max_elapsed_seconds`：整次运行墙钟时间；
- 原有 query、tool call、paper 和 iteration 上限。

Supervisor 在调用前限制 Worker 数；模型 token/费用在调用完成后按 `ModelInvocation` 记账。达到 token、费用或时间上限时，不再安排补充搜索，最终结果进入 `NEEDS_REVIEW`，而不是静默突破预算。

## 故障边界

- 每个 Worker 同时受 `asyncio.timeout` 和带宽限期的 LangGraph Send watchdog 约束；内层先返回结构化 `TIMED_OUT`，外层只兜底失去响应的节点；
- Worker 异常转换为 `FAILED/TIMED_OUT`，不会取消其他 Worker；
- 成功 Artifact 仍会进入后续证据链；
- Worker 状态、耗时和错误类型进入 `ResearchResult.workers` 与 Trace；
- 外部 provider 的局部失败仍由 Composite Provider 降级为 warning。

## 面试演示建议

使用包含 `compare` 或“综述”的问题，观察返回结果中的：

1. Trace 出现 `dispatch_workers` 和 `collect_workers`；
2. `workers` 数量不超过请求的 `max_workers`；
3. 每个 Worker 只有 ArtifactRef，没有论文正文；
4. `budget.used_workers/used_queries/elapsed_ms` 与执行一致；
5. 简单问题的 Trace 不出现 Supervisor 节点。

## 生产演进

- Artifact Store 迁移到 PostgreSQL + 对象存储，并设置 TTL、租户隔离和引用计数；
- checkpoint 迁移到 PostgreSQL，支持恢复、取消和幂等；
- 用历史运行数据训练/评测 Complexity Router；
- 为不同 Worker 设置角色化检索策略，而不是复制相同 prompt；
- 添加全局 semaphore、队列背压、分布式 tracing 和成本熔断。
