import { useEffect, useState } from "react";
import { api, type PluginRow } from "../api";

// 页签式模块容器：现在只有「领域插件」一个子页签；以后的子模块（评测、审计日志、
// 数据管理……）往 MODULES 数组加一项即可，布局与状态管理复用 —— 这就是 US-9 的可扩展性要求。
const MODULES = [
  { key: "domains", label: "领域插件" },
  { key: "future", label: "更多模块（规划中）" },
] as const;

type ModuleKey = (typeof MODULES)[number]["key"];

export default function PluginsPage() {
  const [module, setModule] = useState<ModuleKey>("domains");

  return (
    <div className="h-full overflow-y-auto p-6">
      <div className="mx-auto max-w-4xl">
        <h2 className="text-base font-semibold text-slate-900">插件</h2>
        <p className="mt-0.5 text-xs text-slate-400">
          领域插件启停立即生效（无需重启），操作写入审计并递增全局 tool_epoch
        </p>

        {/* 子页签：为未来模块预留的挂载点 */}
        <div className="mt-4 flex gap-1 border-b border-slate-200">
          {MODULES.map((m) => (
            <button
              key={m.key}
              onClick={() => setModule(m.key)}
              className={`rounded-t-lg px-4 py-2 text-sm ${
                module === m.key
                  ? "border-b-2 border-blue-600 font-medium text-blue-700"
                  : "text-slate-500 hover:text-slate-700"
              }`}
            >
              {m.label}
            </button>
          ))}
        </div>

        {module === "domains" && <DomainPlugins />}
        {module === "future" && (
          <div className="mt-6 rounded-xl border border-dashed border-slate-200 bg-white p-8 text-center text-sm text-slate-400">
            预留挂载点：评测、审计日志、文档摄取（v2.2）等模块将作为新的子页签出现在这里
          </div>
        )}
      </div>
    </div>
  );
}

function DomainPlugins() {
  const [plugins, setPlugins] = useState<PluginRow[]>([]);
  const [epoch, setEpoch] = useState<number | null>(null);
  const [status, setStatus] = useState("");

  async function load() {
    setPlugins(await api.get<PluginRow[]>("/api/plugins"));
  }
  useEffect(() => {
    load().catch((e) => setStatus(`加载失败：${e.message}`));
  }, []);

  async function toggle(pluginId: string, enabled: boolean) {
    try {
      const r = await api.post<{ tool_epoch: number }>(
        `/api/plugins/${pluginId}/toggle`,
        { enabled },
      );
      setEpoch(r.tool_epoch);
      setStatus(`${pluginId} → ${enabled ? "已启用" : "已停用"}（tool_epoch=${r.tool_epoch}）`);
      await load();
    } catch (e) {
      setStatus(`切换失败：${(e as Error).message}`);
      await load();
    }
  }

  return (
    <div className="mt-6 grid gap-3">
      {plugins.map((p) => (
        <div
          key={p.plugin_id}
          className="flex items-center justify-between rounded-xl border border-slate-200 bg-white px-5 py-4"
        >
          <div>
            <div className="flex items-center gap-2">
              <span className="font-medium text-slate-900">{p.display_name}</span>
              <code className="rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-500">
                {p.plugin_id}
              </code>
            </div>
            <p className="mt-1 text-xs text-slate-400">
              {p.enabled ? "已启用：工具对模型可见" : "已停用：工具对模型立即不可见（数据保留）"}
            </p>
          </div>
          <button
            onClick={() => toggle(p.plugin_id, !p.enabled)}
            className={`relative h-6 w-11 rounded-full transition-colors ${
              p.enabled ? "bg-green-500" : "bg-slate-300"
            }`}
            role="switch"
            aria-checked={p.enabled}
          >
            <span
              className="absolute top-0.5 left-0.5 h-5 w-5 rounded-full bg-white shadow transition-transform"
              style={{ transform: p.enabled ? "translateX(20px)" : "none" }}
            />
          </button>
        </div>
      ))}
      {epoch !== null && (
        <p className="rounded-lg bg-green-50 px-3 py-2 text-xs text-green-700">{status}</p>
      )}
      {status && epoch === null && (
        <p className="rounded-lg bg-red-50 px-3 py-2 text-xs text-red-600">{status}</p>
      )}
    </div>
  );
}
