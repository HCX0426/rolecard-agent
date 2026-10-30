// 顶层「记忆」页签（2026-10-30 从设置页「记忆与任务目录」子页签迁出）。
//
// 为什么迁：用户 2026-10-29「记忆躲起来了咋办」/ 10-30「这三个不放在设置界面，跟记忆
// 页签或者角色卡页签走」—— 跨会话记忆与主动开口都是"她跨会话记得什么、什么时候找你"这
// 一族，设置页该只留环境与凭据。任务目录（现更名「工作区」）是授权不是记忆，跟角色卡走
// （`components/WorkspacePanel.tsx`，由角色卡页托管）。
//
// 架构契约（本页是**管理视图**，不动引擎）：记忆只有一个库（`role_memory_item`，SQLite），
// 注入只有一条路径（`core/memory.memory_for_turn` —— 对话页、主动开口、桌宠共用这一份
// 规则）。注入语义检索**缓做**（解锁条件与"同一 SQLite 库上的视图、永不双模式"的形态约束
// 见 docs/开发流程.md 判定表），在这之前"面板上看到的"与"她每轮注入的"是同一份事实。
//
// 怎么保证没改行为：正文从 components/MemoryPanel.tsx **逐字搬来**（两卡的状态与处理器、
// JSX、注释一字未动）；`SettingsPage.test.tsx` 里记忆族的用例整体迁来 `MemoryPage.test.tsx`
// （render 目标换成 `<MemoryPage />`，断言不动），用例数搬走而非删除。
import { useCallback, useEffect, useRef, useState } from "react";
import { api, type RoleCard, type RuntimePayload } from "../api";
import { useConfirm } from "../hooks/useConfirm";
import { Button, Card } from "../components/ui";
import { describeError } from "../lib/errors";

/** 一条记忆（后端 `role_memory_item` 行）。`content` 是这些条目渲染出来的文本，不是第二份事实。 */
type MemoryItem = {
  id: number;
  text: string;
  source: string;
  pinned: boolean;
  hit_count: number;
  /** 显著性档位（0 次要 / 1 一般 / 2 要紧）。后端 `clamp_importance` 保证只会是这三个数。 */
  importance: number;
  /** 落在哪一层（后端 `MEMORY_CATEGORIES`）。旧数据可能缺这个字段，按 longterm 兜底。 */
  category?: string;
  last_hit_at: string | null;
  created_at: string | null;
};

type MemoryPayload = {
  enabled: boolean;
  role_id: string | null;
  /** 当前看的层（None = 全部）。后端按这个过滤 items。 */
  category?: string | null;
  content: string;
  items: MemoryItem[];
  /** 每层各几条（后端一次给齐，不用前端再发 N 次请求自己数）。 */
  counts?: Record<string, number>;
  active_count: number;
  limit: number;
  over_limit: boolean;
  /** 自动提取的节奏（有效值；0 = 关，只留对话页那个手动按钮）。 */
  extract_turns: number;
};

/**
 * 记忆分层（2026-10-30，用户：「每个角色的记忆分多板块，比如长期记忆、会话记忆、项目记忆」，
 * 以及「留一些给它自己想记住啥就记住啥」）。
 *
 * 四层各回答一个不同的问题 —— 这是分层的**唯一理由**：
 *   longterm 长期记忆 —— 跨会话稳定的事实（称呼、偏好、身份）。**每轮注入**。
 *   session  会话记忆 —— 当前对话的上下文摘要。会话结束即失效。
 *   project  项目记忆 —— 某个工作区/项目的状态与决策。按项目命中才注入。
 *   self     角色自记 —— 角色自己决定要记的（"我想起来了…"）。**每轮注入**。
 *
 * 与后端 `core/memory.MEMORY_CATEGORIES` 一一对应（那是唯一出处，这里只做界面文案）。
 */
const CATEGORY_META: {
  key: string;
  label: string;
  hint: string;
  /** 默认是否每轮注入（后端 `DEFAULT_INJECT_CATEGORIES` 的同一口径）。 */
  injectByDefault: boolean;
}[] = [
  {
    key: "longterm",
    label: "长期记忆",
    hint: "跨会话稳定的事实：称呼、偏好、身份背景。每轮都注入。",
    injectByDefault: true,
  },
  {
    key: "self",
    label: "角色自记",
    hint: "角色自己想记住的。每轮注入。",
    injectByDefault: true,
  },
  {
    key: "session",
    label: "会话记忆",
    hint: "当前对话的上下文摘要。会话结束就过期。",
    injectByDefault: false,
  },
  {
    key: "project",
    label: "项目记忆",
    hint: "某个工作区/项目的状态与决策。按项目命中才注入。",
    injectByDefault: false,
  },
];

const CATEGORY_LABEL: Record<string, string> = Object.fromEntries(
  CATEGORY_META.map((c) => [c.key, c.label]),
);

const SOURCE_LABEL: Record<string, string> = {
  manual: "手动",
  chat: "对话",
  proactive: "主动",
  extract: "提取",
  seed: "预置",
};

/** 显著性三档的界面名。**"要紧"这个词是后端渲染注入文本时用的那一个**（`render_memory`
 *  给 2 档加【要紧】标记），界面与模型看到的必须同词，否则用户在面板上标的档与 prompt 里
 *  那个标签对不上，就成了一个猜不透的开关。 */
const IMPORTANCE_LABEL: Record<number, string> = { 0: "次要", 1: "一般", 2: "要紧" };

