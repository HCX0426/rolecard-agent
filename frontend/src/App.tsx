import { Component, Suspense, lazy, useEffect, useState, type ReactNode } from "react";
import ReachoutPanel from "./components/ReachoutPanel";
import { api } from "./api";
import { useTheme } from "./components/useTheme";

// ---- 路由级代码分割：六页各自成 chunk，hover 导航时预加载 -------------------------------
const ChatPage = lazy(() => import("./pages/ChatPage"));
const DataPage = lazy(() => import("./pages/DataPage"));
const KnowledgePage = lazy(() => import("./pages/KnowledgePage"));
const PluginsPage = lazy(() => import("./pages/PluginsPage"));
const RolesPage = lazy(() => import("./pages/RolesPage"));
const SettingsPage = lazy(() => import("./pages/SettingsPage"));

// hover 预加载：用户在导航栏悬停时提前下载目标页 chunk，消除切换延迟
const PRELOAD: Partial<Record<string, () => Promise<unknown>>> = {
  data: () => import("./pages/DataPage"),
  knowledge: () => import("./pages/KnowledgePage"),
  roles: () => import("./pages/RolesPage"),
  plugins: () => import("./pages/PluginsPage"),
  settings: () => import("./pages/SettingsPage"),
};

// ---- 页签注册表 -----------------------------------------------------------------------
// 6 个页签刻意把三件最易混的事分开（docs/UI设计与信息架构（修订）.md）：
//   数据 = 领域数据 · 知识库 = RAG 检索 · 插件 = 能力开关
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

