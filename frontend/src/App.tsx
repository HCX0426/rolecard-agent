import { Suspense, lazy, useState } from "react";
import { useTheme } from "./components/useTheme";

// 路由级代码分割：六个页面各自成 chunk，首屏只加载当前页。
const ChatPage = lazy(() => import("./pages/ChatPage"));
const DataPage = lazy(() => import("./pages/DataPage"));
const KnowledgePage = lazy(() => import("./pages/KnowledgePage"));
const PluginsPage = lazy(() => import("./pages/PluginsPage"));
const RolesPage = lazy(() => import("./pages/RolesPage"));
const SettingsPage = lazy(() => import("./pages/SettingsPage"));

// 页签注册表：以后新增模块在这里加一行即可，不改布局代码。
//
// 6 个顶层页签刻意把三件最容易混淆的事分开（见 docs/UI设计与信息架构（修订）.md）：
//   数据   = 领域数据（随域归属）
//   知识库 = RAG 检索（内核能力，不是插件附属）
//   插件   = 能力开关（只做启停）
// 导航图标用内联 SVG 而非 emoji：emoji 依赖系统彩色字体，Linux/无头浏览器里常渲染成方框
// （截图直接暴露），SVG 在任何环境都一致。
const ICON = {
  width: 16,
  height: 16,
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.8,
  strokeLinecap: "round",
  strokeLinejoin: "round",
} as const;

const TABS = [
  {
    key: "chat",
    label: "对话",
    icon: (
      <svg {...ICON}>
        <path d="M4 5h16v11H8l-4 3z" />
      </svg>
    ),
  },
  {
    key: "data",
    label: "数据",
    icon: (
      <svg {...ICON}>
        <path d="M4 20h16M8 20v-6M13 20V7M18 20v-9" />
      </svg>
    ),
  },
  {
    key: "knowledge",
    label: "知识库",
    icon: (
      <svg {...ICON}>
        <path d="M5 4h13v16H5zM8 4v16M11 9h4M11 13h4" />
      </svg>
    ),
  },
  {
    key: "roles",
    label: "角色卡",
    icon: (
      <svg {...ICON}>
        <path d="M3 6h18v12H3zM7 10h5M7 14h3M16 13.5a2 2 0 1 0 0-4 2 2 0 0 0 0 4z" />
      </svg>
    ),
  },
  {
    key: "plugins",
    label: "插件",
    icon: (
      <svg {...ICON}>
        <path d="M9 3v5M15 3v5M6 8h12v5a6 6 0 0 1-12 0V8ZM12 19v2" />
      </svg>
    ),
  },
  {
    key: "settings",
    label: "设置",
    icon: (
      <svg {...ICON}>
        <path d="M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z" />
        <path d="M4 12h2M18 12h2M12 4v2M12 18v2M6.5 6.5l1.4 1.4M16.1 16.1l1.4 1.4M17.5 6.5l-1.4 1.4M7.9 16.1l-1.4 1.4" />
      </svg>
    ),
  },
] as const;

type TabKey = (typeof TABS)[number]["key"];

export default function App() {
  const [tab, setTab] = useState<TabKey>("chat");
  const [navOpen, setNavOpen] = useState(false); // 移动端导航抽屉
  const { theme, toggle } = useTheme();

  function selectTab(key: TabKey) {
    setTab(key);
    setNavOpen(false); // 移动端选中后收起抽屉
  }

  return (
    <div className="relative flex h-full">
      {/* 移动端：导航抽屉的遮罩 */}
      {navOpen && (
        <button
          aria-label="关闭菜单"
          onClick={() => setNavOpen(false)}
          className="absolute inset-0 z-30 bg-slate-900/40 md:hidden"
        />
      )}

      {/* 移动端顶栏：菜单按钮 + 标题 */}
      <div className="absolute inset-x-0 top-0 z-20 flex items-center gap-2 border-b border-slate-200 bg-white px-3 py-2 dark:border-slate-700 dark:bg-slate-800 md:hidden">
        <button
          aria-label="打开菜单"
          onClick={() => setNavOpen(true)}
          className="rounded-lg p-1.5 text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-700"
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
            <path d="M4 6h16M4 12h16M4 18h16" />
          </svg>
        </button>
        <span className="text-sm font-semibold text-slate-900 dark:text-slate-100">rolecard-agent</span>
      </div>

      {/* 左侧导航：桌面常驻，移动端 off-canvas 抽屉（顶栏给移动端菜单让位） */}
      <nav
        className={`flex w-52 shrink-0 flex-col border-r border-slate-200 bg-white transition-transform dark:border-slate-700 dark:bg-slate-800 max-md:fixed max-md:inset-y-0 max-md:left-0 max-md:z-40 max-md:pt-[41px] ${
          navOpen ? "max-md:translate-x-0" : "max-md:-translate-x-full"
        }`}
      >
        <div className="px-4 py-4">
          <h1 className="text-base font-semibold text-slate-900 dark:text-slate-100">rolecard-agent</h1>
          <p className="mt-0.5 text-xs text-slate-400 dark:text-slate-500">多角色对话 Agent 内核</p>
        </div>
        <div className="flex flex-col gap-1 px-2">
          {TABS.map((t) => (
            <button
              key={t.key}
              onClick={() => selectTab(t.key)}
              className={`flex items-center gap-2.5 rounded-lg px-3 py-2 text-left text-sm transition-colors ${
                tab === t.key
                  ? "bg-blue-50 font-medium text-blue-700 dark:bg-blue-900/30 dark:text-blue-300"
                  : "text-slate-600 hover:bg-slate-50 dark:text-slate-300 dark:hover:bg-slate-700/60"
              }`}
            >
              <span className="shrink-0 opacity-80">{t.icon}</span>
              {t.label}
            </button>
          ))}
        </div>
        <div className="mt-auto px-4 py-3 text-[11px] leading-relaxed text-slate-400 dark:text-slate-500">
          v1 · M1~M5 已落地
          <br />
          v2.1 / v2.2 检索 · 文档摄取
          <button
            onClick={toggle}
            title={`当前${theme === "dark" ? "深色" : "浅色"}，点击切换`}
            className="mt-2 flex w-full items-center justify-center gap-1.5 rounded-lg border border-slate-200 px-2 py-1 text-xs text-slate-600 hover:border-blue-300 dark:border-slate-700 dark:text-slate-300 dark:hover:border-blue-700"
          >
            切换{theme === "dark" ? "浅色" : "深色"}模式
          </button>
        </div>
      </nav>

      {/* 主内容区（移动端给顶栏让位） */}
      <main className="min-h-0 min-w-0 flex-1 pt-[41px] md:pt-0">
        <Suspense fallback={<div className="p-6 text-sm text-slate-400 dark:text-slate-500">加载中…</div>}>
          {tab === "chat" && <ChatPage onOpenSettings={() => selectTab("settings")} />}
          {tab === "data" && <DataPage />}
          {tab === "knowledge" && <KnowledgePage onOpenChat={() => selectTab("chat")} />}
          {tab === "roles" && <RolesPage />}
          {tab === "plugins" && <PluginsPage />}
          {tab === "settings" && <SettingsPage onOpenChat={() => selectTab("chat")} />}
        </Suspense>
      </main>
    </div>
  );
}
