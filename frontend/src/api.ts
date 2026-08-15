import type {
  AuditEvent,
  DeadLetter,
  IngestionResult,
  McpServerStatus,
  ResearchRequest,
  RunEvent,
  RunSnapshot,
  RuntimeSummary,
  Role,
  TokenPair,
  User,
} from "./types";

const ACCESS_TOKEN_STORAGE = "atlas-research-access-token";
const REFRESH_TOKEN_STORAGE = "atlas-research-refresh-token";
const USER_STORAGE = "atlas-research-user";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

export function getApiKey(): string {
  return sessionStorage.getItem(ACCESS_TOKEN_STORAGE) ?? "";
}

export function setApiKey(value: string): void {
  clearSession();
  if (value.trim()) sessionStorage.setItem(ACCESS_TOKEN_STORAGE, value.trim());
  window.dispatchEvent(new Event("atlas:auth-changed"));
}

export function getCurrentUser(): User | null {
  const raw = sessionStorage.getItem(USER_STORAGE);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as User;
  } catch {
    return null;
  }
}

function saveSession(pair: TokenPair): void {
  sessionStorage.setItem(ACCESS_TOKEN_STORAGE, pair.access_token);
  sessionStorage.setItem(REFRESH_TOKEN_STORAGE, pair.refresh_token);
  sessionStorage.setItem(USER_STORAGE, JSON.stringify(pair.user));
  window.dispatchEvent(new Event("atlas:auth-changed"));
}

export function clearSession(): void {
  sessionStorage.removeItem(ACCESS_TOKEN_STORAGE);
  sessionStorage.removeItem(REFRESH_TOKEN_STORAGE);
  sessionStorage.removeItem(USER_STORAGE);
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
  return requestWithRefresh<T>(path, init, true);
}

let refreshInFlight: Promise<boolean> | null = null;

async function refreshSession(): Promise<boolean> {
  const refreshToken = sessionStorage.getItem(REFRESH_TOKEN_STORAGE);
  if (!refreshToken) return false;
  if (!refreshInFlight) {
    refreshInFlight = fetch("/v1/auth/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: refreshToken }),
    }).then(async (response) => {
      if (!response.ok) {
        clearSession();
        window.dispatchEvent(new Event("atlas:auth-changed"));
        return false;
      }
      saveSession((await response.json()) as TokenPair);
      return true;
    }).finally(() => { refreshInFlight = null; });
  }
  return refreshInFlight;
}

async function requestWithRefresh<T>(path: string, init: RequestInit, retry: boolean): Promise<T> {
  const headers = new Headers(init.headers);
  Object.entries(authHeaders()).forEach(([key, value]) => headers.set(key, value));
  if (init.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(path, { ...init, headers });
  if (response.status === 401 && retry && await refreshSession()) {
    return requestWithRefresh<T>(path, init, false);
  }
  if (!response.ok) throw new ApiError(await messageFrom(response), response.status);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const identityApi = {
  async login(tenantId: string, email: string, password: string): Promise<User> {
    const pair = await requestWithRefresh<TokenPair>("/v1/auth/login", {
      method: "POST",
      body: JSON.stringify({ tenant_id: tenantId, email, password }),
    }, false);
    saveSession(pair);
    return pair.user;
  },
  async me(): Promise<User> {
    const user = await request<User>("/v1/auth/me");
    sessionStorage.setItem(USER_STORAGE, JSON.stringify(user));
    window.dispatchEvent(new Event("atlas:auth-changed"));
    return user;
  },
  async logout(): Promise<void> {
    const refreshToken = sessionStorage.getItem(REFRESH_TOKEN_STORAGE);
    try {
      if (refreshToken) {
        await fetch("/v1/auth/logout", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ refresh_token: refreshToken }),
        });
      }
    } finally {
      clearSession();
      window.dispatchEvent(new Event("atlas:auth-changed"));
    }
  },
  async changePassword(currentPassword: string, newPassword: string): Promise<void> {
    await request<void>("/v1/auth/change-password", {
      method: "POST",
      body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
    });
    clearSession();
    window.dispatchEvent(new Event("atlas:auth-changed"));
  },
};

export const usersApi = {
  list: () => request<User[]>("/v1/users?limit=200"),
  create: (payload: { email: string; display_name: string; password: string; roles: Role[] }) =>
    request<User>("/v1/users", { method: "POST", body: JSON.stringify(payload) }),
  update: (userId: string, payload: { display_name?: string; roles?: Role[]; is_active?: boolean }) =>
    request<User>(`/v1/users/${userId}`, { method: "PATCH", body: JSON.stringify(payload) }),
  resetPassword: (userId: string, newPassword: string) =>
    request<void>(`/v1/users/${userId}/reset-password`, {
      method: "POST",
      body: JSON.stringify({ new_password: newPassword }),
    }),
};

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
