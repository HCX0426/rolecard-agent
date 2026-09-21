import { useCallback, useEffect, useState } from "react";
import { ExtensionPanel } from "../components/ExtensionPanel";
import { ModelsPanel } from "../components/ModelsPanel";
import { NativeDirPickerButton } from "../components/NativeDirPickerButton";
import { ServicesPanel } from "../components/ServicesPanel";
import { ShellReleaseCard } from "../components/ShellReleaseCard";
import {
  api,
  type AuditRow,
  type ModelSettings,
  type PluginRow,
  type RoleCard,
  type RuntimeItem,
  type RuntimePayload,
  type SessionRow,
  type TreeResult,
  type WorkspaceDir,
} from "../api";
import { useConfirm } from "../hooks/useConfirm";
import { Button, Card } from "../components/ui";

// 设置页子页签：模型（凭据组 + 模型行，见 components/ModelsPanel）/ 服务（运行时状态与降级
// 策略）/ 记忆与任务目录 / 关于 / 扩展 / 运行环境 / 审计。知识库已升为独立顶层页 ——
// RAG 是内核能力，不该埋在设置里。
const SETTINGS_TABS = [
  { key: "models", label: "模型" },
  { key: "services", label: "服务" },
  { key: "memory", label: "记忆与任务目录" },
  { key: "about", label: "关于与系统状态" },
  { key: "extension", label: "扩展" },
  { key: "runtime", label: "运行环境" },
  { key: "audit", label: "审计" },
] as const;

type SettingsTab = (typeof SETTINGS_TABS)[number]["key"];

