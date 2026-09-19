import { useCallback, useEffect, useState } from "react";
import { ExtensionPanel } from "../components/ExtensionPanel";
import { ModelKeepAlive } from "../components/ModelKeepAlive";
import { ServicesPanel } from "../components/ServicesPanel";
import {
  api,
  type AuditRow,
  type ModelProvider,
  type ModelSettings,
  type PluginRow,
  type RoleCard,
  type RuntimeItem,
  type RuntimePayload,
  type SessionRow,
  type TreeResult,
  type WorkspaceDir,
} from "../api";

// 设置页子页签：通用（系统信息）/ 模型（后端 CRUD + 热切换）/ 服务（运行时状态与降级策略）/
// 审计（操作留痕）。知识库已升为独立顶层页 —— RAG 是内核能力，不该埋在设置里。
const SETTINGS_TABS = [
  { key: "general", label: "通用" },
  { key: "models", label: "模型" },
  { key: "services", label: "服务" },
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
                  : "text-slate-500 dark:text-slate-400 dark:text-slate-500 hover:text-slate-700 dark:text-slate-200"
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>
        {/* 子页签常驻挂载 + hidden 隐藏（P1-11 同款）：条件渲染会让每次切页签重新挂载
            面板并重发请求 —— 服务页的探活会因此"每次点开都加载中"（用户 2026-09-18）。 */}
        <div className={tab === "general" ? "" : "hidden"}>
          <GeneralPanel onOpenChat={onOpenChat} theme={theme} onToggleTheme={onToggleTheme} />
        </div>
        <div className={tab === "models" ? "" : "hidden"}>
          <ModelsPanel />
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

// ---------------------------------------------------------------- 通用

function GeneralPanel({
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
  // 跨会话记忆面板：enabled = 总开关（runtime 覆盖，保存即热生效）；content = 全文。
  const [mem, setMem] = useState<{ enabled: boolean; content: string } | null>(null);
  const [memDraft, setMemDraft] = useState("");
  const [memMsg, setMemMsg] = useState("");
  const [memErr, setMemErr] = useState("");
  // 记忆作用域："" = 全局用户记忆；否则 = 该角色的专属记忆（role_memory，回忆触发用的那份）。
  // 注入开关是全局的，角色作用域只读/改文本、不能改开关。
  const [memScope, setMemScope] = useState("");
  const [memRoles, setMemRoles] = useState<{ role_id: string; role_name: string }[]>([]);
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
  const [reachoutMsg, setReachoutMsg] = useState("");
  const [reachoutErr, setReachoutErr] = useState("");

  const load = useCallback(async () => {
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
    setMemRoles(roles.map((r) => ({ role_id: r.role_id, role_name: r.role_name })));
  }, []);

  const memoryUrl = (scope: string) =>
    scope ? `/api/settings/memory?role_id=${encodeURIComponent(scope)}` : "/api/settings/memory";

  const loadMemory = useCallback(async (scope: string) => {
    const m = await api.get<{ enabled: boolean; content: string }>(memoryUrl(scope));
    setMem(m);
    setMemDraft(m.content);
  }, []);

  useEffect(() => {
    load().catch((e) => setLoadError(`加载失败：${(e as Error).message}`));
    loadMemory(memScope).catch((e) => setMemErr(`加载失败：${(e as Error).message}`));
  }, [load, loadMemory, memScope]);

  function changeMemScope(next: string) {
    if (next === memScope) return;
    // 未保存改动：切换作用域前确认，避免草稿被静默丢弃。
    if (mem && memDraft !== mem.content && !window.confirm("当前作用域有未保存的修改，切换会丢弃，继续？")) {
      return;
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
      const m = await api.put<{ enabled: boolean; content: string }>("/api/settings/memory", {
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
      const m = await api.put<{ enabled: boolean; content: string }>(memoryUrl(memScope), {
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
      const m = await api.del<{ enabled: boolean; content: string }>(memoryUrl(memScope));
      setMem(m);
      setMemDraft(m.content);
      setMemMsg("已清空");
    } catch (e) {
      setMemErr(`清空失败：${(e as Error).message}`);
    }
  }

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
    const item = rt.groups.flatMap((g) => g.items).find((i) => i.field === "reachout_enabled");
    setReachoutOn(item ? item.value !== "0" && item.value !== "未设置" : null);
  }, []);

  useEffect(() => {
    loadReachout().catch(() => setReachoutOn(null));
  }, [loadReachout]);

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
      {onToggleTheme && (
        <div className="rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 p-5">
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
        </div>
      )}

      <div className="rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 p-5">
        <h3 className="text-sm font-medium text-slate-900 dark:text-slate-100">关于</h3>
        <p className="mt-1.5 text-xs leading-relaxed text-slate-500 dark:text-slate-400 dark:text-slate-500">
          rolecard-agent 控制台。多角色对话 Agent：角色卡控制人设与工具权限，
          插件以数据驱动启停。
        </p>
        <p className="mt-1 text-xs text-slate-400 dark:text-slate-500">语言：简体中文（内置）</p>
      </div>

      <div className="rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 p-5">
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
        </div>
        <p className="mt-1.5 text-xs leading-relaxed text-slate-500 dark:text-slate-400 dark:text-slate-500">
          {memScope
            ? "这是该角色自己的记忆（回忆触发只读这一份），与全局记忆和其它角色隔离。编辑/清空只作用于本角色。"
            : "全局记忆注入每个角色的对话。AI 检测到你明确说出的可复用事实（称呼 / 偏好 / 背景）会通过 memory_save 写入，并同步进当前角色的专属记忆。"}
        </p>
        <textarea
          value={memDraft}
          onChange={(e) => setMemDraft(e.target.value)}
          rows={5}
          disabled={!mem}
          placeholder={mem ? (mem.content ? "" : memScope ? "该角色还没有专属记忆。" : "暂无全局记忆。") : "加载中…"}
          className="mt-2.5 w-full resize-y rounded-lg border border-slate-200 bg-white p-2.5 font-mono text-xs text-slate-700 focus:border-blue-400 focus:outline-none dark:border-slate-600 dark:bg-slate-900 dark:text-slate-200"
        />
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
      </div>

      <div className="rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 p-5">
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
        <p className="mt-1.5 text-xs leading-relaxed text-slate-500 dark:text-slate-400 dark:text-slate-500">
          全局总闸：关闭后所有角色都不会主动找你（静默，无提示音）。即使开着，也只有角色卡上
          勾选「角色会主动找你」的角色才会开口。
        </p>
        {reachoutMsg && <p className="mt-1.5 text-xs text-emerald-600 dark:text-emerald-400">{reachoutMsg}</p>}
        {reachoutErr && <p className="mt-1.5 text-xs text-red-600 dark:text-red-400 dark:text-red-500">{reachoutErr}</p>}
      </div>

      <div className="rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 p-5">
        <h3 className="text-sm font-medium text-slate-900 dark:text-slate-100">任务目录</h3>
        <p className="mt-1.5 text-xs leading-relaxed text-slate-500 dark:text-slate-400 dark:text-slate-500">
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
      </div>

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

      <div className="rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 p-5">
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
      </div>

      <div className="rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 p-5">
        <h3 className="text-sm font-medium text-slate-900 dark:text-slate-100">演示数据</h3>
        <p className="mt-1.5 text-xs text-slate-500 dark:text-slate-400 dark:text-slate-500">
          评测 / 演示用的虚构档案可由脚本重建；对话与数据的管理操作在对话页与插件页。
        </p>
        <div className="mt-2 flex gap-2">
          <button
            onClick={() => onOpenChat?.()}
            className="rounded-lg border border-slate-200 dark:border-slate-700 px-3 py-1.5 text-xs text-slate-600 dark:text-slate-300 dark:text-slate-600 hover:border-blue-300 dark:hover:border-blue-700"
          >
            前往对话
          </button>
          <button
            onClick={async () => {
              await load();
              setRefreshed(true);
              setTimeout(() => setRefreshed(false), 2000);
            }}
            className="rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 px-3 py-1.5 text-xs text-slate-600 dark:text-slate-300 dark:text-slate-600 hover:border-blue-300 dark:hover:border-blue-700"
          >
            {refreshed ? "已刷新 ✓" : "刷新状态"}
          </button>
        </div>
      </div>
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
        <div
          key={g.key}
          className="rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 p-5"
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
                    {it.kind === "ro" ? (
                      <span className={`font-mono ${it.changed ? "text-amber-600 dark:text-amber-400" : "text-slate-600 dark:text-slate-300"}`}>
                        {it.value}
                      </span>
                    ) : (
                      inputFor(it)
                    )}
                  </td>
                  <td className="py-1.5 align-top text-slate-400 dark:text-slate-500">
                    {it.note || (it.changed && it.kind === "ro" ? `默认 ${it.default}` : "")}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------- 模型（后端 CRUD + 回退）

// 供应商目录的兜底（后端不可达时仍可用）；正常来自 GET /api/settings/model-providers。
const PROVIDER_FALLBACK: ModelProvider[] = [
  { id: "ollama", label: "本地 Ollama", needs_key: "0", base_url_hint: "http://localhost:11434（可留空）", style: "native" },
  { id: "local", label: "本地模型（Ollama 别名）", needs_key: "0", base_url_hint: "同 Ollama，可留空", style: "native" },
  { id: "openai", label: "OpenAI 兼容", needs_key: "1", base_url_hint: "https://api.openai.com/v1", style: "openai" },
  { id: "siliconflow", label: "SiliconFlow", needs_key: "1", base_url_hint: "https://api.siliconflow.cn/v1", style: "openai" },
  { id: "deepseek", label: "DeepSeek", needs_key: "1", base_url_hint: "https://api.deepseek.com/v1", style: "openai" },
];

interface EditableBackend {
  name: string;
  provider: string;
  base_url: string;
  model: string;
  api_key: string;
  has_key: boolean;
  key_masked: string | null;
  usage: string;
  supports_vision: boolean;
  supports_tools: boolean;
}

// 模型页是云端配置的唯一事实面：usage 标记该行服务谁（服务页按用途引用）。
const USAGE_OPTIONS = [
  { value: "chat", label: "对话推理" },
  { value: "embedding", label: "语义嵌入" },
  { value: "rerank", label: "检索重排" },
  { value: "ocr", label: "OCR 凭据" },
];

function ModelsPanel() {
  const [def, setDef] = useState<string>("");
  const [rows, setRows] = useState<EditableBackend[]>([]);
  const [providers, setProviders] = useState<ModelProvider[]>(PROVIDER_FALLBACK);
  const [status, setStatus] = useState<{ ok: boolean; msg: string } | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [fb1, setFb1] = useState("");
  const [fb2, setFb2] = useState("");

  const load = useCallback(async () => {
    const s = await api.get<ModelSettings>("/api/settings/models");
    setDef(s.default || s.backends[0]?.name || "");
    setRows(
      s.backends.map((b) => ({
        name: b.name,
        provider: b.provider,
        base_url: b.base_url || "",
        model: b.model,
        api_key: "",
        has_key: b.has_key,
        key_masked: b.key_masked ?? null,
        usage: b.usage ?? "chat",
        supports_vision: b.supports_vision ?? false,
        supports_tools: b.supports_tools ?? true,
      })),
    );
    setFb1(s.fallbacks?.[0] || "");
    setFb2(s.fallbacks?.[1] || "");
    setLoaded(true);
  }, []);

  // 供应商目录（动态）：下拉从此来，新增供应商只改后端。
  useEffect(() => {
    api
      .get<{ providers: ModelProvider[] }>("/api/settings/model-providers")
      .then((s) => s.providers?.length && setProviders(s.providers))
      .catch(() => undefined);
  }, []);

  // 按供应商分组展示（同一供应商的 key 归在一起，便于区分用途）。
  const grouped = rows.reduce<Record<string, EditableBackend[]>>((acc, r) => {
    (acc[r.provider] ||= []).push(r);
    return acc;
  }, {});
  const providerOrder = Object.keys(grouped).sort((a, z) => a.localeCompare(z));

  useEffect(() => {
    load().catch((e) => setStatus({ ok: false, msg: `加载失败：${e.message}` }));
  }, [load]);

  function update(i: number, patch: Partial<EditableBackend>) {
    setRows((rs) => rs.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  }

  function addRow() {
    setRows((rs) => [
      ...rs,
      {
        name: "",
        provider: providers[0]?.id || "openai",
        base_url: "",
        model: "",
        api_key: "",
        has_key: false,
        key_masked: null,
        usage: "chat",
        supports_vision: false,
        supports_tools: true,
      },
    ]);
  }

  function removeRow(i: number) {
    setRows((rs) => rs.filter((_, j) => j !== i));
  }

  async function save() {
    try {
      const body = {
        default: def,
        backends: rows.map((r) => ({
          name: r.name.trim(),
          provider: r.provider.trim(),
          base_url: r.base_url.trim() || null,
          model: r.model.trim(),
          usage: r.usage,
          supports_vision: r.supports_vision,
          supports_tools: r.supports_tools,
          // 空串会被后端理解为"清除"；这里区分"没碰过"（保持 None=保留）与"清空"
          api_key: r.api_key === "" && r.has_key ? null : r.api_key,
        })),
        fallbacks: [fb1, fb2].filter(Boolean),
      };
      const saved = await api.put<ModelSettings>("/api/settings/models", body);
      setDef(saved.default || "");
      setRows(
        saved.backends.map((b) => ({
        name: b.name,
        provider: b.provider,
        base_url: b.base_url || "",
        model: b.model,
        api_key: "",
        has_key: b.has_key,
        key_masked: b.key_masked ?? null,
        usage: b.usage ?? "chat",
        supports_vision: b.supports_vision ?? false,
        supports_tools: b.supports_tools ?? true,
      })),
      );
      setFb1(saved.fallbacks?.[0] || "");
      setFb2(saved.fallbacks?.[1] || "");
      setStatus({
        ok: true,
        msg: "已保存并热生效：下一轮对话即使用新模型后端（无需重启）。",
      });
    } catch (e) {
      setStatus({ ok: false, msg: `保存失败：${(e as Error).message}` });
    }
  }

  return (
    <div className="mt-6">
      <ModelKeepAlive />
      {status && (
        <p
          className={`rounded-lg px-3 py-2 text-xs ${
            status.ok ? "bg-green-50 dark:bg-green-900/30 text-green-700 dark:text-green-300" : "bg-red-50 dark:bg-red-900/30 text-red-600 dark:text-red-400 dark:text-red-500"
          }`}
        >
          {status.msg}
        </p>
      )}

      {loaded && (
        <>
          <div className="space-y-5">
            {providerOrder.map((prov) => (
              <div key={prov} className="space-y-3">
                <div className="flex items-center gap-2 border-b border-slate-100 dark:border-slate-800 pb-1">
                  <span className="text-xs font-medium text-slate-600 dark:text-slate-300 dark:text-slate-400">
                    供应商：{providers.find((p) => p.id === prov)?.label ?? prov}
                  </span>
                  <span className="text-[11px] text-slate-400 dark:text-slate-500">
                    {providers.find((p) => p.id === prov)?.base_url_hint || ""}
                  </span>
                </div>
                {grouped[prov].map((r) => {
                  const i = rows.indexOf(r);
                  // key 用行下标而**不是 r.name**：改名时 key 一变整棵子树重挂，
                  // 输入框每敲一个字就失焦（审查报告 P1-12）。
                  return (
              <div key={i} className="rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 p-4">
                <div className="flex items-center gap-3">
                  {r.usage === "chat" && (
                    <label
                      title="对话默认后端（角色未指定后端时使用）"
                      className="flex items-center gap-1.5 text-xs text-slate-500 dark:text-slate-400 dark:text-slate-500"
                    >
                      <input
                        type="radio"
                        name="default-backend"
                        checked={def === r.name}
                        onChange={() => setDef(r.name)}
                      />
                      默认
                    </label>
                  )}
                  <input
                    value={r.name}
                    onChange={(e) => update(i, { name: e.target.value })}
                    placeholder="后端名（如 siliconflow）"
                    className="w-40 rounded-lg border border-slate-200 dark:border-slate-700 px-2.5 py-1.5 font-mono text-sm"
                  />
                  <select
                    value={r.provider}
                    onChange={(e) => update(i, { provider: e.target.value })}
                    className="rounded-lg border border-slate-200 dark:border-slate-700 px-2 py-1.5 text-sm"
                  >
                    {providers.map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.label}
                      </option>
                    ))}
                  </select>
                  <select
                    value={r.usage}
                    onChange={(e) => update(i, { usage: e.target.value })}
                    title="用途：本行配置服务谁（服务页按用途引用；对话菜单只显示「对话推理」行）"
                    className="rounded-lg border border-slate-200 dark:border-slate-700 px-2 py-1.5 text-sm"
                  >
                    {USAGE_OPTIONS.map((u) => (
                      <option key={u.value} value={u.value}>
                        {u.label}
                      </option>
                    ))}
                  </select>
                  <button
                    onClick={() => removeRow(i)}
                    className="ml-auto rounded px-2 py-1 text-xs text-red-400 dark:text-red-500 hover:bg-red-50 dark:bg-red-900/30 hover:text-red-600 dark:text-red-400 dark:text-red-500"
                  >
                    移除
                  </button>
                </div>
                <div className="mt-2.5 grid grid-cols-2 gap-3">
                  <input
                    value={r.model}
                    onChange={(e) => update(i, { model: e.target.value })}
                    placeholder="模型名（如 deepseek-ai/DeepSeek-V4-Flash）"
                    className="w-full rounded-lg border border-slate-200 dark:border-slate-700 px-2.5 py-1.5 text-sm"
                  />
                  <input
                    value={r.base_url}
                    onChange={(e) => update(i, { base_url: e.target.value })}
                    placeholder="base_url（Ollama 可留空，如 https://api.siliconflow.cn/v1）"
                    className="w-full rounded-lg border border-slate-200 dark:border-slate-700 px-2.5 py-1.5 text-sm"
                  />
                  {r.usage === "chat" && (
                    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-500 dark:text-slate-400">
                      <label className="flex cursor-pointer items-center gap-1.5">
                        <input
                          type="checkbox"
                          checked={r.supports_vision}
                          onChange={(e) => update(i, { supports_vision: e.target.checked })}
                        />
                        支持视觉（可收图）
                      </label>
                      <label className="flex cursor-pointer items-center gap-1.5">
                        <input
                          type="checkbox"
                          checked={r.supports_tools}
                          onChange={(e) => update(i, { supports_tools: e.target.checked })}
                        />
                        支持工具调用
                      </label>
                      {!r.supports_tools && (
                        <span className="text-[11px] text-amber-500 dark:text-amber-400">
                          该模型带工具会返回空，已关闭工具（换模型对所有角色仍可用，只是不绑工具）
                        </span>
                      )}
                    </div>
                  )}
                  {r.has_key ? (
                    <>
                      <div className="flex items-center gap-2 rounded-lg border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800/60 px-2.5 py-1.5 text-xs text-slate-500 dark:text-slate-400 dark:text-slate-500">
                        <span className="font-mono">{r.key_masked || "••••••"}</span>
                        <span className="ml-auto text-[11px] text-slate-400 dark:text-slate-500">已保存（不可见明文）</span>
                      </div>
                      <label className="flex items-center gap-1.5 text-xs text-slate-400 dark:text-slate-500">
                        <input
                          type="checkbox"
                          checked={r.api_key.trim() === "" && r.api_key.length > 0}
                          onChange={(e) => update(i, { api_key: e.target.checked ? " " : "" })}
                        />
                        清除已存密钥（勾选并保存即删除）
                      </label>
                    </>
                  ) : (
                    <input
                      type="password"
                      value={r.api_key}
                      onChange={(e) => update(i, { api_key: e.target.value })}
                      placeholder="api_key（可选）"
                      className="w-full rounded-lg border border-slate-200 dark:border-slate-700 px-2.5 py-1.5 text-sm"
                    />
                  )}
                  {!r.has_key && (
                    <span className="text-[11px] text-slate-400 dark:text-slate-500">
                      本地类供应商无需密钥
                    </span>
                  )}
                </div>
              </div>
                  );
                })}
              </div>
            ))}
          </div>

          <div className="mt-4 flex gap-2">
            <button
              onClick={addRow}
              className="rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 px-4 py-2 text-sm text-slate-600 dark:text-slate-300 dark:text-slate-600 hover:bg-slate-50 dark:bg-slate-800/50 dark:hover:bg-slate-700/60"
            >
              ＋ 添加后端
            </button>
            <button
              onClick={save}
              className="rounded-lg bg-blue-600 px-5 py-2 text-sm font-medium text-white hover:bg-blue-700"
            >
              保存并生效
            </button>
          </div>

          <p className="mt-4 rounded-lg bg-slate-50 dark:bg-slate-800/50 px-3 py-2.5 text-xs leading-relaxed text-slate-400 dark:text-slate-500">
            说明：首次启动会把 env 里的后端迁移到这里；<b>此后模型配置以本页为准</b>（env 不再参与，
            在页面里删除的后端重启后也不会回来）。删除所有后端会保存失败 —— 至少保留一个。
            角色可在「角色卡」页经由后端下拉做角色级路由。
          </p>
        </>
      )}
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
      <div className="overflow-x-auto rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800">
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
                <td className="whitespace-nowrap px-4 py-2 font-mono text-slate-500 dark:text-slate-400 dark:text-slate-500">
                  {String(a.ts).replace("T", " ").slice(0, 19)}
                </td>
                <td className="px-4 py-2">{a.actor}</td>
                <td className="px-4 py-2">
                  <code className="rounded bg-slate-100 dark:bg-slate-700/50 px-1.5 py-0.5">{a.action}</code>
                </td>
                <td className="max-w-40 truncate px-4 py-2 font-mono text-slate-500 dark:text-slate-400 dark:text-slate-500">{a.target}</td>
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
      </div>
      <p className="mt-2 text-[11px] text-slate-400 dark:text-slate-500">
        审计由后端在角色切换、插件启停、对话创建、数据修正/删除时写入（US-3）；本页只读。
      </p>
    </div>
  );
}
