import { useCallback, useEffect, useState } from "react";
import {
  api,
  type AuditRow,
  type KnowledgeScope,
  type ModelSettings,
  type PluginRow,
  type RagMetrics,
  type RagStageMs,
  type RoleCard,
  type SessionRow,
} from "../api";

// 设置页子页签：通用（系统信息）/ 模型（后端 CRUD + 热切换）/ 知识库（RAG 库存）/
// 审计（操作留痕）。与插件页同一套子页签挂载模式 —— 新设置分区加一项即可。
const SETTINGS_TABS = [
  { key: "general", label: "通用" },
  { key: "models", label: "模型" },
  { key: "knowledge", label: "知识库" },
  { key: "audit", label: "审计" },
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
        {tab === "knowledge" && <KnowledgePanel />}
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

// ---------------------------------------------------------------- 模型（后端 CRUD + 回退）

const PROVIDERS = ["openai", "ollama"];

interface EditableBackend {
  name: string;
  provider: string;
  base_url: string;
  model: string;
  api_key: string;
  has_key: boolean;
}

function ModelsPanel() {
  const [def, setDef] = useState<string>("");
  const [rows, setRows] = useState<EditableBackend[]>([]);
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
      })),
    );
    setFb1(s.fallbacks?.[0] || "");
    setFb2(s.fallbacks?.[1] || "");
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

          <div className="mt-4 rounded-xl border border-slate-200 bg-white p-4">
            <h3 className="text-sm font-medium text-slate-900">失败自动回退</h3>
            <p className="mt-1 text-xs text-slate-400">
              默认后端请求失败（建流阶段）时按序尝试；最多两级，流开始后的失败不回退（前端重试兜底）。
            </p>
            <div className="mt-2 grid grid-cols-2 gap-3">
              <select
                value={fb1}
                onChange={(e) => setFb1(e.target.value)}
                className="rounded-lg border border-slate-200 px-2.5 py-1.5 text-sm"
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
                className="rounded-lg border border-slate-200 px-2.5 py-1.5 text-sm"
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

// ---------------------------------------------------------------- 知识库（v2.1 RAG）

// 检索延迟细分：阶段顺序 + 空值显示（无样本时后端返回 null，显示 "—" 而非 0）。
const LATENCY_STAGES: [keyof RagStageMs, string][] = [
  ["embed_ms", "嵌入"],
  ["vector_ms", "向量检索"],
  ["rerank_ms", "重排"],
  ["total_ms", "合计"],
];

function fmtMs(v: number | null | undefined): string {
  return v === null || v === undefined ? "—" : v.toFixed(2);
}

