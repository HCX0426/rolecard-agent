import { useEffect, useState } from "react";
import { api, type DomainInfo, type ToolCatalog } from "../api";
import { Card, PageHeader } from "../components/ui";
import { ExtensionPanel } from "../components/ExtensionPanel";

/**
 * 工具页（2026-10-24 IA 重构：原「插件」页改造）。
 *
 * 两块内容，回答"这个助手能用哪些能力、从哪来"：
 *   ① MCP 服务器管理 —— 外接能力（自设置页的扩展面板整体搬入，CRUD + 连通性测试）；
 *   ② 内置工具目录 —— 按域分组的全部工具（角色卡白名单的"选项清单"）。
 *
 * 权限模型（2026-10-24 收口）：**域常开，白名单即唯一边界** —— 本页不再有域开关，
 * 模型能不能用某个工具只看角色卡勾没勾。MCP 的服务器级启停保留（管连接，不管权限）。
 * 安全红线不变：MCP 接入是操作员动作，绝不暴露成 LLM 自助安装。
 */
export default function ToolsPage() {
  const [catalog, setCatalog] = useState<ToolCatalog | null>(null);
  const [domains, setDomains] = useState<DomainInfo[]>([]);

  useEffect(() => {
    api.get<ToolCatalog>("/api/tools/catalog").then(setCatalog).catch(() => undefined);
    api.get<DomainInfo[]>("/api/domains").then(setDomains).catch(() => undefined);
  }, []);

  const domainName = (id: string) =>
    domains.find((d) => d.id === id)?.display_name ?? id;

  return (
    <div className="h-full overflow-y-auto p-6">
      <div className="mx-auto max-w-4xl">
        <PageHeader
          title="工具"
          subtitle={
            <>
              MCP 服务器与内置工具的<b>管理页</b>。模型能不能用某个工具，
              由<b>角色卡的工具白名单</b>决定（域常开，无需先开开关）。
            </>
          }
        />

        {/* ① MCP 服务器管理（自设置 → 扩展 整体搬入） */}
        <h2 className="mt-6 mb-3 text-sm font-semibold text-slate-700 dark:text-slate-200">
          MCP 服务器
        </h2>
        <ExtensionPanel />

        {/* ② 内置工具目录（角色卡白名单的选项清单） */}
        <h2 className="mt-8 mb-3 text-sm font-semibold text-slate-700 dark:text-slate-200">
          内置工具目录
        </h2>
        {catalog && (
          <div className="space-y-4">
            <Card className="p-4">
              <p className="text-xs font-medium text-slate-500 dark:text-slate-400">内核工具</p>
              {catalog.kernel.map((t) => (
                <div key={t.name} className="mt-1 text-xs">
                  <code className="text-slate-700 dark:text-slate-200">{t.name}</code>
                  <span className="ml-2 text-slate-400 dark:text-slate-500">{t.description}</span>
                </div>
              ))}
            </Card>
            {Object.entries(catalog.domains).map(([id, tools]) => (
              <Card key={id} className="p-4">
                <p className="text-xs font-medium text-slate-500 dark:text-slate-400">
                  {domainName(id)}{" "}
                  <code className="rounded bg-slate-100 dark:bg-slate-700/50 px-1 py-0.5 text-11 text-slate-400">
                    {id}
                  </code>
                </p>
                {tools.map((t) => (
                  <div key={t.name} className="mt-1 text-xs">
                    <code className="text-slate-700 dark:text-slate-200">{t.name}</code>
                    <span className="ml-2 text-slate-400 dark:text-slate-500">{t.description}</span>
                  </div>
                ))}
              </Card>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
