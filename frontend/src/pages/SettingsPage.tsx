import { useState } from "react";
import { Tabs } from "../components/ui";
import { AboutPanel } from "../components/AboutPanel";
import { RuntimePanel } from "../components/RuntimePanel";
import { AuditPanel } from "../components/AuditPanel";
import { ModelsPanel } from "../components/ModelsPanel";
import { ServicesPanel } from "../components/ServicesPanel";

// 设置页子页签：模型（凭据组 + 模型行，见 components/ModelsPanel）/ 服务（运行时状态与降级
// 策略）/ 关于 / 运行环境 / 审计。
// IA 迁出的账：2026-10-24「扩展」（MCP）升为顶层「工具」页、知识库并入「资料」页知识库
// 区块；2026-10-30 记忆三卡整族出设置 —— 跨会话记忆 + 主动开口升顶层「记忆」页签
// （pages/MemoryPage），任务目录更名「工作区」由角色卡页托管（components/WorkspacePanel）。
// 设置页从此只装环境与凭据：模型 / 服务 / 关于 / 运行环境 / 审计。
const SETTINGS_TABS = [
  { key: "models", label: "模型" },
  { key: "services", label: "服务" },
  { key: "about", label: "关于与系统状态" },
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
        {/* 子页签条：全站统一的 ui/Tabs（2026-10-30 从下划线式收编，三套语言归一）。 */}
        <Tabs
          className="mt-4"
          ariaLabel="设置页区块"
          items={SETTINGS_TABS.map(({ key, label }) => ({ key, label }))}
          active={tab}
          onChange={setTab}
        />
        {/* 子页签常驻挂载 + hidden 隐藏（P1-11 同款）：条件渲染会让每次切页签重新挂载
            面板并重发请求 —— 服务页的探活会因此"每次点开都加载中"（用户 2026-09-18）。 */}
        <div className={tab === "about" ? "" : "hidden"}>
          <AboutPanel onOpenChat={onOpenChat} theme={theme} onToggleTheme={onToggleTheme} />
        </div>
        <div className={tab === "models" ? "" : "hidden"}>
          <ModelsPanel onOpenServices={() => setTab("services")} />
        </div>
        <div className={tab === "services" ? "" : "hidden"}>
          <ServicesPanel />
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
