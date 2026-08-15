import type {
  AuditEvent,
  DeadLetter,
  IngestionResult,
  McpServerStatus,
  ResearchRequest,
  RunEvent,
  RunSnapshot,
  RuntimeSummary,
} from "./types";

const API_KEY_STORAGE = "atlas-research-api-key";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

export function getApiKey(): string {
  return sessionStorage.getItem(API_KEY_STORAGE) ?? "";
}

export function setApiKey(value: string): void {
  if (value.trim()) sessionStorage.setItem(API_KEY_STORAGE, value.trim());
  else sessionStorage.removeItem(API_KEY_STORAGE);
}

function authHeaders(): HeadersInit {
  const apiKey = getApiKey();
  return apiKey ? { Authorization: `Bearer ${apiKey}` } : {};
}

async function messageFrom(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: string };
    return body.detail || `${response.status} ${response.statusText}`;
  } catch {
    return `${response.status} ${response.statusText}`;
  }
}

export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  Object.entries(authHeaders()).forEach(([key, value]) => headers.set(key, value));
  if (init.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(path, { ...init, headers });
  if (!response.ok) throw new ApiError(await messageFrom(response), response.status);
  return (await response.json()) as T;
}

export const researchApi = {
  submit(payload: ResearchRequest): Promise<RunSnapshot> {
    return request("/v1/research/runs", {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify(payload),
    });
  },
  get(runId: string): Promise<RunSnapshot> {
    return request(`/v1/research/runs/${runId}`);
  },
  cancel(runId: string): Promise<RunSnapshot> {
    return request(`/v1/research/runs/${runId}`, { method: "DELETE" });
  },
  resume(runId: string): Promise<RunSnapshot> {
    return request(`/v1/research/runs/${runId}/resume`, { method: "POST" });
  },
  async stream(runId: string, onEvent: (event: RunEvent) => void): Promise<void> {
    const response = await fetch(`/v1/research/runs/${runId}/events`, {
      headers: authHeaders(),
    });
    if (!response.ok) throw new ApiError(await messageFrom(response), response.status);
    if (!response.body) throw new Error("浏览器不支持实时事件流");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const chunks = buffer.split("\n\n");
      buffer = chunks.pop() ?? "";
      for (const chunk of chunks) {
        const event = chunk.match(/^event: (.+)$/m)?.[1];
        const raw = chunk.match(/^data: (.+)$/m)?.[1];
        if (!event || !raw) continue;
        const payload = JSON.parse(raw) as Omit<RunEvent, "event">;
        onEvent({ event, ...payload });
      }
    }
  },
  exportUrl(runId: string, kind: "bibtex" | "csl-json"): string {
    return `/v1/research/runs/${runId}/export/${kind}`;
  },
};

export async function downloadExport(
  runId: string,
  kind: "bibtex" | "csl-json",
): Promise<void> {
  const response = await fetch(researchApi.exportUrl(runId, kind), { headers: authHeaders() });
  if (!response.ok) throw new ApiError(await messageFrom(response), response.status);
  const blob = await response.blob();
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `atlas-${runId.slice(0, 8)}.${kind === "bibtex" ? "bib" : "json"}`;
  link.click();
  URL.revokeObjectURL(link.href);
}

export function uploadDocument(
  file: File,
  onProgress?: (percent: number) => void,
): Promise<IngestionResult> {
  return new Promise((resolve, reject) => {
    const body = new FormData();
    body.append("file", file);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/v1/corpus/documents");
    const apiKey = getApiKey();
    if (apiKey) xhr.setRequestHeader("Authorization", `Bearer ${apiKey}`);
    xhr.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable) onProgress?.(Math.round((event.loaded / event.total) * 45));
    });
    xhr.addEventListener("load", () => {
      let payload: unknown;
      try {
        payload = JSON.parse(xhr.responseText);
      } catch {
        payload = null;
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        onProgress?.(100);
        resolve(payload as IngestionResult);
        return;
      }
      const detail = typeof payload === "object" && payload && "detail" in payload
        ? String((payload as { detail: unknown }).detail)
        : `${xhr.status} ${xhr.statusText}`;
      reject(new ApiError(detail, xhr.status));
    });
    xhr.addEventListener("error", () => reject(new ApiError("无法连接文献入库服务", 0)));
    xhr.send(body);
    onProgress?.(2);
  });
}

export const adminApi = {
  summary: () => request<RuntimeSummary>("/v1/metrics/summary"),
  audit: () => request<AuditEvent[]>("/v1/audit/events?limit=50"),
  deadLetters: () => request<DeadLetter[]>("/v1/operations/dead-letters?limit=50"),
  mcp: () => request<McpServerStatus[]>("/v1/integrations/mcp"),
};
