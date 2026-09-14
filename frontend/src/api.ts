// 与后端契约一一对应的类型 + fetch 封装 + SSE 流式读取。
// 端点清单见 api/main.py 的模块 docstring —— 这里不发明第二个事实来源。

export interface RoleCard {
  role_id: string;
  role_name: string;
  system_prompt: string;
  temperature: number;
  model_name: string | null;
  tool_whitelist: string[] | null;
  exemplars: { user: string; assistant: string }[] | null;
  knowledge_scopes: string[] | null;
  description: string | null;
  is_builtin: boolean;
}

export interface PluginRow {
  plugin_id: string;
  display_name: string;
  enabled: boolean;
  config: unknown;
}

export interface SessionRow {
  thread_id: string;
  title: string | null;
  role_id: string;
  role_name: string | null;
  updated_at: string;
}

export interface MessageRow {
  role: "user" | "assistant" | "tool";
  content: string;
  tools?: (string | null)[];
  name?: string;
}

export interface BackendRow {
  name: string;
  provider: string;
  base_url: string | null;
  model: string;
  sort_order: number;
  has_key: boolean;
}

export interface ModelSettings {
  default: string | null;
  backends: BackendRow[];
}

export class ApiError extends Error {
  constructor(
    public status: number,
    detail: string,
  ) {
    super(detail);
  }
}

async function request<T>(method: string, url: string, body?: unknown): Promise<T> {
  const opt: RequestInit = { method, headers: {} };
  if (body !== undefined) {
    opt.headers = { "Content-Type": "application/json" };
    opt.body = JSON.stringify(body);
  }
  const res = await fetch(url, opt);
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      detail = ((await res.json()) as { detail?: string }).detail || detail;
    } catch {
      /* 非 JSON 错误体，保留状态码 */
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  get: <T>(url: string) => request<T>("GET", url),
  post: <T>(url: string, body?: unknown) => request<T>("POST", url, body),
  patch: <T>(url: string, body: unknown) => request<T>("PATCH", url, body),
  put: <T>(url: string, body: unknown) => request<T>("PUT", url, body),
  del: <T>(url: string) => request<T>("DELETE", url),
};

// ---- SSE 对话流 ----------------------------------------------------------------
// 事件协议与 api/chat.py 一一对应；前端永远以 message_replace / 权威文本为最终真相。

export type ChatEvent =
  | { type: "start"; role: { role_id: string; role_name: string } }
  | { type: "token"; text: string }
  | { type: "message_replace"; text: string }
  | { type: "tool_call"; name: string; args: Record<string, unknown> }
  | { type: "tool_result"; name: string; content: string }
  | { type: "error"; detail: string }
  | { type: "end" };

export async function streamChat(
  threadId: string,
  message: string,
  onEvent: (ev: ChatEvent) => void,
): Promise<void> {
  const res = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ thread_id: threadId, message }),
  });
  if (!res.ok || !res.body) {
    let detail = `HTTP ${res.status}`;
    try {
      detail = ((await res.json()) as { detail?: string }).detail || detail;
    } catch {
      /* keep */
    }
    onEvent({ type: "error", detail });
    onEvent({ type: "end" });
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx: number;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const frame = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      const line = frame.split("\n").find((l) => l.startsWith("data: "));
      if (!line) continue;
      onEvent(JSON.parse(line.slice(6)) as ChatEvent);
    }
  }
}
