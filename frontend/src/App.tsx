import { Component, Suspense, lazy, useEffect, useState, type ReactNode } from "react";
import ReachoutPanel from "./components/ReachoutPanel";
import DataSourceSwitch from "./components/DataSourceSwitch";
import ApprovalPanel from "./components/ApprovalPanel";
import { ToastProvider } from "./components/Toast";
import Button from "./components/ui/Button";
import { api, UNREAD_POLL_MS } from "./api";
import { shellBridge } from "./lib/shell";
import { usePoll } from "./hooks/usePoll";
import { useTheme } from "./components/useTheme";
import { PageSkeleton } from "./components/ui/Skeleton";
import {
  IconApproval,
  IconBell,
  IconChat,
  IconData,
  IconKnowledge,
  IconPlugins,
  IconRoles,
  IconSettings,
} from "./components/icons";

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
// 6 个页签刻意把三件最易混的事分开（docs/前端设计.md 的 IA 一节）：
//   数据 = 领域数据 · 知识库 = RAG 检索 · 插件 = 能力开关
// 六个页签的图标住在 `components/icons.tsx`（全项目图标的唯一出处，2026-10-24 UI
// 打磨合并：此前这里内联一套 16px stroke 参数、`chat/icons.tsx` 另有一套 13px ——
// 两份参数两处长法）。页签表只声明"哪个页签配哪个图标"。
const TABS = [
  { key: "chat", label: "对话", icon: <IconChat /> },
  { key: "data", label: "数据", icon: <IconData /> },
  { key: "knowledge", label: "知识库", icon: <IconKnowledge /> },
  { key: "roles", label: "角色卡", icon: <IconRoles /> },
  { key: "plugins", label: "插件", icon: <IconPlugins /> },
  { key: "settings", label: "设置", icon: <IconSettings /> },
] as const;

type TabKey = (typeof TABS)[number]["key"];

