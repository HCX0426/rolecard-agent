// 与后端契约一一对应的类型 + fetch 封装 + SSE 流式读取。
// 端点清单见 api/main.py 的模块 docstring —— 这里不发明第二个事实来源。

import { parseSseFrame, splitSseFrames } from "./lib/stream";
// 只取类型（`import type`）：upload() 的返回体形状跟上传结果解读共用一个定义，
// 免得"接口返回什么"在两处各写一遍。uploadOutcome 不 import 本文件，不存在循环。
import type { UploadResponse } from "./lib/uploadOutcome";

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
  /** 角色主动开口资格（架构计划 B）：还需全局 REACHOUT_ENABLED 开着才生效。 */
  reachout_enabled?: boolean;
  /** 关系驱动主动开口（架构计划 §5.2）：回忆触发开关，默认开。 */
  recall_enabled?: boolean;
  /** 关系驱动主动开口（架构计划 §5.2）：时段规律触发开关，默认开。 */
  time_pattern_enabled?: boolean;
  /** 文件事件触发（架构计划 C·§5.2）：该角色可否被任务目录变化触发，默认开。 */
  file_watch_enabled?: boolean;
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
  /** 会话级对话模式（后端返回有效值：会话覆盖 or 全局默认）。 */
  agent_mode?: string;
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
  /** 用户消息附带的图片（data URL；多模态传图，2026-09-18）。回放时用户气泡显示小图。 */
  image?: string;
}

/** 历史消息的分页响应（默认只回最近 500 条，`truncated` 为真时前端要如实说明）。 */
export interface MessagePage {
  messages: MessageRow[];
  total: number;
  limit: number;
  truncated: boolean;
}

