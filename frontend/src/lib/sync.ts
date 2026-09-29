/**
 * 上行同步（M7）的客户端：把**本机这一份**带到云端那台实例。
 *
 * 这个文件刻意**不走 `apiBase()` / `authHeaders()`** —— 那是全应用唯一一条"打到哪儿"的通道，
 * 而这一族请求的目标与别处不同：
 *
 *   * **发往本机后端**（同源）。要推走的是"本机这份数据"，而它在云端态根本不在对面那台上；
 *     走 `apiBase()` 的症状是"让云端把云端自己推给云端"，一圈下来什么都没发生。
 *   * **不带当前那枚云端凭据当调用身份**。本机后端认的是本机的主人；对面是谁、用哪个账号
 *     写过去，是 `target` 里那两个字段的事（从 `dataSource` 现取，不在这里存第二份）。
 *
 * 三档语义与逐条裁决都在后端（`api/routers/sync.py`）算，这里只负责把人的选择送过去、
 * 把回来的读数摆出来。前端不重算任何"什么算冲突"——那是第二个真相源，一定会漂。
 */

import { ApiError, readableDetail } from "../api";
import { read as readDataSource } from "./dataSource";

export type SyncKind = "card" | "thread" | "memory" | "reachout";

/** 界面上可勾的同步项。**健康档案与上传原件这一版没有格子** ——
 *  没有实现的复选框比没有复选框更坏（勾了却什么都不发生，比看不见更让人怀疑整个界面）。 */
export const SYNC_ITEMS: { kind: SyncKind; label: string; hint: string }[] = [
  { kind: "card", label: "角色卡", hint: "含你改过的人设、范例与能力开关" },
  { kind: "thread", label: "会话", hint: "整条历史搬过去，检查点在对面重建" },
  { kind: "memory", label: "记忆", hint: "关于你的长期事实，按 uid 比不按字面" },
  { kind: "reachout", label: "主动消息", hint: "角色主动找过你的那些话，只追加" },
];

export type UploadMode = "merge" | "append" | "replace";

/** 同步方向。上传 = 本机 → 云端；下载 = 云端 → 本机。
 *  三档中「整份替换」只在上传方向可用：下载方向的整份替换会以云端数据覆盖本机全部数据，
 *  那一步应由用户在界面上逐项执行删除，不作为同步档位提供。 */
export type Direction = "up" | "down";

export const DIRECTIONS: { direction: Direction; label: string; sub: string }[] = [
  { direction: "up", label: "上传", sub: "本机 → 云端" },
  { direction: "down", label: "下载", sub: "云端 → 本机" },
];

export function modeLabel(mode: UploadMode): string {
  return { merge: "逐条合并", append: "仅上传新增", replace: "整份替换" }[mode];
}

export const UPLOAD_MODES: {
  mode: UploadMode;
  label: string;
  note: string;
  cost: string;
  /** 下载方向不提供的档位（界面以禁用态呈现并说明原因，而不是悄悄消失）。 */
  upOnly?: boolean;
}[] = [
  {
    mode: "merge",
    label: "逐条合并",
    note: "推荐",
    cost: "两端各自独有的条目直接同步；两端均有修改的条目，逐项由您确认保留的版本。",
  },
  {
    mode: "append",
    label: "仅上传新增",
    note: "快速",
    cost: "只上传对端尚不存在的条目，重复条目保持并存。",
  },
  {
    mode: "replace",
    label: "整份替换",
    note: "将覆盖云端数据",
    upOnly: true,
    cost: "清除云端该账号下的所选类目，以本机数据为准。",
  },
];

export interface BriefItem {
  kind: SyncKind;
  ident: string;
  hash: string;
  at: string;
  preview: string;
  head?: string;
  count?: number;
}

export interface ConflictRow {
  kind: SyncKind;
  ident: string;
  mine: { at: string; preview: string };
  theirs: { at: string; preview: string };
}

export interface Plan {
  counts: Record<string, number>;
  by_kind: Partial<Record<SyncKind, Record<string, number>>>;
  remote_counts: Partial<Record<SyncKind, number>>;
  skipped: { kind: string; ident: string; reason: string; preview?: string }[];
  conflicts: ConflictRow[];
  only_local: BriefItem[];
}

export interface ApplyResult {
  sent: number;
  mode: UploadMode;
  kinds: SyncKind[];
  conflicts_left: number;
  remote: {
    written?: Record<string, number>;
    skipped?: Record<string, number>;
    errors?: { kind: string; ident: string; error: string }[];
  };
}

/** 冲突裁决的键：`kind:ident`。同一枚 ident 在不同类里互不相干。 */
export function conflictKey(kind: SyncKind, ident: string): string {
  return `${kind}:${ident}`;
}

/** 「保留两个版本」只对记忆条目提供：角色卡与会话的身份即其 id，两版并存等于覆盖其一。 */
export function canKeepBoth(kind: SyncKind): boolean {
  return kind === "memory";
}

export interface PullResult {
  pulled: number;
  conflicts_left: number;
  local: {
    written?: Record<string, number>;
    skipped?: Record<string, number>;
    errors?: { kind: string; ident: string; error: string }[];
  };
}

export interface LeftConflict {
  kind: SyncKind;
  ident: string;
  mine: { at: string; preview: string };
  theirs: { at: string; preview: string };
}

export interface ReconcileResult {
  pushed: number;
  pulled: number;
  written: { remote?: Record<string, number>; local?: Record<string, number> };
  left_for_human: LeftConflict[];
}