// ---- Hash 路由：URL 即状态，支持深链 + 浏览器前进/后退 ---------------------------------
// hash 形态：`#/<tab>`，或带一段查询的 `#/<tab>?thread=<id>`（跨页深链：收件箱点"打开对话"、
// 以后桌宠壳的系统通知点击也是同一个入口）。
function hashParts(): { path: string; query: URLSearchParams } {
  const h = window.location.hash.replace(/^#\/?/, "");
  const [path, qs = ""] = h.split("?");
  return { path, query: new URLSearchParams(qs) };
}

function tabFromHash(): TabKey {
  const { path } = hashParts();
  return TABS.some((t) => t.key === path) ? (path as TabKey) : "chat";
}

/** 深链指向的会话（没有则 null）。只当"一次性意图"用，消费后由 ChatPage 清掉。 */
function threadFromHash(): string | null {
  return hashParts().query.get("thread");
}

// ---- 错误边界：单页崩溃不影响其他页签 ---------------------------------------------------
/** 页面 chunk 加载失败的特征。升级会整包替换 dist，旧页签内存里那份 index.html 引用的
 *  hash 文件名在磁盘上已经不存在 → 动态 import 404。**这种错"重试"永远无效**（文件真没了），
 *  唯一有效的动作是强刷拿新 index.html —— 识别它，给用户"刷新"而不是永远失败的"重试"。
 *  覆盖两类形状：Vite 产出的 `error loading dynamically imported module`（Chrome/Edge，
 *  实测 0.4.2→0.4.3 升级后切页签就是它）与 webpack 式 `ChunkLoadError`（防御，换构建器不再漏）。
 *  判据是**子串匹配而非精确相等**：浏览器在 message 里附带的 URL/文件名随版本漂。 */
function isChunkLoadError(error: Error): boolean {
  const message = error.message.toLowerCase();
  return (
    message.includes("dynamically imported module") ||
    message.includes("error loading dynamically imported") ||
    message.includes("chunkloaderror") ||
    message.includes("loading chunk")
  );
}

class PageBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };
  static getDerivedStateFromError(error: Error) {
    return { error };
  }
  render() {
    if (this.state.error) {
      if (isChunkLoadError(this.state.error)) {
        // 升级后的旧页签：给"刷新"这一条真正有效的出路，不给注定失败的"重试"。
        return (
          <div className="flex h-full items-center justify-center p-8">
            <div className="max-w-md rounded-xl border border-amber-200 bg-amber-50 p-6 text-center dark:border-amber-800 dark:bg-amber-900/30">
              <h2 className="text-sm font-medium text-amber-800 dark:text-amber-300">
                应用已更新，这个页面需要刷新
              </h2>
              <p className="mt-2 text-xs leading-relaxed text-amber-700 dark:text-amber-400">
                你打开的这个页面还停在升级前的版本，它要的旧文件已经不在了。刷新会拿到新版界面；
                你的数据（对话、知识库、设置）都在后台，不会因为刷新丢任何东西。
              </p>
              <Button
                onClick={() => window.location.reload()}
                className="mt-4 bg-amber-500 px-4 py-2 text-11 hover:bg-amber-600"
              >
                刷新到新版本
              </Button>
            </div>
          </div>
        );
      }
      return (
        <div className="flex h-full items-center justify-center p-8">
          <div className="max-w-md rounded-xl border border-red-200 bg-red-50 p-6 text-center dark:border-red-800 dark:bg-red-900/30">
            <h2 className="text-sm font-medium text-red-800 dark:text-red-300">页面出了问题</h2>
            <p className="mt-2 text-xs text-red-600 dark:text-red-400">{this.state.error.message}</p>
            <Button
              onClick={() => this.setState({ error: null })}
              className="mt-4 px-4 py-2 text-11"
            >
              重试
            </Button>
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
  // 按角色的未读数：对话页侧栏「她们」那一栏的徽章要用它。**与铃铛红点是同一次轮询的产物**
  // —— 两处各起一个轮询就会读到两个时刻（桌宠红点已经吃过一次这种亏，见
  // `api/reachouts.ts` 里 `UNREAD_POLL_MS` 那条注释）。
  const [reachoutUnreadByRole, setReachoutUnreadByRole] = useState<Record<string, number>>({});
  // 命令执行审批（架构计划 C·§6.2）：待批红点 + 审批抽屉。与主动消息同一套静默轮询
  // （10s）：模型提交审批后 10 秒内红点出现 —— 后端无推送，只能如实降级为轮询。
  const [approvalOpen, setApprovalOpen] = useState(false);
  const [approvalPending, setApprovalPending] = useState(0);

  // 轮询走唯一样板 usePoll（`R102-61`）：防竞态与"抽屉开着不打扰"都住在 hook 里，
  // 这里只剩"拿到快照写哪几个 state"。isLive = 这一拍还没被卸载/关抽屉作废。
  usePoll(
    async (live) => {
      const p = await api.getReachouts();
      if (live()) {
        setReachoutUnread(p.unread);
        setReachoutUnreadByRole(p.unread_by_role ?? {});
      }
    },
    UNREAD_POLL_MS,
    !reachoutOpen,
  );

  usePoll(
    async (live) => {
      const p = await api.getApprovals("pending");
      if (live()) setApprovalPending(p.pending);
    },
    UNREAD_POLL_MS,
    !approvalOpen,
  );

  // tab → URL hash（支持深链 /#/data 等）
  useEffect(() => {
    window.location.hash = `/${tab}`;
  }, [tab]);

  // 跨页深链：`#/chat?thread=…` 里的会话只是"一次性的意图"，ChatPage 打开它之后清掉
  // （见 `clearDeepThread`）。留着会让第二次点同一条收件箱消息时 hash 不变、事件不触发。
  const [deepThread, setDeepThread] = useState<string | null>(threadFromHash);

  // 浏览器前进/后退 → tab 状态同步
  useEffect(() => {
    const handler = () => {
      setTabState(tabFromHash());
      setDeepThread(threadFromHash());
    };
    window.addEventListener("hashchange", handler);
    return () => window.removeEventListener("hashchange", handler);
  }, []);

  function selectTab(key: TabKey) {
    setTabState(key);
    setNavOpen(false);
  }

  /** 打开某个会话（收件箱"打开对话并回复"的入口；桌面壳的通知点击将来也走这里）。 */
  function openThread(threadId: string) {
    // 先写 URL 再改状态：地址栏与前进/后退两条路都经同一个 hashchange handler 收敛。
    window.location.hash = `/chat?thread=${encodeURIComponent(threadId)}`;
    setTabState("chat");
    setDeepThread(threadId);
    setReachoutOpen(false);
    setNavOpen(false);
  }

  function clearDeepThread() {
    setDeepThread(null);
    // replaceState 而不是改 hash：不触发 hashchange，也就不会被上面的 effect 弹回来。
    window.history.replaceState(null, "", "#/chat");
  }

  // 桌面壳的"点系统通知 / 点桌宠气泡 → 打开那条主动会话"：控制台这边只需要接住一个会话 id，
  // 走的就是收件箱"打开对话并回复"那条现成的路 —— 壳不另开一套跳转语义（B/S 下桥不存在，
  // 这段直接不成立，行为不变）。
  useEffect(() => {
    const shell = shellBridge();
    if (!shell) return;
    shell.onRequestOpenThread((threadId) => openThread(threadId));
    return () => shell.onRequestOpenThread(null);
    // openThread 只用到稳定的 setState 与 location，没有需要跟随渲染的闭包状态。
  }, []);

  return (
    <ToastProvider>
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
            onClick={() => setApprovalOpen(true)}
            className="relative flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-left text-sm text-slate-600 hover:bg-slate-50 dark:text-slate-300 dark:hover:bg-slate-700/60"
            title="命令执行审批（run_command 待批命令）"
          >
            {/* 2026-10-24 UI 打磨：导航里的两个 emoji（⚙️/🔔）换成本文件 TABS 同一套
                stroke-SVG —— 跨操作系统的渲染重量一致，双图标系统归一。 */}
            <span className="shrink-0 opacity-80">
              <IconApproval />
            </span>
            命令审批
            {approvalPending > 0 && (
              <span className="ml-auto flex h-5 min-w-[20px] items-center justify-center rounded-full bg-amber-500 px-1.5 text-10 font-medium text-white">
                {approvalPending}
              </span>
            )}
          </button>
          <button
            onClick={() => setReachoutOpen(true)}
            className="relative flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-left text-sm text-slate-600 hover:bg-slate-50 dark:text-slate-300 dark:hover:bg-slate-700/60"
            title="角色主动找你（收件箱）"
          >
            <span className="shrink-0 opacity-80">
              <IconBell />
            </span>
            主动消息
            {reachoutUnread > 0 && (
              <span className="ml-auto flex h-5 min-w-[20px] items-center justify-center rounded-full bg-red-500 px-1.5 text-10 font-medium text-white">
                {reachoutUnread}
              </span>
            )}
          </button>
          {/* 数据源那一行（M5）：本机 / 云端，常驻侧栏底部，与上面两个抽屉入口同区。 */}
          <DataSourceSwitch />
        </div>
      </nav>

      {/* 主内容区。外层包一个 PageBoundary：chunk 加载失败那一类错误（升级后旧页签
          引用的 hash 文件已不在磁盘）在四个常驻页上没有各自的边界 —— 没有它，症状是
          白屏 + 控制台一条 404（实测 0.4.2→0.4.3 升级后切知识库/插件页就是它）。
          内层 keyed 的那个管"单页崩溃不串页"；这个外层只兜住内层没接住的（含常驻页），
          对页面崩溃行为零改变 —— 正常页根本到不了这里。 */}
      <main className="min-h-0 min-w-0 flex-1 pt-[41px] md:pt-0">
        <PageBoundary>
        {/* 对话页**常驻挂载**（用 hidden 隐藏，而不是条件渲染）。
            它有几样只存在于组件内的状态：正在生成的回答、输入草稿、当前对话与勾选 ——
            条件渲染会在切页时把整页卸载，这些全丢，而服务端那条流还在跑（审查报告 P1-11）。
            也正因为常驻，它必须放在 keyed 的 PageBoundary **外面**：那个 key={tab}
            会在每次切页时把子树整个重挂，放进去等于没改。 */}
        <div className={tab === "chat" ? "h-full" : "hidden"}>
          <Suspense
            fallback={<PageSkeleton />}
          >
            <ChatPage
              active={tab === "chat"}
              deepThread={deepThread}
              onDeepThreadUsed={clearDeepThread}
              unreadByRole={reachoutUnreadByRole}
            />
          </Suspense>
        </div>
        {/* 设置页同样**常驻挂载**（hidden 隐藏）：其内部 6 个子页签都有各自的加载
            状态与表单，条件渲染会在切走顶层页签时整棵卸载 —— 切回设置→服务又从头
            重新请求（与 ChatPage 同源问题，服务页"每次点开都加载中"的完整根因）。 */}
        <div className={tab === "settings" ? "h-full" : "hidden"}>
          <Suspense
            fallback={<PageSkeleton />}
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
            fallback={<PageSkeleton />}
          >
            <DataPage />
          </Suspense>
        </div>
        <div className={tab === "roles" ? "h-full" : "hidden"}>
          <Suspense
            fallback={<PageSkeleton />}
          >
            <RolesPage onOpenThread={openThread} />
          </Suspense>
        </div>
        {tab !== "chat" && tab !== "settings" && tab !== "data" && tab !== "roles" && (
          <PageBoundary key={tab}>
            <Suspense
              fallback={
                <PageSkeleton />
              }
            >
              {tab === "knowledge" && <KnowledgePage onOpenChat={() => selectTab("chat")} />}
              {tab === "plugins" && <PluginsPage />}
            </Suspense>
          </PageBoundary>
        )}
        </PageBoundary>
      </main>

      {/* 角色主动开口收件箱抽屉（全局，所有页签可见） */}
      <ReachoutPanel
        open={reachoutOpen}
        onClose={() => setReachoutOpen(false)}
        onUnreadChange={setReachoutUnread}
        onOpenThread={openThread}
      />
      {/* 命令执行审批抽屉（全局；面板内操作会回传 pending 数，保持与侧栏红点同步） */}
      <ApprovalPanel
        open={approvalOpen}
        onClose={() => setApprovalOpen(false)}
        onPendingChange={setApprovalPending}
      />
    </div>
    </ToastProvider>
  );
}