// ---- Hash 路由：URL 即状态，支持深链 + 浏览器前进/后退 ---------------------------------
function tabFromHash(): TabKey {
  const h = window.location.hash.replace(/^#\/?/, "");
  return TABS.some((t) => t.key === h) ? (h as TabKey) : "chat";
}

// ---- 错误边界：单页崩溃不影响其他页签 ---------------------------------------------------
class PageBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };
  static getDerivedStateFromError(error: Error) {
    return { error };
  }
  render() {
    if (this.state.error) {
      return (
        <div className="flex h-full items-center justify-center p-8">
          <div className="max-w-md rounded-xl border border-red-200 bg-red-50 p-6 text-center dark:border-red-800 dark:bg-red-900/30">
            <h2 className="text-sm font-medium text-red-800 dark:text-red-300">页面出了问题</h2>
            <p className="mt-2 text-xs text-red-600 dark:text-red-400">{this.state.error.message}</p>
            <button
              onClick={() => this.setState({ error: null })}
              className="mt-4 rounded-lg bg-red-500 px-4 py-2 text-xs text-white hover:bg-red-600"
            >
              重试
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

// ---- App ------------------------------------------------------------------------------
export default function App() {
  const [tab, setTabState] = useState<TabKey>(tabFromHash);
  const [navOpen, setNavOpen] = useState(false);
  const { theme, toggle } = useTheme();
  // 角色主动开口（架构计划 B）：铃铛红点 + 收件箱抽屉。静音轮询（10s）：
  // 页面开着时新开口 10 秒内上角标；页面关着 = 回来看到堆积（web 无常驻推送，如实降级）。
  const [reachoutOpen, setReachoutOpen] = useState(false);
  const [reachoutUnread, setReachoutUnread] = useState(0);

  useEffect(() => {
    if (reachoutOpen) return; // 抽屉开着时不打扰轮询，抽屉自己会刷新
    let cancelled = false;
    const poll = async () => {
      try {
        const p = await api.getReachouts();
        if (!cancelled) setReachoutUnread(p.unread);
      } catch {
        /* 静默：后端没起/网络抖动不值得打扰用户 */
      }
    };
    poll();
    const timer = setInterval(poll, 10_000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [reachoutOpen]);

  // tab → URL hash（支持深链 /#/data 等）
  useEffect(() => {
    window.location.hash = `/${tab}`;
  }, [tab]);

  // 浏览器前进/后退 → tab 状态同步
  useEffect(() => {
    const handler = () => setTabState(tabFromHash());
    window.addEventListener("hashchange", handler);
    return () => window.removeEventListener("hashchange", handler);
  }, []);

  function selectTab(key: TabKey) {
    setTabState(key);
    setNavOpen(false);
  }

  return (
    <div className="relative flex h-full">
      {navOpen && (
        <button
          aria-label="关闭菜单"
          onClick={() => setNavOpen(false)}
          className="absolute inset-0 z-30 bg-slate-900/40 md:hidden"
        />
      )}

      {/* 移动端顶栏 */}
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

      {/* 左侧导航 */}
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
              onMouseEnter={() => PRELOAD[t.key]?.()}
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
        <div className="mt-auto border-t border-slate-100 px-2 py-2 dark:border-slate-700">
          <button
            onClick={() => setReachoutOpen(true)}
            className="relative flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-left text-sm text-slate-600 hover:bg-slate-50 dark:text-slate-300 dark:hover:bg-slate-700/60"
            title="角色主动找你（收件箱）"
          >
            <span className="shrink-0 opacity-80">🔔</span>
            主动消息
            {reachoutUnread > 0 && (
              <span className="ml-auto flex h-5 min-w-[20px] items-center justify-center rounded-full bg-red-500 px-1.5 text-[10px] font-medium text-white">
                {reachoutUnread}
              </span>
            )}
          </button>
        </div>
      </nav>

      {/* 主内容区 */}
      <main className="min-h-0 min-w-0 flex-1 pt-[41px] md:pt-0">
        {/* 对话页**常驻挂载**（用 hidden 隐藏，而不是条件渲染）。
            它有几样只存在于组件内的状态：正在生成的回答、输入草稿、当前对话与勾选 ——
            条件渲染会在切页时把整页卸载，这些全丢，而服务端那条流还在跑（审查报告 P1-11）。
            也正因为常驻，它必须放在 keyed 的 PageBoundary **外面**：那个 key={tab}
            会在每次切页时把子树整个重挂，放进去等于没改。 */}
        <div className={tab === "chat" ? "h-full" : "hidden"}>
          <Suspense
            fallback={<div className="p-6 text-sm text-slate-400 dark:text-slate-500">加载中…</div>}
          >
            <ChatPage />
          </Suspense>
        </div>
        {/* 设置页同样**常驻挂载**（hidden 隐藏）：其内部 5 个子页签都有各自的加载
            状态与表单，条件渲染会在切走顶层页签时整棵卸载 —— 切回设置→服务又从头
            重新请求（与 ChatPage 同源问题，服务页"每次点开都加载中"的完整根因）。 */}
        <div className={tab === "settings" ? "h-full" : "hidden"}>
          <Suspense
            fallback={<div className="p-6 text-sm text-slate-400 dark:text-slate-500">加载中…</div>}
          >
            <SettingsPage
              onOpenChat={() => selectTab("chat")}
              theme={theme}
              onToggleTheme={toggle}
            />
          </Suspense>
        </div>
        {/* 数据页 / 角色页同样**常驻挂载**（hidden 隐藏）：两者都有未提交的表单与分页
            位置 —— 数据页的追加报告多行指标草稿、指标修正 draft、分页 page；角色页的
            编辑表单 form。条件渲染会在切走时整棵卸载丢光（2026-09-18 与服务页同源）。
            注意常驻 = 组件**无条件渲染**（`tab === x && <Page/>` 会在切走时卸载，
            与条件渲染等效）。知识库 / 插件页无此类草稿，保留 PageBoundary 按需挂载。 */}
        <div className={tab === "data" ? "h-full" : "hidden"}>
          <Suspense
            fallback={<div className="p-6 text-sm text-slate-400 dark:text-slate-500">加载中…</div>}
          >
            <DataPage />
          </Suspense>
        </div>
        <div className={tab === "roles" ? "h-full" : "hidden"}>
          <Suspense
            fallback={<div className="p-6 text-sm text-slate-400 dark:text-slate-500">加载中…</div>}
          >
            <RolesPage />
          </Suspense>
        </div>
        {tab !== "chat" && tab !== "settings" && tab !== "data" && tab !== "roles" && (
          <PageBoundary key={tab}>
            <Suspense
              fallback={
                <div className="p-6 text-sm text-slate-400 dark:text-slate-500">加载中…</div>
              }
            >
              {tab === "knowledge" && <KnowledgePage onOpenChat={() => selectTab("chat")} />}
              {tab === "plugins" && <PluginsPage />}
            </Suspense>
          </PageBoundary>
        )}
      </main>

      {/* 角色主动开口收件箱抽屉（全局，所有页签可见） */}
      <ReachoutPanel
        open={reachoutOpen}
        onClose={() => setReachoutOpen(false)}
        onUnreadChange={setReachoutUnread}
      />
    </div>
  );
}
