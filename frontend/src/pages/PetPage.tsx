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

import { useCallback, useEffect, useRef, useState } from "react";

import ThinkingPanel from "../components/chat/ThinkingPanel";
import PetContextMenu, { type MenuEntry } from "../components/PetContextMenu";
import PetSprite, { type PetStatus } from "../components/pet/PetSprite";
import Live2dPet from "../components/pet/Live2dPet";
import { resolvePack, type PetPack, type PetPackListing } from "../pets/registry";
import QuietLine from "../components/QuietLine";
import { api, streamChat, streamEdit, UNREAD_POLL_MS, type MessagePage, type MessageRow, type QuietStatus, type ReachoutRow, type RoleCard } from "../api";
import { useAutoScroll } from "../hooks/useAutoScroll";
import { usePoll } from "../hooks/usePoll";
import { useChatStream } from "../hooks/useChatStream";
import { read as readDataSource } from "../lib/dataSource";
import { shellBridge } from "../lib/shell";
import { type StreamMeta } from "../lib/stream";

const BUBBLE_MS = 30_000; // 气泡到点自己收起：驻留件不该把一句话长期戳在桌面上
// （不做淡出：这扇窗是透明窗，半透明的每一毫秒透出来的都是桌面本身 —— 那正是"重影"）
const MAX_BUBBLE_CHARS = 64;
/** 指针离开桌面件多久之后，趴着的才自己回去：给"划过它身上"留的宽限。 */
const RETUCK_MS = 900;
/**
 * "点哪儿算点桌宠"的余量：色片那 88 见方之外，再算上**壳能把窗挪走多远**。
 * 横向 96 = 壳里的 `SIDE_TUCK`（趴边 ↔ 拉出来那一步），纵向 44 = `BOTTOM_TUCK`。
 * 宽限期里整扇窗都吃点击（为的是"色片刚从边上滑走而手还在原地"），所以落在这段带上的一下
 * 要认成点它；再往外（画布左右各还有 84px、上面 280px）就是"点在桌宠旁边的桌面上"了。
 */
const HIT_SLACK_X = 96;
const HIT_SLACK_Y = 44;
/** 面板里摊开最近几条。**这不是"对话的全部"** —— 一条主动会话今天实量到 44 条，
 *  截到这儿只剩最近这一截，所以列表顶部必须说出"上面还有几条"（见 `historyTotal`），
 *  否则驻留件就在悄悄冒充整段历史。再多就不在这里翻了：这块是透明置顶窗，
 *  几百条的绘制与逐帧命中区上报都不该压给它，完整历史在控制台那一扇。 */
const PANEL_MESSAGES = 30;

/** 读这条主动会话的最近几条。URL 只写一处：展开时与一轮跑完两条路径必须读同一个东西。 */
const messagesPath = (tid: string) => `/api/session/${tid}/messages?limit=${PANEL_MESSAGES}`;

function shorten(text: string): string {
  return text.length > MAX_BUBBLE_CHARS ? `${text.slice(0, MAX_BUBBLE_CHARS)}…` : text;
}

/**
 * 一行对话。**说话人只挂在 `aria-label` 上，不再印成"它：/你："那两个可见的字**
 * （用户 09-26："每次对话都有个它：，你：，这是不需要的"）：
 *
 *   · 视觉上谁说的已经由**位置 + 泡**分开了（我的靠右带蓝泡，见 `MINE_ROW`）；
 *   · 但"谁说的"这件事不能只靠颜色与位置 —— 读屏软件与无障碍树要看得到，
 *     所以标注留在 `aria-label` 里，等于把原来那块可见文字降级成语义标签。
 *
 * 一个 helper 而不是三处各写一遍：三种来源（回放的历史 / 刚发出去还没落库的那句 /
 * 正在流的这一轮）在数据上长得都不一样，很容易改漏一处 —— 漏了就又变成"位置说不清"。
 */
function rowOf(mine: boolean): { className: string; "aria-label": string } {
  return mine
    ? { className: MINE_ROW, "aria-label": "我说" }
    : { className: "break-words", "aria-label": "对方说" };
}

/** 我这一方靠右 + 一个浅蓝泡（用户 09-26："像对话界面那样我的回复显示在右边，才有对话感"）。
 *
 * 形状抄 `ChatPage` 那条用户气泡（`ml-auto w-fit max-w-*` + `rounded-br-sm` 的"尾巴"
 * + 蓝边蓝底），只是这面板尺寸小，留白与字阶跟着 `text-[11px]` 那一档收一号。
 * 说话人不印成可见的"它：/你："（用户 09-26 撤掉的），但那条信息在 `aria-label` 里
 * —— 见 `rowOf`：位置与颜色不足以让读屏软件与色弱用户分辨谁说的。
 */
const MINE_ROW =
  "ml-auto w-fit max-w-[86%] break-words rounded-xl rounded-br-sm border border-blue-200" +
  " bg-blue-50 px-1.5 py-0.5 dark:border-slate-700 dark:bg-slate-800/70";