/** 界面上给的自动提取档位（"0" = 关）。env 里设成别的数仍会原样回显，不假装是这些档之一。 */
const CADENCES = ["0", "6", "12", "24"];

/** 一条记忆的字数上限（后端 `memory.MAX_ITEM_CHARS`）。**超了是被截断、不是报错**，
 *  所以行内编辑那个计数器不是装饰 —— 它是"再打下去要被切掉"的唯一提示。 */
const MAX_FACT = 200;

export default function MemoryPage() {
  // 跨会话记忆面板：enabled = 总开关（runtime 覆盖，保存即热生效）；
  // items = 事实面（逐条可钉/改/删），content = 同一批条目的渲染文本（下方"原文视图"）。
  const [mem, setMem] = useState<MemoryPayload | null>(null);
  const [memDraft, setMemDraft] = useState("");
  const [memMsg, setMemMsg] = useState("");
  const [memErr, setMemErr] = useState("");
  // 记忆作用域："" = 全局用户记忆；否则 = 该角色的专属记忆（回忆触发用的那个桶）。
  // 注入开关是全局的，角色作用域只读/改条目、不能改开关。
  const [memScope, setMemScope] = useState("");
  const [memRoles, setMemRoles] = useState<{ role_id: string; role_name: string }[]>([]);
  /** 当前看哪一层（"" = 全部层）。分板块看（2026-10-30）。 */
  const [memLayer, setMemLayer] = useState("");
  /** 哪些层每轮注入。后端 `DEFAULT_INJECT_CATEGORIES` 是出厂值，用户可改。 */
  const [injectLayers, setInjectLayers] = useState<string[]>(
    CATEGORY_META.filter((c) => c.injectByDefault).map((c) => c.key),
  );
  // 新增一条的草稿。逐条录入取代了"只能在 textarea 里手改整段"。
  const [newFact, setNewFact] = useState("");
  const [itemBusy, setItemBusy] = useState(false);
  /** 行内编辑：正在改哪一条（null = 没有）+ 那一条的草稿。同一时刻只开一条 —— 两条同时
   *  编辑会让"屏幕上哪份是真的"没有答案，而这条面板的全部意义就是它是事实面。 */
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editDraft, setEditDraft] = useState("");
  /** 合并的选择：**按点击顺序**排、最多两条（第三条挤掉最早那条）。顺序有含义 ——
   *  先勾的那条是"留下的那条"（id、用过几次、钉住状态都跟着它走）。 */
  const [mergePick, setMergePick] = useState<number[]>([]);
  const [mergeDraft, setMergeDraft] = useState("");
  // 「整理记忆」进行中：一次真模型调用，本地卡上可能几十秒，所以按钮要有明确的进行中态。
  const [consolidating, setConsolidating] = useState(false);
  // 自动提取那一档（0 = 关）单独一个忙碌态：它走的是同一个 PUT，但保存后要读回**新有效值**。
  const [extractBusy, setExtractBusy] = useState(false);
  // 主动开口的总闸与收件箱折叠窗口**不在这一页**（2026-10-30 搬去角色卡页）：
  // per-role 的开口开关长在每张角色卡里，全局总闸在它们家门口；记忆页签只管记忆。
  const confirm = useConfirm();
  // 竞态守卫：快速切作用域时两个 loadMemory 都在途，A 的慢响应不许盖掉 B 的桶 ——
  // 那不只是显示错位：盖进草稿的旧内容一旦被「保存」发出，就是跨桶覆盖（数据损坏）。
  const loadSeqRef = useRef(0);
  // 有效档位（"0" = 自动提取关着）。字符串是为了让 `<select>` 与 option 的 value 对得上。
  const cadence = String(mem?.extract_turns ?? 12);

  const loadRoles = useCallback(async () => {
    const roles = await api.get<RoleCard[]>("/api/roles");
    setMemRoles(roles.map((r) => ({ role_id: r.role_id, role_name: r.role_name })));
  }, []);

  /** 读一份记忆。`layer` 非空 = 只看那一层（后端按它过滤 items）。 */
  const memoryUrl = (scope: string, layer: string) => {
    const params = new URLSearchParams();
    if (scope) params.set("role_id", scope);
    if (layer) params.set("category", layer);
    const q = params.toString();
    return q ? `/api/settings/memory?${q}` : "/api/settings/memory";
  };

  /** 写/删一份记忆。**层走请求体**（PUT 的 body / DELETE 的查询参数由后端读），
   *  所以这里的 URL 只带作用域 —— 读写用同一个 URL 会让人以为"URL 上的层已经生效了"。 */
  const memoryScopeUrl = (scope: string) =>
    scope ? `/api/settings/memory?role_id=${encodeURIComponent(scope)}` : "/api/settings/memory";

  const loadMemory = useCallback(async (scope: string, layer: string) => {
    const seq = ++loadSeqRef.current;
    const m = await api.get<MemoryPayload>(memoryUrl(scope, layer));
    if (seq !== loadSeqRef.current) return; // 已切到别的桶：这份旧响应整包作废
    setMem(m);
    setMemDraft(m.content);
  }, []);

  useEffect(() => {
    loadRoles().catch((e) => setMemErr(`角色列表加载失败：${describeError(e)}`));
    loadMemory(memScope, memLayer).catch((e) => setMemErr(`加载失败：${describeError(e)}`));
  }, [loadRoles, loadMemory, memScope, memLayer]);

  async function changeMemScope(next: string) {
    if (next === memScope) return;
    // 未保存改动：切换作用域前确认，避免草稿被静默丢弃。
    if (mem && memDraft !== mem.content) {
      const ok = await confirm({
        title: "切换作用域将丢弃未保存的修改",
        body: "当前作用域有未保存的修改，切换会丢弃。是否继续？",
      });
      if (!ok) return;
    }
    setMemScope(next);
    setMemMsg("");
    setMemErr("");
  }

  async function toggleMemory() {
    if (!mem || memScope) return; // 注入开关是全局的，角色作用域不可改
    setMemErr("");
    setMemMsg("");
    try {
      const m = await api.put<MemoryPayload>("/api/settings/memory", {
        enabled: !mem.enabled,
      });
      setMem(m);
      setMemDraft(m.content);
      setMemMsg(m.enabled ? "已开启（此后的对话会带上记忆）" : "已关闭");
    } catch (e) {
      setMemErr(`保存失败：${describeError(e)}`);
    }
  }

  async function saveMemory() {
    setMemErr("");
    setMemMsg("");
    try {
      // `category` = 只覆写**当前看的那一层**（记忆分层，2026-10-30）。
      // 不带它就会按整桶删 —— 用户在"项目记忆"板块里点一下保存，另外三层就没了。
      const m = await api.put<MemoryPayload>(memoryScopeUrl(memScope), {
        content: memDraft,
        category: memLayer || null,
      });
      setMem(m);
      setMemDraft(m.content);
      setMemMsg(memLayer ? `已保存到「${CATEGORY_LABEL[memLayer]}」` : "已保存");
    } catch (e) {
      setMemErr(`保存失败：${describeError(e)}`);
    }
  }

  async function clearMemory() {
    // 破坏性大过"删除一条"：一键抹掉整桶，必须有二次确认 —— 单条删除有、整桶清空反而
    // 裸奔是交互审计抓出来的不一致（收件箱/知识库的清空全都有确认）。
    const what = memLayer
      ? `「${CATEGORY_LABEL[memLayer] ?? memLayer}」这一层`
      : memScope
        ? "该角色"
        : "全局";
    const ok = await confirm({
      title: memLayer ? `清空${what}？` : memScope ? "清空该角色的专属记忆？" : "清空全局记忆？",
      body: memLayer
        ? `这一层的条目会从这里消失（真删，不是失效）。其它层不受影响。`
        : `${what}记忆的全部条目会从这里消失（真删，不是失效）。`,
      confirmText: "确认清空",
      danger: true,
    });
    if (!ok) return;
    setMemErr("");
    setMemMsg("");
    try {
      // DELETE 的层走查询参数（后端 DELETE 端点从 query 读 category）。
      // 用 URLSearchParams 拼而不是手接 `&`：作用域为空时那条 URL **没有 `?`**，
      // 手接会拼出 `/api/settings/memory&category=x`（没有 ? 就不算查询串）。
      const delParams = new URLSearchParams();
      if (memScope) delParams.set("role_id", memScope);
      if (memLayer) delParams.set("category", memLayer);
      const delQ = delParams.toString();
      const m = await api.del<MemoryPayload>(
        `/api/settings/memory${delQ ? `?${delQ}` : ""}`,
      );
      setMem(m);
      setMemDraft(m.content);
      setMemMsg(memLayer ? `已清空「${CATEGORY_LABEL[memLayer]}」` : "已清空");
    } catch (e) {
      setMemErr(`清空失败：${describeError(e)}`);
    }
  }

  /**
   * 自动提取那一档：`0` = 关掉自动那条（只留对话页的手动按钮），否则 = 每 N 轮兜底一次。
   *
   * 保存后要**读回**一次：这个 select 显示的是有效值（env + 在线覆盖叠加），而 PUT 的
   * 响应在重建之前生成，直接用它会把界面停在旧数字上。
   */
  async function changeExtractCadence(next: string) {
    setMemErr("");
    setMemMsg("");
    setExtractBusy(true);
    // 档位本身是全局设置，但**读回**要走当前作用域的桶：从前打裸全局 URL，角色作用域
    // 下改这一档会把整屏（列表+原文草稿）静默换成全局记忆，而下拉还停在角色上。
    const scope = memScope;
    const seq = loadSeqRef.current;
    try {
      await api.put<MemoryPayload>("/api/settings/memory", {
        extract_turns: Number.parseInt(next, 10),
      });
      const m = await api.get<MemoryPayload>(memoryUrl(scope, memLayer));
      if (seq !== loadSeqRef.current) return; // 期间已切作用域：交还给那次的 loadMemory
      setMem(m);
      setMemDraft(m.content);
      setMemMsg(
        m.extract_turns
          ? `已设为每 ${m.extract_turns} 轮自动提取一次（跑在回答出完之后，不影响对话）`
          : "已关掉自动提取：只留对话页那个「提取精华」按钮",
      );
    } catch (e) {
      setMemErr(`保存失败：${describeError(e)}`);
    } finally {
      setExtractBusy(false);
    }
  }

  /**
   * 整理记忆：让模型对**当前作用域这个桶**做 MERGE / INVALID（一次真模型调用，点才跑）。
   *
   * 为什么不自动：超限时的保底淘汰已经在写入路径上做了（不调模型），真正的合并会改写
   * 事实 —— 什么时候花这个钱由用户决定。响应里带整理后的同一份视图，所以列表当场刷新。
   */
  async function consolidateNow() {
    setMemErr("");
    setMemMsg("");
    setConsolidating(true);
    try {
      const r = await api.consolidateMemory(memScope || undefined);
      setMem(r);
      setMemDraft(r.content);
      const p = r.report;
      const parts = [
        p.merged && `合并 ${p.merged} 组`,
        p.invalidated && `让 ${p.invalidated} 条过时事实失效`,
        p.skipped && `${p.skipped} 条没读懂，没有写入`,
      ].filter(Boolean);
      const cost = p.tokens ? ` · 花掉 ${p.tokens} 个 token` : "";
      setMemMsg(
        parts.length
          ? `已整理：${parts.join("、")}${cost}（活跃 ${r.active_count}/${r.limit} 条，没删任何一行）`
          : p.detail || "没有需要整理的条目。",
      );
    } catch (e) {
      setMemErr(`整理失败：${describeError(e)}`);
    } finally {
      setConsolidating(false);
    }
  }

  /** 逐条操作都直接采用响应里的 payload —— 面板显示的就是后端算完的那份，不在前端猜顺序。 */
  async function withItems(run: () => Promise<MemoryPayload>, ok: string) {
    setMemErr("");
    setMemMsg("");
    setItemBusy(true);
    try {
      const m = await run();
      setMem(m);
      setMemDraft(m.content);
      setMemMsg(ok);
    } catch (e) {
      setMemErr(`操作失败：${describeError(e)}`);
    } finally {
      setItemBusy(false);
    }
  }

  const addFact = () => {
    const text = newFact.trim();
    if (!text) return;
    void withItems(
      () =>
        api.post<MemoryPayload>(`/api/settings/memory/item${scopeQs()}`, {
          text,
          // 落到**当前看的层**（看的是"全部层"时 = 长期记忆）。
          // 这一句是"分板块"与"逐条录入"接上的地方：不带它，界面在"项目记忆"板块里
          // 加一条东西，它会悄悄落到长期记忆 —— 用户看着板块标签就知道自己加到哪儿了。
          category: memLayer || "longterm",
        }),
      "已记住",
    ).then(() => setNewFact(""));
  };

  const togglePin = (item: MemoryItem) =>
    withItems(
      () =>
        api.patch<MemoryPayload>(
          `/api/settings/memory/item/${item.id}${scopeQs()}`,
          { pinned: !item.pinned },
        ),
      item.pinned ? "已取消钉住" : "已钉住（不参与淘汰与整理）",
    );

  const setItemImportance = (item: MemoryItem, tier: number) =>
    withItems(
      () =>
        api.patch<MemoryPayload>(
          `/api/settings/memory/item/${item.id}${scopeQs()}`,
          { importance: tier },
        ),
      `已标为「${IMPORTANCE_LABEL[tier] ?? "一般"}」`,
    );

  const removeItem = async (item: MemoryItem) => {
    const ok = await confirm({
      title: "删除这条记忆？",
      body: `「${item.text}」会从记忆里删除（真删，不是失效）。`,
      confirmText: "确认删除",
      danger: true,
    });
    if (!ok) return;
    await withItems(
      () =>
        api.del<MemoryPayload>(
          `/api/settings/memory/item/${item.id}${scopeQs()}`,
        ),
      "已删除",
    );
  };

  // ---------------------------------------------------------------- 改一条 / 合两条（S-3）

  /** 逐条操作的 URL 尾巴：作用域靠 query 传，五处拼法必须一模一样（少一处就会改到全局桶上）。 */
  const scopeQs = () => (memScope ? `?role_id=${encodeURIComponent(memScope)}` : "");

  const startEdit = (item: MemoryItem) => {
    setEditingId(item.id);
    setEditDraft(item.text);
  };

  const cancelEdit = () => {
    setEditingId(null);
    setEditDraft("");
  };

  /** 保存行内编辑。**空文本 = 不改**（后端 `edit_item` 就是这个口径：清空交给删除去做），
   *  所以这里在清空时把「保存」置灰而不是发一个空句子过去。 */
  const saveEdit = (item: MemoryItem) => {
    const text = editDraft.trim();
    if (!text) return;
    if (text === item.text) return cancelEdit(); // 一个字没改：不发没必要的写
    void withItems(
      () => api.patch<MemoryPayload>(`/api/settings/memory/item/${item.id}${scopeQs()}`, { text }),
      "已更新这条记忆",
    ).then(cancelEdit);
  };

  const togglePick = (id: number) =>
    setMergePick((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id].slice(-2)));

  /** 选中的两条（按点击顺序：[0] = 留下的那条）。 */
  const picked = mergePick
    .map((id) => (mem?.items ?? []).find((i) => i.id === id))
    .filter((i): i is MemoryItem => Boolean(i));

  // 预填那两句的拼接结果（= 后端 `merged_text` 那个兜底写法）。它是**起点不是结果**：
  // 用户在这个框里把句子改顺，改完点「合并」才发请求。
  useEffect(() => {
    if (picked.length !== 2) return;
    setMergeDraft(`${picked[0].text}；${picked[1].text}`.slice(0, MAX_FACT));
  }, [mergePick, mem]); // eslint-disable-line react-hooks/exhaustive-deps

  const doMerge = () => {
    const [keep, drop] = picked;
    if (!keep || !drop) return;
    void withItems(
      () =>
        api.post<MemoryPayload>(`/api/settings/memory/item/${keep.id}/merge/${drop.id}${scopeQs()}`, {
          text: mergeDraft.trim() || null,
        }),
      "已合并成一条（原来那条没有删，只是不再被引用）",
    ).then(() => setMergePick([]));
  };

  /** 读"哪几层每轮注入"的**生效值**（runtime 覆盖；"未设置" = 出厂默认 longterm+self）。 */
  const loadInjectLayers = useCallback(async () => {
    const rt = await api.get<RuntimePayload>("/api/settings/runtime");
    const items = (rt.groups ?? []).flatMap((g) => g.items ?? []);
    const inject = items.find((i) => i.field === "memory_inject_categories");
    const raw = (inject?.value ?? "").trim();
    setInjectLayers(
      raw && raw !== "未设置"
        ? raw
            .split(",")
            .map((s) => s.trim())
            .filter(Boolean)
        : CATEGORY_META.filter((c) => c.injectByDefault).map((c) => c.key),
    );
  }, []);

  useEffect(() => {
    loadInjectLayers().catch(() => undefined);
  }, [loadInjectLayers]);

  /**
   * 改"哪几层每轮注入"（记忆分层，2026-10-30）。
   *
   * 写的是 runtime 覆盖（`memory_inject_categories`，逗号分隔的层名），保存即热重建生效 ——
   * 和"记忆注入总闸"是同一套机制，所以**保存后读回**生效值（界面显示的必须是真在跑的那份）。
   */
  async function toggleInjectLayer(layer: string, on: boolean) {
    setMemErr("");
    setMemMsg("");
    const next = on
      ? [...new Set([...injectLayers, layer])]
      : injectLayers.filter((k) => k !== layer);
    setInjectLayers(next);
    try {
      await api.put<RuntimePayload>("/api/settings/runtime", {
        values: { memory_inject_categories: next.join(",") },
      });
      const rt = await api.get<RuntimePayload>("/api/settings/runtime");
      const items = (rt.groups ?? []).flatMap((g) => g.items ?? []);
      const inject = items.find((i) => i.field === "memory_inject_categories");
      const raw = (inject?.value ?? "").trim();
      setInjectLayers(
        raw && raw !== "未设置"
          ? raw
              .split(",")
              .map((s) => s.trim())
              .filter(Boolean)
          : CATEGORY_META.filter((c) => c.injectByDefault).map((c) => c.key),
      );
      const label = CATEGORY_LABEL[layer] ?? layer;
      setMemMsg(on ? `「${label}」现在每轮都会带上` : `「${label}」不再每轮注入`);
    } catch (e) {
      setMemErr(`保存失败：${describeError(e)}`);
      // 保存失败要把界面**退回**真实状态 —— 留着乐观的勾选会骗人。
      loadInjectLayers().catch(() => undefined);
    }
  }

  return (
    <div className="h-full overflow-y-auto p-6">
      <div className="mx-auto max-w-3xl">
        {/* 页头 = 这页的英雄：注入总闸从卡片头提升到页头（2026-10-30 重设计）。它回答的是
            "她这轮带不带记忆上桌"——整页的状态，不该藏在卡里找；样式沿用侧栏当前角色块
            那套 accent 药丸语法（bg-blue-600 已被 token 映射到 --accent，随角色变色）。 */}
        <div className="flex items-center justify-between gap-3">
          <h2 className="text-base font-semibold text-slate-900 dark:text-slate-100">记忆</h2>
          <button
            onClick={toggleMemory}
            disabled={!mem || !!memScope}
            title={memScope ? "注入开关是全局设置，切到「全局记忆」才能改" : undefined}
            className={`shrink-0 rounded-full px-3 py-1 text-xs font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${
              mem?.enabled
                ? "bg-blue-600 text-white"
                : "bg-slate-200 text-slate-500 dark:bg-slate-700 dark:text-slate-300"
            }`}
          >
            {mem ? (mem.enabled ? "记忆注入：已开启" : "记忆注入：已关闭") : "…"}
          </button>
        </div>
        <p className="mt-1 text-xs leading-relaxed text-slate-500 dark:text-slate-400">
          每轮对话都带上这里的事实 —— 对话、主动开口、桌宠共用这一份。
        </p>
      <Card className="mt-4 p-5">
        <h3 className="text-sm font-medium text-slate-900 dark:text-slate-100">跨会话记忆</h3>
        <div className="mt-2 flex items-center gap-2">
          <label className="text-xs text-slate-500 dark:text-slate-400">作用域</label>
          <select
            value={memScope}
            onChange={(e) => changeMemScope(e.target.value)}
            /* 定宽（用户 2026-10-29：控件宽度不该随角色名长短变）：原生 select 的宽度由
               "当前选中项"的文字决定，角色名一长就把框撑宽、换选项时整行跟着跳。给一个
               固定宽度 + 超长省略，换谁选都是同一格宽。 */
            className="w-64 min-w-0 truncate rounded-lg border border-slate-200 bg-white px-2 py-1 text-xs text-slate-700 dark:border-slate-600 dark:bg-slate-900 dark:text-slate-200"
          >
            {/* 选项不带括号注释（2026-10-31）：作用域的语义由上方那行说明讲，选项只给名字。 */}
            <option value="">全局记忆</option>
            {memRoles.map((r) => (
              <option key={r.role_id} value={r.role_id}>
                {r.role_name}
              </option>
            ))}
          </select>
          {/* 整理 = 对"当前作用域这一个桶"的动作，所以按钮跟着作用域走，不跟列表走。 */}
          <div className="ml-auto">
            <Button
              variant="outline"
              size="sm"
              onClick={consolidateNow}
              disabled={!mem || consolidating || (mem?.active_count ?? 0) < 2}
              disabledHint={
                (mem?.active_count ?? 0) < 2 ? "至少两条活跃记忆才有可整理的" : undefined
              }
              title="让模型合并同义的条目、把过时的转成失效（一次模型调用；只写标记，不删任何一行）"
            >
              {consolidating ? "整理中…" : "整理记忆"}
            </Button>
          </div>
        </div>
        <p className="mt-1.5 text-xs leading-relaxed text-slate-500 dark:text-slate-400">
          {memScope
            ? "该角色自己的记忆，与全局记忆和其它角色隔离。"
            : "全局记忆注入每个角色；AI 检测到明确的事实（称呼 / 偏好 / 背景）会自己记下来。"}
        </p>

        {/* 分板块标签（2026-10-30 记忆分层）：记忆会越来越多，按层分开才看得见也调得动。
            标签上带**条数**（后端一次给齐 counts）—— 没有条数的分板块等于把一堆条目
            藏进了抽屉，用户不知道哪一层是空的。 */}
        <div className="mt-3 flex flex-wrap items-center gap-1.5">
          <button
            onClick={() => setMemLayer("")}
            className={`rounded-full px-2.5 py-1 text-xs transition-colors ${
              memLayer === ""
                ? "bg-slate-700 text-white dark:bg-slate-200 dark:text-slate-900"
                : "bg-slate-100 text-slate-600 hover:bg-slate-200 dark:bg-slate-800 dark:text-slate-300 dark:hover:bg-slate-700"
            }`}
          >
            全部 {mem?.active_count ?? 0}
          </button>
          {CATEGORY_META.map((c) => {
            const n = mem?.counts?.[c.key] ?? 0;
            const on = memLayer === c.key;
            return (
              <button
                key={c.key}
                onClick={() => setMemLayer(on ? "" : c.key)}
                title={c.hint}
                className={`rounded-full px-2.5 py-1 text-xs transition-colors ${
                  on
                    ? "bg-slate-700 text-white dark:bg-slate-200 dark:text-slate-900"
                    : "bg-slate-100 text-slate-600 hover:bg-slate-200 dark:bg-slate-800 dark:text-slate-300 dark:hover:bg-slate-700"
                }`}
              >
                {c.label} {n}
              </button>
            );
          })}
        </div>
        {/* 当前层的说明 + 注入开关：每层能单独决定"要不要每轮带上"（2026-10-30）。
            这一行是"自动整理/注入"这类开关的落点 —— 用户抱怨的"看不到就没法调"就解在这里。 */}
        {memLayer && (
          <div className="mt-2 flex items-center justify-between gap-3 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 dark:border-slate-700 dark:bg-slate-800/50">
            <span className="min-w-0 flex-1 text-11 text-slate-500 dark:text-slate-400">
              {CATEGORY_META.find((c) => c.key === memLayer)?.hint}
            </span>
            <label className="flex shrink-0 cursor-pointer items-center gap-1.5 text-11 text-slate-600 dark:text-slate-300">
              <input
                type="checkbox"
                checked={injectLayers.includes(memLayer)}
                onChange={(e) => void toggleInjectLayer(memLayer, e.target.checked)}
              />
              每轮注入
            </label>
          </div>
        )}
        {/* 自动提取的节奏：**一个**控件同时管"要不要自动跑"和"多久跑一次"（0 = 只留手动）。
            2026-10-31 用户："这个有必要这么详细吗，摆很难看"——长说明收进悬停（title），
            正文只留控件本身，文字与下拉同一行对齐。 */}
        <div
          className="mt-2 flex items-center gap-2 text-xs text-slate-500 dark:text-slate-400"
          title="一次提取 = 一次模型调用，跑在回答已经出完之后（不影响对话，失败只留痕）"
        >
          <span className="shrink-0">每 N 轮模型调用完成后自动提取一次</span>
          <select
            value={cadence}
            disabled={!mem || extractBusy}
            onChange={(e) => void changeExtractCadence(e.target.value)}
            className="rounded-lg border border-slate-200 bg-white px-2 py-1 text-xs text-slate-700 dark:border-slate-600 dark:bg-slate-900 dark:text-slate-200"
          >
            <option value="0">关闭</option>
            <option value="6">每 6 轮</option>
            <option value="12">每 12 轮</option>
            <option value="24">每 24 轮</option>
            {/* env 里设成了别的数（比如 8）时不能显示成"每 12 轮"—— 那是假回显。 */}
            {!CADENCES.includes(cadence) && <option value={cadence}>每 {cadence} 轮</option>}
          </select>
        </div>
        {/* 逐条列表 = 事实面（后端按 近因×频次 排，钉住的在前）；下面的 textarea 只是同一批
            条目的"原文视图 + 整段覆写"，不是第二份记忆。 */}
        <div className="mt-2.5 flex gap-2">
          <input
            value={newFact}
            onChange={(e) => setNewFact(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") addFact();
            }}
            disabled={!mem || itemBusy}
            placeholder="手动记一条事实（一句话）"
            className="min-w-0 flex-1 rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-xs text-slate-700 focus:border-blue-400 focus:outline-none dark:border-slate-600 dark:bg-slate-900 dark:text-slate-200"
          />
          <Button variant="primary" onClick={addFact} disabled={!mem || itemBusy || !newFact.trim()}
            className="rounded-lg px-3 py-1.5 text-xs">
            记住
          </Button>
        </div>
        {/* 合并那条工具条只在选了东西时长出来（平时列表就是列表，不摆一排永远用不上的控件）。 */}
        {picked.length > 0 && (
          <div className="mt-2 rounded-lg border border-slate-600 bg-slate-100/70 px-2.5 py-2 text-xs dark:border-slate-400 dark:bg-slate-900/20">
            <div className="flex items-center justify-between gap-2">
              <span className="text-slate-600 dark:text-slate-300">
                已选 {picked.length} 条{picked.length < 2 ? " —— 再选一条就能合并" : ""}
              </span>
              <button
                onClick={() => setMergePick([])}
                className="shrink-0 rounded px-1.5 py-0.5 text-11 text-slate-500 hover:text-slate-700 dark:hover:text-slate-200"
              >
                取消选择
              </button>
            </div>
            {picked.length === 2 && (
              <>
                <label className="mt-1.5 block text-11 text-slate-500 dark:text-slate-400">
                  合并成一条（先勾的那条留下，含它"用过几次"与钉住状态）
                  {/* 钉住状态不跟着搬：留下的永远是先勾那条，所以"先勾了个没钉住的、
                      后勾的钉住了"这个顺序会让钉住悄悄掉档 —— 说清楚比偷偷换更好。 */}
                  {!picked[0].pinned && picked[1].pinned && (
                    <span className="mt-0.5 block text-amber-600 dark:text-amber-400">
                      注意：会留下「未钉住」的那条，钉住状态不跟着搬。要反过来留，先取消选择、
                      再按你想要的顺序重选一遍。
                    </span>
                  )}
                </label>
                <textarea
                  value={mergeDraft}
                  onChange={(e) => setMergeDraft(e.target.value)}
                  rows={2}
                  maxLength={MAX_FACT}
                  aria-label="合并后的句子"
                  className="mt-1 w-full resize-none rounded-lg border border-slate-200 bg-white px-2 py-1.5 text-xs text-slate-700 focus:border-blue-400 focus:outline-none dark:border-slate-600 dark:bg-slate-900 dark:text-slate-200"
                />
                <div className="mt-1 flex items-center justify-between gap-2">
                  <span className="text-10 text-slate-400 dark:text-slate-500">
                    {mergeDraft.length}/{MAX_FACT}
                  </span>
                  <Button variant="primary" onClick={doMerge} disabled={itemBusy}
                    className="rounded-lg px-3 py-1 text-xs">
                    合并（留 1 条）
                  </Button>
                </div>
              </>
            )}
          </div>
        )}
        <ul className="mt-2 max-h-56 space-y-1 overflow-y-auto">
          {(mem?.items ?? []).map((item) => (
            <li
              key={item.id}
              className="group flex items-start gap-2 rounded-lg border border-slate-100 px-2 py-1.5 text-xs dark:border-slate-700"
            >
              <input
                type="checkbox"
                checked={mergePick.includes(item.id)}
                onChange={() => togglePick(item.id)}
                disabled={itemBusy}
                title="选来合并：一次合两条，先勾的那条留下"
                aria-label={`选择这条记忆用于合并`}
                className="mt-1 shrink-0"
              />
              {editingId === item.id ? (
                // 行内编辑：整行换成输入框 + 计数器 + 取消/保存。不做 contenteditable ——
                // 那一套会让"点哪儿算编辑、点哪儿算勾选"变成猜，而字数上限也没地方显示。
                <span className="min-w-0 flex-1">
                  <input
                    value={editDraft}
                    onChange={(e) => setEditDraft(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") saveEdit(item);
                      if (e.key === "Escape") cancelEdit();
                    }}
                    maxLength={MAX_FACT}
                    disabled={itemBusy}
                    aria-label="改这条记忆"
                    className="w-full rounded-lg border border-slate-200 bg-white px-2 py-1 text-xs text-slate-700 focus:border-blue-400 focus:outline-none dark:border-slate-600 dark:bg-slate-900 dark:text-slate-200"
                  />
                  <span className="mt-1 flex items-center justify-between gap-2">
                    <span className="text-10 text-slate-400 dark:text-slate-500">
                      {editDraft.length}/{MAX_FACT}
                      {/* 超上限后端是**截断**不是报错，所以这里提前说一声。 */}
                      {editDraft.length >= MAX_FACT ? " · 再打会被截断" : ""}
                    </span>
                    <span className="flex shrink-0 gap-1.5">
                      <button
                        onClick={cancelEdit}
                        className="rounded px-1.5 py-0.5 text-11 text-slate-500 hover:text-slate-700 dark:hover:text-slate-200"
                      >
                        取消
                      </button>
                      <Button variant="primary" onClick={() => saveEdit(item)} disabled={itemBusy || !editDraft.trim()}
                        className="rounded px-2 py-0.5 text-11">
                        保存
                      </Button>
                    </span>
                  </span>
                </span>
              ) : (
                <>
                  <span className="min-w-0 flex-1 break-words text-slate-700 dark:text-slate-200">
                    {item.text}
                    {/* 元信息收干净（2026-10-31，dsh 生态共识：界面呈现"记忆是什么、归哪一轨"，
                        来源与命中次数是内部统计 → 收进 title 悬停）。要紧程度是真实生效的档位
                        （注入带【要紧】标记），保留但悬停行才出现——平时每行只留归属。 */}
                    <span
                      className="mt-0.5 block text-10 text-slate-400 dark:text-slate-500"
                      title={`来源：${SOURCE_LABEL[item.source] ?? item.source} · 注入过 ${item.hit_count} 次`}
                    >
                      {CATEGORY_LABEL[item.category ?? "longterm"] ?? "长期记忆"}
                      {item.pinned ? " · 已钉住" : ""}
                      <label className="ml-1.5 inline-flex items-center gap-1 opacity-0 transition-opacity group-hover:opacity-100">
                        <select
                          value={String(item.importance ?? 1)}
                          disabled={itemBusy}
                          onChange={(e) => void setItemImportance(item, Number(e.target.value))}
                          aria-label="要紧程度"
                          title={
                            "要紧 = 注入时带【要紧】标记、排序里权重最高；次要 = 活跃数超限时最先被退役（不删）。\n" +
                            "与「钉住」是两件事：钉住完全不进淘汰与整理，这一档只管还在池里时排多前。"
                          }
                          className="rounded border border-slate-200 bg-white px-1 py-0 text-10 text-slate-600 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-300"
                        >
                          {[0, 1, 2].map((tier) => (
                            <option key={tier} value={tier}>
                              {IMPORTANCE_LABEL[tier]}
                            </option>
                          ))}
                        </select>
                      </label>
                    </span>
                  </span>
                  <button
                    onClick={() => startEdit(item)}
                    disabled={itemBusy}
                    title="改这条的文字（事实对、话说错了就用它，不用删了重记）"
                    className="shrink-0 rounded px-1.5 py-0.5 text-11 text-slate-500 hover:bg-slate-100 dark:hover:bg-slate-700"
                  >
                    改
                  </button>
                  <button
                    onClick={() => void togglePin(item)}
                    disabled={itemBusy}
                    title={item.pinned ? "取消钉住（允许被淘汰/整理）" : "钉住（不参与淘汰与整理）"}
                    className="shrink-0 rounded px-1.5 py-0.5 text-11 text-slate-500 hover:bg-slate-100 dark:hover:bg-slate-700"
                  >
                    {item.pinned ? "取消钉住" : "钉住"}
                  </button>
                  <button
                    onClick={() => void removeItem(item)}
                    disabled={itemBusy}
                    className="shrink-0 rounded px-1.5 py-0.5 text-11 text-slate-500 hover:text-red-600 dark:hover:text-red-400"
                  >
                    删除
                  </button>
                </>
              )}
            </li>
          ))}
        </ul>
        {mem?.over_limit && (
          <p className="mt-1.5 text-11 text-amber-600 dark:text-amber-400">
            活跃记忆已达上限（{mem.active_count}/{mem.limit}）—— 最弱的已被退役（没删，可撤销）。
            点上面的「整理记忆」合并同义的、让过时的失效，比继续堆着更准。
          </p>
        )}
        <details className="mt-2">
          <summary className="cursor-pointer text-11 text-slate-500 dark:text-slate-400">
            原文视图 / 整段编辑（注入到 prompt 的就是这段）
            {/* 说实在的代价，不藏：整段覆写只删非钉住的条目再按行重建，而"哪一行还是哪一条"
                是按**文本**认的 —— 所以在TextArea里改了某条的措辞，那条的「要紧程度」会回到一般。
                逐条列表里改不受影响（按 id 走）。 */}
            <span className="ml-1 text-10 text-slate-400 dark:text-slate-500">
              （在这里改写某条的措辞，那条的「要紧程度」会回到一般；钉住的条目整段保存不动它）
            </span>
          </summary>
          <textarea
          value={memDraft}
          onChange={(e) => setMemDraft(e.target.value)}
          rows={5}
          disabled={!mem}
          placeholder={mem ? (mem.content ? "" : memScope ? "该角色还没有专属记忆。" : "暂无全局记忆。") : "加载中…"}
          className="mt-2.5 w-full resize-y rounded-lg border border-slate-200 bg-white p-2.5 font-mono text-xs text-slate-700 focus:border-blue-400 focus:outline-none dark:border-slate-600 dark:bg-slate-900 dark:text-slate-200"
        />
        </details>
        <div className="mt-2 flex items-center gap-2">
          <Button variant="primary" onClick={saveMemory} disabled={!mem}
            className="rounded-lg px-3 py-1.5 text-xs disabled:opacity-50">
            保存
          </Button>
          <button
            onClick={clearMemory}
            disabled={!mem}
            className="rounded-lg border border-slate-200 px-3 py-1.5 text-xs text-slate-600 hover:border-red-300 hover:text-red-600 disabled:opacity-50 dark:border-slate-600 dark:text-slate-300"
          >
            清空
          </button>
          {memMsg && <span className="text-xs text-emerald-600 dark:text-emerald-400">{memMsg}</span>}
          {memErr && <span className="text-xs text-red-600 dark:text-red-400 dark:text-red-500">{memErr}</span>}
          {mem && <span className="ml-auto text-11 text-slate-400 dark:text-slate-500">{memDraft.length} 字</span>}
        </div>
      </Card>

      {/* 主动开口卡不再在这一页（2026-10-30）：总闸与收件箱折叠窗口搬去了角色卡页
          （components/ReachoutMasterCard，由 RolesPage 托管）—— per-role 的开口开关
          就在每张角色卡里，全局总闸在它们家门口；这一页只管记忆。 */}
      </div>
    </div>
  );
}