const LOCAL_TIMEOUT_MS = 30_000;
/** 一次上行可能真的搬几百条会话过网，30s 会把"还在跑"报成"失败了"。 */
const APPLY_TIMEOUT_MS = 180_000;

/** 打到**本机**后端：URL 不加 `apiBase()` 前缀，头里也不带云端凭据（理由见文件头）。 */
async function toLocal<T>(method: string, url: string, body?: unknown, timeout = LOCAL_TIMEOUT_MS): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  let res: Response;
  try {
    res = await fetch(url, {
      method,
      headers: body === undefined ? {} : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    });
  } catch (e) {
    if ((e as Error).name === "AbortError") throw new ApiError(0, "本机后端没在限时内回答。");
    throw new ApiError(0, `网络错误：${(e as Error).message}`);
  } finally {
    clearTimeout(timer);
  }
  const text = await res.text();
  let raw: unknown = null;
  try {
    raw = text ? JSON.parse(text) : null;
  } catch {
    raw = text;
  }
  if (!res.ok) {
    throw new ApiError(
      res.status,
      readableDetail(
        raw && typeof raw === "object" && "detail" in raw ? (raw as { detail: unknown }).detail : null,
        `HTTP ${res.status}`,
      ),
    );
  }
  return raw as T;
}

/** 对面是哪台、用谁的身份写过去：从数据源那一处现取（不在这里存第二份）。 */
export function uploadTarget(): { base_url: string; user: string; secret: string } | null {
  const source = readDataSource();
  if (source.mode !== "cloud") return null;
  return { base_url: source.base, user: source.user, secret: source.secret };
}

export function fetchPlan(target: unknown): Promise<Plan> {
  return toLocal<Plan>("POST", "/api/sync/plan", target);
}

export function applyUpload(
  target: unknown,
  kinds: SyncKind[],
  mode: UploadMode,
  resolutions: Record<string, string>,
): Promise<ApplyResult> {
  return toLocal<ApplyResult>(
    "POST",
    "/api/sync/apply",
    { ...(target as object), kinds, mode, resolutions },
    APPLY_TIMEOUT_MS,
  );
}

/** 下载：把云端那份里本机没有的并回本机。默认（未裁决的冲突）保护本机现有版本。 */
export function pullDownload(
  target: unknown,
  kinds: SyncKind[],
  resolutions: Record<string, string> = {},
): Promise<PullResult> {
  return toLocal<PullResult>(
    "POST",
    "/api/sync/pull",
    { ...(target as object), kinds, mode: "merge", resolutions },
    APPLY_TIMEOUT_MS,
  );
}

/** 登录对账：双向各走一遍自动策略，歧义项留在 left_for_human 里由用户裁决。 */
export function reconcile(target: unknown): Promise<ReconcileResult> {
  return toLocal<ReconcileResult>(
    "POST",
    "/api/sync/reconcile",
    target,
    APPLY_TIMEOUT_MS,
  );
}

/** 上次同步的读数（侧栏那一行）。存 localStorage：它是"发生过的事实"，重载后还要显示。 */
const LAST_SYNC_KEY = "rolecard.sync.lastSync";

export interface LastSync {
  at: string;
  pushed: number;
  pulled: number;
}

export function readLastSync(): LastSync | null {
  try {
    const raw = localStorage.getItem(LAST_SYNC_KEY);
    return raw ? (JSON.parse(raw) as LastSync) : null;
  } catch {
    return null;
  }
}

export function saveLastSync(result: { pushed: number; pulled: number }): void {
  try {
    localStorage.setItem(
      LAST_SYNC_KEY,
      JSON.stringify({ at: new Date().toISOString(), ...result } satisfies LastSync),
    );
  } catch {
    /* 存不下就少一行状态文字，不是坏消息。 */
  }
}

/** 侧栏那一行的状态文字：「上次同步：09-27 21:40（↑3 ↓1）」。没同步过 = null。 */
export function formatLastSync(s: LastSync | null, now: Date = new Date()): string | null {
  if (!s) return null;
  const at = new Date(s.at);
  if (Number.isNaN(at.getTime())) return null;
  const hm = `${String(at.getHours()).padStart(2, "0")}:${String(at.getMinutes()).padStart(2, "0")}`;
  const sameDay =
    at.getFullYear() === now.getFullYear() &&
    at.getMonth() === now.getMonth() &&
    at.getDate() === now.getDate();
  const day = sameDay ? "今天" : `${at.getMonth() + 1}-${at.getDate()}`;
  return `上次同步：${day} ${hm}（↑${s.pushed} ↓${s.pulled}）`;
}

/** 预检屏的四格读数：数字来自两边的实际比对，不是估算。列名随方向翻转 ——
 *  接收侧一律「保留」，发出侧才是「将同步」。 */
export function planReads(
  plan: Plan,
  direction: Direction,
): { value: number; label: string; tone: string }[] {
  const outgoing = direction === "up" ? "本机独有 · 将上传" : "云端独有 · 将下载";
  const keep = direction === "up" ? "云端独有 · 保留" : "本机独有 · 保留";
  return [
    { value: plan.counts.only_local ?? 0, label: direction === "up" ? outgoing : keep, tone: "plain" },
    { value: plan.counts.only_remote ?? 0, label: direction === "up" ? keep : outgoing, tone: "plain" },
    { value: plan.counts.same ?? 0, label: "两端相同 · 跳过", tone: "plain" },
    { value: plan.counts.conflicts ?? 0, label: "冲突 · 需确认", tone: "warn" },
  ];
}
