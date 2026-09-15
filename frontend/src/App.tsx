import { useState } from "react";
import ChatPage from "./pages/ChatPage";
import PluginsPage from "./pages/PluginsPage";
import RolesPage from "./pages/RolesPage";
import SettingsPage from "./pages/SettingsPage";

// 页签注册表：以后新增模块（插件子页、评测页……）在这里加一行即可，不改布局代码。
const TABS = [
  { key: "chat", label: "对话", icon: "💬" },
  { key: "roles", label: "角色卡", icon: "🗂️" },
  { key: "plugins", label: "插件", icon: "🔌" },
  { key: "settings", label: "设置", icon: "⚙️" },
] as const;

type TabKey = (typeof TABS)[number]["key"];

export default function App() {
  const [tab, setTab] = useState<TabKey>("chat");

  return (
    <div className="flex h-full">
      {/* 左侧导航栏 */}
      <nav className="flex w-52 shrink-0 flex-col border-r border-slate-200 bg-white">
        <div className="px-4 py-4">
          <h1 className="text-base font-semibold text-slate-900">rolecard-agent</h1>
          <p className="mt-0.5 text-xs text-slate-400">多角色对话 Agent 内核</p>
        </div>
        <div className="flex flex-col gap-1 px-2">
          {TABS.map((t) => (
            <button
              key={t.key}
              onClick={() => setTab(t.key)}
              className={`flex items-center gap-2.5 rounded-lg px-3 py-2 text-left text-sm transition-colors ${
                tab === t.key
                  ? "bg-blue-50 font-medium text-blue-700"
                  : "text-slate-600 hover:bg-slate-50"
              }`}
            >
              <span className="text-base leading-none">{t.icon}</span>
              {t.label}
            </button>
          ))}
        </div>
        <div className="mt-auto px-4 py-3 text-[11px] leading-relaxed text-slate-400">
          v1 · M1~M5 已落地
          <br />
          v2.1 / v2.2 检索 · 文档摄取
        </div>
      </nav>

      {/* 主内容区 */}
      <main className="min-w-0 flex-1">
        {tab === "chat" && <ChatPage onOpenSettings={() => setTab("settings")} />}
        {tab === "roles" && <RolesPage />}
        {tab === "plugins" && <PluginsPage />}
        {tab === "settings" && <SettingsPage />}
      </main>
    </div>
  );
}
