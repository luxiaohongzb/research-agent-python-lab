import {
  Activity,
  AlertTriangle,
  Bot,
  CheckCircle2,
  CircleDollarSign,
  FileCheck2,
  KeyRound,
  Radio,
  RefreshCw,
  ScrollText,
  ServerCog,
  ShieldCheck,
  Users,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { adminApi, ApiError } from "../api";
import { useApp } from "../app-context";
import type { AuditEvent, DeadLetter, McpServerStatus, RuntimeSummary } from "../types";

interface AdminData {
  summary: RuntimeSummary;
  audit: AuditEvent[];
  deadLetters: DeadLetter[];
  mcp: McpServerStatus[];
}

const emptySummary: RuntimeSummary = {
  total_runs: 0,
  active_runs: 0,
  status_counts: {},
  total_papers: 0,
  total_claims: 0,
  total_workers: 0,
  estimated_cost_usd: 0,
};

const actionLabels: Record<string, string> = {
  "research.submitted": "提交研究任务",
  "research.cancelled": "取消研究任务",
  "research.resumed": "恢复研究任务",
  "research.reviewed": "记录人工审核",
};

export function AdminPage(): React.JSX.Element {
  const { openAccessKey } = useApp();
  const [data, setData] = useState<AdminData>({ summary: emptySummary, audit: [], deadLetters: [], mcp: [] });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<"audit" | "deadletters">("audit");

  const load = useCallback(async (): Promise<void> => {
    setLoading(true);
    setError(null);
    try {
      const [summary, audit, deadLetters, mcp] = await Promise.all([
        adminApi.summary(), adminApi.audit(), adminApi.deadLetters(), adminApi.mcp(),
      ]);
      setData({ summary, audit, deadLetters, mcp });
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : "无法读取管理数据";
      setError(message);
      if (caught instanceof ApiError && [401, 403].includes(caught.status)) openAccessKey();
    } finally {
      setLoading(false);
    }
  }, [openAccessKey]);

  useEffect(() => { void load(); }, [load]);

  const statusTotal = useMemo(
    () => Math.max(1, Object.values(data.summary.status_counts).reduce((sum, count) => sum + count, 0)),
    [data.summary.status_counts],
  );
  const healthyMcp = data.mcp.filter((server) => server.connected && server.configured_tool_available).length;

  return (
    <div className="admin-page">
      <section className="admin-hero">
        <div>
          <p className="eyebrow"><ShieldCheck size={13} /> GOVERNANCE & OPERATIONS</p>
          <h2>系统运行，一目了然。</h2>
          <p>集中查看研究吞吐、证据产出、外部工具连接和治理事件。</p>
        </div>
        <button className="button button-dark" type="button" disabled={loading} onClick={() => void load()}>
          <RefreshCw className={loading ? "spin" : ""} size={16} /> 刷新数据
        </button>
      </section>

      {error && (
        <div className="admin-error">
          <span><KeyRound size={19} /></span>
          <div><strong>需要管理员访问权限</strong><p>{error}</p></div>
          <button className="button button-ghost" type="button" onClick={openAccessKey}>配置密钥</button>
        </div>
      )}

      <section className={`admin-metrics ${loading ? "loading" : ""}`}>
        <AdminMetric icon={<Activity size={19} />} label="累计研究" value={data.summary.total_runs} detail={`${data.summary.active_runs} 个正在运行`} />
        <AdminMetric icon={<FileCheck2 size={19} />} label="论文证据" value={data.summary.total_papers} detail={`${data.summary.total_claims} 条原子声明`} />
        <AdminMetric icon={<Users size={19} />} label="研究 Worker" value={data.summary.total_workers} detail="按需动态派发" />
        <AdminMetric icon={<CircleDollarSign size={19} />} label="预估模型费用" value={`$${data.summary.estimated_cost_usd.toFixed(3)}`} detail="运行级预算留痕" accent />
      </section>

      <div className="admin-grid">
        <section className="surface status-panel">
          <div className="surface-head"><div><span className="section-number">01</span><h3>任务状态分布</h3></div><span className="surface-meta">ALL RUNS</span></div>
          {Object.keys(data.summary.status_counts).length === 0 ? (
            <div className="compact-empty"><Radio size={22} /><p>暂无运行数据</p></div>
          ) : (
            <div className="status-chart">
              {Object.entries(data.summary.status_counts).map(([status, count]) => (
                <div className="status-chart-row" key={status}>
                  <div><span>{status.replace("_", " ")}</span><strong>{count}</strong></div>
                  <div className="chart-track"><i className={`chart-${status.toLowerCase()}`} style={{ width: `${Math.max(4, (count / statusTotal) * 100)}%` }} /></div>
                </div>
              ))}
            </div>
          )}
        </section>

        <section className="surface integration-panel">
          <div className="surface-head"><div><span className="section-number">02</span><h3>MCP 科研工具</h3></div><span className="surface-meta">{healthyMcp}/{data.mcp.length} ONLINE</span></div>
          {data.mcp.length === 0 ? (
            <div className="integration-empty">
              <span><Bot size={24} /></span><div><strong>尚未启用 MCP Server</strong><p>配置后，外部论文搜索工具会显示在这里。</p></div>
            </div>
          ) : (
            <div className="integration-list">
              {data.mcp.map((server) => (
                <article key={server.name}>
                  <span className={`integration-state ${server.connected ? "online" : "offline"}`}>{server.connected ? <CheckCircle2 size={17} /> : <AlertTriangle size={17} />}</span>
                  <div><strong>{server.name}</strong><p>{server.server_name ?? server.url}</p><small>{server.protocol_version ?? server.error_type ?? "协议未协商"}</small></div>
                  <span className="tool-count">{server.tools.length} TOOLS</span>
                </article>
              ))}
            </div>
          )}
        </section>
      </div>

      <section className="surface operations-panel">
        <div className="operations-head">
          <div className="operation-tabs" role="tablist" aria-label="运营日志视图">
            <button className={tab === "audit" ? "active" : ""} type="button" role="tab" onClick={() => setTab("audit")}><ScrollText size={16} />审计日志 <span>{data.audit.length}</span></button>
            <button className={tab === "deadletters" ? "active" : ""} type="button" role="tab" onClick={() => setTab("deadletters")}><AlertTriangle size={16} />失败死信 <span>{data.deadLetters.length}</span></button>
          </div>
          <span className="surface-meta">LATEST 50 EVENTS</span>
        </div>
        {tab === "audit" && (
          data.audit.length === 0 ? <div className="table-empty"><ScrollText size={22} /><p>暂无审计事件</p></div> :
          <div className="admin-table">
            <div className="admin-row admin-row-head"><span>操作</span><span>执行人</span><span>关联任务</span><span>时间</span></div>
            {data.audit.map((event) => <article className="admin-row" key={event.event_id}><div><span className="event-icon"><ServerCog size={16} /></span><strong>{actionLabels[event.action] ?? event.action}</strong></div><span>{event.actor}</span><code>{event.run_id?.slice(0, 12) ?? "—"}</code><time>{new Date(event.created_at).toLocaleString("zh-CN", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}</time></article>)}
          </div>
        )}
        {tab === "deadletters" && (
          data.deadLetters.length === 0 ? <div className="table-empty success-empty"><CheckCircle2 size={23} /><p>没有失败死信，任务队列运行正常</p></div> :
          <div className="admin-table deadletter-table">
            <div className="admin-row admin-row-head"><span>消息 ID</span><span>运行 ID</span><span>错误摘要</span></div>
            {data.deadLetters.map((item) => <article className="admin-row" key={item.message_id}><code>{item.message_id}</code><code>{item.run_id}</code><span>{item.error}</span></article>)}
          </div>
        )}
      </section>
    </div>
  );
}

function AdminMetric({ icon, label, value, detail, accent = false }: { icon: React.ReactNode; label: string; value: string | number; detail: string; accent?: boolean }): React.JSX.Element {
  return <article className={`admin-metric ${accent ? "admin-metric-accent" : ""}`}><div><span>{icon}</span><small>{label}</small></div><strong>{value}</strong><p>{detail}</p></article>;
}
