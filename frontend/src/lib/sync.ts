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
  { kind: "reachout", label: "主动消息", hint: "她主动找过你的那些话，只追加" },
];

export type UploadMode = "merge" | "append" | "replace";

export const UPLOAD_MODES: { mode: UploadMode; label: string; note: string; cost: string }[] = [
  {
    mode: "merge",
    label: "逐条合并",
    note: "默认 · 推荐",
    cost: "只有本机有的、只有云端有的直接过去；两边都有但内容不同的，一条条拿给你挑。",
  },
  {
    mode: "append",
    label: "只追加",
    note: "最快、绝不丢东西",
    cost: "只推云端还没有的，重复的让它并存。代价是以后可能看见两条相似的记忆。",
  },
  {
    mode: "replace",
    label: "整份替换",
    note: "会丢东西",
    cost: "把云端那一份清掉，只留本机这份。云端上别人（或你在别处）新写的会话与记忆会直接没了。",
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

/** 「两份都留」只对记忆讲得通：卡与会话的身份就是那个 id，留两份 = 覆盖。
 *  所以界面只在记忆那一格上给第三个选择，别的地方给了就是骗人。 */
export function canKeepBoth(kind: SyncKind): boolean {
  return kind === "memory";
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

/** 四格读数（预检那一屏）：数字来自两边的实际比对，不是估算。 */
export function planReads(plan: Plan): { value: number; label: string; tone: string }[] {
  return [
    { value: plan.counts.only_local ?? 0, label: "本机独有 · 会过去", tone: "plain" },
    { value: plan.counts.only_remote ?? 0, label: "对面独有 · 不动", tone: "plain" },
    { value: plan.counts.same ?? 0, label: "两边相同 · 跳过", tone: "plain" },
    { value: plan.counts.conflicts ?? 0, label: "冲突 · 要你挑", tone: "warn" },
  ];
}
