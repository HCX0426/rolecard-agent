// 桌宠（D②-2 极简驻留形态 + D②-3 原生桥 + §7 悬停面板）：一张色片 + 一句气泡，
// 200×240 透明无边框置顶；鼠标停上去就长成一块面板（约 380×520），里面是**这个角色的
// 主动会话**最近几条。
//
// 这一页**只在桌面壳里出现**：浏览器直接开 #/pet 也能看（就是窗口不透明而已），但真正
// 的透明/置顶/无边框由壳的第二扇窗给（shell/main/windows.ts）。界面仍在这份前端里，
// 不在壳里另写一份 —— 两壳看到同一个 dist 是里程碑 D 的立身之本。
//
// 做四件事：角色主动说话时抬眼能看到；一眼看出攒了几条；点气泡把那条主动会话打开（壳负责
// 把控制台拉到前台）；有新开口时拍一条系统通知。§7 再加第五件：想回话不用开控制台。
//
// "什么算新消息、要不要通知"故意留在这份前端里，而不是塞进后端的调度器：调度器不知道自己
// 面对的是谁（B/S 在轮询、壳也在轮询），而壳只是"页面说了才拍一块 toast"的那只手。同一份
// dist 的两种形态因此不会分叉成两套通知判断。
//
// 展开时**页面只告诉壳"要不要摊开"**，尺寸与往哪边翻由主进程按工作区算（§7.2）：让后端
// 托管的那个源报坐标，等于把"窗口能摆到哪"交了出去。

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api, type MessagePage, type MessageRow, type ReachoutRow } from "../api";
import { shellBridge } from "../lib/shell";

const POLL_MS = 10_000; // 与铃铛红点同一节奏：后端没有推送，如实降级为轮询
const BUBBLE_MS = 30_000; // 气泡自己淡出：驻留件不该把一句话长期戳在桌面上
const MAX_BUBBLE_CHARS = 64;
/** 鼠标离开后等多久收起。留一段"游过去"的时间，否则永远点不到面板里的东西。 */
const COLLAPSE_MS = 400;
/** 面板里摊开最近几条（再多就该去控制台翻了）。 */
const PANEL_MESSAGES = 8;

/** role_id → 稳定色相。同一个角色的色片跨设备/跨主题都一样，认脸靠它。 */
function hueOf(roleId: string): number {
  let hash = 0;
  for (const ch of roleId) hash = (hash * 31 + ch.codePointAt(0)!) % 360;
  return hash;
}

function shorten(text: string): string {
  return text.length > MAX_BUBBLE_CHARS ? `${text.slice(0, MAX_BUBBLE_CHARS)}…` : text;
}