export default function SettingsPage({
  onOpenChat,
  theme,
  onToggleTheme,
}: {
  onOpenChat?: () => void;
  theme?: string;
  onToggleTheme?: () => void;
}) {
  const [tab, setTab] = useState<SettingsTab>("models");

  return (
    <div className="h-full overflow-y-auto p-6">
      <div className="mx-auto max-w-3xl">
        <h2 className="text-base font-semibold text-slate-900 dark:text-slate-100">设置</h2>
        <div className="mt-4 flex gap-1 border-b border-slate-200 dark:border-slate-700">
          {SETTINGS_TABS.map((t) => (
            <button
              key={t.key}
              onClick={() => setTab(t.key)}
              className={`rounded-t-lg px-4 py-2 text-sm ${
                tab === t.key
                  ? "border-b-2 border-blue-600 font-medium text-blue-700 dark:text-blue-300"
                  : "text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-200"
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>
        {/* 子页签常驻挂载 + hidden 隐藏（P1-11 同款）：条件渲染会让每次切页签重新挂载
            面板并重发请求 —— 服务页的探活会因此"每次点开都加载中"（用户 2026-09-18）。 */}
        <div className={tab === "memory" ? "" : "hidden"}>
          <MemoryPanel />
        </div>
        <div className={tab === "about" ? "" : "hidden"}>
          <AboutPanel onOpenChat={onOpenChat} theme={theme} onToggleTheme={onToggleTheme} />
        </div>
        <div className={tab === "models" ? "" : "hidden"}>
          <ModelsPanel onOpenServices={() => setTab("services")} />
        </div>
        <div className={tab === "services" ? "" : "hidden"}>
          <ServicesPanel />
        </div>
        <div className={tab === "extension" ? "" : "hidden"}>
          <ExtensionPanel />
        </div>
        <div className={tab === "runtime" ? "" : "hidden"}>
          <RuntimePanel />
        </div>
        <div className={tab === "audit" ? "" : "hidden"}>
          <AuditPanel />
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- 记忆与任务目录

/** 一条记忆（后端 `role_memory_item` 行）。`content` 是这些条目渲染出来的文本，不是第二份事实。 */
type MemoryItem = {
  id: number;
  text: string;
  source: string;
  pinned: boolean;
  hit_count: number;
  last_hit_at: string | null;
  created_at: string | null;
};

type MemoryPayload = {
  enabled: boolean;
  role_id: string | null;
  content: string;
  items: MemoryItem[];
  active_count: number;
  limit: number;
  over_limit: boolean;
};

const SOURCE_LABEL: Record<string, string> = {
  manual: "手动",
  chat: "对话",
  proactive: "主动",
  extract: "提取",
  seed: "预置",
};

function MemoryPanel() {
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
  // 新增一条的草稿。逐条录入取代了"只能在 textarea 里手改整段"。
  const [newFact, setNewFact] = useState("");
  const [itemBusy, setItemBusy] = useState(false);
  // 「整理记忆」进行中：一次真模型调用，本地卡上可能几十秒，所以按钮要有明确的进行中态。
  const [consolidating, setConsolidating] = useState(false);
  // 任务目录（file1）：角色可读写的授权范围。wsDir = 生效值；树抽屉 = 目录选择器。
  const [wsDir, setWsDir] = useState<WorkspaceDir | null>(null);
  const [wsDraft, setWsDraft] = useState("");
  const [wsMsg, setWsMsg] = useState("");
  const [wsErr, setWsErr] = useState("");
  const [treeOpen, setTreeOpen] = useState(false);
  const [tree, setTree] = useState<TreeResult | null>(null);
  const [treeErr, setTreeErr] = useState("");
  // 主动开口全局总闸（覆盖运行环境的 REACHOUT_ENABLED）：rejectOn = 有效值。
  const [reachoutOn, setReachoutOn] = useState<boolean | null>(null);
  // 收件箱折叠窗口（天，1/3/7）：与总闸同帖单写点，运行环境只读展示。
  const [mergeDays, setMergeDays] = useState<number | null>(null);
  const [reachoutMsg, setReachoutMsg] = useState("");
  const [reachoutErr, setReachoutErr] = useState("");
  const confirm = useConfirm();

  const loadRoles = useCallback(async () => {
    const roles = await api.get<RoleCard[]>("/api/roles");
    setMemRoles(roles.map((r) => ({ role_id: r.role_id, role_name: r.role_name })));
  }, []);

  const memoryUrl = (scope: string) =>
    scope ? `/api/settings/memory?role_id=${encodeURIComponent(scope)}` : "/api/settings/memory";

  const loadMemory = useCallback(async (scope: string) => {
    const m = await api.get<MemoryPayload>(memoryUrl(scope));
    setMem(m);
    setMemDraft(m.content);
  }, []);

  useEffect(() => {
    loadRoles().catch(() => {});
    loadMemory(memScope).catch((e) => setMemErr(`加载失败：${(e as Error).message}`));
  }, [loadRoles, loadMemory, memScope]);

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
      setMemErr(`保存失败：${(e as Error).message}`);
    }
  }

  async function saveMemory() {
    setMemErr("");
    setMemMsg("");
    try {
      const m = await api.put<MemoryPayload>(memoryUrl(memScope), {
        content: memDraft,
      });
      setMem(m);
      setMemDraft(m.content);
      setMemMsg("已保存");
    } catch (e) {
      setMemErr(`保存失败：${(e as Error).message}`);
    }
  }

  async function clearMemory() {
    setMemErr("");
    setMemMsg("");
    try {
      const m = await api.del<MemoryPayload>(memoryUrl(memScope));
      setMem(m);
      setMemDraft(m.content);
      setMemMsg("已清空");
    } catch (e) {
      setMemErr(`清空失败：${(e as Error).message}`);
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
        p.skipped && `忽略 ${p.skipped} 行看不懂的输出`,
      ].filter(Boolean);
      const cost = p.tokens ? ` · 用去 ${p.tokens} tokens` : "";
      setMemMsg(
        parts.length
          ? `已整理：${parts.join("、")}${cost}（活跃 ${r.active_count}/${r.limit} 条，没删任何一行）`
          : p.detail || "没有需要整理的条目。",
      );
    } catch (e) {
      setMemErr(`整理失败：${(e as Error).message}`);
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
      setMemErr(`操作失败：${(e as Error).message}`);
    } finally {
      setItemBusy(false);
    }
  }

  const addFact = () => {
    const text = newFact.trim();
    if (!text) return;
    void withItems(
      () =>
        api.post<MemoryPayload>(`/api/settings/memory/item${memScope ? `?role_id=${encodeURIComponent(memScope)}` : ""}`, {
          text,
        }),
      "已记住",
    ).then(() => setNewFact(""));
  };

  const togglePin = (item: MemoryItem) =>
    withItems(
      () =>
        api.patch<MemoryPayload>(
          `/api/settings/memory/item/${item.id}${memScope ? `?role_id=${encodeURIComponent(memScope)}` : ""}`,
          { pinned: !item.pinned },
        ),
      item.pinned ? "已取消钉住" : "已钉住（不参与淘汰与整理）",
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
          `/api/settings/memory/item/${item.id}${memScope ? `?role_id=${encodeURIComponent(memScope)}` : ""}`,
        ),
      "已删除",
    );
  };

  const loadWorkspace = useCallback(async () => {
    const d = await api.getWorkspaceDir();
    setWsDir(d);
    setWsDraft(d.path);
  }, []);

  useEffect(() => {
    loadWorkspace().catch((e) => setWsErr(`加载任务目录失败：${(e as Error).message}`));
  }, [loadWorkspace]);

  async function saveWorkspace() {
    setWsErr("");
    setWsMsg("");
    try {
      const d = await api.setWorkspaceDir(wsDraft.trim());
      setWsDir(d);
      setWsDraft(d.path);
      setWsMsg("已设置（保存即对角色下一轮生效）");
    } catch (e) {
      setWsErr(`保存失败：${(e as Error).message}`);
    }
  }

  async function clearWorkspace() {
    setWsErr("");
    setWsMsg("");
    try {
      const d = await api.clearWorkspaceDir();
      setWsDir(d);
      setWsDraft(d.path);
      setWsMsg("已清除，回落 env 默认目录");
    } catch (e) {
      setWsErr(`清除失败：${(e as Error).message}`);
    }
  }

  async function browseAt(path?: string) {
    setTreeErr("");
    try {
      setTree(await api.browseTree(path));
    } catch (e) {
      setTreeErr(`浏览失败：${(e as Error).message}`);
    }
  }

  const loadReachout = useCallback(async () => {
    const rt = await api.get<RuntimePayload>("/api/settings/runtime");
    const items = rt.groups.flatMap((g) => g.items);
    const sw = items.find((i) => i.field === "reachout_enabled");
    setReachoutOn(sw ? sw.value !== "0" && sw.value !== "未设置" : null);
    // 收件箱折叠窗口：读的是**生效值**（env + 在线覆盖叠加），和总闸同一个来源。
    const merge = items.find((i) => i.field === "reachout_merge_days");
    setMergeDays(merge ? Number.parseInt(merge.value, 10) || 1 : null);
  }, []);

  useEffect(() => {
    loadReachout().catch(() => setReachoutOn(null));
  }, [loadReachout]);

  async function changeMergeDays(next: string) {
    setReachoutErr("");
    setReachoutMsg("");
    try {
      await api.put<RuntimePayload>("/api/settings/runtime", {
        values: { reachout_merge_days: next },
      });
      setMergeDays(Number.parseInt(next, 10));
      setReachoutMsg(
        `收件箱已改为每 ${next} 天一摞：同一角色在这个窗口里的主动开口会折成一行`,
      );
    } catch (e) {
      setReachoutErr(`保存失败：${(e as Error).message}`);
    }
  }

  async function toggleReachout() {
    setReachoutErr("");
    setReachoutMsg("");
    try {
      await api.put<RuntimePayload>("/api/settings/runtime", {
        values: { reachout_enabled: reachoutOn ? "0" : "1" },
      });
      setReachoutOn(!reachoutOn);
      setReachoutMsg(reachoutOn ? "已关闭：所有角色都不会主动找你" : "已开启：角色可以主动找你（还需各角色卡的开关）");
    } catch (e) {
      setReachoutErr(`保存失败：${(e as Error).message}`);
    }
  }

  return (
    <div className="mt-6 space-y-4">
      <Card className="p-5">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-medium text-slate-900 dark:text-slate-100">跨会话记忆</h3>
          <button
            onClick={toggleMemory}
            disabled={!mem || !!memScope}
            title={memScope ? "注入开关是全局设置，切到「全局记忆」才能改" : undefined}
            className={`rounded-full px-3 py-1 text-xs font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${
              mem?.enabled
                ? "bg-blue-600 text-white"
                : "bg-slate-200 text-slate-500 dark:bg-slate-700 dark:text-slate-300"
            }`}
          >
            {mem ? (mem.enabled ? "记忆注入：已开启" : "记忆注入：已关闭") : "…"}
          </button>
        </div>
        <div className="mt-2 flex items-center gap-2">
          <label className="text-xs text-slate-500 dark:text-slate-400">作用域</label>
          <select
            value={memScope}
            onChange={(e) => changeMemScope(e.target.value)}
            className="rounded-lg border border-slate-200 bg-white px-2 py-1 text-xs text-slate-700 dark:border-slate-600 dark:bg-slate-900 dark:text-slate-200"
          >
            <option value="">全局（跨角色共享的用户事实）</option>
            {memRoles.map((r) => (
              <option key={r.role_id} value={r.role_id}>
                {r.role_name}（该角色专属记忆）
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
            ? "这是该角色自己的记忆（回忆触发只读这一份），与全局记忆和其它角色隔离。编辑/清空只作用于本角色。"
            : "全局记忆注入每个角色的对话。AI 检测到你明确说出的可复用事实（称呼 / 偏好 / 背景）会通过 memory_save 写入，并同步进当前角色的专属记忆。"}
        </p>
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
          <button
            onClick={addFact}
            disabled={!mem || itemBusy || !newFact.trim()}
            className="rounded-lg bg-blue-600 px-3 py-1.5 text-xs text-white hover:bg-blue-500 disabled:bg-slate-300 dark:disabled:bg-slate-700"
          >
            记住
          </button>
        </div>
        <ul className="mt-2 max-h-56 space-y-1 overflow-y-auto">
          {(mem?.items ?? []).map((item) => (
            <li
              key={item.id}
              className="flex items-start gap-2 rounded-lg border border-slate-100 px-2 py-1.5 text-xs dark:border-slate-700"
            >
              <span className="min-w-0 flex-1 break-words text-slate-700 dark:text-slate-200">
                {item.text}
                <span className="mt-0.5 block text-[10px] text-slate-400 dark:text-slate-500">
                  {SOURCE_LABEL[item.source] ?? item.source}
                  {" · "}用过 {item.hit_count} 次
                  {item.pinned ? " · 已钉住" : ""}
                </span>
              </span>
              <button
                onClick={() => void togglePin(item)}
                disabled={itemBusy}
                title={item.pinned ? "取消钉住（允许被淘汰/整理）" : "钉住（不参与淘汰与整理）"}
                className="shrink-0 rounded px-1.5 py-0.5 text-[11px] text-slate-500 hover:bg-slate-100 dark:hover:bg-slate-700"
              >
                {item.pinned ? "取消钉住" : "钉住"}
              </button>
              <button
                onClick={() => void removeItem(item)}
                disabled={itemBusy}
                className="shrink-0 rounded px-1.5 py-0.5 text-[11px] text-slate-500 hover:text-red-600 dark:hover:text-red-400"
              >
                删除
              </button>
            </li>
          ))}
        </ul>
        {mem?.over_limit && (
          <p className="mt-1.5 text-[11px] text-amber-600 dark:text-amber-400">
            活跃记忆已达上限（{mem.active_count}/{mem.limit}）—— 最弱的已被退役（没删，可撤销）。
            点上面的「整理记忆」合并同义的、让过时的失效，比继续堆着更准。
          </p>
        )}
        <details className="mt-2">
          <summary className="cursor-pointer text-[11px] text-slate-500 dark:text-slate-400">
            原文视图 / 整段编辑（注入到 prompt 的就是这段）
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
          <button
            onClick={saveMemory}
            disabled={!mem}
            className="rounded-lg bg-blue-600 px-3 py-1.5 text-xs text-white hover:bg-blue-500 disabled:opacity-50"
          >
            保存
          </button>
          <button
            onClick={clearMemory}
            disabled={!mem}
            className="rounded-lg border border-slate-200 px-3 py-1.5 text-xs text-slate-600 hover:border-red-300 hover:text-red-600 disabled:opacity-50 dark:border-slate-600 dark:text-slate-300"
          >
            清空
          </button>
          {memMsg && <span className="text-xs text-emerald-600 dark:text-emerald-400">{memMsg}</span>}
          {memErr && <span className="text-xs text-red-600 dark:text-red-400 dark:text-red-500">{memErr}</span>}
          {mem && <span className="ml-auto text-[11px] text-slate-400 dark:text-slate-500">{memDraft.length} 字</span>}
        </div>
      </Card>

      <Card className="p-5">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-medium text-slate-900 dark:text-slate-100">主动开口</h3>
          <button
            onClick={toggleReachout}
            disabled={reachoutOn === null}
            className={`rounded-full px-3 py-1 text-xs font-medium transition-colors ${
              reachoutOn
                ? "bg-blue-600 text-white"
                : "bg-slate-200 text-slate-500 dark:bg-slate-700 dark:text-slate-300"
            }`}
          >
            {reachoutOn === null ? "…" : reachoutOn ? "已开启" : "已关闭"}
          </button>
        </div>
        <p className="mt-1.5 text-xs leading-relaxed text-slate-500 dark:text-slate-400">
          全局总闸：关闭后所有角色都不会主动找你（静默，无提示音）。即使开着，也只有角色卡上
          勾选「角色会主动找你」的角色才会开口。
        </p>
        <label className="mt-3 flex items-center gap-2 text-xs text-slate-500 dark:text-slate-400">
          <span>收件箱折叠窗口</span>
          <select
            value={String(mergeDays ?? 1)}
            disabled={mergeDays === null}
            onChange={(e) => void changeMergeDays(e.target.value)}
            className="rounded border border-slate-200 bg-white px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-800"
          >
            <option value="1">每 1 天一摞（按天）</option>
            <option value="3">每 3 天一摞</option>
            <option value="7">每 7 天一摞</option>
          </select>
          {mergeDays === null && <span className="text-[10px] text-slate-400">读取中…</span>}
        </label>
        <p className="mt-1 text-[11px] leading-relaxed text-slate-400 dark:text-slate-500">
          同一角色在一个窗口里攒下的开口折成一行（行上有未读角标，展开看每一条）。
          有未读的那摞总是摊开 —— 折叠只为把旧消息收干净，不藏新消息。
        </p>
        {reachoutMsg && <p className="mt-1.5 text-xs text-emerald-600 dark:text-emerald-400">{reachoutMsg}</p>}
        {reachoutErr && <p className="mt-1.5 text-xs text-red-600 dark:text-red-400 dark:text-red-500">{reachoutErr}</p>}
      </Card>

      <Card className="p-5">
        <h3 className="text-sm font-medium text-slate-900 dark:text-slate-100">任务目录</h3>
        <p className="mt-1.5 text-xs leading-relaxed text-slate-500 dark:text-slate-400">
          角色读写文件的授权范围：只看得到、只碰得到这个目录里的内容（目录外一律拒绝）。
          修改保存后对角色下一轮对话即时生效。
        </p>
        <div className="mt-2.5 flex items-center gap-2">
          <input
            value={wsDraft}
            onChange={(e) => setWsDraft(e.target.value)}
            disabled={!wsDir}
            placeholder={wsDir ? "例如 D:\\my-tasks" : "加载中…"}
            className="min-w-0 flex-1 rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 font-mono text-xs text-slate-700 focus:border-blue-400 focus:outline-none dark:border-slate-600 dark:bg-slate-900 dark:text-slate-200"
          />
          <button
            onClick={() => {
              setTreeOpen(true);
              browseAt(wsDraft || undefined);
            }}
            disabled={!wsDir}
            className="shrink-0 rounded-lg border border-slate-200 px-3 py-1.5 text-xs text-slate-600 hover:border-blue-300 disabled:opacity-50 dark:border-slate-600 dark:text-slate-300"
          >
            浏览…
          </button>
          {/* 桌面壳里多一条系统对话框的路子（B/S 下整个按钮不出现）；挑完同样只填草稿，
              要用户按「保存」才生效 —— 授权范围不该被一次点击顺手改掉。 */}
          <NativeDirPickerButton disabled={!wsDir} onPicked={(p) => setWsDraft(p)} />
          <button
            onClick={saveWorkspace}
            disabled={!wsDir}
            className="shrink-0 rounded-lg bg-blue-600 px-3 py-1.5 text-xs text-white hover:bg-blue-500 disabled:opacity-50"
          >
            保存
          </button>
          {wsDir?.overridden && (
            <button
              onClick={clearWorkspace}
              className="shrink-0 rounded-lg border border-slate-200 px-3 py-1.5 text-xs text-slate-600 hover:border-red-300 hover:text-red-600 dark:border-slate-600 dark:text-slate-300"
            >
              清除（回落默认）
            </button>
          )}
        </div>
        <div className="mt-2 flex items-center gap-2 text-xs">
          {wsMsg && <span className="text-emerald-600 dark:text-emerald-400">{wsMsg}</span>}
          {wsErr && <span className="text-red-600 dark:text-red-400 dark:text-red-500">{wsErr}</span>}
          <span className="text-slate-400 dark:text-slate-500">
            当前：{wsDir ? (wsDir.overridden ? "自定义" : "env 默认") : "…"}
          </span>
        </div>
        {wsDir?.overridden && (
          <p className="mt-1.5 text-[11px] text-slate-400 dark:text-slate-500">
            该目录仅存于本机数据库；删除即回落 .env 的 WORKSPACE_DIR。
          </p>
        )}
      </Card>

      {/* 目录树选择抽屉（设置页专用，人用的选择器；后端/树契约见 /api/workspace/tree） */}
      {treeOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4">
          <div className="flex h-[70vh] w-full max-w-lg flex-col rounded-xl border border-slate-200 bg-white shadow-xl dark:border-slate-600 dark:bg-slate-800">
            <div className="flex items-center justify-between border-b border-slate-100 px-4 py-2.5 dark:border-slate-700">
              <span className="text-sm font-medium text-slate-900 dark:text-slate-100">选择任务目录</span>
              <button
                onClick={() => setTreeOpen(false)}
                className="text-xs text-slate-400 hover:text-slate-600 dark:hover:text-slate-200"
              >
                关闭
              </button>
            </div>
            <div className="flex items-center gap-1.5 border-b border-slate-100 px-3 py-2 text-xs dark:border-slate-700">
              <button
                onClick={() => browseAt(tree?.parent)}
                disabled={!tree}
                className="rounded border border-slate-200 px-2 py-0.5 text-slate-600 hover:border-blue-300 disabled:opacity-40 dark:border-slate-600 dark:text-slate-300"
              >
                ← 上一级
              </button>
              <span className="min-w-0 flex-1 truncate font-mono text-slate-500 dark:text-slate-400">
                {tree?.path ?? "…"}
              </span>
            </div>
            <div className="flex-1 overflow-y-auto p-2">
              {treeErr && <p className="px-2 py-1 text-xs text-red-600 dark:text-red-400">{treeErr}</p>}
              {(tree?.entries ?? []).map((e) => (
                <button
                  key={e.name}
                  onClick={() => e.is_dir && browseAt(`${tree!.path}\\${e.name}`)}
                  disabled={!e.is_dir}
                  className="flex w-full items-center justify-between rounded-lg px-2.5 py-1.5 text-left text-xs hover:bg-blue-50 disabled:cursor-default disabled:opacity-70 dark:hover:bg-blue-900/30"
                >
                  <span className={`truncate ${e.is_dir ? "text-slate-800 dark:text-slate-100" : "text-slate-400 dark:text-slate-500"} ${
                      e.is_dir ? "" : "pl-4"
                    }`}>
                    {e.is_dir ? "📁 " : ""}{e.name}
                  </span>
                  {!e.is_dir && e.size > 0 && (
                    <span className="ml-2 shrink-0 text-[10px] text-slate-400">
                      {e.size > 1024 ? `${(e.size / 1024).toFixed(0)} KB` : `${e.size} B`}
                    </span>
                  )}
                </button>
              ))}
              {tree && tree.entries.length === 0 && (
                <p className="px-2 py-4 text-center text-xs text-slate-400 dark:text-slate-500">（空目录）</p>
              )}
              {tree?.truncated && (
                <p className="px-2 py-1 text-[11px] text-slate-400 dark:text-slate-500">（条目过多，仅显示部分）</p>
              )}
            </div>
            <div className="flex items-center justify-end gap-2 border-t border-slate-100 px-4 py-2.5 dark:border-slate-700">
              <button
                onClick={() => setTreeOpen(false)}
                className="rounded-lg border border-slate-200 px-3 py-1.5 text-xs text-slate-600 hover:border-blue-300 dark:border-slate-600 dark:text-slate-300"
              >
                取消
              </button>
              <button
                onClick={() => {
                  if (!tree) return;
                  setWsDraft(tree.path);
                  setTreeOpen(false);
                }}
                className="rounded-lg bg-blue-600 px-3 py-1.5 text-xs text-white hover:bg-blue-500"
              >
                选中当前目录
              </button>
            </div>
          </div>
        </div>
      )}

    </div>
  );
}

// ---------------------------------------------------------------- 关于与系统状态

function AboutPanel({
  onOpenChat,
  theme,
  onToggleTheme,
}: {
  onOpenChat?: () => void;
  theme?: string;
  onToggleTheme?: () => void;
}) {
  const [info, setInfo] = useState<{
    defaultBackend: string;
    plugins: string;
    roles: number;
    sessions: number;
  }>({ defaultBackend: "…", plugins: "…", roles: 0, sessions: 0 });
  const [refreshed, setRefreshed] = useState(false);
  const [loadError, setLoadError] = useState("");

  const loadInfo = useCallback(async () => {
    const [ms, plugins, roles, sessions] = await Promise.all([
      api.get<ModelSettings>("/api/settings/models"),
      api.get<PluginRow[]>("/api/plugins"),
      api.get<RoleCard[]>("/api/roles"),
      api.get<SessionRow[]>("/api/sessions"),
    ]);
    const enabled = plugins.filter((p) => p.enabled).length;
    setInfo({
      defaultBackend: ms.default || "env 默认（local）",
      plugins: `${enabled} / ${plugins.length} 启用`,
      roles: roles.length,
      sessions: sessions.length,
    });
  }, []);

  useEffect(() => {
    loadInfo().catch((e) => setLoadError(`加载失败：${(e as Error).message}`));
  }, [loadInfo]);

  return (
    <div className="mt-6 space-y-4">
      {onToggleTheme && (
        <Card className="p-5">
          <h3 className="text-sm font-medium text-slate-900 dark:text-slate-100">外观</h3>
          <p className="mt-1 text-xs text-slate-400 dark:text-slate-500">
            界面配色跟随本机偏好时可手动覆盖；切换即时生效、随浏览器记住。
          </p>
          <div className="mt-2.5 flex items-center gap-2">
            <span className="text-xs text-slate-500 dark:text-slate-400">
              当前：{theme === "dark" ? "深色" : "浅色"}
            </span>
            <button
              onClick={onToggleTheme}
              className="rounded-lg border border-slate-200 px-2.5 py-1 text-xs text-slate-600 hover:border-blue-300 dark:border-slate-600 dark:text-slate-300 dark:hover:border-blue-700"
            >
              切换为{theme === "dark" ? "浅色" : "深色"}
            </button>
          </div>
        </Card>
      )}

      <Card className="p-5">
        <h3 className="text-sm font-medium text-slate-900 dark:text-slate-100">关于</h3>
        <p className="mt-1.5 text-xs leading-relaxed text-slate-500 dark:text-slate-400">
          rolecard-agent 控制台。多角色对话 Agent：角色卡控制人设与工具权限，
          插件以数据驱动启停。
        </p>
        <p className="mt-1 text-xs text-slate-400 dark:text-slate-500">语言：简体中文（内置）</p>
      </Card>

      <Card className="p-5">
        <h3 className="text-sm font-medium text-slate-900 dark:text-slate-100">系统状态</h3>
        {loadError && <p className="mt-1 text-xs text-red-600 dark:text-red-400 dark:text-red-500">{loadError}</p>}
        <dl className="mt-2 grid grid-cols-2 gap-x-6 gap-y-2 text-xs">
          <div className="flex justify-between border-b border-slate-50 py-1">
            <dt className="text-slate-400 dark:text-slate-500">默认模型后端</dt>
            <dd className="font-mono">{info.defaultBackend}</dd>
          </div>
          <div className="flex justify-between border-b border-slate-50 py-1">
            <dt className="text-slate-400 dark:text-slate-500">领域插件（启用/注册）</dt>
            <dd className="font-mono">{info.plugins}</dd>
          </div>
          <div className="flex justify-between border-b border-slate-50 py-1">
            <dt className="text-slate-400 dark:text-slate-500">角色卡数量</dt>
            <dd className="font-mono">{info.roles}</dd>
          </div>
          <div className="flex justify-between border-b border-slate-50 py-1">
            <dt className="text-slate-400 dark:text-slate-500">对话数量</dt>
            <dd className="font-mono">{info.sessions}</dd>
          </div>
        </dl>
        <p className="mt-2 text-[11px] text-slate-400 dark:text-slate-500">
          修改默认后端请前往「模型」页签；插件启停在「插件」页签（停用立即生效）。
        </p>
      </Card>

      <Card className="p-5">
        <h3 className="text-sm font-medium text-slate-900 dark:text-slate-100">演示数据</h3>
        <p className="mt-1.5 text-xs text-slate-500 dark:text-slate-400">
          评测 / 演示用的虚构档案可由脚本重建；对话与数据的管理操作在对话页与插件页。
        </p>
        <div className="mt-2 flex gap-2">
          <button
            onClick={() => onOpenChat?.()}
            className="rounded-lg border border-slate-200 dark:border-slate-700 px-3 py-1.5 text-xs text-slate-600 dark:text-slate-300 hover:border-blue-300 dark:hover:border-blue-700"
          >
            前往对话
          </button>
          <button
            onClick={async () => {
              await loadInfo();
              setRefreshed(true);
              setTimeout(() => setRefreshed(false), 2000);
            }}
            className="rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 px-3 py-1.5 text-xs text-slate-600 dark:text-slate-300 hover:border-blue-300 dark:hover:border-blue-700"
          >
            {refreshed ? "已刷新 ✓" : "刷新状态"}
          </button>
        </div>
      </Card>

      {/* 桌面壳安装包（D②-4）：没有产物时整卡不渲染，所以它放在最后也不会在页面上留空位。 */}
      <ShellReleaseCard />
    </div>
  );
}

// ---------------------------------------------------------------- 运行环境（可改 + 只读混合）

function RuntimePanel() {
  const [data, setData] = useState<RuntimePayload | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [baseline, setBaseline] = useState<Record<string, string>>({});
  const [err, setErr] = useState("");
  const [saved, setSaved] = useState("");
  const [busy, setBusy] = useState(false);

  const absorb = useCallback((p: RuntimePayload) => {
    setData(p);
    const d: Record<string, string> = {};
    const b: Record<string, string> = {};
    for (const g of p.groups) {
      for (const it of g.items) {
        if (it.kind !== "ro") {
          d[it.key] = it.override_value ?? "";
          b[it.key] = it.override_value ?? "";
        }
      }
    }
    setDraft(d);
    setBaseline(b);
  }, []);

  useEffect(() => {
    api
      .get<RuntimePayload>("/api/settings/runtime")
      .then(absorb)
      .catch((e) => setErr(`加载失败：${(e as Error).message}`));
  }, [absorb]);

  const changedCount = Object.keys(draft).filter((k) => draft[k] !== baseline[k]).length;

  async function save() {
    const values: Record<string, string | null> = {};
    for (const k of Object.keys(draft)) {
      if (draft[k] !== baseline[k]) values[k] = draft[k] === "" ? null : draft[k];
    }
    if (Object.keys(values).length === 0) return;
    setBusy(true);
    setErr("");
    try {
      absorb(await api.put<RuntimePayload>("/api/settings/runtime", { values }));
      setSaved("已保存并生效");
      setTimeout(() => setSaved(""), 3000);
    } catch (e) {
      setErr(`保存失败：${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  if (err && !data) {
    return <p className="mt-6 text-xs text-red-600 dark:text-red-400 dark:text-red-500">{err}</p>;
  }
  if (!data) {
    return <p className="mt-6 text-xs text-slate-400 dark:text-slate-500">加载中…</p>;
  }

  function inputFor(it: RuntimeItem) {
    const set = (v: string) => setDraft((d) => ({ ...d, [it.key]: v }));
    if (it.kind === "bool") {
      return (
        <select
          value={draft[it.key] ?? ""}
          onChange={(e) => set(e.target.value)}
          className="w-28 rounded border border-slate-200 px-1.5 py-1 text-xs outline-none focus:border-blue-400 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-200"
        >
          <option value="">跟随 .env（{it.default}）</option>
          <option value="1">开</option>
          <option value="0">关</option>
        </select>
      );
    }
    // 模型名单（动态选项来自模型页后端）→ 勾选组：勾一个算一个，存成逗号串。
    if (it.key === "MODEL_THINKING_MODELS" && it.choices && it.choices.length > 0) {
      const selected = (draft[it.key] ?? "").split(",").map((s) => s.trim()).filter(Boolean);
      const toggle = (name: string) => {
        const next = selected.includes(name)
          ? selected.filter((n) => n !== name)
          : [...selected, name];
        set(next.join(","));
      };
      return (
        <div className="flex flex-wrap gap-2">
          {it.choices.map((name) => (
            <label key={name} className="flex cursor-pointer items-center gap-1 font-mono text-xs text-slate-600 dark:text-slate-300">
              <input
                type="checkbox"
                checked={selected.includes(name)}
                onChange={() => toggle(name)}
                className="accent-blue-600"
              />
              {name}
            </label>
          ))}
        </div>
      );
    }
    // 枚举 → 下拉单选（含"跟随 .env"兜底项）
    if (it.choices && it.choices.length > 0) {
      const current = draft[it.key] ?? "";
      const extra = current && !it.choices.includes(current) ? [current] : [];
      return (
        <select
          value={current}
          onChange={(e) => set(e.target.value)}
          className="w-40 rounded border border-slate-200 px-1.5 py-1 text-xs outline-none focus:border-blue-400 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-200"
        >
          <option value="">跟随 .env（{it.value}）</option>
          {[...it.choices, ...extra].map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
      );
    }
    const type = it.kind === "secret" ? "password" : it.kind === "float" || it.kind === "int" ? "number" : "text";
    return (
      <input
        type={type}
        value={draft[it.key] ?? ""}
        onChange={(e) => set(e.target.value)}
        placeholder={it.overridden ? `覆盖中：${it.value}` : `未覆盖（当前 ${it.value}）`}
        className="w-56 rounded border border-slate-200 px-1.5 py-1 font-mono text-xs outline-none focus:border-blue-400 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-200"
      />
    );
  }

  return (
    <div className="mt-6 space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-[11px] leading-relaxed text-slate-400 dark:text-slate-500">{data.note}</p>
        <div className="flex shrink-0 items-center gap-2">
          {saved && <span className="text-xs text-green-600 dark:text-green-400">{saved}</span>}
          {err && <span className="text-xs text-red-600 dark:text-red-400">{err}</span>}
          <button
            onClick={save}
            disabled={busy || changedCount === 0}
            className="rounded-lg bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-700 disabled:bg-slate-300 dark:bg-slate-600 dark:disabled:bg-slate-700"
          >
            {busy ? "保存中…" : `保存${changedCount ? `（${changedCount} 项修改）` : ""}`}
          </button>
        </div>
      </div>
      {data.groups.map((g) => (
        <Card
          key={g.key}
          className="p-5"
        >
          <h3 className="text-sm font-medium text-slate-900 dark:text-slate-100">{g.label}</h3>
          <table className="mt-2 w-full text-xs">
            <tbody>
              {g.items.map((it) => (
                <tr key={it.key} className="border-b border-slate-50 last:border-0 dark:border-slate-700/50">
                  <td className="w-40 py-1.5 align-top">
                    <span className="font-medium text-slate-700 dark:text-slate-200">{it.label}</span>
                    <span className="ml-1.5 font-mono text-[10px] text-slate-300 dark:text-slate-600">{it.key}</span>
                  </td>
                  <td className="py-1.5 align-top">
                    {/* 主动开口总闸（reachout_enabled）是「记忆与任务目录」页签的单写点：
                        这里只读展示当前生效值，避免同一开关两处可写（设计稿 §2.2.2）。 */}
                    {/* 主动开口总闸与收件箱折叠窗口是「记忆与任务目录」页签的单写点：这里
                        只读展示当前生效值，避免同一开关两处可写（设计稿 §2.2.2）。
                        比对必须用 `field`（保存路径用的稳定身份），**不能用 `key`** —— `key`
                        是 env 名（REACHOUT_ENABLED），以前写成比 env 名的小串，运行环境里
                        其实一直是可编辑的，用例因为 stub 把 key 写成了 field 名而没发现。 */}
                    {it.kind === "ro"
                    || it.field === "reachout_enabled"
                    || it.field === "reachout_merge_days" ? (
                      <span className={`font-mono ${it.changed ? "text-amber-600 dark:text-amber-400" : "text-slate-600 dark:text-slate-300"}`}>
                        {it.value}
                      </span>
                    ) : (
                      inputFor(it)
                    )}
                  </td>
                  <td className="py-1.5 align-top text-slate-400 dark:text-slate-500">
                    {it.field === "reachout_enabled" || it.field === "reachout_merge_days"
                      ? "在「记忆与任务目录」页签修改"
                      : it.note || (it.changed && it.kind === "ro" ? `默认 ${it.default}` : "")}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      ))}
    </div>
  );
}
// ---------------------------------------------------------------- 审计（F3）

function AuditPanel() {
  const [rows, setRows] = useState<AuditRow[]>([]);
  const [status, setStatus] = useState("");
  const [filter, setFilter] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);

  useEffect(() => {
    api.get<AuditRow[]>("/api/audit?limit=200").then(setRows).catch((e) => setStatus(`加载失败：${e.message}`));
  }, []);

  // 按动作名 / 对象 / 详情模糊过滤
  const filtered = filter.trim()
    ? rows.filter((r) =>
        [r.action, r.target, r.detail_json].some((v) =>
          (v || "").toLowerCase().includes(filter.trim().toLowerCase()),
        ),
      )
    : rows;

  return (
    <div className="mt-6">
      <div className="mb-3 flex items-center gap-2">
        <input
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="筛选：动作名 / 对象 / 详情…"
          className="w-64 rounded-lg border border-slate-200 px-3 py-1.5 text-xs outline-none focus:border-blue-400 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-200"
        />
        <span className="text-[11px] text-slate-400">{filtered.length} / {rows.length} 条</span>
      </div>
      {status && <p className="rounded-lg bg-red-50 dark:bg-red-900/30 px-3 py-2 text-xs text-red-600 dark:text-red-400 dark:text-red-500">{status}</p>}
      {/* 横向可滚动：审计列（详情 JSON）天然宽，容器必须给滚动条而不是裁掉。 */}
      <Card className="overflow-x-auto">
        <table className="w-full min-w-[640px] text-left text-xs">
          <thead>
            <tr className="border-b border-slate-100 dark:border-slate-800 text-slate-400 dark:text-slate-500">
              <th className="px-4 py-2 font-medium">时间</th>
              <th className="px-4 py-2 font-medium">操作者</th>
              <th className="px-4 py-2 font-medium">动作</th>
              <th className="px-4 py-2 font-medium">对象</th>
              <th className="px-4 py-2 font-medium">详情</th>
            </tr>
          </thead>
          <tbody>
            {filtered.length === 0 && (
              <tr>
                <td colSpan={5} className="px-4 py-6 text-center text-slate-400 dark:text-slate-500">
                  暂无审计记录
                </td>
              </tr>
            )}
            {/* 审计行没有唯一 id（M7）：数据锚定 + 序号消歧的组合键；
                列表整体替换不重排，同 (ts,actor,action,target) 重复行靠 i 区分。 */}
            {filtered.map((a, i) => (
              <tr
                key={`${a.ts}|${a.actor}|${a.action}|${a.target ?? ""}|${i}`}
                className="border-b border-slate-50 last:border-0"
              >
                <td className="whitespace-nowrap px-4 py-2 font-mono text-slate-500 dark:text-slate-400">
                  {String(a.ts).replace("T", " ").slice(0, 19)}
                </td>
                <td className="px-4 py-2">{a.actor}</td>
                <td className="px-4 py-2">
                  <code className="rounded bg-slate-100 dark:bg-slate-700/50 px-1.5 py-0.5">{a.action}</code>
                </td>
                <td className="max-w-40 truncate px-4 py-2 font-mono text-slate-500 dark:text-slate-400">{a.target}</td>
                <td className="max-w-56 px-4 py-2 align-top text-slate-400 dark:text-slate-500">
                  {/* 详情可点开：截断摘要 + title 悬浮不够看全 JSON（审计走查反馈） */}
                  {a.detail_json && expanded === `${a.ts}|${a.action}` ? (
                    <pre className="max-w-72 whitespace-pre-wrap break-all rounded bg-slate-50 p-1.5 font-mono text-[11px] text-slate-600 dark:bg-slate-700/40 dark:text-slate-300">
                      {a.detail_json}
                    </pre>
                  ) : (
                    <button
                      onClick={() => setExpanded(a.detail_json ? `${a.ts}|${a.action}` : null)}
                      className="max-w-56 cursor-pointer truncate text-left hover:text-slate-600 dark:hover:text-slate-300"
                      title="点击展开完整详情"
                    >
                      {a.detail_json || "—"}
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
      <p className="mt-2 text-[11px] text-slate-400 dark:text-slate-500">
        审计由后端在角色切换、插件启停、对话创建、数据修正/删除时写入（US-3）；本页只读。
      </p>
    </div>
  );
}
