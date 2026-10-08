// 后端调用的唯一出口：transport 核心（`request`/`upload`/`ApiError`）+ `api` 对象。
// 契约类型按路由域住在隔壁 15 个文件、SSE 一族在 `./sse`，本文件末尾用 `export *`
// 把它们并成同一个接口面 —— 消费者只认 `../api`，不认哪个名字住在哪个文件。
// 与后端契约一一对应：端点的事实面是 `api/routers/` 各文件里的路由声明（FastAPI 注册
// 即真值），每个路由的 docstring 讲它自己的口径 —— 这里不发明第二个事实来源。
// （从前这行指的"api/main.py 的模块 docstring 端点清单"并不存在，2026-10-08 加进度
// 端点时核对发现：指针指向了没有的东西，按"living 引用"的规矩改指真住址。）
//
// 住址说明（快照 P3-1 第三、四刀）：本文件从前是 `src/api.ts`，先成 `src/api/index.ts`，
// 契约类型再按路由域拆进隔壁 15 个文件（本文件只留 transport 核心 + `api` 对象 + 再导出面）。
// 消费者写的都是 `./api` / `../api`（不认文件名），bundler 解析认目录 index ⇒
// **调用点一字未动**（条数刻意不写在这里：它会随每次新增消费者漂，同"行号不进名单"那条理由）；
// 真正需要改的是本文件**自己向外**的三条 `./lib/*`（搬进目录后深度 +1 ⇒ `../lib/*`）。
// 这条不对称是本刀的全部风险：向内引用靠解析器兜住，向外引用不会报错在消费者身上，
// 而是报错在本文件里 —— 少改一条，tsc 当场 TS2307（实测过）。

