import { useEffect, useState } from "react";
import { Button, Notice, PageHeader } from "../components/ui";
import {
  api,
  type KnowledgeScopes,
  type ModelSettings,
  type RoleCard,
  type ToolCatalog,
  type ToolEntry,
} from "../api";
import { useConfirm } from "../hooks/useConfirm";

// 范例草稿行：rowId 是稳定 React key（M7），删行/插行时输入框状态不串行；
// 提交时只挑 user/assistant，rowId 不会外泄。
type ExemplarDraft = { rowId: number; user: string; assistant: string };

// 行 id 发号器：模块级自增（key 只需在当前列表实例内唯一）。
let exemplarSeq = 0;
const nextExemplarId = () => (exemplarSeq += 1);

const EMPTY_FORM = {
  role_id: "",
  role_name: "",
  system_prompt: "",
  temperature: 0.7,
  model_name: "",
  tool_whitelist: [] as string[],
  knowledge_scopes: [] as string[],
  exemplars: [] as ExemplarDraft[],
  description: "",
  reachout_enabled: false,
  recall_enabled: true,
  time_pattern_enabled: true,
  file_watch_enabled: true,
};

const SCOPE_PATTERN = /^[a-z][a-z0-9_]{0,63}$/;

/** 一组工具的复选框：工具名 + 一句话说明（title 悬浮给全文） */
function ToolGroup({
  label,
  entries,
  selected,
  onToggle,
}: {
  label: string;
  entries: ToolEntry[];
  selected: string[];
  onToggle: (name: string) => void;
}) {
  return (
    <div>
      <p className="text-xs font-medium text-slate-500 dark:text-slate-400">{label}</p>
      <div className="mt-1 grid grid-cols-2 gap-x-4 gap-y-1.5">
        {entries.map((t) => (
          <label
            key={t.name}
            className="flex cursor-pointer items-start gap-1.5 text-xs"
            title={t.description}
          >
            <input
              type="checkbox"
              checked={selected.includes(t.name)}
              onChange={() => onToggle(t.name)}
              className="mt-0.5"
            />
            <span className="min-w-0">
              <code className="text-[11px] text-slate-700 dark:text-slate-200">{t.name}</code>
              <span className="block truncate text-slate-400 dark:text-slate-500">{t.description}</span>
            </span>
          </label>
        ))}
      </div>
    </div>
  );
}

