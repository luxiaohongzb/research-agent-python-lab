import {
  Activity,
  ArrowDown,
  BrainCircuit,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  CircleCheck,
  Eye,
  LoaderCircle,
  Radio,
  Search,
  ShieldCheck,
  Sparkles,
  Terminal,
  Wrench,
  XCircle,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
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
  candidates_considered: "候选",
  candidates_rejected: "已拒绝",
  candidates_selected: "Top-K 入选",
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
  retrieval_queries: "实际查询",
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
    observation: localizeObservation(
      typeof raw.observation === "string"
        ? raw.observation
        : event.event === "heartbeat" ? "当前步骤仍在执行。" : "步骤已完成。",
    ),
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

function localizeObservation(value: string): string {
  const rules: Array<[RegExp, (...parts: string[]) => string]> = [
    [/^Created (\d+) search tasks\.$/, (count) => `已生成 ${count} 个可执行检索任务。`],
    [/^Supervisor dispatched (\d+) bounded research workers\.$/, (count) => `已调度 ${count} 个受预算约束的研究 Worker。`],
    [/^Worker (.+) finished sub-question retrieval with status=(.+)\.$/, (worker, state) => `${worker} 已完成子问题检索 · ${state}`],
    [/^Supervisor merged (\d+)\/(\d+) worker artifacts\.$/, (done, total) => `已汇总 ${done}/${total} 个 Worker 检索产物。`],
    [/^Retained (\d+) unique papers and (\d+) full-text passages\.$/, (papers, passages) => `去重后保留 ${papers} 篇论文与 ${passages} 个全文段落。`],
    [/^Created (\d+) evidence cards\.$/, (count) => `已生成 ${count} 张可追溯证据卡。`],
    [/^Coverage score=([\d.]+); (\d+)\/(\d+) sub-questions covered\.$/, (score, done, total) => `证据覆盖率 ${Math.round(Number(score) * 100)}% · 已覆盖 ${done}/${total} 个子问题。`],
    [/^Drafted a report with (\d+) claims\.$/, (count) => `已综合生成报告草稿与 ${count} 条候选声明。`],
    [/^Validated (\d+) atomic claims\.$/, (count) => `已拆分并校验 ${count} 条原子声明。`],
    [/^Verified (\d+) claims independently\.$/, (count) => `已独立核验 ${count} 条声明。`],
    [/^Final status=(.+); low_coverage=(.+); budget_limits=(.+)\.$/, (state, lowCoverage, limits) => `质量门禁完成 · ${state} · 低覆盖=${lowCoverage} · 预算限制=${limits}`],
  ];
  for (const [pattern, render] of rules) {
    const match = value.match(pattern);
    if (match) return render(...match.slice(1));
  }
  return value;
}

function localizeDecisionReason(value: unknown): string {
  const reason = String(value ?? "无诊断信息");
  if (reason === "selected by relevance-gated Top-K") return "通过相关性门槛并进入 Top-K。";
  if (reason === "relevant candidate ranked below Top-K") return "已通过相关性门槛，但综合排名位于 Top-K 之外。";
  const rejected = reason.match(/^rejected: only (\d+)\/(\d+) required query terms matched$/);
  if (rejected) return `相关词仅命中 ${rejected[1]}/${rejected[2]}，未达到准入门槛。`;
  if (reason === "rejected: no query terms in title and no searchable abstract") {
    return "标题未命中查询词，且没有可检索摘要。";
  }
  return reason;
}

export function ReasonActTrace({ events, status }: { events: RunEvent[]; status: RunStatus }): React.JSX.Element {
  const [expanded, setExpanded] = useState(true);
  const [followLive, setFollowLive] = useState(true);
  const [openSteps, setOpenSteps] = useState<Set<string>>(new Set());
  const scrollRef = useRef<HTMLDivElement>(null);
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
  const activeStep = steps.at(-1);
  const activeNode = String(activeStep?.details?.node ?? "plan");
  const isRunning = !TERMINAL.has(status);
  useEffect(() => {
    if (!expanded || !followLive || !scrollRef.current) return;
    scrollRef.current.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [events, expanded, followLive]);

  useEffect(() => {
    if (!activeStep || TERMINAL.has(status)) return;
    const key = `${activeStep.sequence ?? steps.length - 1}-${activeStep.event}`;
    setOpenSteps((current) => new Set(current).add(key));
  }, [activeStep, status, steps.length]);

  function handleScroll(): void {
    const element = scrollRef.current;
    if (!element) return;
    setFollowLive(element.scrollHeight - element.scrollTop - element.clientHeight < 48);
  }

  function toggleStep(key: string): void {
    setOpenSteps((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  return (
    <section className="surface reason-act-trace deep-trace-shell">
      <div className="reason-act-head">
        <div className="reason-act-title">
          <span className={isRunning ? "trace-orb trace-orb-live" : "trace-orb"}><Sparkles size={18} /></span>
          <div>
            <small>03 · DEEP RESEARCH PROCESS</small>
            <h3>{isRunning ? `正在${stageLabel(activeNode)}` : `已完成 ${steps.length} 个研究步骤`}</h3>
            <p>{isRunning ? "依据真实工具结果持续推进" : "完整执行记录可展开复查"}</p>
          </div>
        </div>
        <div className="reason-act-head-actions">
          <span className={isRunning ? "trace-status trace-status-live" : "trace-status"}>
            <i />{isRunning ? "深度研究中" : status === "COMPLETED" ? "研究完成" : "流程已停止"}
          </span>
          <button
            className={followLive ? "follow-live active" : "follow-live"}
            type="button"
            onClick={() => setFollowLive((value) => !value)}
          >
            <ArrowDown size={15} />{followLive ? "跟随最新" : "恢复跟随"}
          </button>
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
            <p><strong>可审计推演</strong>显示真实查询、工具动作、Top-K 筛选与节点观察；决策文字是结构化摘要，不是模型私有思维链。</p>
          </div>
          <div className="reason-act-scroll" ref={scrollRef} onScroll={handleScroll}>
          {latestModelStream && (
            <section className={`model-stream-panel ${modelRunning ? "model-stream-panel-live" : ""}`}>
              <header>
                <div><Terminal size={14} /><strong>LIVE MODEL OUTPUT · {String(modelDetails.provider ?? "MODEL").toUpperCase()}</strong></div>
                <span>{modelRunning ? "STREAMING" : modelPhase === "completed" ? "SCHEMA VALIDATED" : "RETRYING"}</span>
              </header>
              <div className="model-stream-meta">
                <span>{stageLabel(modelDetails.node)}</span>
                <span>{String(modelDetails.model ?? "structured model")}</span>
                <span>{Number(modelDetails.accumulated_chars ?? modelPreview.length)} CHARS</span>
              </div>
              <pre>{modelPreview || "正在建立安全流式连接……"}<i aria-hidden="true" /></pre>
              <p><Radio size={10} /> 实时输出最终结构化内容，聚合后继续进行 Schema 与证据校验。</p>
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
                const traceDetails = asRecord(event.details?.trace_details);
                const retrievalQueries = Array.isArray(traceDetails.retrieval_queries)
                  ? traceDetails.retrieval_queries.map(String)
                  : typeof traceDetails.retrieval_query === "string"
                    ? [traceDetails.retrieval_query]
                    : [];
                const decisions = Array.isArray(traceDetails.retrieval_decisions)
                  ? traceDetails.retrieval_decisions.map(asRecord)
                  : [];
                const stepKey = `${event.sequence ?? index}-${event.event}`;
                const isOpen = openSteps.has(stepKey) || running;
                return (
                  <article className={`reason-act-step ${running ? "reason-act-step-live" : ""} ${isOpen ? "reason-act-step-open" : ""}`} key={stepKey}>
                    <div className="trace-step-rail">
                      <span>{running ? <LoaderCircle size={14} /> : <CircleCheck size={14} />}</span><i />
                    </div>
                    <div className="trace-step-content">
                      <button className="trace-step-summary" type="button" onClick={() => toggleStep(stepKey)} aria-expanded={isOpen}>
                        <span className="trace-step-copy">
                          <span><strong>{stageLabel(node)}</strong>{running && <span className="live-tag"><i /> LIVE</span>}</span>
                          <small>{step.observation}</small>
                        </span>
                        <span className="trace-step-meta"><time>{eventTime(event)}</time>{isOpen ? <ChevronUp size={15} /> : <ChevronDown size={15} />}</span>
                      </button>
                      {isOpen && <div className="trace-step-detail">
                        <div className="reason-act-grid">
                          <section className="trace-reason"><span><BrainCircuit size={14} /> 判断依据</span><p>{step.reason}</p></section>
                          <section className="trace-action"><span><Wrench size={14} /> 执行动作</span><p>{step.action}</p></section>
                          <section className="trace-observe"><span><Eye size={14} /> 观察结果</span><p>{step.observation}</p></section>
                        </div>
                      {(retrievalQueries.length > 0 || decisions.length > 0) && (
                        <section className="retrieval-audit">
                          <header><Search size={14} /><strong>RETRIEVAL AUDIT · 真实检索与 Top-K 筛选</strong></header>
                          {retrievalQueries.map((query, queryIndex) => (
                            <code key={`${query}-${queryIndex}`}>{query}</code>
                          ))}
                          {decisions.length > 0 && (
                            <div className="candidate-list">
                              {decisions.map((decision, decisionIndex) => {
                                const selected = Boolean(decision.selected);
                                const accepted = Boolean(decision.accepted);
                                return (
                                  <article className={selected ? "candidate-selected" : "candidate-rejected"} key={`${String(decision.paper_id)}-${decisionIndex}`}>
                                    {selected ? <CheckCircle2 size={15} /> : <XCircle size={15} />}
                                    <div>
                                      <strong>{String(decision.title ?? "未命名候选")}</strong>
                                      <small>
                                        {selected ? "TOP-K 入选" : accepted ? "相关但排在 Top-K 外" : "相关性门槛拒绝"}
                                        {` · relevance ${Number(decision.relevance_score ?? 0).toFixed(3)}`}
                                        {` · ${String(decision.source ?? "unknown")}`}
                                      </small>
                                      <p>{localizeDecisionReason(decision.reason)}</p>
                                    </div>
                                  </article>
                                );
                              })}
                            </div>
                          )}
                        </section>
                      )}
                      <footer>
                        <span className="next-node">NEXT · {stageLabel(step.nextNode)}</span>
                        {metrics.map(([key, value]) => (
                          <span className="trace-metric" key={key}>{metricLabels[key] ?? key} · {metricValue(key, value)}</span>
                        ))}
                      </footer>
                      </div>}
                    </div>
                  </article>
                );
              })}
            </div>
          )}
          </div>
        </div>
      )}
    </section>
  );
}
