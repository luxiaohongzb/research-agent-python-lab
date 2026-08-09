# Research Agent Python Lab

一个证据优先、预算受控、可核验的 Python 智能科研助理。项目用于学习 Agent 工程、准备面试，也可以继续演进为生产系统。

它不是“让一个模型无限搜索并直接写报告”的 Demo，而是把研究过程拆成可测试的状态图：

```text
Plan → Search → Normalize → Evidence → Coverage Judge
  ↑                                      │
  └──────── bounded refinement loop ─────┘
                                         ↓
Synthesize → Atomic Claims → Verify → Quality Gate
```

## 已实现

- LangGraph 条件工作流、checkpoint 和有上限的补充检索循环
- `ResearchBudget`：限制查询数、论文数、迭代数和工具调用
- `Paper → Passage → EvidenceCard → AtomicClaim → VerificationResult` 可追溯链路
- `SUPPORTED / PARTIAL / CONFLICT / UNSUPPORTED` Claim-level 核验
- 离线可复现语料，以及可选 OpenAlex、Crossref 实时检索
- OpenAI 原生 JSON Schema 结构化 Planner、Extractor、Synthesizer、Verifier
- 每阶段 schema/语义校验、有限重试、确定性 fallback 与失败调用留痕
- 模型、prompt 版本、延迟、token 和可配置费用估算
- 确定性 reasoner，未配置模型也可以运行全部流程和测试
- FastAPI 同步接口、异步任务接口和运行 Trace
- 24 条中英双语 golden cases，以及引用精度、覆盖率、支持率 CI 门禁

## 技术栈

- Python 3.11+
- LangGraph 1.2
- Pydantic 2
- FastAPI
- HTTPX
- Pytest、Ruff、Mypy

## 快速开始

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
python -m pip install -e ".[dev]"
pytest
research-agent-eval datasets/golden.jsonl --min-pass-rate 1.0
research-agent "agentic RAG 如何提高科研综述的可信度"
uvicorn research_agent.api:app --reload
```

打开 `http://127.0.0.1:8000/docs` 查看接口文档。

### 同步执行

```bash
curl -X POST http://127.0.0.1:8000/v1/research/run \
  -H "Content-Type: application/json" \
  -d '{"question":"How does claim-level verification improve research agents?"}'
```

### 异步任务

```bash
curl -X POST http://127.0.0.1:8000/v1/research/runs \
  -H "Content-Type: application/json" \
  -d '{"question":"Compare agentic RAG and one-shot RAG"}'
curl http://127.0.0.1:8000/v1/research/runs/{run_id}
```

## 数据源模式

默认 `offline`，适合 CI 和演示。设置以下变量启用实时元数据搜索：

```bash
RESEARCH_AGENT_PROVIDER_MODE=hybrid
RESEARCH_AGENT_OPENALEX_EMAIL=you@example.com
```

`hybrid` 会并行查询离线语料、OpenAlex 和 Crossref。单个远端源失败不会让整个研究任务失败，错误会进入 Trace。

## 结构化 LLM 模式

离线模式始终是默认值。启用 OpenAI 结构化输出：

```bash
python -m pip install -e ".[openai]"
export OPENAI_API_KEY="..."
export RESEARCH_AGENT_REASONER_MODE=openai
export RESEARCH_AGENT_MODEL=gpt-5-mini
```

Windows PowerShell 使用 `$env:OPENAI_API_KEY="..."` 形式。模型调用采用原生 `json_schema`，所有模型返回值还会经过业务语义校验。单阶段失败会回退确定性 reasoner，并在 `warnings` 和 `model_invocations` 中留痕。

模型价格不会硬编码。需要费用估算时，配置当前模型的每百万 token 价格：

```bash
RESEARCH_AGENT_MODEL_INPUT_COST_PER_MILLION_USD=...
RESEARCH_AGENT_MODEL_OUTPUT_COST_PER_MILLION_USD=...
```

## 目录

```text
src/research_agent/
├── api.py                 FastAPI 与异步任务接口
├── application.py         用例编排与运行存储
├── config.py              环境配置
├── domain.py              科研领域契约
├── eval_cli.py            Golden dataset 质量门禁
├── evaluation.py          分层评测指标
├── llm_reasoner.py         结构化 LLM 与阶段级 fallback
├── prompts.py              版本化、安全边界明确的 prompts
├── providers.py           Offline/OpenAlex/Crossref adapters
├── reasoner.py            可替换推理策略
└── workflow.py            LangGraph 状态图和质量门禁
```

详细设计见 [架构说明](docs/architecture.md)，评测方法见 [评测指南](docs/evaluation.md)，迭代计划见 [路线图](docs/roadmap.md)，面试讲法见 [面试指南](docs/interview-guide.md)。

## 设计原则

1. 来源内容是数据，不是系统指令。
2. 模型只产生结构化领域对象；ID、引用编号和状态迁移由代码负责。
3. 生成与核验分离，证据不足时允许 `NEEDS_REVIEW`。
4. 多 Agent 和搜索轮数都必须由价值、复杂度与预算共同决定。
5. 默认不记录 prompt、全文和工具返回值等敏感内容。

## 当前边界

这是 MVP-2：全文 PDF 解析、pgvector、引用图、多 Worker fan-out、持久化 checkpoint 和人工审批 UI 位于后续路线。离线语料与 golden cases 用于验证架构和回归，不代表真实科学结论。
