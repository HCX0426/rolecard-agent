// 与后端契约一一对应的类型 + fetch 封装 + SSE 流式读取。
// 端点清单见 api/main.py 的模块 docstring —— 这里不发明第二个事实来源。

import { parseSseFrame, splitSseFrames } from "./lib/stream";

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
  /** 消息在 checkpoint 中的寻址 id：编辑 / 删除按它定位（LangGraph RemoveMessage）。 */
  id?: string;
  /** 思考过程（仅思考模型；随 checkpoint 一起回放，因此刷新后仍在）。 */
  reasoning?: string;
  tools?: (string | null)[];
  name?: string;
  /** 工具行入参摘要（AI 消息 tool_calls 按 id 配对）：历史里"搜了什么"可见。 */
  args?: Record<string, unknown>;
  /** 消息创建时间（本地时间字符串）；旧 checkpoint 消息没有该字段。 */
  ts?: string;
}

export interface BackendRow {
  name: string;
  provider: string;
  base_url: string | null;
  model: string;
  /** 配置用途（模型页=云端配置唯一事实面）：chat=对话推理 | embedding | rerank | ocr 凭据。 */
  usage: string;
  sort_order: number;
  /** 本地 Ollama 的实际上下文窗口（tokens）；null = 引擎默认。 */
  num_ctx: number | null;
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

/** 运行环境（env + DB 覆盖）的展示项。kind=ro 表示不可在线修改；secret 永不回明文。 */
export interface RuntimeItem {
  key: string;
  field: string;
  label: string;
  value: string;
  default: string;
  changed: boolean;
  /** DB 覆盖在位（≠ changed：env 也可能与出厂默认不同）。 */
  overridden: boolean;
  /** 覆盖的原始值（可编辑初值）；secret / 未覆盖 = null。 */
  override_value: string | null;
  kind: "bool" | "str" | "secret" | "float" | "int" | "ro";
  choices: string[] | null;
  note: string;
}

export interface RuntimeGroup {
  key: string;
  label: string;
  items: RuntimeItem[];
}

export interface RuntimePayload {
  note: string;
  groups: RuntimeGroup[];
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

/**
 * 请求超时（毫秒）。
 *
 * 为什么必须有：`fetch` 默认**永远不等**——后端挂起（进程在但不响应）时，页面会一直转圈，
 * 用户既看不到错误也看不到结果，只能刷新。浏览器自身的兜底要几分钟之后才触发。
 *
 * 取值：上传与抽取走独立路径且耗时不可预期，这里只约束**普通 JSON 请求**。
 * 30s 远大于本地 SQLite + 本地模型的正常响应，又足够短到"卡了能看见"。
 */
const REQUEST_TIMEOUT_MS = 30_000;

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
  const controller = new AbortController();
  // 用全局 setTimeout 而不是 window.setTimeout：本文件也在 node 环境下被测试
  // （api.test.ts），那里没有 window —— 只有 jsdom 环境才有。
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  let res: Response;
  try {
    res = await fetch(url, { ...opt, signal: controller.signal });
  } catch (e) {
    // 把超时与网络错误区分开：前者要告诉用户"后端没响应"，而不是笼统的 fetch failed。
    if ((e as Error).name === "AbortError") {
      throw new ApiError(0, `请求超过 ${REQUEST_TIMEOUT_MS / 1000}s 无响应（后端可能已挂起）`);
    }
    throw new ApiError(0, `网络错误：${(e as Error).message}`);
  } finally {
    clearTimeout(timer);
  }
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
  setModelContext: (name: string, numCtx: number | null) =>
    request<{ name: string; num_ctx: number | null }>(
      "PATCH",
      `/api/settings/models/${name}/context`,
      { num_ctx: numCtx },
    ),
  enhancePrompt: (text: string) =>
    request<{ text: string }>("POST", "/api/prompt/enhance", { text }),
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
  | { type: "thinking"; text: string }
  | { type: "message_replace"; text: string }
  | { type: "context_trimmed"; dropped: number; kept: number }
  | { type: "tool_call"; name: string; args: Record<string, unknown> }
  | { type: "tool_result"; name: string; content: string }
  | { type: "error"; detail: string }
  | { type: "end" };

/** 会话的上下文预算事实（`GET /api/session/{id}/context`）。 */
export interface SessionContext {
  /** 最近一轮被裁掉的历史条数（0 = 没裁，界面不该提示）。 */
  trimmed: number;
  /** 最近一轮实际送进 prompt 的条数。 */
  kept: number;
  /** 当前配置的字符预算。可能与历史那一轮不同（操作员改过配置），所以一起给出。 */
  budget: number;
}

/** 上传目录里没被任何 ingestion 台账引用的文件（`/api/uploads/orphans`）。 */
export interface OrphanUpload {
  name: string;
  size: number;
  companion: boolean;
}

export interface OrphanReport {
  orphans: OrphanUpload[];
  total_bytes: number;
  scanned: number;
  referenced: number;
}

export interface CleanupResult {
  deleted: number;
  freed_bytes: number;
  scanned: number;
  referenced: number;
}

/** 删除会话中选中的问答对（后端按整轮扩展）。 */
export function deleteMessages(
  threadId: string,
  messageIds: string[],
): Promise<{ deleted: number; remaining: number }> {
  return request("POST", `/api/session/${threadId}/messages/delete`, { message_ids: messageIds });
}

/** 编辑一条自己发过的消息并从那里重新生成（SSE 事件流与 streamChat 完全一致）。 */
export async function streamEdit(
  threadId: string,
  messageId: string,
  content: string,
  onEvent: (ev: ChatEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  let res: Response;
  try {
    res = await fetch(`/api/session/${threadId}/messages/edit`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message_id: messageId, content }),
      signal,
    });
  } catch (e) {
    if ((e as Error).name !== "AbortError") onEvent({ type: "error", detail: (e as Error).message });
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
      const { frames, rest } = splitSseFrames(buf);
      buf = rest;
      for (const frame of frames) {
        const ev = parseSseFrame(frame);
        if (ev) onEvent(ev as ChatEvent);
      }
    }
  } catch (e) {
    if ((e as Error).name !== "AbortError") {
      onEvent({ type: "error", detail: (e as Error).message });
    }
  }
}

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
      // 帧切分与解析走 lib/stream.ts 的纯函数（可测）：半帧留在缓冲里，
      // 坏帧被消化成"这一帧没有事件"而不是抛异常中断整条流。
      const { frames, rest } = splitSseFrames(buf);
      buf = rest;
      for (const frame of frames) {
        const ev = parseSseFrame(frame);
        if (ev) onEvent(ev as ChatEvent);
      }
    }
  } catch (e) {
    // 中断不是错误：静默结束，由调用方做收尾（回放 checkpoint 拿到已生成的部分）
    if ((e as Error).name !== "AbortError") {
      onEvent({ type: "error", detail: (e as Error).message });
    }
  }
}
