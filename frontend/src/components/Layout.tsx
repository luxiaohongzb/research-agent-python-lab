import {
  Activity,
  BookOpen,
  ChevronRight,
  FileText,
  KeyRound,
  LayoutDashboard,
  LogOut,
  Menu,
  Settings2,
  ShieldCheck,
  UserRound,
  X,
} from "lucide-react";
import { useEffect, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { getApiKey, getCurrentUser, identityApi, setApiKey } from "../api";
import { useApp } from "../app-context";
import type { User } from "../types";

const pageNames: Record<string, { eyebrow: string; title: string }> = {
  "/workbench": { eyebrow: "RESEARCH STUDIO", title: "研究工作台" },
  "/library": { eyebrow: "KNOWLEDGE LIBRARY", title: "文献与知识库" },
  "/admin": { eyebrow: "SYSTEM OBSERVATORY", title: "系统管理" },
};

export function Layout(): React.JSX.Element {
  const [menuOpen, setMenuOpen] = useState(false);
  const [keyOpen, setKeyOpen] = useState(false);
  const [keyValue, setKeyValue] = useState("");
  const [authMethod, setAuthMethod] = useState<"account" | "key">("account");
  const [tenantId, setTenantId] = useState("default");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [passwordOpen, setPasswordOpen] = useState(false);
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [authBusy, setAuthBusy] = useState(false);
  const [user, setUser] = useState<User | null>(() => getCurrentUser());
  const [ready, setReady] = useState<boolean | null>(null);
  const location = useLocation();
  const { notify } = useApp();
  const page = pageNames[location.pathname] ?? pageNames["/workbench"];

  useEffect(() => {
    setMenuOpen(false);
  }, [location.pathname]);

  useEffect(() => {
    fetch("/ready")
      .then((response) => setReady(response.ok))
      .catch(() => setReady(false));
  }, []);

  useEffect(() => {
    const open = (): void => {
      setKeyValue(getApiKey());
      setAuthMethod("account");
      setPasswordOpen(false);
      setKeyOpen(true);
    };
    const changed = (): void => setUser(getCurrentUser());
    window.addEventListener("atlas:open-key", open);
    window.addEventListener("atlas:auth-changed", changed);
    if (getApiKey()) void identityApi.me().then(setUser).catch(() => undefined);
    return () => {
      window.removeEventListener("atlas:open-key", open);
      window.removeEventListener("atlas:auth-changed", changed);
    };
  }, []);

  function saveKey(event: React.FormEvent): void {
    event.preventDefault();
    setApiKey(keyValue);
    setKeyOpen(false);
    notify(keyValue.trim() ? "访问密钥已保存到当前标签页" : "已切换为无密钥访问", "success");
  }

  async function signIn(event: React.FormEvent): Promise<void> {
    event.preventDefault();
    setAuthBusy(true);
    try {
      const current = await identityApi.login(tenantId.trim(), email.trim(), password);
      setUser(current);
      setPassword("");
      setKeyOpen(false);
      notify(`欢迎回来，${current.display_name}`, "success");
    } catch (error) {
      notify(error instanceof Error ? error.message : "登录失败", "error");
    } finally {
      setAuthBusy(false);
    }
  }

  async function signOut(): Promise<void> {
    await identityApi.logout();
    setUser(null);
    setKeyOpen(false);
    notify("已安全退出当前账号", "info");
  }

  async function changePassword(event: React.FormEvent): Promise<void> {
    event.preventDefault();
    setAuthBusy(true);
    try {
      await identityApi.changePassword(currentPassword, newPassword);
      setUser(null);
      setCurrentPassword("");
      setNewPassword("");
      setPasswordOpen(false);
      notify("密码已修改，请重新登录", "success");
    } catch (error) {
      notify(error instanceof Error ? error.message : "密码修改失败", "error");
    } finally {
      setAuthBusy(false);
    }
  }

  return (
    <div className="app-shell">
      <aside className={`sidebar ${menuOpen ? "sidebar-open" : ""}`}>
        <div className="brand-lockup">
          <span className="brand-seal">A</span>
          <span>
            <strong>ATLAS</strong>
            <small>RESEARCH OS</small>
          </span>
        </div>
        <button
          className="sidebar-close icon-button"
          type="button"
          aria-label="关闭导航"
          onClick={() => setMenuOpen(false)}
        >
          <X size={20} />
        </button>

        <nav className="primary-nav" aria-label="主导航">
          <p className="nav-label">研究空间</p>
          <NavLink to="/workbench">
            <LayoutDashboard size={18} />
            <span>研究工作台</span>
            <ChevronRight className="nav-chevron" size={15} />
          </NavLink>
          <NavLink to="/library">
            <BookOpen size={18} />
            <span>文献与知识库</span>
            <ChevronRight className="nav-chevron" size={15} />
          </NavLink>
          <p className="nav-label nav-label-spaced">运营与治理</p>
          <NavLink to="/admin">
            <ShieldCheck size={18} />
            <span>系统管理</span>
            <ChevronRight className="nav-chevron" size={15} />
          </NavLink>
          <a href="/docs" target="_blank" rel="noreferrer">
            <FileText size={18} />
            <span>接口文档</span>
            <ChevronRight className="nav-chevron" size={15} />
          </a>
        </nav>

        <div className="sidebar-note">
          <div className="note-icon"><Activity size={16} /></div>
          <p>证据优先</p>
          <span>结论必须回到论文证据，证据不足时交由人工复核。</span>
        </div>

        <button
          className="profile-card"
          type="button"
          onClick={() => window.dispatchEvent(new Event("atlas:open-key"))}
        >
          <span className="avatar">AR</span>
          <span className="profile-copy">
            <strong>{user?.display_name ?? "访问凭证"}</strong>
            <small>{user ? `${user.tenant_id} · ${user.roles.join(" / ")}` : getApiKey() ? "已配置密钥" : "本地开发模式"}</small>
          </span>
          <Settings2 size={17} />
        </button>
      </aside>

      {menuOpen && <button className="sidebar-scrim" aria-label="关闭导航" onClick={() => setMenuOpen(false)} />}

      <div className="app-main">
        <header className="app-header">
          <div className="header-title">
            <button className="mobile-menu icon-button" type="button" aria-label="打开导航" onClick={() => setMenuOpen(true)}>
              <Menu size={21} />
            </button>
            <div>
              <span>{page.eyebrow}</span>
              <h1>{page.title}</h1>
            </div>
          </div>
          <div className="header-actions">
            <span className={`service-pill ${ready === false ? "service-down" : ""}`}>
              <i /> {ready === null ? "检测中" : ready ? "服务就绪" : "服务异常"}
            </span>
            <button className="button button-ghost header-key" type="button" onClick={() => window.dispatchEvent(new Event("atlas:open-key"))}>
              {user ? <UserRound size={16} /> : <KeyRound size={16} />} {user ? user.display_name : "登录 / 密钥"}
            </button>
          </div>
        </header>
        <main className="page-content"><Outlet /></main>
      </div>

      {keyOpen && (
        <div className="modal-backdrop" role="presentation" onMouseDown={() => setKeyOpen(false)}>
          <section className="modal" role="dialog" aria-modal="true" aria-labelledby="key-title" onMouseDown={(event) => event.stopPropagation()}>
            <div className="modal-head">
              <div className="modal-icon"><KeyRound size={20} /></div>
              <button className="icon-button" type="button" aria-label="关闭" onClick={() => setKeyOpen(false)}><X size={19} /></button>
            </div>
            <p className="eyebrow">SECURE ACCESS</p>
            <h2 id="key-title">{user ? "当前账号" : "登录研究空间"}</h2>
            {user ? (
              <>
                <div className="account-summary">
                  <span className="avatar">{user.display_name.slice(0, 2).toUpperCase()}</span>
                  <div><strong>{user.display_name}</strong><p>{user.email}</p><small>{user.tenant_id} · {user.roles.join(" / ")}</small></div>
                  <div className="account-actions">
                    <button className="button button-ghost" type="button" onClick={() => setPasswordOpen((open) => !open)}>修改密码</button>
                    <button className="button button-ghost" type="button" onClick={() => void signOut()}><LogOut size={15} />退出</button>
                  </div>
                </div>
                {passwordOpen && (
                  <form className="password-form" onSubmit={(event) => void changePassword(event)}>
                    <label htmlFor="current-password">当前密码</label>
                    <input id="current-password" type="password" autoComplete="current-password" value={currentPassword} onChange={(event) => setCurrentPassword(event.target.value)} required />
                    <label htmlFor="new-password">新密码</label>
                    <input id="new-password" type="password" autoComplete="new-password" minLength={12} value={newPassword} onChange={(event) => setNewPassword(event.target.value)} required />
                    <div className="modal-actions">
                      <button className="button button-ghost" type="button" onClick={() => setPasswordOpen(false)}>取消</button>
                      <button className="button button-dark" type="submit" disabled={authBusy}>{authBusy ? "正在保存" : "保存密码"}</button>
                    </div>
                  </form>
                )}
              </>
            ) : (
              <>
                <div className="auth-method-tabs">
                  <button className={authMethod === "account" ? "active" : ""} type="button" onClick={() => setAuthMethod("account")}>账号登录</button>
                  <button className={authMethod === "key" ? "active" : ""} type="button" onClick={() => setAuthMethod("key")}>API Key</button>
                </div>
                {authMethod === "account" ? (
                  <form onSubmit={(event) => void signIn(event)}>
                    <label htmlFor="tenant-id">租户 ID</label>
                    <input id="tenant-id" value={tenantId} onChange={(event) => setTenantId(event.target.value)} autoComplete="organization" required />
                    <label htmlFor="login-email">邮箱</label>
                    <input id="login-email" type="email" value={email} onChange={(event) => setEmail(event.target.value)} autoComplete="username" required />
                    <label htmlFor="login-password">密码</label>
                    <input id="login-password" type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" minLength={12} required />
                    <div className="modal-actions">
                      <button className="button button-ghost" type="button" onClick={() => setKeyOpen(false)}>取消</button>
                      <button className="button button-dark" type="submit" disabled={authBusy}>{authBusy ? "正在登录" : "登录"}</button>
                    </div>
                  </form>
                ) : (
                  <form onSubmit={saveKey}>
                    <p className="modal-description">兼容自动化和迁移场景。凭据只保存在当前浏览器标签页。</p>
                    <label htmlFor="access-key">API Key</label>
                    <input id="access-key" type="password" autoComplete="off" value={keyValue} onChange={(event) => setKeyValue(event.target.value)} placeholder="粘贴访问密钥" />
                    <div className="modal-actions">
                      <button className="button button-ghost" type="button" onClick={() => setKeyOpen(false)}>取消</button>
                      <button className="button button-dark" type="submit">保存密钥</button>
                    </div>
                  </form>
                )}
              </>
            )}
          </section>
        </div>
      )}
    </div>
  );
}
