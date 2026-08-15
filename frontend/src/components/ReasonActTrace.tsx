import {
  Activity,
  BrainCircuit,
  ChevronDown,
  ChevronUp,
  Eye,
  Radio,
  ShieldCheck,
  Wrench,
} from "lucide-react";
import { useMemo, useState } from "react";
import type { RunEvent, RunStatus } from "../types";

const TERMINAL = new Set<RunStatus>(["COMPLETED", "NEEDS_REVIEW", "FAILED", "CANCELLED"]);

const stageLabels: Record<string, string> = {
  plan: "规划研究问题",
  retrieval: "检索与汇总文献",
  search: "执行多源检索",
  dispatch_workers: "调度研究员",
  research_workers: "并行检索文献",
  research_worker: "研究员检索",
  collect_workers: "汇总 Worker 结果",
  normalize: "文献去重与规范化",
  extract_evidence: "提取可追溯证据",
  assess_coverage: "评估证据覆盖率",
  refine: "补充检索",
  synthesize: "综合研究报告",
  split_claims: "拆分原子声明",
  verify: "逐条核验声明",
  quality_gate: "执行质量门禁",
  finalize: "保存研究结果",
};

const fallbackNarratives: Record<string, { reason: string; action: string }> = {
  plan: {
    reason: "开放式问题需要先拆成可检索、可核验的研究任务。",
    action: "判断问题复杂度并生成子问题与检索计划。",
  },
  search: {
    reason: "研究计划需要可追溯的外部文献或私有全文作为依据。",
    action: "调用当前允许的数据源完成受限检索。",
  },
  normalize: {
    reason: "多来源结果可能重复且格式不同，需要建立统一论文身份。",
    action: "按 DOI 与标题去重，融合排序并保留全文段落。",
  },
  extract_evidence: {
    reason: "结论必须绑定原始段落，不能只依赖模型记忆。",
    action: "从候选段落提取 Evidence Card。",
  },
  assess_coverage: {
    reason: "需要判断当前证据是否足以停止搜索。",
    action: "计算独立来源覆盖率并作出继续或收敛决策。",
  },
  synthesize: {
    reason: "证据达到停止条件后才能进入报告综合。",
    action: "基于证据卡片生成报告和候选声明。",
  },
  verify: {
    reason: "生成文本可能包含不受支持的表达，需要独立复核。",
    action: "将每条声明与绑定段落逐条比对。",
  },
  quality_gate: {
    reason: "交付前必须检查覆盖率、冲突和预算边界。",
    action: "决定完成交付或转入人工复核。",
  },
};

const metricLabels: Record<string, string> = {
  blocked_claims: "阻断声明",
  budget_limits: "预算限制",
  claim_count: "声明",
  complexity: "复杂度",
  compound_claim_count: "复合声明",
  coverage_score: "覆盖率",
  decision: "决策",
  elapsed_ms: "耗时",
  evidence_count: "证据卡",
  iteration: "轮次",
  low_coverage: "低覆盖",
  paper_count: "论文",
  passage_count: "段落",
  retrieval_errors: "检索错误",
  retrieval_lanes: "检索通道",
  search_task_count: "检索任务",
  source_scope: "来源范围",
  sources: "数据源",
  status: "状态",
  sub_question_count: "子问题",
  successful_workers: "成功 Worker",
  unique_sources: "独立来源",
  verification_counts: "核验结果",
  worker_count: "Worker",
  worker_status: "Worker 状态",
};

interface StepTrace {
  reason: string;
  action: string;
  observation: string;
  nextNode: string;
  metrics: Record<string, unknown>;
}

function asRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function stageLabel(node: unknown): string {
  const value = String(node ?? "unknown");
  return stageLabels[value] ?? value;
}

function readStep(event: RunEvent): StepTrace {
  const details = event.details ?? {};
  const node = String(details.node ?? "unknown");
  const raw = asRecord(details.step_trace);
  const fallback = fallbackNarratives[node] ?? {
    reason: "研究工作流需要完成当前受控步骤后才能继续。",
    action: `执行工作流节点 ${stageLabel(node)}。`,
  };
  return {
    reason: typeof raw.reason === "string" ? raw.reason : fallback.reason,
    action: typeof raw.action === "string" ? raw.action : fallback.action,
    observation: typeof raw.observation === "string"
      ? raw.observation
      : event.event === "heartbeat" ? "当前步骤仍在执行。" : "步骤已完成。",
    nextNode: String(raw.next_node ?? details.next_node ?? node),
    metrics: asRecord(raw.metrics),
  };
}