export default function PetPage() {
  const [items, setItems] = useState<ReachoutRow[]>([]);
  const [offline, setOffline] = useState(false);
  const [bubbleShownAt, setBubbleShownAt] = useState(0);
  const [faded, setFaded] = useState(false);
  // 悬停展开（§7）。窗口尺寸由壳改，这里只管"面板画不画"。
  const [expanded, setExpanded] = useState(false);
  const [history, setHistory] = useState<MessageRow[] | null>(null);
  const [historyError, setHistoryError] = useState("");
  const collapseRef = useRef<number | null>(null);
  // 已经"见过"的最新一条 id。**在第一次真正拿到快照之前保持 null**：初始的空白状态不是
  // 一次快照，拿它当基线会让每次开机都把积压的最后一条当新消息拍出去。
  const seenNewestRef = useRef<number | null>(null);

  const load = useCallback(async () => {
    let page;
    try {
      page = await api.getReachouts();
    } catch {
      setOffline(true); // 后端没起来 / 在换：桌面件必须说清"我现在是哑的"，不能装作没有消息
      return;
    }
    setItems(page.items);
    setOffline(false);

    const newest = page.items[0]?.id ?? 0;
    const seen = seenNewestRef.current;
    seenNewestRef.current = newest;
    // 系统通知只给"这次真的新到"的那条：首次快照立基线，之后 id 没变大就不弹。
    if (seen === null || newest <= seen) return;
    const arrived = page.items[0];
    if (arrived?.state === "unread") {
      shellBridge()?.notify(arrived.role_name || "主动消息", shorten(arrived.text), arrived.thread_id);
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = setInterval(() => void load(), POLL_MS);
    return () => clearInterval(timer);
  }, [load]);

  const latest = items[0] ?? null;
  const unread = useMemo(() => items.filter((row) => row.state === "unread"), [items]);

  // 新的一条（id 变大）出现时重新计时；同一条不再弹第二次。
  useEffect(() => {
    if (!latest) return;
    setBubbleShownAt(Date.now());
    setFaded(false);
  }, [latest?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  // 系统通知的判断在 `load()` 里（"拿到快照"那一刻才是"新到"的定义），这里只管气泡计时。
  useEffect(() => {
    if (!bubbleShownAt || faded) return;
    const timer = setTimeout(() => setFaded(true), BUBBLE_MS);
    return () => clearTimeout(timer);
  }, [bubbleShownAt, faded]);

  const unreadOfLatest = latest ? unread.filter((row) => row.role_id === latest.role_id).length : 0;
  const hue = latest ? hueOf(latest.role_id) : 210;
  const name = latest?.role_name || latest?.role_id || "助手";
  const threadId = latest?.thread_id ?? null;

  /** 展开/收起。桥不在（浏览器直接开 #/pet 的调试入口）时面板照样画，只是窗口不跟着变大。 */
  function expand(next: boolean) {
    if (collapseRef.current !== null) {
      window.clearTimeout(collapseRef.current);
      collapseRef.current = null;
    }
    setExpanded(next);
    void shellBridge()?.setPetExpanded(next);
  }

  function leave() {
    if (collapseRef.current !== null) window.clearTimeout(collapseRef.current);
    collapseRef.current = window.setTimeout(() => expand(false), COLLAPSE_MS);
  }

  /**
   * 手动拖桌宠（实测换来的形状）。
   *
   * 原来靠 CSS 的 `-webkit-app-region: drag`，整块根节点都是拖拽区 —— 而**拖拽区会把鼠标
   * 事件整个吞掉**：色片上悬停根本收不到 mouseenter，悬停展开就没法工作（那篇 Electron
   * 悬浮球实现说的"drag 与点击冲突"是同一件事，只是我当时以为我们能共存，实测不能）。
   * 所以色片改成 no-drag + 自己处理拖：
   *  - 用 `screenX/screenY` 的**增量**而不是 `clientX`：窗口正跟着鼠标走，页面坐标会被
   *    一起拖回去，用 clientX 算出来的差值恒为 0（这类"看起来在动其实没动"的坑很典型）；
   *  - 只把增量交给壳（`movePetBy`），绝对坐标与出屏夹取都在主进程 —— 页面拿不到"把窗
   *    放到哪"的权力；
   *  - 色片没有点击动作，所以不需要"移动多少算拖"的阈值，按下即拖、抬起即止。
   */
  const dragRef = useRef<{ x: number; y: number } | null>(null);

  function dragStart(event: React.PointerEvent<HTMLDivElement>) {
    dragRef.current = { x: event.screenX, y: event.screenY };
    event.currentTarget.setPointerCapture?.(event.pointerId);
  }

  function dragMove(event: React.PointerEvent<HTMLDivElement>) {
    const last = dragRef.current;
    if (!last) return;
    const dx = event.screenX - last.x;
    const dy = event.screenY - last.y;
    if (!dx && !dy) return;
    dragRef.current = { x: event.screenX, y: event.screenY };
    shellBridge()?.movePetBy(dx, dy);
  }

  function dragEnd() {
    dragRef.current = null;
  }

  // 卸载时把待收起的定时器收掉：留着它会在组件没了之后去调桥。
  useEffect(
    () => () => {
      if (collapseRef.current !== null) window.clearTimeout(collapseRef.current);
    },
    [],
  );

  // 历史只在**展开时**读：驻留件不该为了一个没被看到的面板每 10 秒打一次接口。
  useEffect(() => {
    if (!expanded) {
      setHistory(null);
      setHistoryError("");
      return;
    }
    if (!threadId) return; // 还没有主动会话：面板显示"还没有你们的对话"，不发请求
    let alive = true;
    setHistoryError("");
    api
      .get<MessagePage>(`/api/session/${threadId}/messages?limit=${PANEL_MESSAGES}`)
      .then((page) => alive && setHistory(page.messages))
      .catch((e: Error) => alive && setHistoryError(`历史没读到：${e.message}`));
    return () => {
      alive = false;
    };
  }, [expanded, threadId]);

  async function acknowledge(row: ReachoutRow) {
    // 先标已读再收气泡：标失败就留着，让用户知道"这条还没真被读过"。
    try {
      await api.markRoleReachoutsRead(row.role_id);
    } catch {
      setOffline(true);
      return;
    }
    setFaded(true);
    await load();
  }

  async function openRow(row: ReachoutRow) {
    if (!row.thread_id) {
      // 这个功能上线之前落库的老消息没有对应的主动会话：只能标已读，不给死链。
      await acknowledge(row);
      return;
    }
    try {
      await api.markRoleReachoutsRead(row.role_id);
    } catch {
      setOffline(true); // 标记失败不拦跳转：会话就在那儿，点得开比红点准更重要
    }
    setFaded(true);
    // 跳转交给壳：它要把控制台那扇窗拉到前台，而这一页自己**不是**控制台（浏览器里直接开
    // #/pet 只是看看的调试入口，真要读历史就点侧栏的主动消息收件箱）。桥不存在时这里就只是
    // 标已读 —— 不留一个"点了什么都没发生"的假链接。
    shellBridge()?.openSession(row.thread_id);
  }

  return (
    // 拖拽靠 CSS `-webkit-app-region`（Chromium 自己处理，不需要页面拿到任何壳能力）。
    // Tauri 那边"后端源拿不到注入 → data-tauri-drag-region 拖不动"的坑在这里不存在；
    // 气泡与面板要能点/能选字，所以它们各自标 no-drag。
    //
    // **悬停检测挂在色片与面板上，不挂在这块拖拽区上**：drag 区域由 Chromium 接管鼠标
    // 按下，整窗根节点上监听会时灵时不灵（掘金那篇悬浮球实现说的"drag 与点击冲突"就是
    // 同一族问题）。色片是 drag 区，但 mouseenter 不吃 —— 拖只在"按下 + 移动"时才成立。
    <div className="pet-drag flex h-full select-none flex-col items-center justify-end gap-2 pb-1">
      {expanded ? (
        // 展开态：面板取代气泡（气泡那条就是面板最后一条，重复摆一遍只是噪音）。
        <section
          onMouseEnter={() => expand(true)}
          onMouseLeave={leave}
          className="pet-nodrag flex max-h-full w-full flex-col rounded-2xl border border-slate-200/70 bg-white/95 text-slate-700 shadow-md backdrop-blur-sm dark:border-slate-600/70 dark:bg-slate-800/95 dark:text-slate-100"
        >
          <header className="flex shrink-0 items-center justify-between gap-2 border-b border-slate-100 px-3 py-2 dark:border-slate-700">
            <span className="truncate text-xs font-medium">{name}</span>
            {threadId && (
              <button
                onClick={() => shellBridge()?.openSession(threadId)}
                className="shrink-0 text-[10px] text-blue-600 hover:underline dark:text-blue-400"
                title="在控制台里打开这条会话（能翻完整历史、能发图）"
              >
                在控制台打开
              </button>
            )}
          </header>
          <div className="max-h-[300px] min-h-0 flex-1 space-y-1.5 overflow-y-auto px-3 py-2 text-[11px] leading-relaxed">
            {historyError && (
              <p className="text-red-600 dark:text-red-400">{historyError}</p>
            )}
            {!threadId && !historyError && (
              <p className="text-slate-400 dark:text-slate-500">
                还没有你们的对话 —— 它主动找你的那次会出现在这里。
              </p>
            )}
            {threadId && history === null && !historyError && (
              <p className="text-slate-400 dark:text-slate-500">读取中…</p>
            )}
            {(history ?? [])
              .filter((m) => m.role === "user" || m.role === "assistant")
              .map((m, i) => (
                <p key={m.id ?? i} className="break-words">
                  <span className="text-slate-400 dark:text-slate-500">
                    {m.role === "user" ? "你：" : "它："}
                  </span>
                  {m.content}
                </p>
              ))}
          </div>
        </section>
      ) : (
        latest &&
        !faded && (
          <button
            onClick={() => void openRow(latest)}
            onMouseEnter={() => expand(true)}
            onMouseLeave={leave}
            title={
              latest.thread_id
                ? unreadOfLatest > 1
                  ? `打开与 ${name} 的对话（还有 ${unreadOfLatest - 1} 条未读）`
                  : `打开与 ${name} 的对话`
                : "点击标记已读"
            }
            className="pet-nodrag w-full rounded-2xl border border-slate-200/70 bg-white/90 px-3 py-2 text-left text-[11px] leading-relaxed text-slate-700 shadow-sm backdrop-blur-sm transition-opacity duration-500 dark:border-slate-600/70 dark:bg-slate-800/90 dark:text-slate-100"
          >
            {shorten(latest.text)}
            {unreadOfLatest > 1 && (
              <span className="ml-1 rounded-full bg-blue-600 px-1.5 text-[10px] text-white">
                +{unreadOfLatest - 1}
              </span>
            )}
          </button>
        )
      )}

      <div
        onMouseEnter={() => expand(true)}
        onMouseLeave={leave}
        onPointerDown={dragStart}
        onPointerMove={dragMove}
        onPointerUp={dragEnd}
        onPointerCancel={dragEnd}
        className="pet-nodrag grid h-[88px] w-[88px] shrink-0 cursor-grab place-items-center rounded-full text-2xl font-medium text-white shadow-md active:cursor-grabbing"
        style={{ background: `hsl(${hue} 62% 48%)` }}
        title={`${name}${offline ? " · 连不上本地服务" : ""} · 悬停看你们最近聊了什么`}
      >
        {name.slice(0, 1)}
      </div>

      {offline && (
        <span className="text-[10px] text-amber-600 dark:text-amber-400">连不上本地服务</span>
      )}
    </div>
  );
}
