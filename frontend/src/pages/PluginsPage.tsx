import { useCallback, useEffect, useState } from "react";
import {
  api,
  type PluginRow,
  type ToolCatalog,
} from "../api";

/**
 * 插件页 = 纯领域插件。
 *
 * 概念边界（读 UI 的人最容易混淆的三件事，这里一次说清）：
 *   1. 插件 = 领域插件（domains/<id>/ 代码包，显式注册进 DOMAINS）——不是 MCP，
 *      本项目设计上不做界面动态加载/市场安装（安全取舍，见 docs/实施计划.md §2.2）。
 *   2. RAG 检索 = 内核能力（search_knowledge），不属于任何插件；角色经
 *      knowledge_scopes 授权使用（设置页 → 知识库 可查看库内容）。
 *   3. 数据随域归属：health 域的档案数据在 health 插件的详情里管理。
 * 新增一个领域插件 = 写一个 domains/<id>/ 包（models/service/tools/schema）并注册
 * 进 DOMAINS —— 代码路径在 README「架构」有说明。
 */

export default function PluginsPage() {
  return (
    <div className="h-full overflow-y-auto p-6">
      <div className="mx-auto max-w-4xl">
        <h2 className="text-base font-semibold text-slate-900">领域插件</h2>
        <p className="mt-0.5 text-xs text-slate-400">
          启停立即生效（无需重启），操作写入审计并递增全局 tool_epoch。
          插件由代码显式注册（DOMAINS）——界面只做启停，不支持动态安装。
          <br />
          领域数据在「数据」页；检索（RAG）在「知识库」页 —— 这次刻意把三件事分开。
        </p>
        <DomainPlugins />

        <div className="mt-6 rounded-xl border border-dashed border-slate-300 bg-white p-5">
          <h3 className="text-sm font-medium text-slate-700">如何新增一个领域插件？</h3>
          <ol className="mt-2 list-decimal space-y-1 pl-5 text-xs leading-relaxed text-slate-500">
            <li>
              新建 <code>src/rolecard_agent/domains/&lt;id&gt;/</code> 包：models.py /
              service.py / tools.py / schema.sql
            </li>
            <li>
              在 <code>domains/registry.py</code> 的 <code>DOMAINS</code> 追加 id，并在
              build_registry 里接线它的工具工厂（漏接线启动即报错）
            </li>
            <li>重启服务：建表、插件行、工具注册自动完成；在角色卡里为角色勾选新工具</li>
          </ol>
          <p className="mt-2 text-[11px] leading-relaxed text-slate-400">
            为什么不做界面动态安装（MCP 市场）：让运行中的 Agent 自己扩权，等于把权限边界
            交给运行时注入——显式注册是本项目权限模型的前提。RAG 检索不受此限：
            它是内核能力，按角色作用域授权（设置页 → 知识库）。
          </p>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- 领域插件卡片

function DomainPlugins() {
  const [plugins, setPlugins] = useState<PluginRow[]>([]);
  const [catalog, setCatalog] = useState<ToolCatalog | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [status, setStatus] = useState("");

  const load = useCallback(async () => {
    setPlugins(await api.get<PluginRow[]>("/api/plugins"));
  }, []);
  useEffect(() => {
    load().catch((e) => setStatus(`加载失败：${e.message}`));
    api.get<ToolCatalog>("/api/tools/catalog").then(setCatalog).catch(() => {});
  }, [load]);

  async function toggle(pluginId: string, enabled: boolean) {
    try {
      const r = await api.post<{ tool_epoch: number }>(
        `/api/plugins/${pluginId}/toggle`,
        { enabled },
      );
      setStatus(`${pluginId} → ${enabled ? "已启用" : "已停用"}（tool_epoch=${r.tool_epoch}）`);
      await load();
    } catch (e) {
      setStatus(`切换失败：${(e as Error).message}`);
      await load();
    }
  }

  return (
    <div className="mt-6 grid gap-3">
      {status && (
        <p className="rounded-lg bg-green-50 px-3 py-2 text-xs text-green-700">{status}</p>
      )}
      {plugins.map((p) => {
        const tools = catalog?.domains[p.plugin_id] || [];
        const isOpen = expanded === p.plugin_id;
        return (
          <div
            key={p.plugin_id}
            className="rounded-xl border border-slate-200 bg-white px-5 py-4"
          >
            <div className="flex items-center justify-between">
              <div>
                <div className="flex items-center gap-2">
                  <span className="font-medium text-slate-900">{p.display_name}</span>
                  <code className="rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-500">
                    {p.plugin_id}
                  </code>
                </div>
                <p className="mt-1 text-xs text-slate-400">
                  {p.enabled
                    ? `已启用：${tools.length} 个工具对模型可见`
                    : `已停用：${tools.length} 个工具立即对模型不可见（数据保留）`}
                </p>
              </div>
              <div className="flex items-center gap-3">
                {tools.length > 0 && (
                  <button
                    onClick={() => setExpanded(isOpen ? null : p.plugin_id)}
                    className="rounded-lg border border-slate-200 px-2.5 py-1 text-xs text-slate-500 hover:border-blue-300 hover:text-blue-600"
                  >
                    {isOpen ? "收起详情 ▴" : "详情（工具与数据）▾"}
                  </button>
                )}
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
            </div>
            {isOpen && (
              <div className="mt-3 space-y-4 rounded-lg border border-slate-100 bg-slate-50 p-3">
                <div>
                  <p className="text-xs font-medium text-slate-500">本插件贡献的工具：</p>
                  {tools.map((t) => (
                    <div key={t.name} className="mt-1 text-xs">
                      <code className="text-slate-700">{t.name}</code>
                      <span className="ml-2 text-slate-400">{t.description}</span>
                    </div>
                  ))}
                </div>
                <p className="text-[11px] text-slate-400">
                  本域数据已移到「数据」页 —— 数据随领域归属，插件停用不影响数据保留。
                </p>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

