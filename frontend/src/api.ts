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
  /** 配置用途（模型页=云端配置唯一事实面）：chat=对话推理 | embedding | rerank | ocr 凭据。 */
  usage: string;
  sort_order: number;
  has_key: boolean;
  key_masked: string | null;
}

export interface ModelProvider {
  id: string;
  label: string;
  needs_key: string; // "0" | "1"
  base_url_hint: string;
  style: string; // native | openai
}

export interface KnowledgeScopes {
  scopes: string[];
}

export interface GenericRecord {
  id: string;
  domain: string;
  label: string;
  value_text: string | null;
  value_num: number | null;
  unit: string | null;
  note: string | null;
  created_at: string;
}

export interface ModelSettings {
  default: string | null;
  backends: BackendRow[];
  fallbacks: string[];
}

export interface ToolEntry {
  name: string;
  description: string;
}

export interface ToolCatalog {
  kernel: ToolEntry[];
  domains: Record<string, ToolEntry[]>;
}

export interface IndexRow {
  index_id: string;
  index_name: string;
  index_value: number | null;
  value_text: string | null;
  unit: string | null;
  ref_range: string | null;
  is_verified: number | boolean;
  source: string;
  raw_text: string | null;
}

export interface ReportRecord {
  report_id: string;
  report_type: string;
  check_time: string;
  institution: string | null;
  note: string | null;
  indices: IndexRow[];
}

export interface AuditRow {
  ts: string;
  actor: string;
  action: string;
  target: string | null;
  detail_json: string | null;
}

export interface KnowledgeScope {
  scope: string;
  chunks: number;
  sources: string[];
  embedder: string;
}

/** 检索延迟分位（每阶段，单位 ms；无样本时各字段为 null）。 */
export interface RagStageMs {
  embed_ms: number | null;
  vector_ms: number | null;
  rerank_ms: number | null;
  total_ms: number | null;
}

export interface RagMetrics {
  samples: number;
  rerank_enabled: boolean;
  embedder: string;
  p50: RagStageMs;
  p95: RagStageMs;
  p99: RagStageMs;
}

/** 结构化抽取：写入档案的指标行 */
export interface ExtractWritten {
  index_name: string;
  index_value: number | null;
  value_text: string | null;
  unit: string | null;
}

/** 抽取时没能通过校验 / 两次识别不一致的项 —— 交给人确认，未写库 */
export interface ExtractConflict {
  index_name: string;
  reason: string;
  primary: Record<string, unknown> | null;
  verify: Record<string, unknown> | null;
}

export interface ExtractResult {
  mode: string; // cross（双模型）| self（同模型复查，弱校对）| off
  report_type: string;
  check_time: string;
  institution: string | null;
  written: ExtractWritten[];
  conflicts: ExtractConflict[];
  notes: string[];
  /** 降级原因：no_model / no_text / already_extracted */
  skipped?: string;
  detail?: string;
  report_id?: string;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    detail: string,
  ) {
    super(detail);
  }
}

/** FastAPI 的 detail 可能是字符串（业务错误）或数组（422 校验错误）——
 *  统一转成可读文本，杜绝 "[object Object]" 这种不可诊断的报错。 */
function readableDetail(raw: unknown, fallback: string): string {
  if (typeof raw === "string" && raw.trim()) return raw;
  if (Array.isArray(raw)) {
    const lines = raw
      .map((item) => {
        const o = item as { msg?: string; loc?: unknown[] };
        const loc = Array.isArray(o.loc) ? o.loc.join(".") : "";
        return o.msg ? (loc ? `${loc}: ${o.msg}` : o.msg) : JSON.stringify(item);
      })
      .filter(Boolean);
    if (lines.length) return lines.join("；");
  }
  if (raw && typeof raw === "object") {
    const text = JSON.stringify(raw);
    return text === "{}" ? fallback : text;
  }
  return fallback;
}

async function request<T>(method: string, url: string, body?: unknown): Promise<T> {
  const opt: RequestInit = { method, headers: {} };
  if (body !== undefined) {
    if (body instanceof FormData) {
      // FormData 交给浏览器设置 multipart 边界，绝不能手动盖 JSON 头
      opt.body = body;
    } else {
      // 关键：fetch 不会自动序列化对象 —— 必须显式 JSON.stringify。
      // 否则 body 会退化成 "[object Object]"，服务端 JSON 解析失败 → 422。
      // （踩过的坑：只设 Content-Type 不序列化，页面 GET 全正常，所有写操作静默 422。）
      opt.headers = { "Content-Type": "application/json" };
      opt.body = JSON.stringify(body);
    }
  }
  const res = await fetch(url, opt);
  if (!res.ok) {
    const fallback = `${res.status} ${res.statusText}`;
    let raw: unknown = null;
    try {
      raw = await res.json();
    } catch {
      /* 非 JSON 错误体，保留状态码 */
    }
    const detail =
      raw && typeof raw === "object" && "detail" in raw
        ? readableDetail((raw as { detail: unknown }).detail, fallback)
        : fallback;
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
  /** 结构化抽取：把已上传的报告文本抽成指标行（三层校验，只写双方一致的项）。 */
  extractRecord: (taskId: string) =>
    request<ExtractResult>("POST", "/api/records/extract", { task_id: taskId }),
  /** 模型供应商目录（设置页下拉动态来源）。 */
  getProviders: () => request<{ providers: ModelProvider[] }>("GET", "/api/settings/model-providers"),
  /** 知识作用域候选（角色卡下拉来源：真实已建的知识集合）。 */
  getKnowledgeScopes: () => request<KnowledgeScopes>("GET", "/api/knowledge/scopes"),
  /** 通用领域记录（非 health 域的数据增删改查）。 */
  listDomainRecords: (domain: string) =>
    request<GenericRecord[]>("GET", `/api/domains/${domain}/records`),
  addDomainRecord: (domain: string, body: unknown) =>
    request<GenericRecord>("POST", `/api/domains/${domain}/records`, body),
  patchDomainRecord: (domain: string, id: string, body: unknown) =>
    request<GenericRecord>("PATCH", `/api/domains/${domain}/records/${id}`, body),
  deleteDomainRecord: (domain: string, id: string) =>
    request<void>("DELETE", `/api/domains/${domain}/records/${id}`),
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
  signal?: AbortSignal,
): Promise<void> {
  let res: Response;
  try {
    res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ thread_id: threadId, message }),
      signal, // 用户点「停止」→ controller.abort()，这里会以 AbortError 结束
    });
  } catch (e) {
    if ((e as Error).name !== "AbortError") {
      onEvent({ type: "error", detail: (e as Error).message });
    }
    onEvent({ type: "end" });
    return;
  }
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
  try {
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
  } catch (e) {
    // 中断不是错误：静默结束，由调用方做收尾（回放 checkpoint 拿到已生成的部分）
    if ((e as Error).name !== "AbortError") {
      onEvent({ type: "error", detail: (e as Error).message });
    }
  }
}