export default function PetPage() {
  const [items, setItems] = useState<ReachoutRow[]>([]);
  // "某个角色有几条没读"读后端那一份（`unread_by_role`），不在这里 filter 第二遍。
  const [unreadByRole, setUnreadByRole] = useState<Record<string, number>>({});
  // "她此刻为什么静默"（`S-8` 的负载跟 `/api/reachouts` 一起到）—— 桌宠这侧过去压根不显示，
  // 用户天天盯着这扇窗，查不到的地方等于没有（R26-45 那条尾巴）。
  const [quiet, setQuiet] = useState<QuietStatus[]>([]);
  const [offline, setOffline] = useState(false);
  const [bubbleShownAt, setBubbleShownAt] = useState(0);
  const [faded, setFaded] = useState(false);
  // 悬停展开（§7）。窗口尺寸由壳改，这里只管"面板画不画"。
  const [expanded, setExpanded] = useState(false);
  const [history, setHistory] = useState<MessageRow[] | null>(null);
  /** 这条会话**一共有**几条（后端 `total`）。面板只画最近 `PANEL_MESSAGES` 条，
   *  差额要在屏幕上说清楚 —— 少了这一格，面板看起来就像"你们的对话只有这几条"。 */
  const [historyTotal, setHistoryTotal] = useState(0);
  const [historyError, setHistoryError] = useState("");
  const [roles, setRoles] = useState<RoleCard[]>([]);
  /** 形象包清单；`undefined` = 还没读，`null` = 读失败（两者都画随包那张默认图，不退化）。 */
  const [packs, setPacks] = useState<PetPackListing | null | undefined>(undefined);
  const [picked, setPicked] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  // 发出去但还没落库的那句：流结束后以服务端回放为准，所以它只活在这一轮里。
  const [pendingUser, setPendingUser] = useState("");

  /** 别处（控制台那一扇窗）发起、**此刻还在生成**的那半句（R26-38 的镜像，桌宠这一侧）。
   *  `null` = 没人在生成。不另开请求：`/messages` 那份回放里已经带着 `inflight` 了，
   *  而这块面板本来就每 3 秒重读一次这条线程。 */
  const [mirror, setMirror] = useState<string | null>(null);
  const [streamError, setStreamError] = useState("");
  // 右键菜单（页内自绘，见 `components/PetContextMenu`）。null = 没开。
  const [menu, setMenu] = useState<{ x: number; y: number; entries: MenuEntry[] } | null>(null);
  // 托盘「显示消息内容」的旗子。**默认显示**：拿不到旗子（浏览器里开这页、或界面跑在还没
  // 有这面旗子的旧壳里）时不该把功能藏起来，那等于用一个用户找不到的开关把他锁在门外。
  const [showContent, setShowContent] = useState(true);
  // 同一个值的"读时不重订阅"版本：`load` 是轮询回调，依赖里加旗子会让每次改开关都
  // 把 10 秒的节拍重置一遍（而它只是想知道这次要不要拍原文）。
  const showContentRef = useRef(true);
  const retuckRef = useRef<number | null>(null);
  /** 色片那块 DOM：只用来量"点的那一下离它有多远"（见 `onPet` 与 `HIT_SLACK_*`）。 */
  const spriteRef = useRef<HTMLDivElement | null>(null);
  // `expanded` 的"读时不重渲染"版本：拖拽期间 pointermove 密集触发，而 React 状态要到下一次
  // 渲染才更新，只认状态就会在一次拖里连发十几次"收起"给壳。
  const expandedRef = useRef(false);
  /**
   * 面板在画布里往屏内挪多少像素（壳推过来的，贴边展开时才非零）。
   *
   * 为什么页面要接这个数：色片贴着右边趴着时它中心离屏幕右沿只有 100px，而面板要 380 宽 ——
   * 居中放不下。旧解法是"把窗口往屏内推 + 色片在窗内自挪回原位"，代价是一次 `setBounds`，
   * 而 DWM 会在那一帧把旧内容拉伸成新尺寸 —— 就是用户报了三次的重影。现在窗口是常驻画布
   * （两侧各 180px 余量）**永远不变尺寸**，只有这一块的 transform 在动：面板朝屏幕内侧长，
   * 色片一格都不动。旧壳不推这个数 ⇒ 保持 0，等于回到"面板在画布正中"那个老画法，不会更坏。
   */
  const [panelShift, setPanelShift] = useState(0);
  // 已经"见过"的最新一条 id。**在第一次真正拿到快照之前保持 null**：初始的空白状态不是
  // 一次快照，拿它当基线会让每次开机都把积压的最后一条当新消息拍出去。
  const seenNewestRef = useRef<number | null>(null);
  /** `threadId` 的"读时不重订阅"版本：轮询回调要知道"此刻开着的是哪条会话"，
   *  而把 threadId 放进 `load` 的依赖会让每次换角色都重建一遍 3 秒节拍。 */
  const threadIdRef = useRef<string | null>(null);
  // 上一次落到面板上的那份历史"长什么样"（条数 + 最后一条 id）。轮询靠它判有没有变，
  // 没变就一次 set 都不发 —— 否则每 3 秒都算"新字长出来了"，自动滚动会把用户往上翻的
  // 那一眼拽回底部。
  const historyShapeRef = useRef("");

  // 流式那一轮：与对话页**同一份**归约（lib/stream）与同一个 hook，桌宠不写第二套。
  const { busy, setBusy, live, setLive, liveRef, onEvent, startBubble, stop, sendingRef } =
    useChatStream((meta: StreamMeta) => {
      if (meta.errored) setStreamError(meta.errorDetail || "模型调用失败");
    });
  // `busy` 的"读时不重渲染"版本：回放到手那一刻要判断"这一轮还在流吗"（见 `handoff`），
  // 而闭包里的 `busy` 是上一次渲染的值，正在流的这一轮会被误判成已结束。
  const busyRef = useRef(false);

  /**
   * 回放到手 = 服务端已经是这一轮的唯一真相：把乐观那条与流式气泡一起交出去。
   *
   * 为什么必须在**这一处**做：回放里已经有她那句了，而气泡里还挂着同一句 —— 两个都画就是
   * 用户报的"重影"（发一条，屏幕上回两条一模一样的）。对话页在流结束时做的是同一件事
   * （`ChatPage` 里那句 `setLive(null)`），桌宠这边先前漏了，于是气泡一辈子不消失，
   * 直到下一次发送才被 `startBubble` 覆盖。
   */
  const handoff = useCallback(
    (page: MessagePage) => {
      setHistory(page.messages);
      setHistoryTotal(page.total);
      if (busyRef.current) return; // 还在流：气泡是唯一的实时反馈，不能被一次回放吃掉
      setLive(null);
      liveRef.current = null;
      setPendingUser("");
    },
    [setLive, liveRef],
  );

  /** 这一页面上的"多少条 + 最后一条是谁"——轮询靠它判有没有变。 */
  function shapeOf(page: MessagePage): string {
    return `${page.total}:${page.messages[page.messages.length - 1]?.id ?? "-"}`;
  }

  /**
   * 面板开着的时候，把这条会话重新读一遍。
   *
   * 用户 09-26 报的："我在对话界面对话时，桌宠打开的消息却不会更新" —— 根因是历史只在
   * **展开那一下**读一次（`useEffect` 依赖 `expanded`），而 3 秒轮询只刷收件箱那份 `items`。
   * 同一条线程被两边写，面板却停在打开它的那一瞬间。
   *
   * 三道闸，每一道都对应一次踩过的坑：
   *  - 没展开 / 关了显示 ⇒ 不打接口（驻留件不该为一个没被看的面板常驻轮询）；
   *  - **自己这一轮还在流 ⇒ 不抢**。收尾归 `handoff` 管，这里插一脚就是"我发一条她回两条"
   *    那个重影的另一版；
   *  - 内容没变 ⇒ 一次 set 都不发。否则每 3 秒都被自动滚动当成"新字长出来了"，
   *    把用户往上翻找旧消息的那一眼拽回底部。
   */
  /** 一份回放到手时，镜像那一格该走到哪儿。展开那一次与每 3 秒那一拍**共用这一句** ——
   *  两处各自解释 `inflight` 的话，早晚有一处忘了接。 */
  const setMirrorOf = useCallback(
    (page: MessagePage) => setMirror(page.inflight ? page.inflight.text : null),
    [],
  );

  const refreshHistory = useCallback(async () => {
    const tid = threadIdRef.current;
    if (!tid || !expandedRef.current || !showContentRef.current || busyRef.current) {
      // 闸没开的时候这一格也不该留着：面板收起时读到的"对方在说"，到展开那一刻已经作数了；
      // 而自己这扇窗在流的时候，屏幕上已经有 `live` 那个气泡，两处都画就是重影。
      setMirror(null);
      return;
    }
    let page: MessagePage;
    try {
      page = await api.get<MessagePage>(messagesPath(tid));
    } catch {
      return; // 读不到就留着上一份：接口抖一下不该把面板清空，那比陈旧更像"她忘了"
    }
    // 在"内容没变就不 set"那道闸**之前**取：在飞的那半句每一帧都在变，
    // 而 `shapeOf` 只看已落库的那些行，等它变了这一格就慢了一整轮。
    setMirrorOf(page);
    const shape = shapeOf(page);
    if (shape === historyShapeRef.current) return;
    historyShapeRef.current = shape;
    handoff(page);
  }, [handoff, setMirrorOf]);

  const load = useCallback(async () => {
    let page;
    try {
      page = await api.getReachouts();
    } catch {
      setOffline(true); // 后端没起来 / 在换：桌面件必须说清"我现在是哑的"，不能装作没有消息
      return;
    }
    setItems(page.items);
    setUnreadByRole(page.unread_by_role ?? {});
    // 静默原因跟同一份响应走（`S-8` 的负载挂在 `/api/reachouts` 上）—— 每 3 秒已经拿
    // 到手的东西，为它再开一个轮询等于多加一个时刻源。
    setQuiet(page.quiet ?? []);
    setOffline(false);
    // 同一个节拍顺带把这条会话重读一遍：面板开着的时候，控制台也在往同一条线程里写
    // （用户 09-26 报的"对话界面对话时桌宠不更新"）。三道闸在 `refreshHistory` 里。
    void refreshHistory();

    const newest = page.items[0]?.id ?? 0;
    const seen = seenNewestRef.current;
    seenNewestRef.current = newest;
    // 系统通知只给"这次真的新到"的那条：首次快照立基线，之后 id 没变大就不弹。
    if (seen === null || newest <= seen) return;
    const arrived = page.items[0];
    if (arrived?.state === "unread") {
      // 旗子关掉时连系统通知也不拍原文：toast 是桌面上最显眼的一块"别人也能读"的内容，
      // 让它绕过隐藏就等于这个开关只糊住了半张脸。谁找你了照样说清（标题是角色名）。
      shellBridge()?.notify(
        arrived.role_name || "主动消息",
        showContentRef.current ? shorten(arrived.text) : "内容已隐藏",
        arrived.thread_id,
      );
      // 语音与通知同源同时刻（都是"这次真的新到"那条）：开关与内容旗子都在 `speak` 里判。
      speak(arrived.text);
    }
  }, [refreshHistory]);

  // 轮询走唯一样板 usePoll（`R102-61`）：卸载后不再打拍 —— 从前这两处是同形状里
  // 唯一没有 cancelled 旗的半边。
  usePoll(() => load(), UNREAD_POLL_MS);

  const latest = items[0] ?? null;

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

  const unreadOfLatest = latest ? (unreadByRole[latest.role_id] ?? 0) : 0;
  /**
   * 面板跟谁说话：手动选的 > 最近主动找你的 > 角色表里的第一个。
   *
   * 为什么要兜到"角色表第一个"：一个从没被主动找过的新用户也该能在桌宠上开口，
   * 而那时 `latest` 是空的 —— 只按气泡定角色会让面板变成一只不能说话的摆件。
   */
  const activeRole = picked ?? latest?.role_id ?? roles[0]?.role_id ?? null;
  // 数据源（本机 / 云端 · 账号）：桌宠走的是同一个 `apiBase()`，数据确实跟着切了 ——
  // 这行只是把"连的是哪份"说出口（M5 没做三件之②）。它是 localStorage 上的纯读，渲染时取即可。
  const source = readDataSource();
  const cloud = source.mode === "cloud" ? source : null;
  // 「她此刻为什么静默」按当前这个角色取（与收件箱抽屉的 quiet_status 同一份数据）。
  const myQuiet = quiet.find((q) => q.role_id === activeRole) ?? null;
  const activeName =
    roles.find((r) => r.role_id === activeRole)?.role_name ??
    latest?.role_name ??
    latest?.role_id ??
    "助手";
  /**
   * 这一只画哪个包：当前角色的 `pet_pack` → 清单里的那个包。清单还没读回来时落随包那张
   * 默认图（**不是**几何体兜底），所以起窗那一瞬与做这件事之前一字不差。三种"没配上"的
   * 分工见 `pets/registry.ts`。
   */
  const looked = resolvePack(
    packs ?? null,
    roles.find((r) => r.role_id === activeRole)?.pet_pack,
  );
  /**
   * Live2D 模型加载失败（缺运行时 / moc3 版本对不上 / 模型自己坏了）时**退回默认包**，
   * 并把原因留在界面上 —— 一条都不可见的坏模型比退回默认那只难查得多。
   * 换角色（包 id 变了）就把它清掉：那是另一个包的另一件事，不该一直挂着上一只的错误。
   */
  const [live2dFailure, setLive2dFailure] = useState<{ id: string; reason: string } | null>(null);
  useEffect(() => {
    setLive2dFailure((prev) => (prev && prev.id !== looked.pack?.id ? null : prev));
  }, [looked.pack?.id]);
  const failedLive2d =
    live2dFailure && looked.pack?.kind === "live2d" && live2dFailure.id === looked.pack.id
      ? live2dFailure.reason
      : "";
  // 真正要画的那一份：live2d 失败时退回清单里的默认包（没有默认包就交 null ⇒ SVG 兜底）。
  const drawn: PetPack | null = failedLive2d
    ? (packs?.packs.find((p) => p.id === "default") ?? null)
    : looked.pack;
  // 形象的表现状态：完全由本页已有的信号推导，不另开通道。
  //   · 自己这扇窗在流 / 她正在生成 → speaking（嘴动 + 浮沉）；
  //   · 别处那扇窗在生成（`mirror` 是 R26-38 的镜像登记）→ thinking（"…"泡泡）；
  //   · 你在打字 → listening；其余 → idle（呼吸 + 眨眼）。
  const petStatus: PetStatus =
    busy || (live && (live.text || live.streaming || live.thinking))
      ? "speaking"
      : mirror !== null
        ? "thinking"
        : draft.trim()
          ? "listening"
          : "idle";
  // 投递记录给的线程 id（有最近一条时用它，省一次请求）。
  const knownThreadId = latest && latest.role_id === activeRole ? latest.thread_id ?? null : null;
  /** 这条主动会话的 id。清空抽屉之后 `latest` 就没了，而**会话与历史一直在**
   *  （用户 2026-09-23："桌宠的历史消息没记录了，切换角色也没"）—— 所以没有行时问一次
   *  后端"这条线在不在"（只读，不建行：只打开面板看一眼不该在侧栏长出一条会话）。 */
  const [resolvedThreadId, setResolvedThreadId] = useState<string | null>(null);
  const threadId = knownThreadId ?? resolvedThreadId;
  // 轮询回调读的是"此刻开着的是哪条会话"（见 `threadIdRef`）：换角色、清空抽屉、第一次
  // 问出 id，都要让它跟着走，否则 `refreshHistory` 会去刷一条已经不是当前对象的线程。
  useEffect(() => {
    threadIdRef.current = threadId;
  }, [threadId]);
  const name = activeName;
  // 隐藏内容时面板要说"有几条没读"，那数的是**当前对象**的（切到别的角色就不是那一堆了）。
  const unreadOfActive = activeRole ? (unreadByRole[activeRole] ?? 0) : 0;

  /**
   * 面板列表贴着底部就跟随新内容（与对话页同一个 hook，规则也一致：用户往上翻时不拽回来）。
   *
   * 依赖里有 `live`：她正在回话时每一帧气泡都在长，没有这一项就是用户 2026-09-25 报的
   * "回答时滚动条不自动到最新"—— 面板只有 300px 高，新那几行一直长在看不见的下面。
   */
  const scrollRef = useAutoScroll(threadId, [history, pendingUser, live, mirror]);

  useEffect(() => {
    if (knownThreadId || !activeRole) {
      setResolvedThreadId(knownThreadId);
      return;
    }
    // 换角色时先清掉上一个角色的解析结果：留着会让面板拿旧线程读一拍历史。
    setResolvedThreadId(null);
    let alive = true;
    api
      .proactiveThread(activeRole)
      .then((r) => alive && setResolvedThreadId(r.thread_id))
      .catch(() => alive && setResolvedThreadId(null)); // 问不到就当没有这条线，面板走"还没有对话"
    return () => {
      alive = false;
    };
  }, [knownThreadId, activeRole]);

  /** 展开/收起面板。由**点击**触发（悬停不算，理由见下面 `reveal`）。
   *  桥不在（浏览器直接开 #/pet 的调试入口）时面板照样画，只是窗口不跟着变大。 */
  function expand(next: boolean) {
    if (expandedRef.current === next) return; // 一次拖拽里连着十几次 pointermove：只发一次
    expandedRef.current = next;
    setExpanded(next);
    void shellBridge()?.setPetExpanded(next);
  }

  /** 只在"画了像素的地方"吃桌面点击（审计 §12.3）。
   *
   *  200×240 里色片只占中间 88 见方，两侧各 56px 是全透明的，可整扇窗都在吃鼠标 ——
   *  结果那两条带上面的桌面图标点不到（用户 2026-09-22 就问过）。这页只把"画了东西的块"
   *  （`.pet-nodrag`：色片、气泡、面板）报给壳，壳每 40ms 拿光标位置比一次。
   *  为什么不在页面里判 hover 再回一个布尔：真机量过 —— 窗带上 `WS_EX_TRANSPARENT` 之后
   *  `forward:true` 一条 mousemove 都送不进来，翻成"放行"就没有回来的路。
   *  浏览器里开 #/pet、或新界面跑在旧壳里（没这两个方法）时一行都不做 ⇒ 保持整窗吃点击。
   */
  const hotKeyRef = useRef("");

  function reportHotRects() {
    const send = shellBridge()?.petHotRects;
    if (!send) return; // 没壳 / 旧壳：不报，壳那边就不会把窗设成忽略
    const rects = Array.from(document.querySelectorAll(".pet-nodrag")).map((el) => {
      const r = el.getBoundingClientRect();
      return [r.left, r.top, r.width, r.height].map((n) => Math.round(n));
    });
    const key = JSON.stringify(rects);
    if (key === hotKeyRef.current) return; // 每次渲染都跑一遍：形状没变就不发消息
    hotKeyRef.current = key;
    send(rects);
  }

  // 挂载：声明"这页会报像素块"，卸载：立刻还给壳（壳那边同时停表）。
  useEffect(() => {
    const arm = shellBridge()?.petHitTest;
    if (!arm) return;
    arm(true);
    return () => {
      arm(false);
      hotKeyRef.current = "";
    };
  }, []);

  // 每次渲染后重报一遍形状，再补一次给 CSS 动画落定的那帧（面板/气泡是长出来的）。
  useEffect(() => {
    reportHotRects();
    const later = window.setTimeout(reportHotRects, 260);
    return () => window.clearTimeout(later);
  });

  /** 悬停只做一件事：把趴在边上的那半只拉出来（§7.6）。**不再弹面板** —— 悬停会改窗口尺寸，
   *  而改尺寸就把宠物从光标底下挪走了，于是"离开→收起→又进入→展开"自激（用户报的"鼠标移过去
   *  桌宠乱晃"）。面板改成点击才弹，这条环就断了。 */
  function reveal() {
    if (retuckRef.current !== null) {
      window.clearTimeout(retuckRef.current);
      retuckRef.current = null;
    }
    shellBridge()?.petReveal?.();
  }

  /** 指针真的离开桌面件：宽限一会儿再让壳把它趴回去（面板开着时壳自己会拒绝，见 `petRetuck`）。 */
  function pointerLeft() {
    if (retuckRef.current !== null) window.clearTimeout(retuckRef.current);
    retuckRef.current = window.setTimeout(() => {
      retuckRef.current = null;
      shellBridge()?.petRetuck?.();
    }, RETUCK_MS);
  }

  /** 这次指针动作是不是落在"有内容的东西"上（面板、气泡）。
   *
   *  根节点整块都接点击与拖拽（见下面那段注释），而面板里的输入框、发送键、角色下拉
   *  也在这块范围内 —— 不区分的话，在面板里打一个字按回车都会顺着冒泡把面板收起来。
   */
  function insideUi(event: { target: EventTarget | null }): boolean {
    const el = event.target as HTMLElement | null;
    return Boolean(el?.closest?.("[data-pet-ui]"));
  }

  /**
   * 手动拖桌宠（实测换来的形状）。
   *
   * 原来靠 CSS 的 `-webkit-app-region: drag`，整块根节点都是拖拽区 —— 而**拖拽区会把鼠标
   * 事件整个吞掉**：色片上悬停根本收不到 mouseenter（那篇 Electron 悬浮球实现说的"drag 与
   * 点击冲突"就是这件事）。既然拖动已经改成手动，那个 drag 标记就只剩坏处了：它连根节点的
   * "指针离开"都吞掉，而趴回去的判据正需要它 ⇒ 根节点现在不标 drag。
   *  - 用 `screenX/screenY` 的**增量**而不是 `clientX`：窗口正跟着鼠标走，页面坐标会被
   *    一起拖回去，用 clientX 算出来的差值恒为 0（这类"看起来在动其实没动"的坑很典型）；
   *  - 只把增量交给壳（`movePetBy`），绝对坐标与出屏夹取都在主进程 —— 页面拿不到"把窗
   *    放到哪"的权力。
   *  - **按下/点击挂在根节点，不挂在色片上**：200×240 里色片只占中间 88 见方，两侧各 56px
   *    是透明的。实测从右边趴着的状态点它：悬停把窗口往屏内拉 96px，色片就从光标底下滑走了，
   *    而那时光标还在窗口内、只是落到了透明边上 —— 挂在色片上的 handler 收不到，用户看到的
   *    就是"我点它，它跑了"。透明区对用户是不可见的，整扇窗都该算"点的是桌宠"。
   */
  const dragRef = useRef<{ x: number; y: number } | null>(null);
  const dragMovedRef = useRef(false);

  function dragStart(event: React.PointerEvent<HTMLDivElement>) {
    if (insideUi(event)) return;
    dragMovedRef.current = false;
    dragRef.current = { x: event.screenX, y: event.screenY };
    event.currentTarget.setPointerCapture?.(event.pointerId);
    // 这里**不收面板**：按下不等于要拖。以前在这儿收一次，紧随其后的 click 又把面板开回来
    // ⇒ 一次来回跳（用户报的"点击桌宠也摇晃"）。收起挪到 `dragMove` 里，只有真拖起来才收。
  }

  function dragMove(event: React.PointerEvent<HTMLDivElement>) {
    const last = dragRef.current;
    if (!last) return;
    const dx = event.screenX - last.x;
    const dy = event.screenY - last.y;
    if (!dx && !dy) return;
    dragMovedRef.current = true;
    // 拖的时候先收起面板。两个理由，第二个是用户报的那个 bug：
    //  ① 拖的是宠物不是面板，摊着一块 380×520 的面板挡视野、还跟着晃；
    //  ② 壳按**窗口当前形状**夹取，面板开着时色片中心最远只能到离屏幕边 190px，
    //     而靠边隐藏判的是收起态那块矩形离边 ≤ 24px ⇒ 永远够不到，怎么拖都不吸。
    if (expandedRef.current) expand(false);
    dragRef.current = { x: event.screenX, y: event.screenY };
    shellBridge()?.movePetBy(dx, dy);
  }

  function dragEnd() {
    dragRef.current = null;
    // 松手才让壳判"要不要吸到边上"（§7.6）。零参数：吸哪条边由壳按当前工作区量，
    // 而移动过程中判会抖（一路拖过去会"吸上→拉开→吸上"）。旧壳没这个方法就是不吸，无害。
    shellBridge()?.petDragEnd?.();
    // `dragMovedRef` 留给紧随其后的 click 事件看完，下一次按下才清 —— 浏览器在拖完之后照样
    // 会补一个 click，不认这一点就是"拖完一松手面板自己弹出来了"。
  }

  function togglePanel() {
    if (dragMovedRef.current) return; // 刚拖完，松手那一下不算"点"
    expand(!expandedRef.current);
  }

  /** 点哪儿算"点桌宠"：色片那 88 见方，**外加壳能把窗挪走的距离**（见 `HIT_SLACK_*`）。
   *  宽限期里整扇窗都吃点击，所以这一层必须自己认清楚落点是不是它。 */
  function onPet(event: { clientX: number; clientY: number; target: EventTarget | null }): boolean {
    const el = spriteRef.current;
    if (!el) return false;
    const hit = event.target as HTMLElement | null;
    if (hit && (hit === el || el.contains(hit))) return true; // 就点在色片身上，不用量坐标
    const r = el.getBoundingClientRect();
    if (!r.width && !r.height) return true; // 量不到形状（无布局的环境）就按老规矩：算点它
    return (
      event.clientX >= r.left - HIT_SLACK_X &&
      event.clientX <= r.right + HIT_SLACK_X &&
      event.clientY >= r.top - HIT_SLACK_Y &&
      event.clientY <= r.bottom + HIT_SLACK_Y
    );
  }

  /** 根节点的点击分工：**"点开面板"这个动作只属于色片那一块（含上面那段位移余量）**。
   *
   *  以前是"整块窗都是桌宠"（200×240 里色片占中间 88，边上是 56px 透明带），点哪儿都算点它。
   *  2026-09-24 把窗改成常驻画布（560×520，为了根治展开重影）之后，"边上"变成了左右各 180px、
   *  上面 280px —— 于是"我明明没点到桌宠，点它旁边的桌面却调出了消息框"（用户报的这条）。
   *
   *  分工：色片那一带 = 开 / 合；摊着的时候点别处 = 收起来（点空白关闭，这条留着有用）；
   *  收起着的时候点别处 = **什么都不做**。
   */
  function rootClick(event: React.MouseEvent<HTMLDivElement>) {
    if (insideUi(event)) return; // 面板/气泡自己的按钮，见 `insideUi`
    if (onPet(event)) {
      togglePanel();
      return;
    }
    if (expandedRef.current) expand(false);
  }

  // 卸载时把待趴回的定时器收掉：留着它会在组件没了之后去调桥。
  useEffect(
    () => () => {
      if (retuckRef.current !== null) window.clearTimeout(retuckRef.current);
    },
    [],
  );

  // 壳推来的"面板要水平挪多少"：色片贴着屏幕边时，画布（560 宽）有一截本来就在屏外，
  // 面板得对齐到屏内那 380px。窗口不因此动一格 —— 旧写法是推窗口、再让色片在窗内自挪回去，
  // 那一次 setBounds 就是"展开时从色片上扩大出来的重影"的载体（DWM 会拉伸上一帧）。
  // 旧壳不推 ⇒ 一直是 0，面板照旧居中。
  useEffect(() => {
    const bridge = shellBridge();
    if (!bridge?.onPetPanelShift) return;
    bridge.onPetPanelShift((px) => setPanelShift(px));
    return () => bridge.onPetPanelShift?.(null);
  }, []);

  // 旗子的首值要 pull（push 早于监听就是丢消息），之后托盘每改一次收一次通知。
  // 注销放在清理里：React 严格模式下挂载两次，留着旧的会把上一次的 `setShowContent` 也带上。
  useEffect(() => {
    const bridge = shellBridge();
    if (!bridge?.petContentVisible) return;
    let alive = true;
    void bridge
      .petContentVisible()
      .then((value) => {
        if (!alive) return;
        showContentRef.current = value;
        setShowContent(value);
      })
      .catch(() => undefined); // 问不到就保持"显示"：这是调试入口和旧壳共同的默认，不是故障
    bridge.onPetContentVisible?.((value) => {
      if (!alive) return;
      showContentRef.current = value;
      setShowContent(value);
    });
    return () => {
      alive = false;
      bridge.onPetContentVisible?.(null);
    };
  }, []);

  // 语音（系统 TTS）。托盘「朗读消息」是唯一的写入口，页面只读 —— 与内容旗子同一个形状。
  // 为什么用 `window.speechSynthesis` 而不是引一个 TTS 库：这是**系统自带**的能力
  // （Chromium 走 Windows SAPI / 各平台原生引擎），零依赖、零许可问题、离线可用 ——
  // 与"复用设施"同一条纪律；要换更好的音色（Edge-TTS 等）时，换的是这一处实现。
  const voiceRef = useRef(false);
  useEffect(() => {
    const bridge = shellBridge();
    if (!bridge?.petVoiceEnabled) return;
    let alive = true;
    void bridge
      .petVoiceEnabled()
      .then((value) => {
        if (alive) voiceRef.current = value;
      })
      .catch(() => undefined); // 问不到就保持"不朗读"：安静比误读好
    bridge.onPetVoice?.((value) => {
      voiceRef.current = value;
    });
    return () => {
      alive = false;
      bridge.onPetVoice?.(null);
    };
  }, []);

  /** 读出这一句。**两道闸**：托盘语音开关 **且**「显示消息内容」开着 —— 声音和文字一样
   *  会把"你们聊了什么"播给屋里的人听，只藏字不藏声等于那面隐私旗子只糊了半张脸。 */
  function speak(text: string) {
    if (!voiceRef.current || !showContentRef.current) return;
    const clean = text.trim();
    if (!clean) return;
    const synth = window.speechSynthesis;
    // 没有 TTS 的环境（jsdom、极简系统）静默跳过：读不出来不该影响对话框本身。
    if (!synth || typeof SpeechSynthesisUtterance === "undefined") return;
    try {
      synth.cancel(); // 新的一句盖掉正在读的：两句叠着读最像故障
      const utter = new SpeechSynthesisUtterance(clean);
      // 语言跟着文档声明走（index.html 的 lang），不硬编码 zh-CN —— 换语言的角色卡
      // 不必重打包也能读对。拿不到就按中文（这个项目的主语言）。
      utter.lang = document.documentElement.lang || "zh-CN";
      utter.rate = 1.05;
      synth.speak(utter);
    } catch {
      // 系统没装语音引擎：静默 —— 出声失败不是故障级事件
    }
  }

  /** 把一份回放里**她最后说的那句**读出来（这一轮刚跑完时用）。 */
  function speakLastFrom(page: MessagePage) {
    const last = [...page.messages].reverse().find((m) => m.role === "assistant" && m.content);
    if (last?.content) speak(last.content);
  }

  // 卸载时把正在读的那句掐掉：驻留件不该在你让它消失之后还在说话。
  useEffect(() => () => window.speechSynthesis?.cancel(), []);

  // 历史只在**展开时**读：驻留件不该为了一个没被看到的面板每 10 秒打一次接口。
  // 一轮跑完的那次重读在 `send()` 里 inline 做（要等它到手才敢收气泡，见 `handoff`）。
  // 关掉「显示消息内容」时连读都不读：藏起来的东西不该只是不画，还留在页面里等着被看到。
  useEffect(() => {
    if (!expanded || !showContent) {
      setHistory(null);
      setHistoryTotal(0);
      historyShapeRef.current = ""; // 收起 = 基线作废：下次展开要认新读到那份
      setHistoryError("");
      return;
    }
    if (!threadId) return; // 还没有主动会话：面板显示"还没有你们的对话"，不发请求
    let alive = true;
    setHistoryError("");
    api
      .get<MessagePage>(messagesPath(threadId))
      .then((page) => {
        if (!alive) return;
        // 与 `refreshHistory` 同一份"收到一份回放就做什么"：展开那一次也不能只画历史
        // 不接在飞的那半句 —— 两处各写一遍的话，漂掉的总是"另一处"。
        setMirrorOf(page);
        historyShapeRef.current = shapeOf(page);
        handoff(page);
      })
      .catch((e: Error) => alive && setHistoryError(`历史没读到：${e.message}`));
    return () => {
      alive = false;
    };
  }, [expanded, threadId, showContent, handoff, setMirrorOf]);

  // 角色表跟着上面那条未读轮询一起重读（同一个 3 秒节奏，不另起一个定时器）。
  // 为什么不能只在起窗时读一次：形象是按角色解析的，而"换哪个角色用哪个形象"这件事
  // 是在**控制台那一侧**改的 —— 只读一次的症状就是"我明明选了雏雾，怎么还是这只"，
  // 且没有任何地方承认它听见了那个改动（挂着的那一只才是桌宠的常态）。
  // 拉失败**不清空**：手里那份角色表比"诚实显示成默认包"更有用，未读那条自己会说掉线。
  usePoll(() => {
    api
      .get<RoleCard[]>("/api/roles")
      .then(setRoles)
      .catch(() => {
        // 保持上一次读到的那份：少一个切换器不拦对话本身。
      });
  }, UNREAD_POLL_MS);

  // 形象包清单也是起窗一次。换数据源不用重读：那个切换是整页 reload。
  // 读失败记 null —— 旧后端根本没有这条端点，此刻的表现必须与做这件事之前一字不差。
  useEffect(() => {
    api
      .get<PetPackListing>("/api/pets")
      // 只认"里面有 packs 数组"那一种形状：对面是旧后端 / 反代返回 HTML / 测试替身给了
      // 空数组时，一律按"清单没读到"办（画随包默认图），而不是把垃圾喂进解析函数。
      .then((body) => setPacks(Array.isArray(body?.packs) ? body : null))
      .catch(() => setPacks(null));
  }, []);

  /**
   * 发一条：桌宠上的回话落进**这个角色的主动会话**（§7.2.1 拍定的那条线）。
   *
   * 三个不显然的点：
   *  - **不新建会话**：那条线程不存在时按确定 id 幂等地建出来（`/api/session/proactive`），
   *    而不是 `POST /api/session` 开一条随机的 —— 后者会让"角色记得的"和"你说的"分家。
   *  - **收起不打断**：鼠标移开只是把面板收起来，正在跑的这一轮继续到底（`streamChat` 的
   *    signal 只在点「停止」时才 abort）。驻留件不该因为手一抖就丢掉一个回答。
   *  - 发之前先标已读：你正在回它的话，"看见 = 读过"在这里成立。标失败不拦发送。
   *  - **收尾必须等回放到手再收气泡**：见 `handoff`。先前这里直接把乐观那句清掉、把刷新
   *    丢给一个计数器，于是回放里那句和气泡里那句并排画了两遍（"我发一条消息，他回两条"）。
   */
  async function send() {
    const text = draft.trim();
    if (!text || busy || sendingRef.current || !activeRole) return;
    sendingRef.current = true;
    setDraft("");
    setPendingUser(text);
    setStreamError(""); // 上一轮的错误不能一直挂着：它会跟着回放一起被读成"这一轮又出事了"
    let usedTid = threadId;
    try {
      // 进对话 = 都看过（用户 2026-09-23 定的口径）：不再是"只标这一个角色"。
      void api.markAllReachoutsRead().catch(() => undefined);
      if (!usedTid) {
        const ensured = await api.post<{ thread_id: string }>("/api/session/proactive", {
          role_id: activeRole,
        });
        usedTid = ensured.thread_id;
      }
      setBusy(true);
      busyRef.current = true;
      const controller = startBubble(usedTid);
      try {
        await streamChat(usedTid, text, onEvent, controller.signal);
      } finally {
        setBusy(false);
        busyRef.current = false;
      }
    } catch (e) {
      setStreamError((e as Error).message);
    } finally {
      sendingRef.current = false;
      // 服务端 checkpoint 是对话的唯一真相：一轮跑完重读一次，面板显示的就是库里真存下的东西。
      await load();
      if (!usedTid) {
        setPendingUser(""); // 线程都没建起来（第一句就失败）：没有回放可等，乐观那条自己收掉
        return;
      }
      try {
        const page = await api.get<MessagePage>(messagesPath(usedTid));
        handoff(page);
        // 这轮她说的那句读出来（`speak` 里判两道闸）。与气泡收了才读同一个顺序：
        // 回放到手 = 这一轮已经落库，读的才是"她真的说过的那一句"。
        speakLastFrom(page);
      } catch (e) {
        // 读不到回放就**留着**气泡与乐观那条：宁可屏幕上重一遍，也不能让用户以为"我说的话没了"。
        setHistoryError(`历史没读到：${(e as Error).message}`);
      }
    }
  }

  /** 重新生成这一轮：走对话页**同一条通道**（`POST /api/session/{tid}/messages/edit`，
   *  编辑保存即"从那条用户消息重问、丢弃其后的历史"），不在桌宠里另写第二套重问逻辑。
   *  要的是"触发这一轮的那一句"，所以从右击那一行往回找最近的一条 user 消息。 */
  async function regenerateFrom(messageId: string | undefined) {
    if (!threadId || busy || sendingRef.current || !messageId) return;
    const index = (history ?? []).findIndex((m) => m.id === messageId);
    const trigger = index > 0
      ? history?.slice(0, index).reverse().find((m) => m.role === "user" && m.id)
      : undefined;
    if (!trigger?.id || !trigger.content) return;
    sendingRef.current = true;
    setStreamError("");
    setBusy(true);
    busyRef.current = true;
    try {
      const controller = startBubble(threadId);
      try {
        await streamEdit(
          threadId,
          trigger.id,
          trigger.content,
          onEvent,
          controller.signal,
          trigger.image ?? null,
        );
      } finally {
        setBusy(false);
        busyRef.current = false;
      }
      const page = await api.get<MessagePage>(messagesPath(threadId));
      handoff(page);
      speakLastFrom(page); // 重生那版也要读出来：屏幕上换了内容、声音还念旧的就是两份事实
    } catch (e) {
      setStreamError(`重新生成失败：${(e as Error).message}`);
    } finally {
      sendingRef.current = false;
    }
  }

  /** 右键落在哪一行：`.closest("p")`（行上有 `aria-label`，历史行还有 `data-mid`）。 */
  function openMenu(event: React.MouseEvent<HTMLDivElement>) {
    event.preventDefault(); // 不给浏览器/壳留默认菜单的机会
    const row = (event.target as HTMLElement).closest("p");
    const selected = window.getSelection()?.toString().trim() ?? "";
    const rowText = row?.textContent?.trim() ?? "";
    const copy = (text: string) => {
      if (text) navigator.clipboard?.writeText(text).catch(() => setStreamError("复制没成功：剪贴板被拦了"));
    };
    const entries: MenuEntry[] = [
      { label: "复制选中的文字", disabled: !selected, run: () => copy(selected) },
      { label: "复制整条消息", disabled: !rowText, run: () => copy(rowText) },
    ];
    // 「重新生成」只对**已落库的她那句**提：正在流的那一行还没有 id，重问它等于打断自己；
    // 而这一轮正在跑的时候它也在，只是点不动（灰着比消失更诚实："现在不能，等它完"）。
    const mid = row?.getAttribute("data-mid") || undefined;
    if (row?.getAttribute("aria-label") === "对方说" && mid) {
      entries.push({ label: "重新生成这一轮", disabled: busy, run: () => void regenerateFrom(mid) });
    }
    if (busy) entries.push({ label: "停止这一轮生成", run: stop });
    if (entries.every((entry) => entry.disabled)) return; // 全灰的菜单不如不弹
    setMenu({ x: event.clientX, y: event.clientY, entries });
  }

  async function acknowledge() {
    // 先标已读再收气泡：标失败就留着，让用户知道"这条还没真被读过"。
    // 标的是**所有**未读（用户 2026-09-23 定的口径），所以这里不再需要那一条是谁的。
    try {
      await api.markAllReachoutsRead();
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
      await acknowledge();
      return;
    }
    try {
      await api.markAllReachoutsRead();
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
    // **根节点不再标 `-webkit-app-region: drag`**：拖动早就改成手动了（见 `dragMove`），而
    // drag 区域会把鼠标事件整个吞掉 —— 连"指针离开了这扇窗"都收不到，而"趴回去"的判据正
    // 需要它。悬停也因此能挂在根上：它只负责把宠物从边上拉出来，不弹面板。
    <div
      className="flex h-full select-none flex-col items-center justify-end gap-2 pb-1"
      onMouseEnter={reveal}
      onMouseLeave={pointerLeft}
      onPointerDown={dragStart}
      onPointerMove={dragMove}
      onPointerUp={dragEnd}
      onPointerCancel={dragEnd}
      onClick={rootClick}
    >
      {expanded ? (
        // 展开态：面板取代气泡（气泡那条就是面板最后一条，重复摆一遍只是噪音）。
        // `min-h-0` 是这条链路上唯一"看得见"的修复：根节点是 `justify-end` 的一列，形象那一格
        // 已经 `shrink-0`，而面板默认 `min-height:auto` 不许被压到内容高度以下 —— 于是"面板 + 形象"
        // 一旦高出窗口，多出来的部分是往**窗口上沿之外**溢出的，表头（连同换角色的下拉框）整条
        // 看不见，症状是"切换不了角色"，而 DOM 里它一直都在。给了 `min-h-0` 之后溢出由面板自己
        // 吸收（列表内部本来就能滚），内容短时仍然贴着内容长。
        <section
          data-pet-ui="panel"
          className="pet-panel-in pet-nodrag flex max-h-full min-h-0 w-[380px] flex-col rounded-2xl border border-slate-200 bg-white text-slate-700 shadow-md dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100"
          /* 屏内对齐：见上面 `panelShift`。窗口没动，动的是这一块在画布里的位置。 */
          style={{ transform: `translateX(${panelShift}px)` }}
        >
          <header className="flex shrink-0 items-center justify-between gap-2 border-b border-slate-100 px-3 py-2 dark:border-slate-700">
            {roles.length > 1 ? (
              <select
                value={activeRole ?? ""}
                onChange={(e) => {
                  setPicked(e.target.value);
                  setHistory(null);
                  setHistoryTotal(0); // 换角色 = 换一条会话，上一角色的"上面还有 N 条"不能跟着搬
                  historyShapeRef.current = "";
                }}
                className="max-w-[150px] truncate rounded border border-slate-200 bg-white px-1 py-0.5 text-xs dark:border-slate-600 dark:bg-slate-800"
                title="换个工作台对象（每个角色是它自己的那条主动会话）"
              >
                {roles.map((r) => (
                  <option key={r.role_id} value={r.role_id}>
                    {r.role_name}
                  </option>
                ))}
              </select>
            ) : (
              <span className="truncate text-xs font-medium">{activeName}</span>
            )}
            <div className="flex shrink-0 items-center gap-2">
              {threadId && (
                <button
                  onClick={() => shellBridge()?.openSession(threadId)}
                  className="text-[10px] text-blue-600 hover:underline dark:text-blue-400"
                  title="在控制台里打开这条会话（能翻完整历史、能发图）"
                >
                  在控制台打开
                </button>
              )}
              {/* 面板是点击打开的，就得给一个显式的关法（再点色片也行）。悬停不再负责关它 ——
                  那正是"移动窗口 ⇒ 把自己挪出光标 ⇒ 自激晃动"的来源。 */}
              <button
                onClick={() => expand(false)}
                className="px-1 text-[11px] leading-none text-slate-400 hover:text-slate-600 dark:text-slate-500 dark:hover:text-slate-300"
                title="收起面板（再点一下色片也能开合）"
                aria-label="收起面板"
              >
                ✕
              </button>
            </div>
          </header>
          {/* 状态行：这一格是"我在场子里连的是哪份 + 她此刻为什么没说话"。
              只读、不点、不折叠 —— 切回本机那个开关只在侧栏（同一个开关只出现一次，M5），
              这行只是让桌宠不再假装不存在"云端"这件事。按设计稿 §6：只在**显示内容**时画，
              否则"内容已隐藏"那半句话的意义会被这行云端地址搅浑。 */}
          {showContent && (
            <div className="flex shrink-0 items-center gap-2 border-b border-slate-100 px-3 py-1 dark:border-slate-700">
              <span className="shrink-0 text-[10px] text-slate-400 dark:text-slate-500">
                {cloud ? `☁ 云端 · ${cloud.user}` : "● 本机"}
              </span>
              {myQuiet && (
                <div className="min-w-0 flex-1 truncate">
                  <QuietLine q={myQuiet} />
                </div>
              )}
            </div>
          )}
          <div
            ref={scrollRef}
            onContextMenu={openMenu}
            /* `select-text` 是把根节点那块 `select-none` 在面板里取消掉：根上标它是为了拖桌宠
               时别拉出一段橡皮筋选区，但它连"把她说的话选出来复制"一起禁了（用户 09-26 问的
               就是这个）。选中不会把宠物拖走也不会收起面板 —— `dragStart` 与 `rootClick`
               开头都有 `insideUi` 那道闸（面板整块在 `[data-pet-ui]` 里）。 */
            className="max-h-[300px] min-h-0 flex-1 select-text space-y-1.5 overflow-y-auto px-3 py-2 text-[11px] leading-relaxed"
          >
            {historyError && <p className="text-red-600 dark:text-red-400">{historyError}</p>}
            {streamError && <p className="text-red-600 dark:text-red-400">{streamError}</p>}
            {!showContent && (
              // 关掉旗子时这里一个字的对话都不画（连历史都没去读，见上面那个 effect）。
              // 但"它在不在说话"仍然要说 —— 隐藏内容不等于把驻留件变成没有反馈的黑盒。
              <p className="text-slate-400 dark:text-slate-500">
                {busy
                  ? "它正在回话…（内容已隐藏）"
                  : `内容已隐藏${unreadOfActive ? ` · ${unreadOfActive} 条未读` : ""} —— 在控制台里读，或到托盘勾回「显示消息内容」。`}
              </p>
            )}
            {showContent && !threadId && !historyError && !pendingUser && (
              <p className="text-slate-400 dark:text-slate-500">
                还没有你们的对话 —— 说第一句就会开始（它会记进这个角色的记忆与这条会话）。
              </p>
            )}
            {showContent && threadId && history === null && !historyError && !pendingUser && (
              <p className="text-slate-400 dark:text-slate-500">读取中…</p>
            )}
            {/* 面板只画最近 `PANEL_MESSAGES` 条，而"一共有几条"是后端 `total` 报的。不写这一行，
                这块小窗就在冒充"你们的全部对话"（用户 09-26 问的正是这个：那条会话实量 44 条，
                这里只剩最近一截，还一声不吭）。没有壳（浏览器里直接开这页）就只说数目，
                不给一个点了没反应的"打开控制台"。 */}
            {showContent && history && historyTotal > history.length && (
              shellBridge() ? (
                <button
                  type="button"
                  onClick={() => threadId && shellBridge()?.openSession(threadId)}
                  className="block w-full rounded border border-slate-200 bg-slate-50 px-2 py-1 text-left text-[10px] text-slate-500 hover:text-blue-600 dark:border-slate-700 dark:bg-slate-800/60 dark:text-slate-400"
                  title={`这里只放最近 ${PANEL_MESSAGES} 条 —— 点进去就是同一条会话，不是另一份`}
                >
                  ↑ 上面还有 {historyTotal - history.length} 条 · 在控制台看全部
                </button>
              ) : (
                <p className="text-[10px] text-slate-400 dark:text-slate-500">
                  ↑ 上面还有 {historyTotal - history.length} 条（这里只放最近 {PANEL_MESSAGES} 条）
                </p>
              )
            )}
            {showContent &&
              (history ?? [])
                .filter((m) => m.role === "user" || m.role === "assistant")
                .map((m, i) => (
                  // `data-mid` 是给「重新生成这一轮」定位用的：编辑走的是消息 id，而这一行
                  // 在屏幕上的位置不是 id（回放顺序会变）。只有历史行有 id，乐观那条与正在流
                  // 的那一条没有 ⇒ 它们的菜单项自然置灰。
                  <p key={m.id ?? i} data-mid={m.id} {...rowOf(m.role === "user")}>
                    {m.content}
                  </p>
                ))}
            {showContent && pendingUser && (
              <p {...rowOf(true)}>{pendingUser}</p>
            )}
            {showContent && live && (busy || live.text || live.thinking) && (
              <div>
                {/* 思考过程与对话页**同一个组件**（用户 09-26："桌宠那侧也要看思考过程，
                    和对话界面差不多"）：本地档那十几到几十秒全花在思考上（09-26 轮 R26-29），
                    没有这一格就是一块不动的泡。规矩照抄对话页 —— 流式期间展开（"她在打字"
                    本身就是信号，不再另加省略号），本轮结束就折叠。`details` 是不受控的，
                    光改 defaultOpen 收不起来，所以用 key 让它重挂一次。 */}
                <ThinkingPanel
                  key={busy ? "thinking-live" : "thinking-done"}
                  text={live.thinking}
                  defaultOpen={busy}
                />
                <p {...rowOf(false)}>
                  {live.text || (live.streaming && !live.thinking ? "…" : "")}
                </p>
              </div>
            )}
            {/* 别处那一扇窗发起、此刻还在生成的那半句（R26-38）。与对话页那格同一个判据：
                后端在飞登记非空 且 这一扇窗没在流。措辞只说数据支持的这一句 ——
                登记里没有"来源"，所以不写"来自控制台"。 */}
            {showContent && !busy && mirror !== null && (
              <div data-testid="pet-inflight-mirror">
                <p {...rowOf(false)}>{mirror || "对方在说…"}</p>
                <p className="mt-0.5 text-[10px] text-slate-400 dark:text-slate-500">
                  正在生成 · 不是这里发起的
                </p>
              </div>
            )}
          </div>
          <div className="flex shrink-0 items-end gap-1.5 border-t border-slate-100 px-2 py-2 dark:border-slate-700">
            <textarea
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                // Enter 发、Shift+Enter 换行（与对话页同一套键位）；输入法组合期间不发。
                if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                  e.preventDefault();
                  void send();
                }
              }}
              rows={1}
              placeholder={`跟${activeName}说一句`}
              className="max-h-16 min-w-0 flex-1 resize-none rounded-lg border border-slate-200 bg-white px-2 py-1 text-[11px] leading-relaxed focus:border-blue-400 focus:outline-none dark:border-slate-600 dark:bg-slate-900"
            />
            {busy ? (
              <button
                onClick={stop}
                className="shrink-0 rounded-lg border border-slate-200 px-2 py-1 text-[11px] text-slate-500 hover:text-red-600 dark:border-slate-600 dark:text-slate-300"
                title="停止这一轮生成"
              >
                停止
              </button>
            ) : (
              <button
                onClick={() => void send()}
                disabled={!draft.trim() || !activeRole || offline}
                className="shrink-0 rounded-lg bg-blue-600 px-2.5 py-1 text-[11px] text-white hover:bg-blue-500 disabled:bg-slate-300 dark:disabled:bg-slate-700"
              >
                发送
              </button>
            )}
          </div>
        </section>
      ) : (
        latest &&
        !faded && (
          <button
            data-pet-ui="bubble"
            onClick={() => void openRow(latest)}
            title={
              !showContent
                ? "内容已隐藏 —— 托盘里勾回「显示消息内容」就能看到（点开仍然会拉起控制台）"
                : latest.thread_id
                  ? unreadOfLatest > 1
                    ? `打开与 ${name} 的对话（还有 ${unreadOfLatest - 1} 条未读）`
                    : `打开与 ${name} 的对话`
                  : "点击标记已读"
            }
            // 底色必须是**不透明**的，也别加 backdrop-blur：这扇窗是 transparent 窗，
            // 半透明透出来的是桌面本身（壁纸/图标/底下那个窗口的字），blur 也没有东西可糊
            // （backdrop-filter 只看页面自己身后那层），用户报的"重影"就是这两样叠出来的。
            // 同理不再淡出 opacity —— 淡出的那 500ms 就是一块 500ms 的半透明。
            className="pet-nodrag w-[200px] rounded-2xl border border-slate-200 bg-white px-3 py-2 text-left text-[11px] leading-relaxed text-slate-700 shadow-sm dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100"
            // 宽度按锚点那块（200）而不是 w-full：画布是 560 宽，w-full 会把气泡拉成一条
            // 横穿桌面的白带子（它还是不透明白底 ⇒ 读起来像屏幕被划了一道）。
          >
            {showContent ? shorten(latest.text) : "有新消息 · 内容已隐藏"}
            {unreadOfLatest > 1 && (
              <span className="ml-1 rounded-full bg-blue-600 px-1.5 text-[10px] text-white">
                +{unreadOfLatest - 1}
              </span>
            )}
          </button>
        )
      )}

      {/* 形象本体：`PetSprite` 渲染 waifu spritesheet（素材放进 frontend/public/pets/ 即换装），
          没有素材或加载失败时回退 `GeometricPet` —— 兜底这只同样由 `petStatus` 驱动呼吸/
          眨眼/说话/思考。**这一块就是原来的 88 色片**：`.pet-nodrag`（命中区上报）、
          `spriteRef`（点它算点桌宠）、title（右侧菜单与测试都按它认脸）全部原样保留。
          尺寸从 88 提到 160×184 —— 形象本身就"大了一圈"，hit-slack 的量是按窗口能挪多远
          算的、不随形象尺寸变，命中照旧。它自己不挂事件处理器（点/拖都在根节点，一次冒泡
          只触发一遍）。 */}
      <div
        ref={spriteRef}
        className="pet-nodrag shrink-0 cursor-grab active:cursor-grabbing"
        title={`${name}${offline ? " · 连不上本机程序" : ""} · 点开看你们最近聊了什么${
          showContent ? "" : "（内容已隐藏）"
        }`}
      >
        {drawn?.kind === "live2d" ? (
          <Live2dPet
            url={drawn.sheet_url}
            status={petStatus}
            motions={drawn.motions}
            width={160}
            height={184}
            onError={(reason) => setLive2dFailure({ id: drawn.id, reason })}
          />
        ) : (
        <PetSprite
          status={petStatus}
          rows={drawn?.rows}
          width={160}
          height={184}
          className="drop-shadow-md"
          // 这个角色的形象包（`role_card.pet_pack` → `/api/pets` 那份清单）。素材有两处：
          // 随包的 `frontend/dist/pets/`（仓库自绘，许可干净）与数据根下的 `<数据根>/pets/`
          // （用户自己放的，升级不冲、不入库），同名时后者赢。`pack` 为 null 只在"清单读到了
          // 而里面一个可用包都没有"时发生 —— 那才落 SVG 兜底。
          src={drawn?.sheet_url}
        />
        )}
      </div>

      {failedLive2d && (
        // 模型在清单里、画不出来：说的是原因，不是"没找到"（那是另一行）。
        <span className="text-[10px] text-amber-600 dark:text-amber-400">
          Live2D 没画出来：{failedLive2d}
        </span>
      )}
      {looked.misassigned && (
        // 配了、但清单里没有：这一行不能省。否则症状是"我明明选了爱莉，怎么还是那只团子"，
        // 而没有任何地方承认它听见了这个选择。
        <span className="text-[10px] text-amber-600 dark:text-amber-400">形象包没找到，先用默认</span>
      )}

      {offline && (
        <span className="text-[10px] text-amber-600 dark:text-amber-400">连不上本机程序</span>
      )}
      {menu && (
        <PetContextMenu x={menu.x} y={menu.y} entries={menu.entries} onClose={() => setMenu(null)} />
      )}
    </div>
  );
}
