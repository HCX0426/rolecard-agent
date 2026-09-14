import { useCallback, useEffect, useState } from "react";
import {
  api,
  type ModelSettings,
  type PluginRow,
  type RoleCard,
  type SessionRow,
} from "../api";

// 设置页子页签：通用（系统信息）/ 模型（后端 CRUD + 热切换）。
// 与插件页同一套子页签挂载模式 —— 新设置分区加一项即可。
const SETTINGS_TABS = [
  { key: "general", label: "通用" },
  { key: "models", label: "模型" },
] as const;

type SettingsTab = (typeof SETTINGS_TABS)[number]["key"];

export default function SettingsPage({ onOpenChat }: { onOpenChat?: () => void }) {
  const [tab, setTab] = useState<SettingsTab>("models");

  return (
    <div className="h-full overflow-y-auto p-6">
      <div className="mx-auto max-w-3xl">
        <h2 className="text-base font-semibold text-slate-900">设置</h2>
        <div className="mt-4 flex gap-1 border-b border-slate-200">
          {SETTINGS_TABS.map((t) => (
            <button
              key={t.key}
              onClick={() => setTab(t.key)}
              className={`rounded-t-lg px-4 py-2 text-sm ${
                tab === t.key
                  ? "border-b-2 border-blue-600 font-medium text-blue-700"
                  : "text-slate-500 hover:text-slate-700"
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>
        {tab === "general" && <GeneralPanel onOpenChat={onOpenChat} />}
        {tab === "models" && <ModelsPanel />}
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
      <div className="rounded-xl border border-slate-200 bg-white p-5">
        <h3 className="text-sm font-medium text-slate-900">关于</h3>
        <p className="mt-1.5 text-xs leading-relaxed text-slate-500">
          rolecard-agent 控制台 · v1（M1 内核 / M2 角色与插件 / M3 领域工具 / M4 接入层 /
          M5 前端工程化）。多角色对话 Agent 内核：角色卡控制人设与工具权限，插件以数据驱动启停。
        </p>
        <p className="mt-1 text-xs text-slate-400">语言：简体中文（内置）</p>
      </div>

      <div className="rounded-xl border border-slate-200 bg-white p-5">
        <h3 className="text-sm font-medium text-slate-900">系统状态</h3>
        {loadError && <p className="mt-1 text-xs text-red-600">{loadError}</p>}
        <dl className="mt-2 grid grid-cols-2 gap-x-6 gap-y-2 text-xs">
          <div className="flex justify-between border-b border-slate-50 py-1">
            <dt className="text-slate-400">默认模型后端</dt>
            <dd className="font-mono">{info.defaultBackend}</dd>
          </div>
          <div className="flex justify-between border-b border-slate-50 py-1">
            <dt className="text-slate-400">领域插件（启用/注册）</dt>
            <dd className="font-mono">{info.plugins}</dd>
          </div>
          <div className="flex justify-between border-b border-slate-50 py-1">
            <dt className="text-slate-400">角色卡数量</dt>
            <dd className="font-mono">{info.roles}</dd>
          </div>
          <div className="flex justify-between border-b border-slate-50 py-1">
            <dt className="text-slate-400">会话数量</dt>
            <dd className="font-mono">{info.sessions}</dd>
          </div>
        </dl>
        <p className="mt-2 text-[11px] text-slate-400">
          修改默认后端请前往「模型」页签；插件启停在「插件」页签（停用立即生效）。
        </p>
      </div>

      <div className="rounded-xl border border-slate-200 bg-white p-5">
        <h3 className="text-sm font-medium text-slate-900">演示数据</h3>
        <p className="mt-1.5 text-xs text-slate-500">
          评测 / 演示用的虚构档案可由脚本重建；会话与数据管理的日常操作在对话页与插件页。
        </p>
        <div className="mt-2 flex gap-2">
          <button
            onClick={() => onOpenChat?.()}
            className="rounded-lg border border-slate-200 px-3 py-1.5 text-xs text-slate-600 hover:border-blue-300"
          >
            前往对话
          </button>
          <button
            onClick={async () => {
              await load();
              setRefreshed(true);
              setTimeout(() => setRefreshed(false), 2000);
            }}
            className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs text-slate-600 hover:border-blue-300"
          >
            {refreshed ? "已刷新 ✓" : "刷新状态"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- 模型（原设置页主体）

function ModelsPanel() {
  const [def, setDef] = useState<string>("");
  const [rows, setRows] = useState<EditableBackend[]>([]);
  const [status, setStatus] = useState<{ ok: boolean; msg: string } | null>(null);
  const [loaded, setLoaded] = useState(false);

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
      })),
    );
    setLoaded(true);
  }, []);

  useEffect(() => {
    load().catch((e) => setStatus({ ok: false, msg: `加载失败：${e.message}` }));
  }, [load]);

  function update(i: number, patch: Partial<EditableBackend>) {
    setRows((rs) => rs.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  }

  function addRow() {
    setRows((rs) => [
      ...rs,
      { name: "", provider: "openai", base_url: "", model: "", api_key: "", has_key: false },
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
          // 空串会被后端理解为"清除"；这里区分"没碰过"（保持 None=保留）与"清空"
          api_key: r.api_key === "" && r.has_key ? null : r.api_key,
        })),
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
        })),
      );
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
            status.ok ? "bg-green-50 text-green-700" : "bg-red-50 text-red-600"
          }`}
        >
          {status.msg}
        </p>
      )}

      {loaded && (
        <>
          <div className="space-y-3">
            {rows.map((r, i) => (
              <div key={i} className="rounded-xl border border-slate-200 bg-white p-4">
                <div className="flex items-center gap-3">
                  <label className="flex items-center gap-1.5 text-xs text-slate-500">
                    <input
                      type="radio"
                      name="default-backend"
                      checked={def === r.name}
                      onChange={() => setDef(r.name)}
                    />
                    默认
                  </label>
                  <input
                    value={r.name}
                    onChange={(e) => update(i, { name: e.target.value })}
                    placeholder="后端名（如 siliconflow）"
                    className="w-40 rounded-lg border border-slate-200 px-2.5 py-1.5 font-mono text-sm"
                  />
                  <select
                    value={r.provider}
                    onChange={(e) => update(i, { provider: e.target.value })}
                    className="rounded-lg border border-slate-200 px-2 py-1.5 text-sm"
                  >
                    {PROVIDERS.map((p) => (
                      <option key={p} value={p}>
                        {p}
                      </option>
                    ))}
                  </select>
                  <button
                    onClick={() => removeRow(i)}
                    className="ml-auto rounded px-2 py-1 text-xs text-red-400 hover:bg-red-50 hover:text-red-600"
                  >
                    移除
                  </button>
                </div>
                <div className="mt-2.5 grid grid-cols-2 gap-3">
                  <input
                    value={r.model}
                    onChange={(e) => update(i, { model: e.target.value })}
                    placeholder="模型名（如 deepseek-ai/DeepSeek-V4-Flash）"
                    className="w-full rounded-lg border border-slate-200 px-2.5 py-1.5 text-sm"
                  />
                  <input
                    value={r.base_url}
                    onChange={(e) => update(i, { base_url: e.target.value })}
                    placeholder="base_url（Ollama 可留空，如 https://api.siliconflow.cn/v1）"
                    className="w-full rounded-lg border border-slate-200 px-2.5 py-1.5 text-sm"
                  />
                  <input
                    type="password"
                    value={r.api_key}
                    onChange={(e) => update(i, { api_key: e.target.value })}
                    placeholder={
                      r.has_key ? "已保存密钥（留空 = 保持不变）" : "api_key（可选）"
                    }
                    className="w-full rounded-lg border border-slate-200 px-2.5 py-1.5 text-sm"
                  />
                  {r.has_key && (
                    <label className="flex items-center gap-1.5 text-xs text-slate-400">
                      <input
                        type="checkbox"
                        checked={r.api_key.trim() === "" && r.api_key.length > 0}
                        onChange={(e) => update(i, { api_key: e.target.checked ? " " : "" })}
                      />
                      清除已存密钥
                    </label>
                  )}
                </div>
              </div>
            ))}
          </div>

          <div className="mt-4 flex gap-2">
            <button
              onClick={addRow}
              className="rounded-lg border border-slate-200 bg-white px-4 py-2 text-sm text-slate-600 hover:bg-slate-50"
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

          <p className="mt-4 rounded-lg bg-slate-50 px-3 py-2.5 text-xs leading-relaxed text-slate-400">
            说明：首次启动会把 env 里的后端迁移到这里；<b>此后模型配置以本页为准</b>（env 不再参与，
            在页面里删除的后端重启后也不会回来）。删除所有后端会保存失败 —— 至少保留一个。
            角色可在「角色卡」页经由后端下拉做角色级路由。
          </p>
        </>
      )}
    </div>
  );
}

const PROVIDERS = ["openai", "ollama"];

interface EditableBackend {
  name: string;
  provider: string;
  base_url: string;
  model: string;
  api_key: string;
  has_key: boolean;
}
