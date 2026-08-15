import {
  Activity,
  BookOpen,
  ChevronRight,
  FileText,
  KeyRound,
  LayoutDashboard,
  Menu,
  Settings2,
  ShieldCheck,
  X,
} from "lucide-react";
import { useEffect, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { getApiKey, setApiKey } from "../api";
import { useApp } from "../app-context";

const pageNames: Record<string, { eyebrow: string; title: string }> = {
  "/workbench": { eyebrow: "RESEARCH STUDIO", title: "研究工作台" },
  "/library": { eyebrow: "KNOWLEDGE LIBRARY", title: "文献与知识库" },
  "/admin": { eyebrow: "SYSTEM OBSERVATORY", title: "系统管理" },
};

export function Layout(): React.JSX.Element {
  const [menuOpen, setMenuOpen] = useState(false);
  const [keyOpen, setKeyOpen] = useState(false);
  const [keyValue, setKeyValue] = useState("");
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
      setKeyOpen(true);
    };
    window.addEventListener("atlas:open-key", open);
    return () => window.removeEventListener("atlas:open-key", open);
  }, []);

  function saveKey(event: React.FormEvent): void {
    event.preventDefault();
    setApiKey(keyValue);
    setKeyOpen(false);
    notify(keyValue.trim() ? "访问密钥已保存到当前标签页" : "已切换为无密钥访问", "success");
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
            <strong>访问凭证</strong>
            <small>{getApiKey() ? "已配置密钥" : "本地开发模式"}</small>
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
              <KeyRound size={16} /> 访问密钥
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
            <h2 id="key-title">配置访问密钥</h2>
            <p className="modal-description">生产环境使用 Bearer API Key。密钥只保存在当前浏览器标签页，关闭后自动清除。</p>
            <form onSubmit={saveKey}>
              <label htmlFor="access-key">API Key</label>
              <input id="access-key" type="password" autoComplete="off" value={keyValue} onChange={(event) => setKeyValue(event.target.value)} placeholder="粘贴访问密钥" autoFocus />
              <div className="modal-actions">
                <button className="button button-ghost" type="button" onClick={() => setKeyOpen(false)}>取消</button>
                <button className="button button-dark" type="submit">保存密钥</button>
              </div>
            </form>
          </section>
        </div>
      )}
    </div>
  );
}