export interface BackendRow {
  name: string;
  provider: string;
  base_url: string | null;
  model: string;
  sort_order: number;
  /** 本地 Ollama 的实际上下文窗口（tokens）；null = 引擎默认。 */
  num_ctx: number | null;
  /** 后端能力位：能否收图。对话页据此渲染「视觉」徽标；后端还把它当作**调用前拦截的一半
   *  证据**（与 Ollama `/api/show` 的实测同时为否才拒，见 core/nodes P1-2）。发图按钮故意
   *  不据此 disabled —— 判定只留在后端一处。 */
  supports_vision: boolean;
  /** 后端能力位：工具调用是否可用（false 时该轮不绑工具，兼容带 tools 会返回空的云端 VLM）。 */
  supports_tools: boolean;
  has_key: boolean;
  key_masked: string | null;
  /** 派生只读：这行被哪些服务引用（模型页不再有用途下拉，见 ProviderModelRow.used_by）。 */
  used_by?: string[];
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

/** 模型设置：**只有分组这一个视图**（旧平铺 `backends` 投影随旧界面一起删了）。
 * 行上的凭据（provider/base_url/has_key）来自它所属的组 —— 需要平铺时用 helper 摊平。 */
export interface ModelSettings {
  default: string | null;
  fallbacks: string[];
  providers: ProviderGroup[];
}

/** 服务类别的内部键 → 给人看的名字。模型页与服务页都在回显 `used_by`，译名只留一份
 *  （两处各写一遍就是"同一个事实两个答案"，两页会各自漂移）。 */
export const USAGE_LABEL: Record<string, string> = {
  chat: "对话",
  embedding: "嵌入",
  rerank: "重排",
  ocr: "OCR",
};

/** 一行模型（属于某个凭据组）。能力位是**三态**：null = 没测过 → 界面渲染 `?`。 */
export interface ProviderModelRow {
  name: string;
  model: string;
  num_ctx: number | null;
  supports_vision: boolean | null;
  supports_tools: boolean | null;
  /** 这行被哪些服务引用（派生自服务页，模型页只读）：chat / embedding / rerank / ocr。 */
  used_by: string[];
  is_default: boolean;
}

/** 凭据组：一组 = 一个 (供应商, 端点)，key 只在这里出现一次。 */
export interface ProviderGroup {
  id: string;
  provider: string;
  label: string;
  base_url: string | null;
  style: string; // native | openai
  needs_key: boolean;
  has_key: boolean;
  key_masked: string | null;
  models: ProviderModelRow[];
}

/** 探测结论（`POST /api/settings/models/probe`）。三态字段 null = 不知道，不是"不行"。 */
export interface ProbeOutcome {
  reachable: boolean;
  detail: string;
  model_listed: boolean | null;
  tools: boolean | null;
  vision: boolean | null;
  vision_source: "free-metadata" | "uploaded-image" | "not-tested" | string;
  calls_used: number;
  models: string[];
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
 * 取值：30s 远大于本地 SQLite + 本地模型的正常响应，又足够短到"卡了能看见"。
 * **但上传与抽取不能套这个值** —— 见 LONG_REQUEST_TIMEOUT_MS。
 */
const REQUEST_TIMEOUT_MS = 30_000;

/**
 * 上传 / 抽取这类"耗时不可预期"的请求用更长超时。
 *
 * 为什么不能沿用 30s：后端的 OCR 子进程单次上限就是 120s（见 sessions.py 的上传路径），
 * 抽取还要跑两次模型调用。30s 到点前端 abort，界面说"上传失败/AI 识别指标失败"，
 * 而后端线程仍在跑并且**已经把文件落了盘、写进了索引/报告库** —— 用户看到的是假失败，
 * 还很可能照着这个假失败再点一次（审查报告 P1-4）。
 * 300s 是"明显比后端任何一条路径都长"的取值：它只用来兜住真挂死，不参与正常判定。
 */
const LONG_REQUEST_TIMEOUT_MS = 300_000;

async function request<T>(
  method: string,
  url: string,
  body?: unknown,
  timeoutMs: number = REQUEST_TIMEOUT_MS,
): Promise<T> {
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
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let res: Response;
  try {
    res = await fetch(url, { ...opt, signal: controller.signal });
  } catch (e) {
    // 把超时与网络错误区分开：前者要告诉用户"后端没响应"，而不是笼统的 fetch failed。
    if ((e as Error).name === "AbortError") {
      const secs = timeoutMs / 1000;
      // 措辞必须诚实：前端不再等了，但**后端很可能还在跑**（上传/抽取就是这样）。
      // 说成"后端已挂起"会让人以为白做了，于是重复提交。
      throw new ApiError(
        0,
        `已等待 ${secs}s 仍未返回，前端停止等待（后端可能仍在处理：稍后刷新看看结果）`,
      );
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

/** 任务目录（角色可读写的授权范围）：path = 生效目录，overridden = 是否 DB 覆盖（否=跟随 env）。 */
export interface WorkspaceDir {
  path: string;
  overridden: boolean;
}

export interface TreeEntry {
  name: string;
  is_dir: boolean;
  size: number;
}

export interface TreeResult {
  path: string;
  parent: string;
  entries: TreeEntry[];
  truncated: boolean;
}

/** 角色主动开口（收件箱）条目：独立于对话历史，未读/已读状态由 state 表达。 */
export interface ReachoutRow {
  id: number;
  role_id: string;
  role_name: string | null;
  text: string;
  state: "unread" | "read";
  created_at: string;
  /** 该角色的"主动会话"线程 id：点进去能翻历史、能直接回话。
   *  null = 这条消息还没有对应会话（本功能上线前落库的老消息）→ 界面只给"标记已读"。 */
  thread_id: string | null;
}

export interface ReachoutsPage {
  items: ReachoutRow[];
  unread: number;
  /** 挂起的任务目录变更条数（文件事件触发开启时 >0 = 角色正攒着素材）。 */
  file_watch_pending?: number;
  /** 收件箱折叠窗口（天，1/3/7）：同一角色在一个窗口里的开口折成一行。 */
  merge_days?: number;
}

// ---- 事件簿（docs/主动消息与记忆设计稿.md §6）---------------------------------------------
// 与 `core/timeline.py` 的响应一一对应。**后端不写文案**：它只给 `kind` + `verb`，
// "记下：/ 更正：/ 开始聊："这些中文动词在这里造句 —— 替界面造句的下一步，就是想改一句
// 措辞时发现得改两端。

export type TimelineKind = "reachout" | "memory" | "memory_correct" | "thread";

export interface TimelineEvent {
  kind: TimelineKind;
  /** "YYYY-MM-DD HH:MM:SS"（库里原文，定宽 ⇒ 界面直接切片，不再解析一遍时区）。 */
  at: string;
  text: string;
  /** 只有会话锚点带 `start` / `recent`；`correct` 只出现在"更正"那一条。 */
  verb: "start" | "recent" | "correct" | null;
  /** "更正"那条被作废的旧事实原文：划掉显示，这是 §3「失效不删」第一次被用户看见的地方。 */
  from_text: string | null;
  /** 可跳转的会话；null = 没有（记忆条目、上线前的老消息）⇒ 不给死链。 */
  thread_id: string | null;
  ref_id: number;
}

export interface TimelinePage {
  role_id: string;
  items: TimelineEvent[];
  next_cursor: string | null;
  /** 某个源扫到了上限：只说明"这里可能还有更早的"，不承诺总量（数全量是归档功能的事）。 */
  truncated: boolean;
}

// ---- 命令执行审批（架构计划 C·§6.2） ----------------------------------------------------
// 与 core/approvals.py 的 `_row`、api/routers/approvals.py 的响应一一对应。

export type ApprovalStatus = "pending" | "approved" | "rejected" | "done";

export interface ApprovalResult {
  exit_code: number | null;
  output: string;
  duration_ms: number;
  output_bytes: number;
}

export interface ApprovalRow {
  id: number;
  command: string;
  cwd: string | null;
  role_id: string | null;
  role_name: string | null;
  thread_id: string | null;
  status: ApprovalStatus;
  result: ApprovalResult | null;
  /** 这条待批下发的**一次性决定令牌**：批准/拒绝必须原样带回（后端读完即清空）。
   *  它不是登录凭据 —— 单机形态本来就不登录；它证明的是"这一条你确实看到过"。 */
  decide_token: string | null;
  created_at: string;
  updated_at: string;
}

export interface ApprovalsPage {
  items: ApprovalRow[];
  /** 待批（pending）条数：侧栏红点计数用，不是"未读"，所以不叫 unread。 */
  pending: number;
}

/** MCP server（架构计划 C·§6.1 operator 接入；仅 http）。headers 值永不出明文（后端掩码）。 */
export interface McpServer {
  id: string;
  display_name: string;
  transport: string;
  url: string;
  headers: Record<string, string>;
  enabled: boolean;
}

export interface McpServersView {
  servers: McpServer[];
  effective_count: number;
}

/** POST /api/mcp/servers/{id}/test 的结果（不落库，真连一次列工具）。 */
export interface McpTestResult {
  id: string;
  ok: boolean;
  tool_count: number;
  tools: string[];
  error?: string;
}

/** POST /api/services/check 的单探测量（ollama 或 openai_compatible 之一）。 */
export interface ConnectivityProbe {
  reachable: boolean;
  detail: string;
  models?: string[];
}
export type ConnectivityResult = Record<string, ConnectivityProbe>;

/** 本地推理服务（Ollama）的一张状态快照：模型页「本地推理服务」卡的数据面。
 *  `pinned` = 常驻（`keep_alive=-1`，自己永远不会让出显存）；`is_local` 决定给不给"常驻"按钮。 */
export interface ResidentModel {
  name: string | null;
  size_bytes: number;
  expires_at: string | null;
  pinned: boolean;
}
export interface LocalServiceStatus {
  base_url: string;
  running: boolean;
  resident: ResidentModel[];
  resident_bytes: number;
  pinned: boolean;
  is_local: boolean;
  model: string | null;
}

/** 桌面壳安装包的可下载状态（D②-4）。`available=false` 时**其余字段都不存在**，
 *  界面也就整卡不渲染 —— "能不能下载"由后端看产物文件在不在决定，不是由前端猜。 */
export interface ShellRelease {
  available: boolean;
  configured: boolean;
  file_name?: string;
  size_bytes?: number;
  built_at?: string;
}

export const api = {
  get: <T>(url: string) => request<T>("GET", url),
  post: <T>(url: string, body?: unknown) => request<T>("POST", url, body),
  patch: <T>(url: string, body: unknown) => request<T>("PATCH", url, body),
  put: <T>(url: string, body: unknown) => request<T>("PUT", url, body),
  del: <T>(url: string) => request<T>("DELETE", url),
  /** 结构化抽取：把已上传的报告文本抽成指标行（三层校验，只写双方一致的项）。 */
  extractRecord: (taskId: string) =>
    request<ExtractResult>(
      "POST",
      "/api/records/extract",
      { task_id: taskId },
      LONG_REQUEST_TIMEOUT_MS,
    ),
  /** 上传文件到某个对话（解析 + 入检索索引）。走长超时：OCR 子进程本身就允许 120s。 */
  upload: (threadId: string, form: FormData) =>
    request<UploadResponse>(
      "POST",
      `/api/session/${threadId}/upload`,
      form,
      LONG_REQUEST_TIMEOUT_MS,
    ),
  /** 模型供应商目录（设置页下拉动态来源）。 */
  setModelContext: (name: string, numCtx: number | null) =>
    request<{ name: string; num_ctx: number | null }>(
      "PATCH",
      `/api/settings/models/${name}/context`,
      { num_ctx: numCtx },
    ),
  enhancePrompt: (text: string) =>
    request<{ text: string }>("POST", "/api/prompt/enhance", { text }),
  /** 本地推理服务状态（在不在跑 / 驻留了哪些模型 / 占多少显存）。 */
  getLocalService: () => request<LocalServiceStatus>("GET", "/api/local-service"),
  /** 桌面壳安装包有没有得下（D②-4；没有产物时 available=false）。 */
  getShellRelease: () => request<ShellRelease>("GET", "/api/shell-release"),
  /** 把默认模型载入显存并常驻。长超时：8GB 卡上冷加载就要十几~几十秒，30s 会报假失败。 */
  pinLocalModel: () =>
    request<{ model: string; resident: ResidentModel[] }>(
      "POST",
      "/api/local-service/pin",
      { keep_alive: -1 },
      LONG_REQUEST_TIMEOUT_MS,
    ),
  /** 释放显存（省略 model = 卸掉当前驻留的全部模型）。卸载是即时动作，默认超时即可。 */
  unloadLocalModel: (model?: string) =>
    request<{ unloaded: (string | null)[]; skipped: (string | null)[] }>(
      "POST",
      "/api/local-service/unload",
      model ? { model } : {},
    ),
  /** 通用领域记录（非 health 域的数据增删改查）。 */
  listDomainRecords: (domain: string) =>
    request<GenericRecord[]>("GET", `/api/domains/${domain}/records`),
  addDomainRecord: (domain: string, body: unknown) =>
    request<GenericRecord>("POST", `/api/domains/${domain}/records`, body),
  patchDomainRecord: (domain: string, id: string, body: unknown) =>
    request<GenericRecord>("PATCH", `/api/domains/${domain}/records/${id}`, body),
  deleteDomainRecord: (domain: string, id: string) =>
    request<void>("DELETE", `/api/domains/${domain}/records/${id}`),
  /** 任务目录（file1）：查看 / 设置 / 清除 + 设置页目录树浏览。 */
  getWorkspaceDir: () => request<WorkspaceDir>("GET", "/api/workspace/dir"),
  setWorkspaceDir: (path: string) =>
    request<WorkspaceDir>("PUT", "/api/workspace/dir", { path }),
  clearWorkspaceDir: () => request<WorkspaceDir>("DELETE", "/api/workspace/dir"),
  browseTree: (path?: string) =>
    request<TreeResult>("GET", `/api/workspace/tree${path ? `?path=${encodeURIComponent(path)}` : ""}`),
  /** 角色主动开口（收件箱）：列表（含未读计数）与标记已读（静音，无提示音）。可按角色过滤。 */
  getReachouts: (roleId?: string) =>
    request<ReachoutsPage>(
      "GET",
      `/api/reachouts${roleId ? `?role_id=${encodeURIComponent(roleId)}` : ""}`,
    ),
  markReachoutRead: (id: number) => request<ReachoutsPage>("POST", `/api/reachouts/${id}/read`),
  /** 点进某角色的「主动会话」时调用：那一摞未读一次标完（看见 = 读过）。 */
  markRoleReachoutsRead: (roleId: string) =>
    request<ReachoutsPage>(
      "POST",
      `/api/reachouts/read-by-role?role_id=${encodeURIComponent(roleId)}`,
    ),
  /** 命令执行审批（架构计划 C·§6.2）：列表（含 pending 计数）与批准/拒绝。 */
  getApprovals: (status?: string) =>
    request<ApprovalsPage>(
      "GET",
      `/api/approvals${status ? `?status=${encodeURIComponent(status)}` : ""}`,
    ),
  decideApproval: (id: number, decision: "approve" | "reject", token: string | null) =>
    request<ApprovalRow>("POST", `/api/approvals/${id}/decide`, { decision, token }),
  /** 提取精华：把这段对话抽成记忆条目（一次真模型调用 → 长超时，本地卡上就要几十秒）。 */
  distillSession: (threadId: string) =>
    request<DistillOutcome>(
      "POST",
      `/api/session/${threadId}/distill`,
      undefined,
      LONG_REQUEST_TIMEOUT_MS,
    ),
  /** 整理记忆：合并同义条目、让过时条目失效（同样是一次真模型调用）。 */
  consolidateMemory: (roleId?: string) =>
    request<ConsolidateOutcome>(
      "POST",
      `/api/settings/memory/consolidate${roleId ? `?role_id=${encodeURIComponent(roleId)}` : ""}`,
      undefined,
      LONG_REQUEST_TIMEOUT_MS,
    ),
};

/**
 * 一次模型调用的账本（`core/memory_distill.py::_report`）。
 *
 * `tokens` 可能是 null：模型没报 usage 时后端**不编一个数**，界面也就不显示成本，
 * 而不是显示 0（0 会被读成"这次没花钱"）。
 */
export interface DistillReport {
  added: number;
  updated: number;
  /** 有几条字面上看着像同一件事。只是提示：合并要人在记忆卡上发起「整理记忆」。 */
  similar: number;
  merged: number;
  invalidated: number;
  noop: number;
  skipped: number;
  detail: string;
  tokens: number | null;
  before: number | null;
  after: number | null;
}

export interface DistillOutcome {
  report: DistillReport;
  /** 距上次提取又攒了几轮（0 = 刚提取过）。 */
  turns_since: number;
}

/** 整理完顺手回一份当前桶的记忆视图：界面不用再发一次 GET 就能刷新列表。 */
export interface ConsolidateOutcome extends DistillOutcome {
  enabled: boolean;
  role_id: string | null;
  content: string;
  items: {
    id: number;
    text: string;
    source: string;
    pinned: boolean;
    hit_count: number;
    last_hit_at: string | null;
    created_at: string | null;
  }[];
  active_count: number;
  limit: number;
  over_limit: boolean;
  extract_turns: number;
}

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

/** 编辑一条自己发过的消息并从那里重新生成（SSE 事件流与 streamChat 完全一致）。 */
export async function streamEdit(
  threadId: string,
  messageId: string,
  content: string,
  onEvent: (ev: ChatEvent) => void,
  signal?: AbortSignal,
  image?: string | null, // 重新生成/编辑时保留原图（多模态传图，2026-09-18）
): Promise<void> {
  let res: Response;
  try {
    res = await fetch(`/api/session/${threadId}/messages/edit`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message_id: messageId, content, ...(image ? { image } : {}) }),
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
  image?: string | null, // 多模态传图：data URL（None = 纯文本）
): Promise<void> {
  let res: Response;
  try {
    res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ thread_id: threadId, message, ...(image ? { image } : {}) }),
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
