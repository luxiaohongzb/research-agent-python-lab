# 模型 Provider 接入

项目默认使用 `deterministic`，无需密钥即可运行。OpenAI 与 DeepSeek 只替换规划、证据提取、综合和核验 Reasoner，不改变检索、预算、引用白名单、质量门禁和阶段级 fallback。

## DeepSeek

在仓库根目录创建或修改 `.env`：

```dotenv
RESEARCH_AGENT_REASONER_MODE=deepseek
DEEPSEEK_API_KEY=sk-replace-with-real-key
RESEARCH_AGENT_DEEPSEEK_MODEL=deepseek-v4-pro
RESEARCH_AGENT_DEEPSEEK_BASE_URL=https://api.deepseek.com
RESEARCH_AGENT_MODEL_TIMEOUT_SECONDS=120
RESEARCH_AGENT_MODEL_MAX_RETRIES=3
RESEARCH_AGENT_MODEL_MAX_CONCURRENCY=3
RESEARCH_AGENT_PROGRESS_HEARTBEAT_SECONDS=10
```

推荐模型：

- `deepseek-v4-pro`：优先科研推理和核验质量；
- `deepseek-v4-flash`：优先响应速度和演示成本。

DeepSeek 使用 OpenAI 兼容的流式 Chat Completions，不使用 OpenAI Responses API。请求发送 `stream=true` 和 `response_format={"type":"json_object"}`，模型最终输出 Token 会通过运行 SSE 的 `model_stream` 事件增量推送到工作台；服务端同时聚合完整 JSON，再执行 Pydantic 和领域语义规则二次校验。失败会有限重试，再回退确定性 Reasoner，并在 `model_invocations` 和 `warnings` 中留痕。

工作台展示的是可交付的结构化输出流，不读取或转发供应商扩展字段中的 `reasoning_content`。这样可以提供实时反馈，同时避免把模型私有思维链、系统提示词或安全策略暴露到前端。

证据提取和声明核验按 `RESEARCH_AGENT_MODEL_MAX_CONCURRENCY` 受控并发，默认最多 3 路；每个批次结束后统一扣减 Token/费用预算，因此最大预算漂移被限制在一个并发批次内。所有模型阶段同时受研究任务的 `max_elapsed_seconds` 全局截止时间约束，超时后使用确定性 fallback 完成可交付结果。异步任务每隔 `RESEARCH_AGENT_PROGRESS_HEARTBEAT_SECONDS` 秒发布当前阶段与阶段耗时，工作台据此显示后台仍在运行。

启动或切换配置：

```bash
docker compose up -d --build --force-recreate api worker
```

密钥只放在 `.env` 或生产 Secret Manager，不写入 React、Compose 文件、运行结果、日志或 Git。官方参考：[模型列表](https://api-docs.deepseek.com/api/list-models)、[JSON Output](https://api-docs.deepseek.com/zh-cn/guides/json_mode/)。

## OpenAI

```dotenv
RESEARCH_AGENT_REASONER_MODE=openai
OPENAI_API_KEY=replace-with-real-key
RESEARCH_AGENT_MODEL=gpt-5-mini
```

OpenAI 路径继续使用 Responses API 与原生严格 JSON Schema。

## 费用留痕

模型价格不会硬编码。需要估算时，根据供应商当前价格设置：

```dotenv
RESEARCH_AGENT_MODEL_INPUT_COST_PER_MILLION_USD=...
RESEARCH_AGENT_MODEL_OUTPUT_COST_PER_MILLION_USD=...
```

如果通过 Docker 运行，还需把这两个可选变量加入部署环境或 Secret/Config 管理系统；未配置时仍记录 Token，但不估算费用。
