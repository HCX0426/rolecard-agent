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

import { api, streamChat, type MessagePage, type MessageRow, type ReachoutRow, type RoleCard } from "../api";
import { useChatStream } from "../hooks/useChatStream";
import { shellBridge } from "../lib/shell";
import { type StreamMeta } from "../lib/stream";

const POLL_MS = 10_000; // 与铃铛红点同一节奏：后端没有推送，如实降级为轮询
const BUBBLE_MS = 30_000; // 气泡自己淡出：驻留件不该把一句话长期戳在桌面上
const MAX_BUBBLE_CHARS = 64;
/** 指针离开桌面件多久之后，趴着的才自己回去：给"划过它身上"留的宽限。 */
const RETUCK_MS = 900;
/** 面板里摊开最近几条（再多就该去控制台翻了）。 */
const PANEL_MESSAGES = 8;

/** 读这条主动会话的最近几条。URL 只写一处：展开时与一轮跑完两条路径必须读同一个东西。 */
const messagesPath = (tid: string) => `/api/session/${tid}/messages?limit=${PANEL_MESSAGES}`;

/** role_id → 稳定色相。同一个角色的色片跨设备/跨主题都一样，认脸靠它。 */
function hueOf(roleId: string): number {
  let hash = 0;
  for (const ch of roleId) hash = (hash * 31 + ch.codePointAt(0)!) % 360;
  return hash;
}

function shorten(text: string): string {
  return text.length > MAX_BUBBLE_CHARS ? `${text.slice(0, MAX_BUBBLE_CHARS)}…` : text;
}

/**
 * 一行前面的说话人。写成组件是因为这块小面板里"谁说的"不能只靠颜色区分 ——
 * 色弱用户和截图里都读不出来，而"它说的"和"我说的"混在一起正是这类气泡最容易出事的地方。
 */
