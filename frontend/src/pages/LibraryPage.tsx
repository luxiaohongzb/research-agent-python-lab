import {
  ArrowUpRight,
  BookCopy,
  CheckCircle2,
  Database,
  FileSearch,
  FileText,
  Fingerprint,
  Layers3,
  LoaderCircle,
  RotateCcw,
  UploadCloud,
  X,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { ApiError, uploadDocument } from "../api";
import { useApp } from "../app-context";
import type { IngestionResult } from "../types";

interface RecentDocument extends IngestionResult {
  filename: string;
  ingested_at: string;
}

const RECENT_KEY = "atlas-recent-documents";
const MAX_SIZE = 30 * 1024 * 1024;

function readRecent(): RecentDocument[] {
  try {
    return JSON.parse(sessionStorage.getItem(RECENT_KEY) ?? "[]") as RecentDocument[];
  } catch {
    return [];
  }
}

export function LibraryPage(): React.JSX.Element {
  const { notify, openAccessKey } = useApp();
  const input = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [progress, setProgress] = useState(0);
  const [recent, setRecent] = useState<RecentDocument[]>(readRecent);
  const [result, setResult] = useState<RecentDocument | null>(null);

  useEffect(() => {
    sessionStorage.setItem(RECENT_KEY, JSON.stringify(recent));
  }, [recent]);

  function choose(candidate?: File): void {
    if (!candidate) return;
    if (candidate.type !== "application/pdf" && !candidate.name.toLowerCase().endsWith(".pdf")) {
      notify("请选择 PDF 格式的论文", "error");
      return;
    }
    if (candidate.size > MAX_SIZE) {
      notify("PDF 不能超过 30MB", "error");
      return;
    }
    setFile(candidate);
    setResult(null);
    setProgress(0);
  }

  async function ingest(): Promise<void> {
    if (!file) return;
    setUploading(true);
    setProgress(2);
    try {
      const payload = await uploadDocument(file, (next) => setProgress(next));
      const document = { ...payload, filename: file.name, ingested_at: new Date().toISOString() };
      setResult(document);
      setRecent((items) => [document, ...items.filter((item) => item.document_hash !== document.document_hash)].slice(0, 8));
      notify("论文已解析并写入检索索引", "success");
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) openAccessKey();
      notify(error instanceof Error ? error.message : "论文入库失败", "error");
    } finally {
      setUploading(false);
    }
  }

  return (
    <div className="library-page">
      <section className="page-intro library-intro">
        <div>
          <p className="eyebrow"><BookCopy size={13} /> PRIVATE RESEARCH CORPUS</p>
          <h2>让你的论文，成为<br /><em>下一次研究的证据。</em></h2>
          <p>上传 PDF 后，系统会保留章节、页码与坐标信息，将全文切分为可检索证据，并建立引用关系。</p>
        </div>
        <div className="intro-index"><span>02</span><p>INGEST · STRUCTURE · RETRIEVE</p></div>
      </section>

      <div className="library-grid">
        <section className="surface upload-panel">
          <div className="surface-head">
            <div><span className="section-number">01</span><h3>导入论文</h3></div>
            <span className="surface-meta">PDF · MAX 30MB</span>
          </div>
          <button
            className={`drop-zone ${dragging ? "drop-zone-active" : ""} ${file ? "drop-zone-selected" : ""}`}
            type="button"
            onClick={() => input.current?.click()}
            onDragEnter={(event) => { event.preventDefault(); setDragging(true); }}
            onDragOver={(event) => event.preventDefault()}
            onDragLeave={() => setDragging(false)}
            onDrop={(event) => { event.preventDefault(); setDragging(false); choose(event.dataTransfer.files[0]); }}
          >
            <input ref={input} type="file" accept="application/pdf,.pdf" onChange={(event) => choose(event.target.files?.[0])} />
            <span className="drop-icon">{file ? <FileText size={30} /> : <UploadCloud size={30} />}</span>
            {file ? (
              <><strong>{file.name}</strong><small>{(file.size / 1024 / 1024).toFixed(2)} MB · 点击重新选择</small></>
            ) : (
              <><strong>拖放论文到这里</strong><small>或者点击从电脑中选择 PDF</small></>
            )}
          </button>

          {file && !result && (
            <div className="upload-actions">
              <button className="button button-ghost" type="button" disabled={uploading} onClick={() => { setFile(null); setProgress(0); }}>移除</button>
              <button className="button button-accent" type="button" disabled={uploading} onClick={() => void ingest()}>
                {uploading ? <><LoaderCircle className="spin" size={17} /> 正在解析论文</> : <><Database size={17} /> 开始解析并入库</>}
              </button>
            </div>
          )}

          {uploading && (
            <div className="ingestion-progress" aria-live="polite">
              <div><span>上传与结构化处理中</span><strong>{progress < 45 ? `${progress}%` : "GROBID 解析中"}</strong></div>
              <div className="progress-track"><i style={{ width: `${Math.max(progress, 8)}%` }} /></div>
              <p>正文解析通常需要十几秒，请保持页面打开。</p>
            </div>
          )}

          {result && (
            <div className="ingestion-success">
              <div className="success-mark"><CheckCircle2 size={24} /></div>
              <div className="success-copy"><span>INGESTION COMPLETE</span><h3>{result.title}</h3><p>{result.passages_indexed} 个证据片段 · {result.citation_edges_indexed} 条引用关系</p></div>
              <button className="icon-button" type="button" aria-label="关闭结果" onClick={() => { setResult(null); setFile(null); }}><X size={18} /></button>
            </div>
          )}
        </section>

        <aside className="surface pipeline-panel">
          <div className="surface-head surface-head-compact"><div><span className="section-number">02</span><h3>入库流程</h3></div></div>
          <ol className="pipeline-list">
            <li><span><FileSearch size={18} /></span><div><strong>全文结构解析</strong><p>识别标题、作者、摘要、章节与表格</p></div><small>01</small></li>
            <li><span><Layers3 size={18} /></span><div><strong>证据级切分</strong><p>保留页码、坐标与章节路径</p></div><small>02</small></li>
            <li><span><Fingerprint size={18} /></span><div><strong>版本与去重</strong><p>计算内容哈希并规范化 DOI</p></div><small>03</small></li>
            <li><span><Database size={18} /></span><div><strong>混合索引</strong><p>写入关键词、向量与引用图索引</p></div><small>04</small></li>
          </ol>
          <div className="privacy-note"><strong>原始 PDF 当前不会长期保存</strong><p>系统只保存结构化论文、证据片段和引用关系。需要原文对象存储时可在后续版本启用。</p></div>
        </aside>
      </div>

      <section className="recent-section">
        <div className="section-heading"><div><p className="eyebrow">THIS SESSION</p><h2>最近入库</h2></div><span>仅显示当前浏览器标签页中的上传记录</span></div>
        {recent.length === 0 ? (
          <div className="empty-library surface"><BookCopy size={25} /><div><strong>还没有入库记录</strong><p>上传第一篇论文后，解析摘要会显示在这里。</p></div></div>
        ) : (
          <div className="document-table surface">
            <div className="document-row document-header"><span>论文</span><span>结构化内容</span><span>解析器</span><span>入库时间</span></div>
            {recent.map((document) => (
              <article className="document-row" key={document.document_hash}>
                <div className="document-title"><span><FileText size={18} /></span><div><strong>{document.title}</strong><small>{document.filename}</small></div></div>
                <div><strong>{document.passages_indexed} passages</strong><small>{document.citation_edges_indexed} citation edges</small></div>
                <div><strong>{document.parser}</strong><small>{document.parser_version}</small></div>
                <div><strong>{new Date(document.ingested_at).toLocaleDateString("zh-CN")}</strong><small>{new Date(document.ingested_at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}</small></div>
              </article>
            ))}
          </div>
        )}
      </section>

      <a className="library-cta" href="/workbench"><span><RotateCcw size={18} /></span><div><strong>带着新证据开始研究</strong><p>新入库的论文会参与后续混合检索</p></div><ArrowUpRight size={20} /></a>
    </div>
  );
}