function KnowledgePanel() {
  const [scopes, setScopes] = useState<KnowledgeScope[]>([]);
  const [metrics, setMetrics] = useState<RagMetrics | null>(null);
  const [status, setStatus] = useState("");
  const [confirmReset, setConfirmReset] = useState<string | null>(null);

  const load = useCallback(async () => {
    setScopes(await api.get<KnowledgeScope[]>("/api/knowledge"));
    // 延迟指标是增强信息：失败静默（不打扰知识库主视图）。
    api
      .get<RagMetrics>("/api/rag/metrics")
      .then(setMetrics)
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    load().catch((e) => setStatus(`加载失败：${e.message}`));
  }, [load]);

  /** 清空一个作用域（破坏性）：换嵌入后端后维度不兼容时的重建入口，走二次确认 + 审计。 */
  async function resetScope(scope: string) {
    try {
      const r = await api.del<{ removed_chunks: number }>(
        `/api/knowledge/${encodeURIComponent(scope)}`,
      );
      setConfirmReset(null);
      setStatus(`已清空作用域 ${scope}（移除 ${r.removed_chunks} 段，写入审计）`);
      await load();
    } catch (e) {
      setStatus(`清空失败：${(e as Error).message}`);
    }
  }

  return (
    <div className="mt-6">
      {status && <p className="rounded-lg bg-red-50 px-3 py-2 text-xs text-red-600">{status}</p>}
      <p className="rounded-lg bg-slate-50 px-3 py-2.5 text-xs leading-relaxed text-slate-400">
        知识库是<b>内核能力</b>（search_knowledge），不属于任何插件：库归内核，角色经
        knowledge_scopes 声明可检索的作用域（角色卡页配置）。上传 .txt/.md/.pdf/.docx/.pptx/.xlsx
        或图片会自动入库到 health_reports 作用域；切换嵌入后端后删除 data/chroma 目录重启即重建。
      </p>

      {metrics && (
        <div className="mt-3 rounded-xl border border-slate-200 bg-white p-4">
          <div className="flex items-center justify-between">
            <span className="text-sm font-medium text-slate-700">检索延迟细分（ms）</span>
            <span className="text-xs text-slate-400">
              样本 {metrics.samples} · 嵌入 {metrics.embedder} · 重排
              {metrics.rerank_enabled ? "开" : "关"}
            </span>
          </div>
          {metrics.samples === 0 ? (
            <p className="mt-2 text-xs text-slate-400">
              尚无检索样本：在对话里提问一次（触发 search_knowledge）即可看到分位。
            </p>
          ) : (
            <table className="mt-2 w-full text-left text-xs">
              <thead className="text-slate-400">
                <tr>
                  <th className="py-1 font-normal">阶段</th>
                  <th className="py-1 font-normal">P50</th>
                  <th className="py-1 font-normal">P95</th>
                  <th className="py-1 font-normal">P99</th>
                </tr>
              </thead>
              <tbody className="text-slate-600">
                {LATENCY_STAGES.map(([key, label]) => (
                  <tr key={key} className="border-t border-slate-100">
                    <td className="py-1">{label}</td>
                    <td className="py-1">{fmtMs(metrics.p50[key])}</td>
                    <td className="py-1">{fmtMs(metrics.p95[key])}</td>
                    <td className="py-1">{fmtMs(metrics.p99[key])}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      <div className="mt-3 space-y-3">
        {scopes.length === 0 && (
          <div className="rounded-xl border border-dashed border-slate-200 bg-white p-8 text-center text-sm text-slate-400">
            知识库还是空的：在对话页上传 .txt/.md 文档，或运行 scripts/seed_demo_data.py
            注入演示知识。
          </div>
        )}
        {scopes.map((s) => (
          <div key={s.scope} className="rounded-xl border border-slate-200 bg-white p-4">
            <div className="flex items-center justify-between">
              <div>
                <code className="rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-700">
                  {s.scope}
                </code>
                <span className="ml-2 text-xs text-slate-500">{s.chunks} 段</span>
                <span className="ml-2 rounded-full bg-slate-100 px-2 py-0.5 text-[11px] text-slate-500">
                  嵌入：{s.embedder}
                </span>
              </div>
              {confirmReset === s.scope ? (
                <div className="flex items-center gap-2">
                  <span className="text-[11px] text-red-600">
                    清空 {s.chunks} 段且不可恢复？
                  </span>
                  <button
                    onClick={() => resetScope(s.scope)}
                    className="rounded bg-red-500 px-2 py-1 text-[11px] text-white hover:bg-red-600"
                  >
                    确认清空
                  </button>
                  <button
                    onClick={() => setConfirmReset(null)}
                    className="rounded px-2 py-1 text-[11px] text-slate-500 hover:bg-slate-100"
                  >
                    取消
                  </button>
                </div>
              ) : (
                <button
                  onClick={() => setConfirmReset(s.scope)}
                  title="删除该作用域的集合 —— 换嵌入后端后维度不兼容时用它重建（写审计）"
                  className="rounded-lg border border-slate-200 px-2.5 py-1 text-xs text-slate-500 hover:border-red-300 hover:text-red-600"
                >
                  重建（清空）
                </button>
              )}
            </div>
            <ul className="mt-2 space-y-0.5">
              {s.sources.map((src) => (
                <li key={src} className="text-xs text-slate-500">
                  · {src}
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- 审计（F3）

function AuditPanel() {
  const [rows, setRows] = useState<AuditRow[]>([]);
  const [status, setStatus] = useState("");

  useEffect(() => {
    api.get<AuditRow[]>("/api/audit?limit=200").then(setRows).catch((e) => setStatus(`加载失败：${e.message}`));
  }, []);

  return (
    <div className="mt-6">
      {status && <p className="rounded-lg bg-red-50 px-3 py-2 text-xs text-red-600">{status}</p>}
      <div className="overflow-hidden rounded-xl border border-slate-200 bg-white">
        <table className="w-full text-left text-xs">
          <thead>
            <tr className="border-b border-slate-100 text-slate-400">
              <th className="px-4 py-2 font-medium">时间</th>
              <th className="px-4 py-2 font-medium">操作者</th>
              <th className="px-4 py-2 font-medium">动作</th>
              <th className="px-4 py-2 font-medium">对象</th>
              <th className="px-4 py-2 font-medium">详情</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr>
                <td colSpan={5} className="px-4 py-6 text-center text-slate-400">
                  暂无审计记录
                </td>
              </tr>
            )}
            {rows.map((a, i) => (
              <tr key={i} className="border-b border-slate-50 last:border-0">
                <td className="whitespace-nowrap px-4 py-2 font-mono text-slate-500">
                  {String(a.ts).replace("T", " ").slice(0, 19)}
                </td>
                <td className="px-4 py-2">{a.actor}</td>
                <td className="px-4 py-2">
                  <code className="rounded bg-slate-100 px-1.5 py-0.5">{a.action}</code>
                </td>
                <td className="max-w-40 truncate px-4 py-2 font-mono text-slate-500">{a.target}</td>
                <td className="max-w-56 truncate px-4 py-2 text-slate-400" title={a.detail_json || ""}>
                  {a.detail_json}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="mt-2 text-[11px] text-slate-400">
        审计由后端在角色切换、插件启停、会话创建、数据修正/删除时写入（US-3）；本页只读。
      </p>
    </div>
  );
}
