# 分布式运行指南

Iteration 6 把异步研究任务从 FastAPI 进程中拆出，使 API、Worker 和 SSE 订阅者可以独立
重启和扩容。默认内存实现仍保留；只有显式启用 Redis/S3 模式时才需要外部基础设施。

## 组件职责

| 组件 | 持有的数据 | 一致性职责 |
|---|---|---|
| PostgreSQL Run Store | 请求、状态、结果、审阅、幂等键 | API 聚合与唯一约束 |
| LangGraph PostgreSQL Saver | 节点 checkpoint | 中断后从同一 thread 恢复 |
| Redis Run Queue | 待执行 run_id、resume 标记 | consumer group 至少一次投递 |
| Redis Event Stream | queued/progress/terminal 事件 | 跨 API 副本重放与实时 SSE |
| Redis Cancellation | 有 TTL 的取消令牌 | Worker 安全点协作取消 |
| Redis Execution Lease | run_id → Worker owner | 抑制重复消息并发执行 |
| S3/MinIO | RetrievalBatch Artifact | 跨进程、跨重启的大对象交接 |

Run Store 和 checkpoint 不可互换，Redis 也不是最终结果数据库。每个组件只承担一种恢复
语义，故障排查时可以明确判断是“业务快照丢失”“图进度丢失”还是“消息正在重投”。

## 投递与故障恢复

1. API 先用数据库唯一键创建 `PENDING` 快照，再向 Redis Stream `XADD`。
2. Worker 使用 `XREADGROUP` 获取消息，并以 run ID 获取带 TTL 的执行租约。
3. 执行期间每隔租期的三分之一续约，节点 checkpoint 持续写 PostgreSQL。
4. 成功、失败或业务取消写入终态后，Worker `XACK` 并删除队列消息。
5. Worker 崩溃时消息停留在 pending；超过租期后其他 Worker 用 `XAUTOCLAIM` 回收。
6. 回收者发现快照为 `RUNNING` 时，用同一 `thread_id` 调用 resume；发现终态则直接确认。

这个协议提供至少一次投递。网络分区可能导致已完成消息再次出现，因此代码必须先检查
run 终态，并用租约避免两个 Worker 同时执行。对于外部不可逆 side effect，应再使用
transactional outbox/inbox 或向下游传递 run 级幂等键。

## 取消与恢复

API 收到取消请求后立即把快照改为 `CANCELLED`，同时写入有 TTL 的 Redis key。Worker 在
LangGraph 节点进度回调和提交最终结果前检查令牌；检测到后停止后续节点，并保留最近
checkpoint。恢复接口清除取消令牌、把快照改回 `PENDING`，然后重新入队 `resume=1`。

取消不是任意时刻杀线程。正在进行的 HTTP/数据库调用仍由自身 timeout 控制，完成后在
安全点停止，这可以避免连接池或外部写入处于未知状态。

## Artifact 格式与隔离

S3 key 为：

```text
{prefix}/{run_id}/{artifact_id}.json
```

对象包含 `schema_version`、完整 `ArtifactRef` 和按类型编码的 payload。当前只接受
`RETRIEVAL_BATCH`，Paper、Passage 和 RetrievalHit 都经 Pydantic JSON 模式序列化；不使用
pickle。读取时同时校验请求中的 run ID、对象内 run ID 和 artifact ID。Store 提供按 run
分页计数及最多 1000 个对象一批的清理原语。

## 扩缩容与观测

Worker 没有本地任务身份，增加副本后使用同一个 consumer group 即可。每个 Worker 使用
主机名和随机后缀作为 consumer name，并在 `9100/metrics` 暴露 Prometheus 指标。API
继续在 `/metrics/` 暴露 HTTP 进程指标；OpenTelemetry service name 对 Worker 自动增加
`-worker` 后缀。

常用检查：

```bash
docker compose ps
docker compose logs -f worker
docker compose exec redis redis-cli XINFO GROUPS research-agent:queue:runs
docker compose exec redis redis-cli XPENDING research-agent:queue:runs research-workers
```

## 本地集成验证

```powershell
$env:POSTGRES_TEST_DSN="postgresql://research:research@127.0.0.1:5432/research_agent"
$env:REDIS_TEST_URL="redis://127.0.0.1:6379/0"
$env:S3_TEST_ENDPOINT_URL="http://127.0.0.1:9000"
pytest tests/test_postgres_runtime.py tests/test_redis_runtime.py tests/test_artifacts_s3.py
```

CI 使用真实 PostgreSQL/pgvector、Redis 和 MinIO，覆盖 checkpoint、队列、事件、取消令牌、
租约、独立 Worker 消费以及 S3 Artifact 往返。
