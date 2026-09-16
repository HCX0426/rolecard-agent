import { useCallback, useEffect, useState } from "react";
import { ServicesPanel } from "../components/ServicesPanel";
import {
  api,
  type AuditRow,
  type ModelProvider,
  type ModelSettings,
  type PluginRow,
  type RoleCard,
  type SessionRow,
} from "../api";

// 设置页子页签：通用（系统信息）/ 模型（后端 CRUD + 热切换）/ 服务（运行时状态与降级策略）/
// 审计（操作留痕）。知识库已升为独立顶层页 —— RAG 是内核能力，不该埋在设置里。
const SETTINGS_TABS = [
  { key: "general", label: "通用" },
  { key: "models", label: "模型" },
  { key: "services", label: "服务" },
  { key: "audit", label: "审计" },
] as const;

type SettingsTab = (typeof SETTINGS_TABS)[number]["key"];

export default function SettingsPage({ onOpenChat }: { onOpenChat?: () => void }) {
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
        {tab === "general" && <GeneralPanel onOpenChat={onOpenChat} />}
        {tab === "models" && <ModelsPanel />}
        {tab === "services" && <ServicesPanel />}
        {tab === "audit" && <AuditPanel />}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- 通用

function GeneralPanel({ onOpenChat }: { onOpenChat?: () => void }) {
  const [info, setInfo] = useState<{
    defaultBackend: string;
    plugins: string;
    roles: number;
    sessions: number;
  }>({ defaultBackend: "…", plugins: "…", roles: 0, sessions: 0 });
  const [refreshed, setRefreshed] = useState(false);
  const [loadError, setLoadError] = useState("");

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
  }, []);

  useEffect(() => {
    load().catch((e) => setLoadError(`加载失败：${(e as Error).message}`));
  }, [load]);

  return (
    <div className="mt-6 space-y-4">
      <div className="rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 p-5">
        <h3 className="text-sm font-medium text-slate-900 dark:text-slate-100">关于</h3>
        <p className="mt-1.5 text-xs leading-relaxed text-slate-500 dark:text-slate-400 dark:text-slate-500">
          rolecard-agent 控制台 · v1（M1 内核 / M2 角色与插件 / M3 领域工具 / M4 接入层 /
          M5 前端工程化）。多角色对话 Agent 内核：角色卡控制人设与工具权限，插件以数据驱动启停。
        </p>
        <p className="mt-1 text-xs text-slate-400 dark:text-slate-500">语言：简体中文（内置）</p>
      </div>

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
            <dt className="text-slate-400 dark:text-slate-500">会话数量</dt>
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
          评测 / 演示用的虚构档案可由脚本重建；会话与数据管理的日常操作在对话页与插件页。
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
                  return (
              <div key={r.name} className="rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 p-4">
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

          <div className="mt-4 rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 p-4">
            <h3 className="text-sm font-medium text-slate-900 dark:text-slate-100">失败自动回退</h3>
            <p className="mt-1 text-xs text-slate-400 dark:text-slate-500">
              默认后端请求失败（建流阶段）时按序尝试；最多两级，流开始后的失败不回退（前端重试兜底）。
            </p>
            <div className="mt-2 grid grid-cols-2 gap-3">
              <select
                value={fb1}
                onChange={(e) => setFb1(e.target.value)}
                className="rounded-lg border border-slate-200 dark:border-slate-700 px-2.5 py-1.5 text-sm"
              >
                <option value="">一级回退：无</option>
                {rows
                  .filter((r) => r.name.trim() && r.name.trim() !== def)
                  .map((r) => (
                    <option key={r.name} value={r.name.trim()}>
                      {r.name.trim()}
                    </option>
                  ))}
              </select>
              <select
                value={fb2}
                onChange={(e) => setFb2(e.target.value)}
                className="rounded-lg border border-slate-200 dark:border-slate-700 px-2.5 py-1.5 text-sm"
              >
                <option value="">二级回退：无</option>
                {rows
                  .filter((r) => r.name.trim() && r.name.trim() !== def && r.name.trim() !== fb1)
                  .map((r) => (
                    <option key={r.name} value={r.name.trim()}>
                      {r.name.trim()}
                    </option>
                  ))}
              </select>
            </div>
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
      <div className="overflow-hidden rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800">
        <table className="w-full text-left text-xs">
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
            {filtered.map((a, i) => (
              <tr key={i} className="border-b border-slate-50 last:border-0">
                <td className="whitespace-nowrap px-4 py-2 font-mono text-slate-500 dark:text-slate-400 dark:text-slate-500">
                  {String(a.ts).replace("T", " ").slice(0, 19)}
                </td>
                <td className="px-4 py-2">{a.actor}</td>
                <td className="px-4 py-2">
                  <code className="rounded bg-slate-100 dark:bg-slate-700/50 px-1.5 py-0.5">{a.action}</code>
                </td>
                <td className="max-w-40 truncate px-4 py-2 font-mono text-slate-500 dark:text-slate-400 dark:text-slate-500">{a.target}</td>
                <td className="max-w-56 truncate px-4 py-2 text-slate-400 dark:text-slate-500" title={a.detail_json || ""}>
                  {a.detail_json}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="mt-2 text-[11px] text-slate-400 dark:text-slate-500">
        审计由后端在角色切换、插件启停、会话创建、数据修正/删除时写入（US-3）；本页只读。
      </p>
    </div>
  );
}