function Speaker({ mine }: { mine: boolean }) {
  return <span className="text-slate-400 dark:text-slate-500">{mine ? "你：" : "它："}</span>;
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
  const [roles, setRoles] = useState<RoleCard[]>([]);
  const [picked, setPicked] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  // 发出去但还没落库的那句：流结束后以服务端回放为准，所以它只活在这一轮里。
  const [pendingUser, setPendingUser] = useState("");
  const [streamError, setStreamError] = useState("");
  // 托盘「显示消息内容」的旗子。**默认显示**：拿不到旗子（浏览器里开这页、或界面跑在还没
  // 有这面旗子的旧壳里）时不该把功能藏起来，那等于用一个用户找不到的开关把他锁在门外。
  const [showContent, setShowContent] = useState(true);
  // 同一个值的"读时不重订阅"版本：`load` 是轮询回调，依赖里加旗子会让每次改开关都
  // 把 10 秒的节拍重置一遍（而它只是想知道这次要不要拍原文）。
  const showContentRef = useRef(true);
  const retuckRef = useRef<number | null>(null);
  // `expanded` 的"读时不重渲染"版本：拖拽期间 pointermove 密集触发，而 React 状态要到下一次
  // 渲染才更新，只认状态就会在一次拖里连发十几次"收起"给壳。
  const expandedRef = useRef(false);
  /**
   * 色片在窗口里自己挪回原位多少像素（壳推过来的，贴边展开时才非零）。
   *
   * 为什么页面要接这个数：色片贴着右边趴着时它中心离屏幕右沿只有 100px，而面板要 380 宽 ——
   * 居中放不下，壳夹取时必然把整扇窗往屏内推（实测推掉 138px），于是"一点它，色片就从光标
   * 底下滑走了"。改成**窗口照夹取、色片在窗内自挪**：面板朝屏幕内侧长，色片一格都不动。
   * 旧壳不推这个数 ⇒ 保持 0，等于回到"色片在窗正中"那个老画法，不会更坏。
   */
  const [spriteShift, setSpriteShift] = useState(0);
  // 已经"见过"的最新一条 id。**在第一次真正拿到快照之前保持 null**：初始的空白状态不是
  // 一次快照，拿它当基线会让每次开机都把积压的最后一条当新消息拍出去。
  const seenNewestRef = useRef<number | null>(null);

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
    (messages: MessageRow[]) => {
      setHistory(messages);
      if (busyRef.current) return; // 还在流：气泡是唯一的实时反馈，不能被一次回放吃掉
      setLive(null);
      liveRef.current = null;
      setPendingUser("");
    },
    [setLive, liveRef],
  );

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
      // 旗子关掉时连系统通知也不拍原文：toast 是桌面上最显眼的一块"别人也能读"的内容，
      // 让它绕过隐藏就等于这个开关只糊住了半张脸。谁找你了照样说清（标题是角色名）。
      shellBridge()?.notify(
        arrived.role_name || "主动消息",
        showContentRef.current ? shorten(arrived.text) : "内容已隐藏",
        arrived.thread_id,
      );
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
  /**
   * 面板跟谁说话：手动选的 > 最近主动找你的 > 角色表里的第一个。
   *
   * 为什么要兜到"角色表第一个"：一个从没被主动找过的新用户也该能在桌宠上开口，
   * 而那时 `latest` 是空的 —— 只按气泡定角色会让面板变成一只不能说话的摆件。
   */
  const activeRole = picked ?? latest?.role_id ?? roles[0]?.role_id ?? null;
  const activeName =
    roles.find((r) => r.role_id === activeRole)?.role_name ??
    latest?.role_name ??
    latest?.role_id ??
    "助手";
  const hue = hueOf(activeRole ?? "general_assistant");
  // 只有"当前这个角色"的那条主动会话能直接读历史；换了角色就得先 ensure（见 `send`）。
  const threadId = latest && latest.role_id === activeRole ? latest.thread_id ?? null : null;
  const name = activeName;
  // 隐藏内容时面板要说"有几条没读"，那数的是**当前对象**的（切到别的角色就不是那一堆了）。
  const unreadOfActive = activeRole ? unread.filter((row) => row.role_id === activeRole).length : 0;

  /** 展开/收起面板。由**点击**触发（悬停不算，理由见下面 `reveal`）。
   *  桥不在（浏览器直接开 #/pet 的调试入口）时面板照样画，只是窗口不跟着变大。 */
  function expand(next: boolean) {
    if (expandedRef.current === next) return; // 一次拖拽里连着十几次 pointermove：只发一次
    expandedRef.current = next;
    setExpanded(next);
    void shellBridge()?.setPetExpanded(next);
  }

  /** 只在"画了东西的地方"吃桌面点击（审计 §12.3）。
   *
   *  200×240 里色片只占中间 88 见方，两侧各 56px 是全透明的，可整扇窗都在吃鼠标 ——
   *  结果那两条带上面的桌面图标点不到（用户 2026-09-22 就问过）。壳把窗设成"忽略鼠标但
   *  仍转发 mousemove"，于是这页还看得见光标，由它判"光标在不在有像素的元素上"
   *  （`.pet-nodrag` 那三块：色片、气泡、面板），只回一个布尔。
   *  浏览器里开 #/pet、或新界面跑在旧壳里（没这两个方法）时一行都不做 ⇒ 保持整窗吃点击。
   */
  const clickableRef = useRef(true);

  function reportClickable(target: EventTarget | null) {
    const report = shellBridge()?.petClickable;
    if (!report) return; // 浏览器里开 #/pet、或旧壳：不判，整窗照旧吃点击
    const el = target as HTMLElement | null;
    const hot = Boolean(el?.closest?.(".pet-nodrag"));
    if (hot === clickableRef.current) return; // 一次移动几十帧：只在变化时报
    clickableRef.current = hot;
    report(hot);
  }

  useEffect(() => {
    const arm = shellBridge()?.petHitTest;
    if (!arm) return;
    arm(true);
    return () => {
      // 卸载 = 没人判了，让壳立刻回到"整窗吃点击"（它也会在本次导航自己复位，两遍都幂等）。
      arm(false);
      clickableRef.current = true;
    };
  }, []);

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

  function rootClick(event: React.MouseEvent<HTMLDivElement>) {
    if (insideUi(event)) return; // 面板/气泡自己的按钮，见 `insideUi`
    togglePanel();
  }

  // 卸载时把待趴回的定时器收掉：留着它会在组件没了之后去调桥。
  useEffect(
    () => () => {
      if (retuckRef.current !== null) window.clearTimeout(retuckRef.current);
    },
    [],
  );

  // 壳每次把展开落点算歪一点，就推一次自挪量过来（旧壳不推 ⇒ 一直是 0，见 `spriteShift`）。
  useEffect(() => {
    const bridge = shellBridge();
    if (!bridge?.onPetSpriteShift) return;
    bridge.onPetSpriteShift((px) => setSpriteShift(px));
    return () => bridge.onPetSpriteShift?.(null);
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

  // 历史只在**展开时**读：驻留件不该为了一个没被看到的面板每 10 秒打一次接口。
  // 一轮跑完的那次重读在 `send()` 里 inline 做（要等它到手才敢收气泡，见 `handoff`）。
  // 关掉「显示消息内容」时连读都不读：藏起来的东西不该只是不画，还留在页面里等着被看到。
  useEffect(() => {
    if (!expanded || !showContent) {
      setHistory(null);
      setHistoryError("");
      return;
    }
    if (!threadId) return; // 还没有主动会话：面板显示"还没有你们的对话"，不发请求
    let alive = true;
    setHistoryError("");
    api
      .get<MessagePage>(messagesPath(threadId))
      .then((page) => alive && handoff(page.messages))
      .catch((e: Error) => alive && setHistoryError(`历史没读到：${e.message}`));
    return () => {
      alive = false;
    };
  }, [expanded, threadId, showContent, handoff]);

  // 角色表只在第一次展开时拉：面板顶上的切换要用它，而收起时没必要占一次请求。
  useEffect(() => {
    if (!expanded || roles.length) return;
    api
      .get<RoleCard[]>("/api/roles")
      .then(setRoles)
      .catch(() => setRoles([])); // 拉不到就少一个切换器，不拦对话本身
  }, [expanded, roles.length]);

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
      void api.markRoleReachoutsRead(activeRole).catch(() => undefined);
      if (!usedTid) {
        const ensured = await api.post<{ thread_id: string }>("/api/session/proactive", {
          role_id: activeRole,
        });
        usedTid = ensured.thread_id;
      }
      setBusy(true);
      busyRef.current = true;
      const controller = startBubble();
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
        handoff((await api.get<MessagePage>(messagesPath(usedTid))).messages);
      } catch (e) {
        // 读不到回放就**留着**气泡与乐观那条：宁可屏幕上重一遍，也不能让用户以为"我说的话没了"。
        setHistoryError(`历史没读到：${(e as Error).message}`);
      }
    }
  }

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
    // **根节点不再标 `-webkit-app-region: drag`**：拖动早就改成手动了（见 `dragMove`），而
    // drag 区域会把鼠标事件整个吞掉 —— 连"指针离开了这扇窗"都收不到，而"趴回去"的判据正
    // 需要它。悬停也因此能挂在根上：它只负责把宠物从边上拉出来，不弹面板。
    <div
      className="flex h-full select-none flex-col items-center justify-end gap-2 pb-1"
      onMouseEnter={reveal}
      onMouseMove={(event) => reportClickable(event.target)}
      onMouseLeave={() => {
        reportClickable(null);
        pointerLeft();
      }}
      onPointerDown={dragStart}
      onPointerMove={dragMove}
      onPointerUp={dragEnd}
      onPointerCancel={dragEnd}
      onClick={rootClick}
    >
      {expanded ? (
        // 展开态：面板取代气泡（气泡那条就是面板最后一条，重复摆一遍只是噪音）。
        <section
          data-pet-ui="panel"
          className="pet-panel-in pet-nodrag flex max-h-full w-full flex-col rounded-2xl border border-slate-200/70 bg-white/95 text-slate-700 shadow-md backdrop-blur-sm dark:border-slate-600/70 dark:bg-slate-800/95 dark:text-slate-100"
        >
          <header className="flex shrink-0 items-center justify-between gap-2 border-b border-slate-100 px-3 py-2 dark:border-slate-700">
            {roles.length > 1 ? (
              <select
                value={activeRole ?? ""}
                onChange={(e) => {
                  setPicked(e.target.value);
                  setHistory(null);
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
          <div className="max-h-[300px] min-h-0 flex-1 space-y-1.5 overflow-y-auto px-3 py-2 text-[11px] leading-relaxed">
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
            {showContent &&
              (history ?? [])
                .filter((m) => m.role === "user" || m.role === "assistant")
                .map((m, i) => (
                  <p key={m.id ?? i} className="break-words">
                    <Speaker mine={m.role === "user"} />
                    {m.content}
                  </p>
                ))}
            {showContent && pendingUser && (
              <p className="break-words">
                <Speaker mine />
                {pendingUser}
              </p>
            )}
            {showContent && live && (busy || live.text) && (
              <p className="break-words">
                <Speaker mine={false} />
                {live.text || (live.streaming ? "…" : "")}
              </p>
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
            className="pet-nodrag w-full rounded-2xl border border-slate-200/70 bg-white/90 px-3 py-2 text-left text-[11px] leading-relaxed text-slate-700 shadow-sm backdrop-blur-sm transition-opacity duration-500 dark:border-slate-600/70 dark:bg-slate-800/90 dark:text-slate-100"
            // 气泡跟着色片走：它标在色片正上方，色片自挪而它不动就会错开一截。
            style={{ transform: `translateX(${spriteShift}px)` }}
          >
            {showContent ? shorten(latest.text) : "它说了话 · 内容已隐藏"}
            {unreadOfLatest > 1 && (
              <span className="ml-1 rounded-full bg-blue-600 px-1.5 text-[10px] text-white">
                +{unreadOfLatest - 1}
              </span>
            )}
          </button>
        )
      )}

      {/* 色片自己**不挂**任何处理器：点击与拖拽都在根节点上（透明边也要接得住，
          见 `dragStart` 那段）。挂两处会因冒泡触发两遍，净效果是"点了没反应"。 */}
      <div
        className="pet-nodrag grid h-[88px] w-[88px] shrink-0 cursor-grab place-items-center rounded-full text-2xl font-medium text-white shadow-md active:cursor-grabbing"
        style={{ background: `hsl(${hue} 62% 48%)`, transform: `translateX(${spriteShift}px)` }}
        title={`${name}${offline ? " · 连不上本地服务" : ""} · 点开看你们最近聊了什么${
          showContent ? "" : "（内容已隐藏）"
        }`}
      >
        {name.slice(0, 1)}
      </div>

      {offline && (
        <span className="text-[10px] text-amber-600 dark:text-amber-400">连不上本地服务</span>
      )}
    </div>
  );
}
