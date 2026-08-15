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
  UserPlus,
  Users,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { adminApi, ApiError, usersApi } from "../api";
import { useApp } from "../app-context";
import type { AuditEvent, DeadLetter, McpServerStatus, Role, RuntimeSummary, User } from "../types";

interface AdminData {
  summary: RuntimeSummary;
  audit: AuditEvent[];
  deadLetters: DeadLetter[];
  mcp: McpServerStatus[];
  users: User[];
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
  "identity.login_succeeded": "用户登录成功",
  "identity.login_failed": "用户登录失败",
  "identity.user_created": "创建用户",
  "identity.user_updated": "更新用户",
  "identity.password_changed": "用户修改密码",
  "identity.password_reset": "管理员重置密码",
  "identity.logout": "用户退出登录",
};

export function AdminPage(): React.JSX.Element {
  const { openAccessKey } = useApp();
  const [data, setData] = useState<AdminData>({ summary: emptySummary, audit: [], deadLetters: [], mcp: [], users: [] });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<"users" | "audit" | "deadletters">("users");
  const [createOpen, setCreateOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [newUser, setNewUser] = useState({ email: "", display_name: "", password: "", roles: ["RESEARCHER"] as Role[] });

  const load = useCallback(async (): Promise<void> => {
    setLoading(true);
    setError(null);
    try {
      const [summary, audit, deadLetters, mcp, users] = await Promise.all([
        adminApi.summary(), adminApi.audit(), adminApi.deadLetters(), adminApi.mcp(), usersApi.list(),
      ]);
      setData({ summary, audit, deadLetters, mcp, users });
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

  async function createUser(event: React.FormEvent): Promise<void> {
    event.preventDefault();
    setCreating(true);
    try {
      await usersApi.create(newUser);
      setNewUser({ email: "", display_name: "", password: "", roles: ["RESEARCHER"] });
      setCreateOpen(false);
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "创建用户失败");
    } finally {
      setCreating(false);
    }
  }

  async function updateUser(user: User, patch: { roles?: Role[]; is_active?: boolean }): Promise<void> {
    try {
      const updated = await usersApi.update(user.user_id, patch);
      setData((current) => ({ ...current, users: current.users.map((item) => item.user_id === updated.user_id ? updated : item) }));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "更新用户失败");
    }
  }

  function toggleRole(user: User, role: Role): void {
    const roles = user.roles.includes(role)
      ? user.roles.filter((item) => item !== role)
      : [...user.roles, role];
    if (roles.length > 0) void updateUser(user, { roles });
  }

  function toggleNewRole(role: Role): void {
    const roles = newUser.roles.includes(role)
      ? newUser.roles.filter((item) => item !== role)
      : [...newUser.roles, role];
    if (roles.length > 0) setNewUser({ ...newUser, roles });
  }

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
            <button className={tab === "users" ? "active" : ""} type="button" role="tab" onClick={() => setTab("users")}><Users size={16} />用户与权限 <span>{data.users.length}</span></button>
            <button className={tab === "audit" ? "active" : ""} type="button" role="tab" onClick={() => setTab("audit")}><ScrollText size={16} />审计日志 <span>{data.audit.length}</span></button>
            <button className={tab === "deadletters" ? "active" : ""} type="button" role="tab" onClick={() => setTab("deadletters")}><AlertTriangle size={16} />失败死信 <span>{data.deadLetters.length}</span></button>
          </div>
          {tab === "users" ? <button className="button button-dark" type="button" onClick={() => setCreateOpen(true)}><UserPlus size={15} />新建用户</button> : <span className="surface-meta">LATEST 50 EVENTS</span>}
        </div>
        {tab === "users" && (
          data.users.length === 0 ? <div className="table-empty"><Users size={22} /><p>当前租户暂无数据库用户</p></div> :
          <div className="admin-table user-admin-table">
            <div className="admin-row admin-row-head"><span>用户</span><span>角色</span><span>最近登录</span><span>状态</span></div>
            {data.users.map((user) => (
              <article className="admin-row" key={user.user_id}>
                <div><span className="event-icon"><Users size={16} /></span><span><strong>{user.display_name}</strong><small>{user.email}</small></span></div>
                <div className="role-pills" aria-label={`修改 ${user.display_name} 的角色`}>
                  {(["ADMIN", "RESEARCHER", "REVIEWER"] as Role[]).map((role) => <button className={user.roles.includes(role) ? "active" : ""} type="button" key={role} onClick={() => toggleRole(user, role)}>{role.slice(0, 3)}</button>)}
                </div>
                <time>{user.last_login_at ? new Date(user.last_login_at).toLocaleString("zh-CN") : "尚未登录"}</time>
                <button className={`account-state ${user.is_active ? "active" : "disabled"}`} type="button" onClick={() => void updateUser(user, { is_active: !user.is_active })}>{user.is_active ? "已启用" : "已停用"}</button>
              </article>
            ))}
          </div>
        )}
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
      {createOpen && (
        <div className="modal-backdrop" role="presentation" onMouseDown={() => setCreateOpen(false)}>
          <section className="modal" role="dialog" aria-modal="true" aria-labelledby="create-user-title" onMouseDown={(event) => event.stopPropagation()}>
            <div className="modal-head"><div className="modal-icon"><UserPlus size={20} /></div><button className="icon-button" type="button" aria-label="关闭" onClick={() => setCreateOpen(false)}>×</button></div>
            <p className="eyebrow">TENANT IDENTITY</p><h2 id="create-user-title">创建租户用户</h2>
            <p className="modal-description">新用户只能访问当前租户。临时密码首次通过安全渠道交付，登录后应立即修改。</p>
            <form onSubmit={(event) => void createUser(event)}>
              <label htmlFor="new-display-name">姓名</label><input id="new-display-name" value={newUser.display_name} onChange={(event) => setNewUser({ ...newUser, display_name: event.target.value })} required />
              <label htmlFor="new-email">邮箱</label><input id="new-email" type="email" value={newUser.email} onChange={(event) => setNewUser({ ...newUser, email: event.target.value })} required />
              <label htmlFor="new-password">临时密码</label><input id="new-password" type="password" minLength={12} value={newUser.password} onChange={(event) => setNewUser({ ...newUser, password: event.target.value })} required />
              <label>角色</label><div className="role-pills role-pills-form">{(["ADMIN", "RESEARCHER", "REVIEWER"] as Role[]).map((role) => <button className={newUser.roles.includes(role) ? "active" : ""} type="button" key={role} onClick={() => toggleNewRole(role)}>{role}</button>)}</div>
              <div className="modal-actions"><button className="button button-ghost" type="button" onClick={() => setCreateOpen(false)}>取消</button><button className="button button-dark" type="submit" disabled={creating}>{creating ? "正在创建" : "创建用户"}</button></div>
            </form>
          </section>
        </div>
      )}
    </div>
  );
}

function AdminMetric({ icon, label, value, detail, accent = false }: { icon: React.ReactNode; label: string; value: string | number; detail: string; accent?: boolean }): React.JSX.Element {
  return <article className={`admin-metric ${accent ? "admin-metric-accent" : ""}`}><div><span>{icon}</span><small>{label}</small></div><strong>{value}</strong><p>{detail}</p></article>;
}
