import { useEffect, useState } from "react";
import { api, type RoleCard, type ToolCatalog, type ToolEntry } from "../api";

const EMPTY_FORM = {
  role_id: "",
  role_name: "",
  system_prompt: "",
  temperature: 0.7,
  model_name: "",
  tool_whitelist: [] as string[],
  knowledge_scopes: "",
  description: "",
};

function splitList(v: string): string[] | null {
  const items = v
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  return items.length ? items : null;
}

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
      <p className="text-xs font-medium text-slate-500">{label}</p>
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
              <code className="text-[11px] text-slate-700">{t.name}</code>
              <span className="block truncate text-slate-400">{t.description}</span>
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
  const [confirmDel, setConfirmDel] = useState<string | null>(null);
  const [status, setStatus] = useState<{ ok: boolean; msg: string } | null>(null);

  async function load() {
    setRoles(await api.get<RoleCard[]>("/api/roles"));
  }
  useEffect(() => {
    load().catch((e) => setStatus({ ok: false, msg: `加载失败：${e.message}` }));
    api.get<ToolCatalog>("/api/tools/catalog").then(setCatalog).catch(() => {});
  }, []);

  function toggleTool(name: string) {
    setForm((f) => ({
      ...f,
      tool_whitelist: f.tool_whitelist.includes(name)
        ? f.tool_whitelist.filter((n) => n !== name)
        : [...f.tool_whitelist, name],
    }));
  }

  function openCreate() {
    setEditing("");
    setForm(EMPTY_FORM);
    setWlMode("all");
    setConfirmDel(null);
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
      knowledge_scopes: (r.knowledge_scopes || []).join(", "),
      description: r.description || "",
    });
    setWlMode(r.tool_whitelist === null ? "all" : "custom");
  }

  async function save() {
    const payload = {
      role_id: form.role_id.trim(),
      role_name: form.role_name.trim(),
      system_prompt: form.system_prompt,
      temperature: Number(form.temperature),
      model_name: form.model_name.trim() || null,
      tool_whitelist: wlMode === "all" ? null : form.tool_whitelist,
      knowledge_scopes: splitList(form.knowledge_scopes),
      description: form.description.trim() || null,
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
      setConfirmDel(null);
      setStatus({ ok: true, msg: `已删除 ${roleId}` });
      await load();
    } catch (e) {
      setStatus({ ok: false, msg: `删除失败：${(e as Error).message}` });
    }
  }

  return (
    <div className="h-full overflow-y-auto p-6">
      <div className="mx-auto max-w-4xl">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-base font-semibold text-slate-900">角色卡</h2>
            <p className="mt-0.5 text-xs text-slate-400">
              人设、温度、工具白名单与知识作用域；内置角色不可删除
            </p>
          </div>
          <button
            onClick={openCreate}
            className="rounded-lg bg-blue-600 px-3.5 py-2 text-sm font-medium text-white hover:bg-blue-700"
          >
            ＋ 新建角色
          </button>
        </div>

        {status && (
          <p
            className={`mt-3 rounded-lg px-3 py-2 text-xs ${
              status.ok ? "bg-green-50 text-green-700" : "bg-red-50 text-red-600"
            }`}
          >
            {status.msg}
          </p>
        )}

        {editing !== null && (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              save();
            }}
            className="mt-4 rounded-xl border border-slate-200 bg-white p-5"
          >
            <div className="grid grid-cols-2 gap-4">
              <label className="block">
                <span className="text-xs text-slate-500">role_id（小写字母/数字/下划线{editing ? "，编辑时不可改" : ""}）</span>
                <input
                  required
                  disabled={!!editing}
                  value={form.role_id}
                  onChange={(e) => setForm({ ...form, role_id: e.target.value })}
                  pattern="[a-z][a-z0-9_]*"
                  className="mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 text-sm disabled:bg-slate-50"
                />
              </label>
              <label className="block">
                <span className="text-xs text-slate-500">role_name</span>
                <input
                  required
                  value={form.role_name}
                  onChange={(e) => setForm({ ...form, role_name: e.target.value })}
                  className="mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 text-sm"
                />
              </label>
            </div>
            <label className="mt-3 block">
              <span className="text-xs text-slate-500">system_prompt（人设规则）</span>
              <textarea
                required
                rows={3}
                value={form.system_prompt}
                onChange={(e) => setForm({ ...form, system_prompt: e.target.value })}
                className="mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 text-sm"
              />
            </label>
            <div className="mt-3 grid grid-cols-2 gap-4">
              <label className="block">
                <span className="text-xs text-slate-500">temperature（0–1）</span>
                <input
                  type="number"
                  step="0.1"
                  min="0"
                  max="1"
                  value={form.temperature}
                  onChange={(e) => setForm({ ...form, temperature: Number(e.target.value) })}
                  className="mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 text-sm"
                />
              </label>
              <label className="block">
                <span className="text-xs text-slate-500">model_name（后端名，留空用默认）</span>
                <input
                  value={form.model_name}
                  onChange={(e) => setForm({ ...form, model_name: e.target.value })}
                  className="mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 text-sm"
                />
              </label>
            </div>
            <label className="mt-3 block">
              <span className="text-xs text-slate-500">工具权限</span>
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
              <div className="mt-2 space-y-3 rounded-lg border border-slate-200 bg-slate-50 p-3">
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
                  <p className="text-xs text-amber-600">
                    未勾选任何工具 = 该角色没有任何工具可用
                  </p>
                )}
              </div>
            )}
            <label className="mt-3 block">
              <span className="text-xs text-slate-500">知识作用域（逗号分隔；留空 = 不检索）</span>
              <input
                value={form.knowledge_scopes}
                onChange={(e) => setForm({ ...form, knowledge_scopes: e.target.value })}
                className="mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 text-sm"
              />
              <span className="mt-1 block text-[11px] leading-relaxed text-slate-400">
                这是对内核知识库（RAG，v2.1 接入）的检索授权：声明 = 可检索该作用域；不声明 =
                不可检索。库归内核，角色只声明 —— 避免 N 个角色 × M 套索引。
              </span>
            </label>
            <label className="mt-3 block">
              <span className="text-xs text-slate-500">描述</span>
              <input
                value={form.description}
                onChange={(e) => setForm({ ...form, description: e.target.value })}
                className="mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 text-sm"
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
                className="rounded-lg px-4 py-2 text-sm text-slate-500 hover:bg-slate-50"
              >
                取消
              </button>
            </div>
          </form>
        )}

        <div className="mt-4 overflow-hidden rounded-xl border border-slate-200 bg-white">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b border-slate-100 text-xs text-slate-400">
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
                  <td className="px-4 py-2.5 text-slate-500">{r.model_name || "默认"}</td>
                  <td className="max-w-52 truncate px-4 py-2.5 text-slate-500">
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
                          ? "bg-green-50 text-green-700"
                          : "bg-slate-100 text-slate-500"
                      }`}
                    >
                      {r.is_builtin ? "内置" : "自定义"}
                    </span>
                  </td>
                  <td className="px-4 py-2.5 text-right">
                    <button
                      onClick={() => {
                        setConfirmDel(null);
                        openEdit(r);
                      }}
                      className="rounded px-2 py-1 text-xs text-blue-600 hover:bg-blue-50"
                    >
                      编辑
                    </button>
                    {!r.is_builtin &&
                      (confirmDel === r.role_id ? (
                        <button
                          onClick={() => remove(r.role_id)}
                          className="rounded bg-red-500 px-2 py-1 text-xs text-white hover:bg-red-600"
                        >
                          确认删除
                        </button>
                      ) : (
                        <button
                          onClick={() => setConfirmDel(r.role_id)}
                          className="rounded px-2 py-1 text-xs text-red-500 hover:bg-red-50"
                        >
                          删除
                        </button>
                      ))}
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
