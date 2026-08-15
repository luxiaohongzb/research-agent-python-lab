import {
  ArrowRight,
  BookMarked,
  Check,
  CircleStop,
  Clock3,
  Download,
  FileCheck2,
  FlaskConical,
  Gauge,
  Lightbulb,
  RefreshCw,
  Search,
  Sparkles,
  Users,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { downloadExport, researchApi } from "../api";
import { useApp } from "../app-context";
import type { ResearchRequest, RunEvent, RunSnapshot, RunStatus } from "../types";

const TERMINAL = new Set<RunStatus>(["COMPLETED", "NEEDS_REVIEW", "FAILED", "CANCELLED"]);
const RUN_KEY = "atlas-active-run";
const examples = [
  "系统梳理大语言模型科研智能体的关键技术路线、评估方法与主要局限。",
  "比较 Agentic RAG 与传统 RAG 在复杂科研任务中的可靠性及评估方法。",
  "分析多智能体协作在科学发现中的应用证据、风险与未来方向。",
];

const eventLabels: Record<string, string> = {
  queued: "任务进入研究队列",
  started: "研究流程已经启动",
  progress: "研究阶段向前推进",
  heartbeat: "当前阶段仍在运行",
  completed: "研究报告生成完成",
  failed: "研究执行遇到错误",
  cancelled: "研究任务已取消",
  resuming: "正在从检查点恢复",
  reviewed: "人工审核已记录",
};

const stageLabels: Record<string, string> = {
  queued: "等待 Worker 接单",
  plan: "规划研究问题",
  retrieval: "检索与汇总文献",
  search: "检索文献",
  research_workers: "并行检索文献",
  dispatch_workers: "调度研究员",
  research_worker: "研究员检索",
  collect_workers: "汇总检索结果",
  normalize: "文献去重与规范化",
  extract_evidence: "并行提取证据",
  assess_coverage: "评估证据覆盖率",
  refine: "补充检索",
  synthesize: "综合研究报告",
  split_claims: "拆分原子声明",
  verify: "并行核验声明",
  quality_gate: "执行质量门禁",
  finalize: "保存研究结果",
};

const stageProgress: Record<string, number> = {
  queued: 12,
  plan: 15,
  retrieval: 28,
  search: 28,
  research_workers: 32,
  dispatch_workers: 25,
  research_worker: 32,
  collect_workers: 38,
  normalize: 44,
  extract_evidence: 62,
  assess_coverage: 68,
  refine: 36,
  synthesize: 78,
  split_claims: 82,
  verify: 90,
  quality_gate: 96,
  finalize: 98,
};

function stageLabel(node: unknown): string {
  const value = String(node ?? "starting");
  return stageLabels[value] ?? value;
}

function statusLabel(status: RunStatus): string {
  return {
    PENDING: "等待执行",
    RUNNING: "研究中",
    COMPLETED: "已完成",
    NEEDS_REVIEW: "需要复核",
    FAILED: "执行失败",
    CANCELLED: "已取消",
  }[status];
}

function eventDetail(event: RunEvent): string {
  const details = event.details ?? {};
  if (event.event === "heartbeat") {
    return `${stageLabel(details.node)} · 阶段持续 ${Number(details.stage_elapsed_seconds ?? 0)}s`;
  }
  if (event.event === "progress" && details.node) {
    return `${stageLabel(details.node)}已完成`;
  }
  return String(details.status ?? details.error_type ?? "状态已经记录");
}

export function ResearchPage(): React.JSX.Element {
  const { notify, openAccessKey } = useApp();
  const [question, setQuestion] = useState("");
  const [showBudget, setShowBudget] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [snapshot, setSnapshot] = useState<RunSnapshot | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [resultTab, setResultTab] = useState<"report" | "claims" | "sources">("report");
  const [budget, setBudget] = useState({ max_papers: 8, max_iterations: 2, max_workers: 3, max_cost_usd: 5 });
  const poller = useRef<number | null>(null);
  const startedAt = useRef<number | null>(null);
  const [elapsed, setElapsed] = useState(0);

  const stopPolling = (): void => {
    if (poller.current) window.clearInterval(poller.current);
    poller.current = null;
  };

  const refresh = async (runId: string): Promise<void> => {
    try {
      const next = await researchApi.get(runId);
      setSnapshot(next);
      if (TERMINAL.has(next.status)) {
        stopPolling();
        setElapsed(
          next.result?.budget?.elapsed_ms
            ? Math.round(next.result.budget.elapsed_ms / 1000)
            : Math.max(0, Math.round((Date.now() - Date.parse(next.created_at ?? new Date().toISOString())) / 1000)),
        );
      }
    } catch (error) {
      stopPolling();
      notify(error instanceof Error ? error.message : "无法读取任务状态", "error");
    }
  };

  const beginTracking = (run: RunSnapshot): void => {
    setSnapshot(run);
    setQuestion((current) => current || run.request?.question || "");
    sessionStorage.setItem(RUN_KEY, run.run_id);
    startedAt.current = run.created_at ? Date.parse(run.created_at) : Date.now();
    setElapsed(Math.max(0, Math.floor((Date.now() - startedAt.current) / 1000)));
    stopPolling();
    poller.current = window.setInterval(() => void refresh(run.run_id), 1200);
    void researchApi.stream(run.run_id, (event) => {
      setEvents((current) => {
        const key = `${event.sequence ?? ""}:${event.event}:${eventDetail(event)}`;
        if (current.some((item) => `${item.sequence ?? ""}:${item.event}:${eventDetail(item)}` === key)) return current;
        const last = current.at(-1);
        if (event.event === "heartbeat" && last?.event === "heartbeat" && last.details?.node === event.details?.node) {
          return [...current.slice(0, -1), event];
        }
        return [...current, event].slice(-12);
      });
    }).catch((error: unknown) => notify(error instanceof Error ? `实时事件：${error.message}` : "实时事件连接中断", "error"));
  };

  useEffect(() => {
    const runId = sessionStorage.getItem(RUN_KEY);
    if (runId) void researchApi.get(runId).then(beginTracking).catch(() => sessionStorage.removeItem(RUN_KEY));
    return stopPolling;
    // Restore the tab-local active run only once.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const ticker = window.setInterval(() => {
      if (startedAt.current && snapshot && !TERMINAL.has(snapshot.status)) {
        setElapsed(Math.floor((Date.now() - startedAt.current) / 1000));
      }
    }, 1000);
    return () => window.clearInterval(ticker);
  }, [snapshot]);

  async function submit(event: React.FormEvent): Promise<void> {
    event.preventDefault();
    if (question.trim().length < 5) return;
    setSubmitting(true);
    setEvents([]);
    try {
      const payload: ResearchRequest = { question: question.trim(), ...budget };
      const run = await researchApi.submit(payload);
      beginTracking(run);
      notify("研究任务已创建", "success");
    } catch (error) {
      if (error instanceof Error && /401|Bearer|key/i.test(error.message)) openAccessKey();
      notify(error instanceof Error ? error.message : "研究任务创建失败", "error");
    } finally {
      setSubmitting(false);
    }
  }

  async function cancel(): Promise<void> {
    if (!snapshot) return;
    try {
      setSnapshot(await researchApi.cancel(snapshot.run_id));
      stopPolling();
      notify("任务已取消", "info");
    } catch (error) {
      notify(error instanceof Error ? error.message : "取消失败", "error");
    }
  }

  async function resume(): Promise<void> {
    if (!snapshot) return;
    try {
      beginTracking(await researchApi.resume(snapshot.run_id));
      notify("任务正在恢复", "success");
    } catch (error) {
      notify(error instanceof Error ? error.message : "恢复失败", "error");
    }
  }

  async function exportResult(kind: "bibtex" | "csl-json"): Promise<void> {
    if (!snapshot) return;
    try {
      await downloadExport(snapshot.run_id, kind);
    } catch (error) {
      notify(error instanceof Error ? error.message : "导出失败", "error");
    }
  }

  const activeStage = useMemo(() => {
    for (let index = events.length - 1; index >= 0; index -= 1) {
      const event = events[index];
      if ((event.event === "heartbeat" || event.event === "progress") && event.details?.node) {
        return {
          node: String(event.event === "progress" ? event.details.next_node ?? event.details.node : event.details.node),
          stageElapsed: Number(event.details.stage_elapsed_seconds ?? 0),
        };
      }
    }
    return { node: snapshot?.status === "PENDING" ? "queued" : "plan", stageElapsed: elapsed };
  }, [elapsed, events, snapshot?.status]);

  const progress = useMemo(() => {
    if (!snapshot) return 0;
    if (TERMINAL.has(snapshot.status)) return 100;
    if (snapshot.status === "PENDING") return 12;
    return stageProgress[activeStage.node] ?? 18;
  }, [activeStage.node, snapshot]);
  const result = snapshot?.result;
  const supportedRate = result
    ? Math.round((result.verifications.filter((item) => item.status === "SUPPORTED").length / Math.max(1, result.verifications.length)) * 100)
    : 0;

  return (
    <div className="research-page">
      <section className="page-intro research-intro">
        <div>
          <p className="eyebrow"><Sparkles size={13} /> EVIDENCE-FIRST INTELLIGENCE</p>
          <h2>把一个问题，推进到<br /><em>经得起追问的结论。</em></h2>
          <p>Atlas 会规划检索、整理证据并逐条核验声明。证据不足时，它会明确停下来，而不是补写一个听起来合理的答案。</p>
        </div>
        <div className="intro-index">
          <span>01</span>
          <p>PLAN · RETRIEVE · VERIFY</p>
        </div>
      </section>

      <div className="research-grid">
        <form className="surface research-composer" onSubmit={submit}>
          <div className="surface-head">
            <div><span className="section-number">01</span><h3>定义研究问题</h3></div>
            <span className="surface-meta">RESEARCH BRIEF</span>
          </div>
          <label className="sr-only" htmlFor="research-question">研究问题</label>
          <textarea
            id="research-question"
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            minLength={5}
            maxLength={2000}
            required
            placeholder="描述你希望调查、比较或验证的科研问题……"
          />
          <div className="prompt-list" aria-label="研究问题示例">
            {examples.map((example, index) => (
              <button key={example} type="button" onClick={() => setQuestion(example)}>
                <span>0{index + 1}</span>{example}
              </button>
            ))}
          </div>
          <button className="budget-toggle" type="button" aria-expanded={showBudget} onClick={() => setShowBudget((value) => !value)}>
            <span><Gauge size={16} /> 研究预算与边界</span>
            <span>{showBudget ? "收起 −" : "调整 +"}</span>
          </button>
          {showBudget && (
            <div className="budget-grid">
              <BudgetInput label="论文上限" value={budget.max_papers} min={1} max={50} onChange={(value) => setBudget({ ...budget, max_papers: value })} />
              <BudgetInput label="检索轮次" value={budget.max_iterations} min={1} max={5} onChange={(value) => setBudget({ ...budget, max_iterations: value })} />
              <BudgetInput label="并行研究员" value={budget.max_workers} min={1} max={5} onChange={(value) => setBudget({ ...budget, max_workers: value })} />
              <BudgetInput label="费用上限 ($)" value={budget.max_cost_usd} min={0.01} max={1000} step={0.01} onChange={(value) => setBudget({ ...budget, max_cost_usd: value })} />
            </div>
          )}
          <div className="composer-footer">
            <p><Check size={14} /> 可恢复执行 · Claim 级核验 · 引用可导出</p>
            <button className="button button-accent button-large" type="submit" disabled={submitting || question.trim().length < 5}>
              {submitting ? <><RefreshCw className="spin" size={17} /> 正在创建</> : <>开始研究 <ArrowRight size={18} /></>}
            </button>
          </div>
        </form>

        <aside className="surface run-monitor">
          <div className="surface-head surface-head-dark">
            <div><span className="section-number">02</span><h3>研究进度</h3></div>
            <span className="surface-meta">LIVE TRACE</span>
          </div>
          {!snapshot ? (
            <div className="monitor-empty">
              <div className="radar"><i /><span /></div>
              <h3>等待研究任务</h3>
              <p>提交问题后，检索、推理与证据核验会在这里留下实时轨迹。</p>
            </div>
          ) : (
            <div className="monitor-live">
              <div className="run-status-row">
                <span className={`status-badge status-${snapshot.status.toLowerCase()}`}><i />{statusLabel(snapshot.status)}</span>
                <span>{TERMINAL.has(snapshot.status) ? snapshot.run_id.slice(0, 10) : `已运行 ${elapsed}s`}</span>
              </div>
              <div className="run-progress"><i style={{ width: `${progress}%` }} /></div>
              <div className="run-question">{question || snapshot.request?.question || "正在恢复上一次研究任务"}</div>
              {!TERMINAL.has(snapshot.status) && (
                <div className="run-stage" aria-live="polite">
                  <span className="stage-pulse" />
                  <div><strong>{stageLabel(activeStage.node)}</strong><small>{activeStage.stageElapsed > 0 ? `阶段持续 ${activeStage.stageElapsed}s` : "阶段刚刚开始"} · 后台仍在工作</small></div>
                </div>
              )}
              <ol className="event-list" aria-live="polite">
                {events.length === 0 && <li><i /><div><strong>正在连接执行轨迹</strong><span>任务 {snapshot.run_id.slice(0, 12)}</span></div></li>}
                {events.map((item, index) => (
                  <li key={`${item.sequence ?? index}-${item.event}`}><i /><div><strong>{eventLabels[item.event] ?? item.event}</strong><span>{eventDetail(item)}</span></div></li>
                ))}
              </ol>
              <div className="monitor-actions">
                {["PENDING", "RUNNING"].includes(snapshot.status) && <button className="button button-dark-outline" type="button" onClick={() => void cancel()}><CircleStop size={16} />取消任务</button>}
                {["FAILED", "CANCELLED"].includes(snapshot.status) && <button className="button button-dark-outline" type="button" onClick={() => void resume()}><RefreshCw size={16} />恢复任务</button>}
              </div>
            </div>
          )}
        </aside>
      </div>

      {result && (
        <section className="result-section">
          <div className="result-title-row">
            <div><p className="eyebrow">VERIFIED RESEARCH OUTPUT</p><h2>{result.report.title}</h2></div>
            <div className="export-row">
              <button className="button button-ghost" type="button" onClick={() => void exportResult("bibtex")}><Download size={16} /> BibTeX</button>
              <button className="button button-dark" type="button" onClick={() => void exportResult("csl-json")}><Download size={16} /> CSL JSON</button>
            </div>
          </div>
          <div className="metric-grid">
            <Metric icon={<BookMarked size={18} />} value={result.papers.length} label="论文来源" />
            <Metric icon={<FileCheck2 size={18} />} value={result.evidence.length} label="证据卡片" />
            <Metric icon={<Lightbulb size={18} />} value={result.claims.length} label="原子声明" />
            <Metric icon={<FlaskConical size={18} />} value={`${supportedRate}%`} label="声明支持率" accent />
          </div>
          <div className="result-card surface">
            <div className="result-tabs" role="tablist" aria-label="研究结果视图">
              <button className={resultTab === "report" ? "active" : ""} role="tab" type="button" onClick={() => setResultTab("report")}>研究报告</button>
              <button className={resultTab === "claims" ? "active" : ""} role="tab" type="button" onClick={() => setResultTab("claims")}>声明核验 <span>{result.claims.length}</span></button>
              <button className={resultTab === "sources" ? "active" : ""} role="tab" type="button" onClick={() => setResultTab("sources")}>论文来源 <span>{result.papers.length}</span></button>
            </div>
            {resultTab === "report" && <article className="report-content"><pre>{result.report.markdown}</pre></article>}
            {resultTab === "claims" && (
              <div className="claim-grid">
                {result.claims.map((claim, index) => {
                  const verification = result.verifications.find((item) => item.claim_id === claim.claim_id);
                  return <article className="claim-card" key={claim.claim_id}><span className="claim-index">{String(index + 1).padStart(2, "0")}</span><div><span className={`verification verification-${verification?.status.toLowerCase()}`}>{verification?.status ?? "UNCHECKED"}</span><p>{claim.text}</p><small>{verification ? `置信度 ${Math.round(verification.confidence * 100)}% · ${claim.evidence_ids.length} 条证据` : `${claim.evidence_ids.length} 条证据`}</small></div></article>;
                })}
              </div>
            )}
            {resultTab === "sources" && (
              <div className="source-list">
                {result.papers.map((paper, index) => <article key={paper.paper_id}><span>{String(index + 1).padStart(2, "0")}</span><div><h4>{paper.title}</h4><p>{paper.authors?.join(" · ") || "作者信息暂缺"}{paper.year ? ` · ${paper.year}` : ""}</p><small>{paper.source}{paper.doi ? ` · DOI ${paper.doi}` : ""}</small></div></article>)}
              </div>
            )}
          </div>
        </section>
      )}

      {!result && (
        <section className="trust-strip" aria-label="系统能力">
          <div><Search size={18} /><span><strong>多源检索</strong>OpenAlex · Crossref · MCP</span></div>
          <div><Users size={18} /><span><strong>自适应协作</strong>复杂问题才启用多 Agent</span></div>
          <div><FileCheck2 size={18} /><span><strong>逐条核验</strong>每个 Claim 绑定原始证据</span></div>
          <div><Clock3 size={18} /><span><strong>预算可控</strong>轮次、时间与费用均设上限</span></div>
        </section>
      )}
    </div>
  );
}

function BudgetInput({ label, value, min, max, step = 1, onChange }: { label: string; value: number; min: number; max: number; step?: number; onChange: (value: number) => void }): React.JSX.Element {
  return <label><span>{label}</span><input type="number" value={value} min={min} max={max} step={step} onChange={(event) => onChange(Number(event.target.value))} /></label>;
}

function Metric({ icon, value, label, accent = false }: { icon: React.ReactNode; value: string | number; label: string; accent?: boolean }): React.JSX.Element {
  return <div className={`metric-card ${accent ? "metric-accent" : ""}`}><span>{icon}</span><strong>{value}</strong><small>{label}</small></div>;
}
