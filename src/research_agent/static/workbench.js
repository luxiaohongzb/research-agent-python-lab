const $ = (selector) => document.querySelector(selector);
const state = { runId: null, startedAt: null, poller: null, events: new Set() };

function headers(json = false) {
  const value = sessionStorage.getItem("research-api-key");
  return { ...(json ? { "Content-Type": "application/json" } : {}), ...(value ? { Authorization: `Bearer ${value}` } : {}) };
}

function toast(message) {
  const node = $("#toast"); node.textContent = message; node.classList.add("show");
  setTimeout(() => node.classList.remove("show"), 2600);
}

async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { ...headers(Boolean(options.body)), ...(options.headers || {}) } });
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try { message = (await response.json()).detail || message; } catch (_) { /* response is not JSON */ }
    throw new Error(message);
  }
  return response;
}

function setRun(snapshot) {
  state.runId = snapshot.run_id;
  $("#run-id").textContent = snapshot.run_id.slice(0, 12);
  $("#run-empty").classList.add("hidden"); $("#run-live").classList.remove("hidden");
  $("#status").textContent = snapshot.status;
  const progress = { PENDING: 8, RUNNING: 52, COMPLETED: 100, NEEDS_REVIEW: 100, FAILED: 100, CANCELLED: 100 }[snapshot.status] || 8;
  $("#progress-bar").style.width = `${progress}%`;
  $("#cancel").classList.toggle("hidden", !["PENDING", "RUNNING"].includes(snapshot.status));
  $("#resume").classList.toggle("hidden", !["FAILED", "CANCELLED"].includes(snapshot.status));
  if (snapshot.result) renderResult(snapshot.result);
}

function addEvent(event, details = {}) {
  const key = `${event}:${JSON.stringify(details)}`; if (state.events.has(key)) return; state.events.add(key);
  const labels = { queued: "任务已入队", started: "研究流程启动", progress: details.node || "阶段推进", completed: "研究完成", failed: "执行失败", cancelled: "任务取消", resuming: "恢复执行", reviewed: "人工审核" };
  const li = document.createElement("li"), strong = document.createElement("strong"), small = document.createElement("span");
  strong.textContent = labels[event] || event; small.textContent = details.status || details.error_type || "流程事件已记录";
  li.append(strong, small); $("#timeline").append(li); li.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function streamEvents(runId) {
  try {
    const response = await api(`/v1/research/runs/${runId}/events`);
    const reader = response.body.getReader(), decoder = new TextDecoder(); let buffer = "";
    while (true) {
      const { value, done } = await reader.read(); if (done) break; buffer += decoder.decode(value, { stream: true });
      const chunks = buffer.split("\n\n"); buffer = chunks.pop() || "";
      for (const chunk of chunks) {
        const type = chunk.match(/^event: (.+)$/m)?.[1]; const raw = chunk.match(/^data: (.+)$/m)?.[1];
        if (type && raw) { const payload = JSON.parse(raw); addEvent(type, payload.details || {}); }
      }
    }
  } catch (error) { if (state.runId === runId) toast(`事件流：${error.message}`); }
}

async function refresh() {
  if (!state.runId) return;
  try {
    const snapshot = await (await api(`/v1/research/runs/${state.runId}`)).json(); setRun(snapshot);
    if (["COMPLETED", "NEEDS_REVIEW", "FAILED", "CANCELLED"].includes(snapshot.status)) { clearInterval(state.poller); state.poller = null; }
  } catch (error) { clearInterval(state.poller); toast(error.message); }
}

function renderResult(result) {
  $("#results").classList.remove("hidden"); $("#report-title").textContent = result.report.title;
  $("#report").textContent = result.report.markdown; $("#claim-count").textContent = `${result.claims.length} ITEMS`;
  const values = [[result.papers.length, "论文来源"], [result.evidence.length, "证据卡片"], [result.claims.length, "原子声明"], [`${Math.round((result.verifications.filter(v => v.status === "SUPPORTED").length / Math.max(1, result.verifications.length)) * 100)}%`, "声明支持率"]];
  const metrics = $("#metrics"); metrics.replaceChildren(); values.forEach(([value, label]) => { const node = document.createElement("div"); node.className = "metric"; const b = document.createElement("b"), span = document.createElement("span"); b.textContent = value; span.textContent = label; node.append(b, span); metrics.append(node); });
  const list = $("#claim-list"); list.replaceChildren(); result.claims.forEach(claim => { const verification = result.verifications.find(v => v.claim_id === claim.claim_id); const node = document.createElement("div"); node.className = "claim-item"; const status = document.createElement("span"), text = document.createElement("p"), confidence = document.createElement("small"); status.textContent = verification?.status || "UNCHECKED"; text.textContent = claim.text; confidence.textContent = verification ? `置信度 ${Math.round(verification.confidence * 100)}% · ${claim.evidence_ids.length} 条证据` : `${claim.evidence_ids.length} 条证据`; node.append(status, text, confidence); list.append(node); });
  $("#results").scrollIntoView({ behavior: "smooth" });
}

$("#research-form").addEventListener("submit", async (event) => {
  event.preventDefault(); $("#submit").disabled = true; state.events.clear(); $("#timeline").replaceChildren();
  const payload = { question: $("#question").value.trim(), max_papers: Number($("#max-papers").value), max_iterations: Number($("#max-iterations").value), max_workers: Number($("#max-workers").value), max_cost_usd: Number($("#max-cost").value) };
  try {
    const response = await api("/v1/research/runs", { method: "POST", body: JSON.stringify(payload), headers: { "Idempotency-Key": crypto.randomUUID() } });
    const snapshot = await response.json(); state.startedAt = Date.now(); setRun(snapshot); addEvent("queued", { status: snapshot.status }); streamEvents(snapshot.run_id); state.poller = setInterval(refresh, 1200); toast("研究任务已创建");
  } catch (error) { toast(error.message); } finally { $("#submit").disabled = false; }
});

$("#cancel").addEventListener("click", async () => { try { setRun(await (await api(`/v1/research/runs/${state.runId}`, { method: "DELETE" })).json()); } catch (error) { toast(error.message); } });
$("#resume").addEventListener("click", async () => { try { setRun(await (await api(`/v1/research/runs/${state.runId}/resume`, { method: "POST" })).json()); streamEvents(state.runId); state.poller = setInterval(refresh, 1200); } catch (error) { toast(error.message); } });
document.querySelectorAll("[data-prompt]").forEach(button => button.addEventListener("click", () => { $("#question").value = button.dataset.prompt; $("#question").focus(); }));
$("#auth-open").addEventListener("click", () => { $("#api-key").value = sessionStorage.getItem("research-api-key") || ""; $("#auth-dialog").showModal(); });
$("#auth-save").addEventListener("click", () => { const value = $("#api-key").value.trim(); if (value) sessionStorage.setItem("research-api-key", value); else sessionStorage.removeItem("research-api-key"); toast("访问密钥已更新"); });
function download(kind, extension) { if (!state.runId) return toast("请先完成一项研究"); api(`/v1/research/runs/${state.runId}/export/${kind}`).then(r => r.blob()).then(blob => { const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = `research-${state.runId.slice(0, 8)}.${extension}`; a.click(); URL.revokeObjectURL(a.href); }).catch(error => toast(error.message)); }
$("#bibtex").addEventListener("click", () => download("bibtex", "bib")); $("#csl").addEventListener("click", () => download("csl-json", "json"));
setInterval(() => { if (state.startedAt && state.poller) $("#elapsed").textContent = `已运行 ${Math.floor((Date.now() - state.startedAt) / 1000)} 秒`; }, 1000);