import { apiBase, authHeaders } from "../lib/dataSource";
// 只取类型（`import type`）：upload() 的返回体形状跟上传结果解读共用一个定义，
// 免得"接口返回什么"在两处各写一遍。uploadOutcome 不 import 本文件，不存在循环。
import type { UploadResponse, UploadTaskProgress } from "../lib/uploadOutcome";
import type { LocalServiceStatus, ResidentModel } from "./models";
import type { ExtractResult } from "./knowledge";
import type { GenericRecord } from "./records";
import type { ConsolidateOutcome, DistillOutcome } from "./memory";
import type { TreeResult, WorkspaceDir } from "./workspace";
import type { ReachoutsPage } from "./reachouts";
import type { ApprovalRow, ApprovalsPage } from "./approvals";
import type { ShellRelease } from "./release";

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
export function readableDetail(raw: unknown, fallback: string): string {
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
    // 数据源（M5）：本机 = 同源相对路径；云端 = 换 origin 并带上那次登录拿到的凭据。
    opt.headers = { ...(opt.headers as Record<string, string>), ...authHeaders() };
    res = await fetch(apiBase() + url, { ...opt, signal: controller.signal });
  } catch (e) {
    // 把超时与网络错误区分开：前者要告诉用户"后端没响应"，而不是笼统的 fetch failed。
    if ((e as Error).name === "AbortError") {
      const secs = timeoutMs / 1000;
      // 措辞必须诚实：前端不再等了，但**后端很可能还在跑**（上传/抽取就是这样）。
      // 说成"后端已挂起"会让人以为白做了，于是重复提交。
      throw new ApiError(
        0,
        `已等待 ${secs}s 仍未返回，前端停止等待（那一端可能仍在处理：稍后刷新看看结果）`,
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

// ---- 事件簿（docs/主动消息与记忆设计稿.md §6）---------------------------------------------
// 与 `core/timeline.py` 的响应一一对应。**后端不写文案**：它只给 `kind` + `verb`，
// "记下：/ 更正：/ 开始聊："这些中文动词在这里造句 —— 替界面造句的下一步，就是想改一句
// 措辞时发现得改两端。

// ---- 命令执行审批（架构计划 C·§6.2） ----------------------------------------------------
// 与 core/approvals.py 的 `_row`、api/routers/approvals.py 的响应一一对应。

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
  /** 上传文件到某个对话（P3-3：201 只是受理，解析/入索引在后台 —— 终局问 uploadTask）。 */
  upload: (threadId: string, form: FormData) =>
    request<UploadResponse>(
      "POST",
      `/api/session/${threadId}/upload`,
      form,
      LONG_REQUEST_TIMEOUT_MS,
    ),
  /** 上传进度查询：`running`（后台是否在跑）+ `status`（台账终态）并读才不含糊。 */
  uploadTask: (taskId: string) =>
    request<UploadTaskProgress>("GET", `/api/uploads/tasks/${taskId}`),
  /** 模型供应商目录（设置页下拉动态来源）。 */
  setModelContext: (name: string, numCtx: number | null) =>
    request<{ name: string; num_ctx: number | null }>(
      "PATCH",
      `/api/settings/models/${name}/context`,
      { num_ctx: numCtx },
    ),
  /** 采样惩罚：一次只改一栏，回的是**库里的现值**（三栏都在）。null = 清回"不传"。 */
  setModelSampling: (
    name: string,
    values: Partial<{ repeat_penalty: number | null; frequency_penalty: number | null; presence_penalty: number | null }>,
  ) =>
    request<{
      name: string;
      repeat_penalty: number | null;
      frequency_penalty: number | null;
      presence_penalty: number | null;
    }>("PATCH", `/api/settings/models/${name}/sampling`, values),
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
  /** 进入对话界面 = 都看过了（用户 2026-09-23 定的口径）：所有未读一次标完。 */
  markAllReachoutsRead: () => request<ReachoutsPage>("POST", "/api/reachouts/read-all"),
  /** 删抽屉里的**那一行投递记录**。她说过的那句仍留在对话里（那是她下次开口的依据）。 */
  deleteReachout: (id: number) => request<ReachoutsPage>("DELETE", `/api/reachouts/${id}`),
  /** 清空主动消息记录：给了角色就只清那个角色，不给就全清。同样不碰对话里的原话。 */
  clearReachouts: (roleId?: string) =>
    request<ReachoutsPage>(`DELETE`, `/api/reachouts${roleId ? `?role_id=${encodeURIComponent(roleId)}` : ""}`),
  /** 只问"这条主动会话在不在"（不建行）：清空抽屉之后面板仍要能读到历史。 */
  proactiveThread: (roleId: string) =>
    request<{ thread_id: string | null; role_id: string }>(
      "GET",
      `/api/session/proactive?role_id=${encodeURIComponent(roleId)}`,
    ),
  /** 命令执行审批（架构计划 C·§6.2）：列表（含 pending 计数）与批准/拒绝。 */
  getApprovals: (status?: string) =>
    request<ApprovalsPage>(
      "GET",
      `/api/approvals${status ? `?status=${encodeURIComponent(status)}` : ""}`,
    ),
  decideApproval: (id: number, decision: "approve" | "reject", token: string | null) =>
    request<ApprovalRow>("POST", `/api/approvals/${id}/decide`, { decision, token }),
  /** 叫停这一轮生成（#18）。幂等；真正的收手发生在服务端消费分块的那一层。 */
  stopTurn: (threadId: string) =>
    request<{ thread_id: string; requested: boolean }>(
      "POST",
      `/api/session/${encodeURIComponent(threadId)}/stop`,
    ),
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

// ---- 契约类型的再导出面（P3-1 第四刀：类型按路由域住在隔壁文件，接口面留在这里）
// 消费者一律 `from "../api"`；这里用通配，而不是再抄一份 61 个名字的清单
// —— 抄一份就是给「哪天改了那边、忘了改这里」留门（与 SSE 那刀的接口面同一口径）。
export * from "./roles";
export * from "./sessions";
export * from "./models";
export * from "./services";
export * from "./knowledge";
export * from "./records";
export * from "./extensions";
export * from "./runtime";
export * from "./audit";
export * from "./memory";
export * from "./workspace";
export * from "./reachouts";
export * from "./approvals";
export * from "./mcp";
export * from "./release";
export { streamChat, streamEdit } from "./sse";
export type { ChatEvent } from "./sse";