export default function RolesPage() {
  const [roles, setRoles] = useState<RoleCard[]>([]);
  const [editing, setEditing] = useState<string | null>(null); // null=关闭, ""=新建, 其他=role_id
  const [form, setForm] = useState(EMPTY_FORM);
  // null=全部工具（后端语义），custom=按勾选的白名单
  const [wlMode, setWlMode] = useState<"all" | "custom">("all");
  const [catalog, setCatalog] = useState<ToolCatalog | null>(null);
  const [backends, setBackends] = useState<string[]>([]);
  const confirm = useConfirm();
  const [newScope, setNewScope] = useState("");
  const [kbScopes, setKbScopes] = useState<string[]>([]);
  const [status, setStatus] = useState<{ ok: boolean; msg: string } | null>(null);

  async function load() {
    setRoles(await api.get<RoleCard[]>("/api/roles"));
  }
  useEffect(() => {
    load().catch((e) => setStatus({ ok: false, msg: `加载失败：${e.message}` }));
    api.get<ToolCatalog>("/api/tools/catalog").then(setCatalog).catch(() => {});
    api
      .get<ModelSettings>("/api/settings/models")
      // 角色级路由只允许指向**参与对话**的模型（`used_by` 含 chat，派生自服务页的引用行）；
      // 只服务嵌入/重排的行不是推理模型。
      .then((s) =>
        setBackends(
          (s.providers ?? [])
            .flatMap((g) => g.models)
            .filter((m) => m.used_by.includes("chat"))
            .map((m) => m.name),
        ),
      )
      .catch(() => {});
    // 可选作用域的真实来源：已建的知识集合（RAG 真实作用域），让下拉"所见即所得"。
    api
      .get<KnowledgeScopes>("/api/knowledge/scopes")
      .then((s) => setKbScopes(s.scopes))
      .catch(() => {});
  }, []);

  // 可选作用域 = 真实已建知识集合 ∪ 现存角色声明过的并集
  const knownScopes = Array.from(
    new Set(roles.flatMap((r) => r.knowledge_scopes || [])),
  );
  const availableScopes = Array.from(
    new Set<string>([...knownScopes, ...kbScopes]),
  )
    .filter((s) => !form.knowledge_scopes.includes(s))
    .sort();

  function toggleTool(name: string) {
    setForm((f) => ({
      ...f,
      tool_whitelist: f.tool_whitelist.includes(name)
        ? f.tool_whitelist.filter((n) => n !== name)
        : [...f.tool_whitelist, name],
    }));
  }

  function toggleScope(name: string) {
    setForm((f) => ({
      ...f,
      knowledge_scopes: f.knowledge_scopes.includes(name)
        ? f.knowledge_scopes.filter((n) => n !== name)
        : [...f.knowledge_scopes, name],
    }));
  }

  function addScope() {
    const name = newScope.trim();
    if (!name) return;
    if (!SCOPE_PATTERN.test(name)) {
      setStatus({
        ok: false,
        msg: "作用域名不合法：小写字母开头，只含小写字母/数字/下划线（如 health_reports）。",
      });
      return;
    }
    setStatus(null);
    setForm((f) => ({
      ...f,
      knowledge_scopes: f.knowledge_scopes.includes(name)
        ? f.knowledge_scopes
        : [...f.knowledge_scopes, name],
    }));
    setNewScope("");
  }

  function openCreate() {
    setEditing("");
    setForm(EMPTY_FORM);
    setWlMode("all");
  }
  function setExemplar(i: number, key: "user" | "assistant", value: string) {
    setForm((f) => ({
      ...f,
      exemplars: f.exemplars.map((e, j) => (j === i ? { ...e, [key]: value } : e)),
    }));
  }

  function openEdit(r: RoleCard) {
    setEditing(r.role_id);
    setForm({
      role_id: r.role_id,
      role_name: r.role_name,
      system_prompt: r.system_prompt,
      temperature: r.temperature,
      model_name: r.model_name || "",
      tool_whitelist: r.tool_whitelist || [],
      knowledge_scopes: r.knowledge_scopes || [],
      // 服务端范例行没有 id：装载时补发稳定 key（M7）。
      exemplars: (r.exemplars || []).map((e) => ({ ...e, rowId: nextExemplarId() })),
      description: r.description || "",
      reachout_enabled: !!r.reachout_enabled,
      recall_enabled: r.recall_enabled !== false,
      time_pattern_enabled: r.time_pattern_enabled !== false,
      file_watch_enabled: r.file_watch_enabled !== false,
    });
    setWlMode(r.tool_whitelist === null ? "all" : "custom");
  }

  async function save() {
    // 半填的范例行不提交（后端要求 user/assistant 都非空）
    const exemplars = form.exemplars
      .map((e) => ({ user: e.user.trim(), assistant: e.assistant.trim() }))
      .filter((e) => e.user && e.assistant);
    const payload = {
      role_id: form.role_id.trim(),
      role_name: form.role_name.trim(),
      system_prompt: form.system_prompt,
      temperature: Number(form.temperature),
      model_name: form.model_name.trim() || null,
      tool_whitelist: wlMode === "all" ? null : form.tool_whitelist,
      knowledge_scopes: form.knowledge_scopes.length ? form.knowledge_scopes : null,
      exemplars: exemplars.length ? exemplars : null,
      description: form.description.trim() || null,
      reachout_enabled: form.reachout_enabled,
      recall_enabled: form.recall_enabled,
      time_pattern_enabled: form.time_pattern_enabled,
      file_watch_enabled: form.file_watch_enabled,
    };
    try {
      if (editing) {
        const { role_id: _omit, ...patch } = payload;
        await api.patch(`/api/roles/${form.role_id}`, patch);
        setStatus({ ok: true, msg: `已更新角色 ${form.role_id}` });
      } else {
        await api.post("/api/roles", payload);
        setStatus({ ok: true, msg: `已创建角色 ${payload.role_id}` });
      }
      setEditing(null);
      await load();
    } catch (e) {
      setStatus({ ok: false, msg: `保存失败：${(e as Error).message}` });
    }
  }

  async function remove(roleId: string) {
    try {
      await api.del(`/api/roles/${roleId}`);
      setStatus({ ok: true, msg: `已删除 ${roleId}` });
      await load();
    } catch (e) {
      setStatus({ ok: false, msg: `删除失败：${(e as Error).message}` });
    }
  }

  return (
    <div className="h-full overflow-y-auto p-6">
      <div className="mx-auto max-w-4xl">
        <PageHeader
          title="角色卡"
          subtitle="人设、温度、工具白名单与知识作用域；内置角色不可删除"
          actions={<Button onClick={openCreate}>＋ 新建角色</Button>}
        />

        {status && (
          <Notice className="mt-3" tone={status.ok ? "ok" : "error"}>
            {status.msg}
          </Notice>
        )}

        {editing !== null && (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              save();
            }}
            className="mt-4 rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 p-5"
          >
            <div className="grid grid-cols-2 gap-4">
              <label className="block">
                <span className="text-xs text-slate-500 dark:text-slate-400">role_id（小写字母/数字/下划线{editing ? "，编辑时不可改" : ""}）</span>
                <input
                  required
                  disabled={!!editing}
                  value={form.role_id}
                  onChange={(e) => setForm({ ...form, role_id: e.target.value })}
                  pattern="[a-z][a-z0-9_]*"
                  className="mt-1 w-full rounded-lg border border-slate-200 dark:border-slate-700 px-3 py-2 text-sm disabled:bg-slate-50 dark:bg-slate-800/50"
                />
              </label>
              <label className="block">
                <span className="text-xs text-slate-500 dark:text-slate-400">role_name</span>
                <input
                  required
                  value={form.role_name}
                  onChange={(e) => setForm({ ...form, role_name: e.target.value })}
                  className="mt-1 w-full rounded-lg border border-slate-200 dark:border-slate-700 px-3 py-2 text-sm"
                />
              </label>
            </div>
            <label className="mt-3 block">
              <span className="text-xs text-slate-500 dark:text-slate-400">system_prompt（人设规则）</span>
              <textarea
                required
                rows={3}
                value={form.system_prompt}
                onChange={(e) => setForm({ ...form, system_prompt: e.target.value })}
                className="mt-1 w-full rounded-lg border border-slate-200 dark:border-slate-700 px-3 py-2 text-sm"
              />
            </label>
            <div className="mt-3 grid grid-cols-2 gap-4">
              <label className="block">
                <span className="text-xs text-slate-500 dark:text-slate-400">temperature（0–1）</span>
                <input
                  type="number"
                  step="0.1"
                  min="0"
                  max="1"
                  value={form.temperature}
                  onChange={(e) => setForm({ ...form, temperature: Number(e.target.value) })}
                  className="mt-1 w-full rounded-lg border border-slate-200 dark:border-slate-700 px-3 py-2 text-sm"
                />
              </label>
              <label className="block">
                <span className="text-xs text-slate-500 dark:text-slate-400">
                  模型后端（角色级路由：该角色的对话走此后端，US-8）
                </span>
                <select
                  value={form.model_name}
                  onChange={(e) => setForm({ ...form, model_name: e.target.value })}
                  className="mt-1 w-full rounded-lg border border-slate-200 dark:border-slate-700 px-3 py-2 text-sm"
                >
                  <option value="">默认后端</option>
                  {backends.map((b) => (
                    <option key={b} value={b}>
                      {b}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <label className="mt-3 flex cursor-pointer items-center gap-2 text-xs">
              <input
                type="checkbox"
                checked={form.reachout_enabled}
                onChange={(e) => setForm({ ...form, reachout_enabled: e.target.checked })}
              />
              <span className="text-slate-600 dark:text-slate-300">角色会主动找你（需全局「主动开口」总闸开着）</span>
            </label>
            <div className="mt-2 ml-5 flex flex-col gap-1.5 text-xs">
              <label className="flex cursor-pointer items-center gap-2">
                <input
                  type="checkbox"
                  checked={form.recall_enabled}
                  disabled={!form.reachout_enabled}
                  onChange={(e) => setForm({ ...form, recall_enabled: e.target.checked })}
                />
                <span className="text-slate-600 dark:text-slate-300">关系驱动·回忆：记得的事会主动提起（按角色记忆隔离）</span>
              </label>
              <label className="flex cursor-pointer items-center gap-2">
                <input
                  type="checkbox"
                  checked={form.time_pattern_enabled}
                  disabled={!form.reachout_enabled}
                  onChange={(e) => setForm({ ...form, time_pattern_enabled: e.target.checked })}
                />
                <span className="text-slate-600 dark:text-slate-300">关系驱动·时段规律：按你常互动的时段主动找你</span>
              </label>
              <label className="flex cursor-pointer items-center gap-2">
                <input
                  type="checkbox"
                  checked={form.file_watch_enabled}
                  disabled={!form.reachout_enabled}
                  onChange={(e) => setForm({ ...form, file_watch_enabled: e.target.checked })}
                />
                <span className="text-slate-600 dark:text-slate-300">文件事件：任务目录有变化时主动说（需全局「文件事件触发」开着）</span>
              </label>
            </div>
            <label className="mt-3 block">
              <span className="text-xs text-slate-500 dark:text-slate-400">工具权限</span>
              <div className="mt-1 flex gap-5 text-sm">
                <label className="flex cursor-pointer items-center gap-1.5">
                  <input
                    type="radio"
                    checked={wlMode === "all"}
                    onChange={() => setWlMode("all")}
                  />
                  使用全部可用工具（随插件启停自动伸缩）
                </label>
                <label className="flex cursor-pointer items-center gap-1.5">
                  <input
                    type="radio"
                    checked={wlMode === "custom"}
                    onChange={() => setWlMode("custom")}
                  />
                  自定义白名单
                </label>
              </div>
            </label>
            {wlMode === "custom" && catalog && (
              <div className="mt-2 space-y-3 rounded-lg border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800/50 p-3">
                <ToolGroup
                  label="内核工具（所有领域通用）"
                  entries={catalog.kernel}
                  selected={form.tool_whitelist}
                  onToggle={toggleTool}
                />
                {Object.entries(catalog.domains).map(([domain, entries]) => (
                  <ToolGroup
                    key={domain}
                    label={`领域插件：${domain}`}
                    entries={entries}
                    selected={form.tool_whitelist}
                    onToggle={toggleTool}
                  />
                ))}
                {form.tool_whitelist.length === 0 && (
                  <p className="text-xs text-amber-600 dark:text-amber-400">
                    未勾选任何工具 = 该角色没有任何工具可用
                  </p>
                )}
              </div>
            )}
            <label className="mt-3 block">
              <span className="text-xs text-slate-500 dark:text-slate-400">
                知识作用域（声明可检索的范围；不选 = 不可检索）
              </span>
              <div className="mt-1.5 rounded-lg border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800/50 p-3">
                {/* 已选：可移除的 chips */}
                {form.knowledge_scopes.length > 0 && (
                  <div className="mb-2 flex flex-wrap gap-1.5">
                    {form.knowledge_scopes.map((s) => (
                      <span
                        key={s}
                        className="flex items-center gap-1 rounded-full bg-blue-50 dark:bg-blue-900/30 px-2.5 py-1 text-xs text-blue-700 dark:text-blue-300"
                      >
                        {s}
                        <button
                          type="button"
                          onClick={() => toggleScope(s)}
                          className="text-blue-300 hover:text-blue-600 dark:text-blue-400"
                        >
                          ✕
                        </button>
                      </span>
                    ))}
                  </div>
                )}
                {/* 可选作用域下拉：来自真实已建知识集合 + 现存角色声明；选中即加入 */}
                {availableScopes.length > 0 && (
                  <div className="mb-2 flex flex-wrap items-center gap-2">
                    <span className="text-[11px] text-slate-400 dark:text-slate-500">从已有作用域添加：</span>
                    <select
                      value=""
                      onChange={(e) => {
                        if (e.target.value) toggleScope(e.target.value);
                      }}
                      className="rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 px-2 py-1.5 text-xs text-slate-600 dark:text-slate-300 outline-none focus:border-blue-400"
                    >
                      <option value="">＋ 选择作用域…</option>
                      {availableScopes.map((s) => (
                        <option key={s} value={s}>
                          {s}
                        </option>
                      ))}
                    </select>
                  </div>
                )}
                {/* 仍允许声明一个新作用域（库里还没有的） */}
                <div className="flex gap-2">
                  <input
                    value={newScope}
                    onChange={(e) => setNewScope(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") {
                        e.preventDefault();
                        addScope();
                      }
                    }}
                    placeholder="或新建作用域（如 health_reports）"
                    className="flex-1 rounded-lg border border-slate-200 dark:border-slate-700 px-2.5 py-1.5 text-xs"
                  />
                  <button
                    type="button"
                    onClick={addScope}
                    className="rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 px-3 py-1.5 text-xs text-slate-600 dark:text-slate-300 hover:border-blue-300 dark:hover:border-blue-700"
                  >
                    新建
                  </button>
                </div>
              </div>
              <span className="mt-1 block text-[11px] leading-relaxed text-slate-400 dark:text-slate-500">
                这是对内核知识库（RAG）的检索授权：声明 = 可检索该作用域；不声明 =
                不可检索。库归内核，角色只声明 —— 避免 N 个角色 × M 套索引。可选列表来自
                现存角色的声明并集。
              </span>
            </label>
            <div className="mt-3">
              <div className="flex items-center justify-between">
                <span className="text-xs text-slate-500 dark:text-slate-400">
                  范例（few-shot：教它「怎么答」，比讲规则更省 token）
                </span>
                {form.exemplars.length < 4 && (
                  <button
                    type="button"
                    onClick={() =>
                      setForm((f) => ({
                        ...f,
                        exemplars: [...f.exemplars, { rowId: nextExemplarId(), user: "", assistant: "" }],
                      }))
                    }
                    className="rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 px-2.5 py-1 text-xs text-slate-600 dark:text-slate-300 hover:border-blue-300 dark:hover:border-blue-700"
                  >
                    ＋ 加一条范例
                  </button>
                )}
              </div>
              <div className="mt-1.5 space-y-2">
                {form.exemplars.map((ex, i) => (
                  <div key={ex.rowId} className="rounded-lg border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800/50 p-2">
                    <div className="flex items-start gap-2">
                      <span className="mt-1.5 shrink-0 text-[11px] text-slate-400 dark:text-slate-500">用户</span>
                      <input
                        value={ex.user}
                        onChange={(e) => setExemplar(i, "user", e.target.value)}
                        placeholder="用户会怎么问"
                        className="flex-1 rounded-lg border border-slate-200 dark:border-slate-700 px-2.5 py-1.5 text-xs"
                      />
                      <button
                        type="button"
                        onClick={() =>
                          setForm((f) => ({
                            ...f,
                            exemplars: f.exemplars.filter((_, j) => j !== i),
                          }))
                        }
                        className="rounded px-1.5 py-1 text-xs text-slate-400 dark:text-slate-500 hover:bg-slate-100 dark:bg-slate-700/50 dark:hover:bg-slate-700 hover:text-red-500"
                        title="删除这条范例"
                      >
                        ✕
                      </button>
                    </div>
                    <div className="mt-1.5 flex items-start gap-2">
                      <span className="mt-1.5 shrink-0 text-[11px] text-slate-400 dark:text-slate-500">回答</span>
                      <textarea
                        value={ex.assistant}
                        onChange={(e) => setExemplar(i, "assistant", e.target.value)}
                        rows={2}
                        placeholder="理想的回答（体现语气与边界）"
                        className="flex-1 rounded-lg border border-slate-200 dark:border-slate-700 px-2.5 py-1.5 text-xs"
                      />
                    </div>
                  </div>
                ))}
                {form.exemplars.length === 0 && (
                  <p className="text-[11px] text-slate-400 dark:text-slate-500">
                    还没有范例。范例插在角色设定与安全规则之间（上限 4 条 / 共 3000 字）。
                  </p>
                )}
              </div>
              <span className="mt-1 block text-[11px] leading-relaxed text-slate-400 dark:text-slate-500">
                装配顺序：角色设定 → <b>范例</b> → 安全规则（安全规则永远最后，不可被覆盖）。
                半填（只写一半）的范例不会被提交。
              </span>
            </div>
            <label className="mt-3 block">
              <span className="text-xs text-slate-500 dark:text-slate-400">描述</span>
              <input
                value={form.description}
                onChange={(e) => setForm({ ...form, description: e.target.value })}
                className="mt-1 w-full rounded-lg border border-slate-200 dark:border-slate-700 px-3 py-2 text-sm"
              />
            </label>
            <div className="mt-4 flex gap-2">
              <button
                type="submit"
                className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700"
              >
                保存
              </button>
              <button
                type="button"
                onClick={() => setEditing(null)}
                className="rounded-lg px-4 py-2 text-sm text-slate-500 dark:text-slate-400 hover:bg-slate-50 dark:bg-slate-800/50 dark:hover:bg-slate-700/60"
              >
                取消
              </button>
            </div>
          </form>
        )}

        <div className="mt-4 overflow-hidden rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b border-slate-100 dark:border-slate-800 text-xs text-slate-400 dark:text-slate-500">
                <th className="px-4 py-2.5 font-medium">role_id</th>
                <th className="px-4 py-2.5 font-medium">名称</th>
                <th className="px-4 py-2.5 font-medium">后端</th>
                <th className="px-4 py-2.5 font-medium">工具白名单</th>
                <th className="px-4 py-2.5 font-medium">类型</th>
                <th className="px-4 py-2.5"></th>
              </tr>
            </thead>
            <tbody>
              {roles.map((r) => (
                <tr key={r.role_id} className="border-b border-slate-50 last:border-0">
                  <td className="px-4 py-2.5 font-mono text-xs">{r.role_id}</td>
                  <td className="px-4 py-2.5">{r.role_name}</td>
                  <td className="px-4 py-2.5 text-slate-500 dark:text-slate-400">{r.model_name || "默认"}</td>
                  <td className="max-w-52 truncate px-4 py-2.5 text-slate-500 dark:text-slate-400">
                    {r.tool_whitelist === null
                      ? "（全部）"
                      : r.tool_whitelist.length
                        ? r.tool_whitelist.join(", ")
                        : "（无）"}
                  </td>
                  <td className="px-4 py-2.5">
                    <span
                      className={`rounded-full px-2 py-0.5 text-xs ${
                        r.is_builtin
                          ? "bg-green-50 dark:bg-green-900/30 text-green-700 dark:text-green-300"
                          : "bg-slate-100 dark:bg-slate-700/50 text-slate-500 dark:text-slate-400"
                      }`}
                    >
                      {r.is_builtin ? "内置" : "自定义"}
                    </span>
                  </td>
                  <td className="px-4 py-2.5 text-right">
                    <button
                      onClick={() => {
                        openEdit(r);
                      }}
                      className="rounded px-2 py-1 text-xs text-blue-600 dark:text-blue-400 hover:bg-blue-50 dark:bg-blue-900/30"
                    >
                      编辑
                    </button>
                    {!r.is_builtin && (
                      <button
                        onClick={async () => {
                          if (await confirm({ title: "删除这个角色卡？", body: "将删除该角色卡（内置角色不可删）。", confirmText: "确认删除", danger: true })) remove(r.role_id);
                        }}
                        className="rounded px-2 py-1 text-xs text-red-500 hover:bg-red-50 dark:bg-red-900/30"
                      >
                        删除
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