function eventTime(event: RunEvent): string {
  if (!event.occurred_at) return "--:--:--";
  const value = new Date(event.occurred_at);
  if (Number.isNaN(value.getTime())) return "--:--:--";
  return value.toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

function metricValue(key: string, value: unknown): string {
  if (key === "coverage_score" && typeof value === "number") {
    return `${Math.round(value * 100)}%`;
  }
  if (key === "elapsed_ms" && typeof value === "number") return `${value}ms`;
  if (typeof value === "boolean") return value ? "是" : "否";
  if (Array.isArray(value)) return value.length > 0 ? value.join(" · ") : "无";
  if (value !== null && typeof value === "object") {
    return Object.entries(value as Record<string, unknown>)
      .map(([itemKey, itemValue]) => `${itemKey}: ${String(itemValue)}`)
      .join(" · ");
  }
  return String(value);
}

export function ReasonActTrace({ events, status }: { events: RunEvent[]; status: RunStatus }): React.JSX.Element {
  const [expanded, setExpanded] = useState(true);
  const steps = useMemo(() => {
    const completed = events.filter((event) => event.event === "progress");
    if (TERMINAL.has(status)) return completed;
    const heartbeat = [...events].reverse().find((event) => event.event === "heartbeat");
    return heartbeat ? [...completed, heartbeat] : completed;
  }, [events, status]);
  const latestModelStream = useMemo(
    () => [...events].reverse().find((event) => event.event === "model_stream"),
    [events],
  );
  const modelDetails = latestModelStream?.details ?? {};
  const modelPhase = String(modelDetails.phase ?? "delta");
  const modelPreview = typeof modelDetails.preview === "string" ? modelDetails.preview : "";
  const modelRunning = modelPhase === "started" || modelPhase === "delta";

  return (
    <section className="surface reason-act-trace">
      <div className="reason-act-head">
        <div className="reason-act-title">
          <span><BrainCircuit size={18} /></span>
          <div><small>03 · AUDITABLE AGENT TRACE</small><h3>深度研究轨迹</h3></div>
        </div>
        <div className="reason-act-head-actions">
          <span>{steps.length} STEPS</span>
          <button type="button" onClick={() => setExpanded((value) => !value)}>
            {expanded ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
            {expanded ? "收起过程" : "展开过程"}
          </button>
        </div>
      </div>
      {expanded && (
        <div className="reason-act-body">
          <div className="trace-disclosure">
            <ShieldCheck size={15} />
            <p><strong>过程透明说明</strong>这里展示的是由真实节点输入、工具动作和输出生成的决策摘要，不展示模型私有思维链。</p>
          </div>
          {!TERMINAL.has(status) && latestModelStream && (
            <section className={`model-stream-panel ${modelRunning ? "model-stream-panel-live" : ""}`}>
              <header>
                <div><Radio size={14} /><strong>MODEL STREAM · {String(modelDetails.provider ?? "MODEL").toUpperCase()}</strong></div>
                <span>{modelRunning ? "STREAMING" : modelPhase === "completed" ? "SCHEMA VALIDATED" : "RETRYING"}</span>
              </header>
              <div className="model-stream-meta">
                <span>{stageLabel(modelDetails.node)}</span>
                <span>{String(modelDetails.model ?? "structured model")}</span>
                <span>{Number(modelDetails.accumulated_chars ?? modelPreview.length)} CHARS</span>
              </div>
              <pre>{modelPreview || "正在建立安全流式连接……"}<i aria-hidden="true" /></pre>
              <p>实时展示模型最终结构化输出；完整内容聚合后再执行 Schema 与领域校验，不包含私有思维链。</p>
            </section>
          )}
          {steps.length === 0 ? (
            <div className="trace-empty"><Activity size={18} /><span>等待 Planner 产生第一个可审计步骤……</span></div>
          ) : (
            <div className="reason-act-stream">
              {steps.map((event, index) => {
                const node = String(event.details?.node ?? "unknown");
                const step = readStep(event);
                const metrics = Object.entries(step.metrics).slice(0, 6);
                const running = event.event === "heartbeat";
                return (
                  <article className={`reason-act-step ${running ? "reason-act-step-live" : ""}`} key={`${event.sequence ?? index}-${event.event}`}>
                    <div className="trace-step-rail"><span>{String(index + 1).padStart(2, "0")}</span><i /></div>
                    <div className="trace-step-content">
                      <header>
                        <div><strong>{stageLabel(node)}</strong>{running && <span className="live-tag"><i /> LIVE</span>}</div>
                        <time>{eventTime(event)}</time>
                      </header>
                      <div className="reason-act-grid">
                        <section className="trace-reason"><span><BrainCircuit size={14} /> REASON · 决策依据</span><p>{step.reason}</p></section>
                        <section className="trace-action"><span><Wrench size={14} /> ACT · 执行动作</span><p>{step.action}</p></section>
                        <section className="trace-observe"><span><Eye size={14} /> OBSERVE · 节点观察</span><p>{step.observation}</p></section>
                      </div>
                      <footer>
                        <span className="next-node">NEXT · {stageLabel(step.nextNode)}</span>
                        {metrics.map(([key, value]) => (
                          <span className="trace-metric" key={key}>{metricLabels[key] ?? key} · {metricValue(key, value)}</span>
                        ))}
                      </footer>
                    </div>
                  </article>
                );
              })}
            </div>
          )}
        </div>
      )}
    </section>
  );
}
