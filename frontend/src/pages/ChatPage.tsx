import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  streamChat,
  streamEdit,
  type BackendRow,
  type MessagePage,
  type MessageRow,
  type ModelSettings,
  type RoleCard,
  type SessionContext,
  type SessionRow,
  type TurnProbe,
} from "../api";
import { useConfirm } from "../hooks/useConfirm";
import { useToast, type Tone } from "../components/Toast";
import { describeTrim, type StreamMeta } from "../lib/stream";
import { buildTurns, type BuiltTurn } from "../lib/turns";
import ProcessPanel from "../components/chat/ProcessPanel";
import { useAutoScroll } from "../hooks/useAutoScroll";
import { useChatStream } from "../hooks/useChatStream";
import { useMenus } from "../hooks/useMenus";
import { useMessageSelection } from "../hooks/useMessageSelection";
import { useSessions } from "../hooks/useSessions";
import { useUploadFlow } from "../hooks/useUploadFlow";
import ToolStepCard from "../components/chat/ToolStepCard";
import {
  IconClip,
  IconImage,
  IconModel,
  IconSend,
  IconSparkle,
  IconStop,
  IconUser,
} from "../components/chat/icons";
import ThinkingPanel from "../components/chat/ThinkingPanel";
import { Markdown } from "../components/Markdown";
import { Button } from "../components/ui";

/** 心跳。这一拍问的是 `/api/session/{tid}/turn` —— 它只查后端那份进程内登记
 *  （一次字典查找 + 一次主键 SELECT），所以敢 0.8 秒问一次。它决定的就是
 *  「她正在说的那半句」多久出现在这扇窗上：上限 0.8 秒。 */
const TICK_MS = 800;

/** 全量探针（`?limit=1`，带 `total`）的间隔。它比 `turn` 贵一个量级 —— 每次都要把整份
 *  检查点快照反序列化回来 —— 但**只有它看得见已经落地的东西**：主动开口、别处的编辑与
 *  删除、任何不经过一轮生成的写入，`turn` 一概问不出来。所以两者是分工不是重复。
 *  桌宠那条红点轮询是 3 秒，这里 5 秒：真切的场景是"你在桌宠上回了一句，切回控制台"，
 *  那一下靠 `focus` 立刻补读，节拍只是兜底。 */
const FULL_PROBE_MS = 5_000;

/** 每隔几拍付一次全量探针的代价。整数关系写死，免得两处数字各改各的漂掉。 */
const FULL_PROBE_EVERY_TICKS = Math.ceil(FULL_PROBE_MS / TICK_MS);

/** 角色多到几个，抽屉里才出现搜索框：三五个的时候一个框只是多一个要看的控件。 */
const ROLE_SEARCH_FROM = 6;

/** 回答耗时：created_at 配对（用户 → 助手）换算成可读时长；无时间戳的旧消息返回 null。 */
/** 模型设置里"这一页要显示的那些行"：只留**参与对话**的模型（`used_by` 含 chat，派生自
 *  服务页的引用行 —— 拆层后没有 usage 列可筛了）。读的是分组视图 `providers`，把凭据组
 *  的 provider/base_url 摊平回行上，菜单那套按 provider 分组的逻辑因此一行不用改。
 *  两条拉取路径（挂载、改窗口后刷新）共用它，state 因此只有一种形状。 */
function chatRows(settings: ModelSettings): BackendRow[] {
  return (settings.providers ?? []).flatMap((g) =>
    g.models
      .filter((m) => m.used_by.includes("chat"))
      .map((m) => ({
        name: m.name,
        provider: g.provider,
        style: g.style,
        base_url: g.base_url,
        model: m.model,
        sort_order: 0,
        num_ctx: m.num_ctx,
        supports_vision: m.supports_vision ?? false,
        supports_tools: m.supports_tools ?? true,
        // 采样惩罚三栏原样摊平：菜单那一栏要回显"现在是多少 / 根本没设"。
        repeat_penalty: m.repeat_penalty,
        frequency_penalty: m.frequency_penalty,
        presence_penalty: m.presence_penalty,
        has_key: g.has_key,
        key_masked: g.key_masked,
      })),
  );
}

/**
 * 采样惩罚那一栏的三行（设计稿 §8.2：我们此前只露了 num_ctx / temperature）。
 *
 * 档位是**保守地照出厂区间**给的，不是"我们认为更好的值"：`null` = 不传 = 听引擎的，排在第一，
 * 而且默认就停在那儿 —— 小模型上调惩罚容易伤连贯（§8.2 原话），没量过就不替用户决定。
 * `nativeOnly` 那一行对云端根本不出现：OpenAI 兼容体没有 `repeat_penalty` 这个标准字段，
 * 给一个"设了也不知道有没有生效"的控件比不给更糟（后端 PATCH 也会 400 挡）。
 */
const SAMPLING_FIELDS: {
  key: "repeat_penalty" | "frequency_penalty" | "presence_penalty";
  label: string;
  nativeOnly: boolean;
  options: { value: number | null; label: string }[];
}[] = [
  {
    key: "repeat_penalty",
    label: "重复惩罚",
    nativeOnly: true,
    // 标签自己拼：`String(1.0)` 在 JS 里是 "1"，混在 1.1/1.2 旁边读起来像少了一档。
    options: [
      { value: null, label: "未设置" },
      { value: 1.0, label: "1.0" },
      { value: 1.1, label: "1.1" },
      { value: 1.2, label: "1.2" },
      { value: 1.3, label: "1.3" },
    ],
  },
  {
    key: "frequency_penalty",
    label: "频率惩罚",
    nativeOnly: false,
    options: [
      { value: null, label: "未设置" },
      { value: 0, label: "0.0" },
      { value: 0.1, label: "0.1" },
      { value: 0.2, label: "0.2" },
      { value: 0.3, label: "0.3" },
    ],
  },
  {
    key: "presence_penalty",
    label: "存在惩罚",
    nativeOnly: false,
    options: [
      { value: null, label: "未设置" },
      { value: 0, label: "0.0" },
      { value: 0.1, label: "0.1" },
      { value: 0.2, label: "0.2" },
      { value: 0.3, label: "0.3" },
    ],
  },
];

function fmtDuration(from: string, to: string): string | null {
  if (!from || !to) return null;
  const a = new Date(from.replace(" ", "T"));
  const b = new Date(to.replace(" ", "T"));
  if (isNaN(a.getTime()) || isNaN(b.getTime())) return null;
  const s = Math.max(0, Math.round((b.getTime() - a.getTime()) / 1000));
  if (s < 60) return `${s} 秒`;
  return `${Math.floor(s / 60)} 分 ${s % 60} 秒`;
}

export default function ChatPage({
  deepThread = null,
  onDeepThreadUsed,
  unreadByRole = {},
}: {
  /** 深链要打开的会话（收件箱「打开对话并回复」；将来桌宠壳的通知点击同一个入口）。 */
  deepThread?: string | null;
  /** 消费完必须回销：留着不消，下次点同一条就不会再触发跳转。 */
  onDeepThreadUsed?: () => void;
  /** 每个角色还有几条没读的主动开口。侧栏「她们」那一栏的徽章用它 —— 数的是**铃铛那次
   *  轮询**拿到的同一份 `unread_by_role`（与桌宠红点同源），这里不再自己起一个轮询。 */
  unreadByRole?: Record<string, number>;
}) {
  const [roles, setRoles] = useState<RoleCard[]>([]);
  // 「临时话题」的批量清理：选中的线程 id 集合；null = 批量模式没开（那时不画复选框）。
  // 用户 09-26：临时话题攒了几十条，一条条删太麻烦。
  const [tempPick, setTempPick] = useState<Set<string> | null>(null);
  // 角色抽屉里的搜索词（角色少的时候不出现那个框，门槛见 `ROLE_SEARCH_FROM`）。
  const [roleFilter, setRoleFilter] = useState("");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [currentRole, setCurrentRole] = useState<string>("");
  const [messages, setMessages] = useState<MessageRow[]>([]);
  const [input, setInput] = useState("");
  const inputRef = useRef<HTMLTextAreaElement>(null);
  // 多模态传图（2026-09-18）：待发送的图片（data URL），附件就绪后随消息一起发。
  const [pendingImage, setPendingImage] = useState<string | null>(null);
  const imageInputRef = useRef<HTMLInputElement>(null);
  const MAX_IMAGE_BYTES = 15 * 1024 * 1024;
  // 输入框自适应高度：内容多时长高（封顶 160px 后内部滚动），发送/清空后缩回一行。
  useEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  }, [input]);
  const confirm = useConfirm();

  const [editingTitle, setEditingTitle] = useState(false);
  const [titleDraft, setTitleDraft] = useState("");
  // 菜单开关与「鼠标移出后延时关闭」抽到 hooks/useMenus —— 这里只剩业务语义。
  const {
    modelMenuOpen,
    setModelMenuOpen,
    roleMenuOpen,
    setRoleMenuOpen,
    ctxOpen,
    setCtxOpen,
    sampOpen,
    setSampOpen,
    armMenuClose,
    cancelMenuClose,
    closeAllMenus,
  } = useMenus();
  // 对话页只关心**参与对话**的模型行；类型直接用 `api.ts` 的 `BackendRow`，不再自造窄化
  // 形状（审计 §5）：以前挂载路径手挑 6 个字段、改窗口那条路径塞原始行 —— 同一个 state
  // 两种形状，谁先跑过决定字段在不在，`supports_tools` 这类就这样被页面"看不见"了。
  const [backends, setBackends] = useState<BackendRow[]>([]);
  // 供应商 id → 中文档称（分组标题显示"硅基流动"而非原始 id）
  const [providerLabels, setProviderLabels] = useState<Record<string, string>>({});
  const [defaultBackend, setDefaultBackend] = useState("");
  const [sessionModel, setSessionModel] = useState<string | null>(null);
  // 会话级对话模式（对话/智能体）：后端返回**有效值**（会话覆盖 or 全局默认）。
  const [sessionMode, setSessionMode] = useState("chat");
  // 上下文预算事实（H3 的界面部分）：>0 时提示"早期对话已折叠"。
  // 单独放在 state 而不是气泡里，是因为气泡在流结束时会被 checkpoint 回放**整体替换** ——
  // 挂在气泡上的提示会在回答刚结束时消失，用户根本来不及看到。
  const [trim, setTrim] = useState<{ dropped: number; kept: number } | null>(null);
  // 历史消息被分页截断时**还差多少条更早的**（0 = 全部都在）。
  // 为什么要显示：不说的话，用户看到的"最近 500 条"会被当成全部历史。
  const [historyTruncated, setHistoryTruncated] = useState(0);
  // 编辑重生成：正在编辑的那条消息（id + 草稿 + 原图，图随编辑保留）
  const [editing, setEditing] = useState<{ id: string; text: string; image?: string } | null>(null);
  const [copiedKey, setCopiedKey] = useState<string | null>(null); // 复制反馈（按轮 key）
  const [enhancing, setEnhancing] = useState(false); // 增强提示词进行中
  const [distilling, setDistilling] = useState(false); // 提取精海中（一次真模型调用，本地卡上要几十秒）
  const [ctxBudget, setCtxBudget] = useState(0); // 上下文字符预算（后端 context 端点）
  // 多选删除（勾选自动扩展到整轮）抽到 hooks/useMessageSelection。

  const {
    selectMode,
    setSelectMode,
    selected,
    setSelected,
    clearSelection,
    toggleSelect,
  } = useMessageSelection(messages, () => setEditing(null));
  const fileRef = useRef<HTMLInputElement>(null);
  // 本轮是否收到过 error 事件（详情留到收尾时统一提示，见 applyMeta 的说明）。
  const errorRef = useRef<string>("");
  /** 本轮是被叫停的（`End(stopped)`）：留到收尾之后还要看得见，所以是状态不是气泡字段。 */
  const [stoppedHint, setStoppedHint] = useState(false);

  /** 把事件的旁路信息落到对应的界面状态上（气泡正文之外的信息）。 */
  const applyMeta = useCallback((meta: StreamMeta) => {
    if (meta.trimmed && meta.trimmed.dropped > 0) setTrim(meta.trimmed);
    if (meta.stopped) setStoppedHint(true);
    // 错误详情必须**攒起来留到流结束后再说**：气泡会在收尾时被 checkpoint 回放整体替换，
    // 挂在气泡上的 `[错误] …` 跟着一起消失 —— 用户实际上看不到任何提示。
    if (meta.errored) errorRef.current = meta.errorDetail || "模型调用失败";
  }, []);

  // 流式对话的生命周期（busy/live 气泡/发送闸门/abort + SSE 归约）抽到 hooks/useChatStream。
  const {
    busy,
    setBusy,
    live,
    setLive,
    liveRef,
    sendingRef,
    abortRef,
    onEvent,
    startBubble,
    stop,
  } = useChatStream(applyMeta);

  // 状态提示统一走 toast（可叠加、自动消失、带语气）—— 一行 status 会被后来的消息覆盖，
  // 上一个操作的结果还没看清就没了。保留 setStatus 这个名字，既有调用点无需改动。
  const { push: pushToast } = useToast();
  const setStatus = (text: string, tone: Tone = "info") => {
    if (text) pushToast(text, tone);
  };

  // 会话列表（含移动端抽屉开关）抽到 hooks/useSessions。
  const { sessions, sessionsOpen, setSessionsOpen, refreshSessions } = useSessions();

  useEffect(() => {
    refreshSessions().catch((e) => setStatus(`加载对话失败：${e.message}`, "warn"));
    api.get<RoleCard[]>("/api/roles").then(setRoles).catch(() => {});
    api.get<ModelSettings>("/api/settings/models").then((s) => {
      setBackends(chatRows(s));
      setDefaultBackend(s.default || chatRows(s)[0]?.name || "");
      // 分组标题用供应商的中文 displayName（来自 providers 视图，不再另拉一次目录接口）
      setProviderLabels(Object.fromEntries((s.providers ?? []).map((g) => [g.provider, g.label])));
    }).catch(() => {});
  }, [refreshSessions]);

  // 头部的角色选择跟随当前会话（切会话时显示该会话自己的角色）
  useEffect(() => {
    const cur = sessions.find((s) => s.thread_id === sessionId);
    setCurrentRole(cur?.role_id || "");
  }, [sessionId, sessions]);

  /** 别处（桌宠）那一轮**正在生成、还没进检查点**的那半句（R26-38 的镜像）。
   *  `null` = 没人在生成；字符串 = 已经投送到哪儿了（空串 = 她在打字、还没出字）。
   *  只在这一扇窗自己没在流的时候才显 —— 那时候屏幕上已经有 `live` 那个气泡了，
   *  再画一格就是同一个人说两遍。声明在 `useAutoScroll` 之前：滚动要跟的是这一格涨字。 */
  const [mirror, setMirror] = useState<string | null>(null);
  const mirrorRef = useRef<string | null>(null);

  const scrollRef = useAutoScroll(sessionId, [messages, live, mirror]);

  /** 服务端在**上一次我们主动读取时**报的条数。别拿 `messages.length` 当它：那里面混着
   *  乐观发出去的那句和正在流的气泡，一比就误判成"别处写了字"。 */
  const seenTotalRef = useRef<number | null>(null);

  /** 从服务端重读这条会话并记账（要的就是那个 `total`：探针靠它判"别处有没有写字"）。 */
  async function reloadMessages(threadId: string): Promise<void> {
    const page = await api.get<MessagePage>(`/api/session/${threadId}/messages`);
    seenTotalRef.current = page.total;
    setMessages(page.messages);
    // 整句已经落进历史了，镜像那一格的任务就到此为止：不清的话屏幕上会同时有
    // "她正在说的气泡"和"她说完了的那条"，那是重影。
    mirrorRef.current = null;
    setMirror(null);
  }

  /**
   * 别处（桌宠面板）往这条会话里写了字，控制台要跟上 —— 用户 09-26："反过来就看不到了"，
   * 当晚又报"在桌宠那发的收到回答，在对话界面同步得有些慢"。实测一轮 419 字的回答：
   * 他那句 0.21 秒就可读、她那句 14.41 秒才进检查点，界面按 5 秒网格收到 15.0 秒 ——
   * **落后的 12.03 秒里只有 0.59 秒是轮询欠的，其余全是"她正在说"对第二个读者不可见**。
   * 所以这里读两路，各治一截：
   *
   *  * `/api/session/{tid}/turn`（每拍一次，便宜）：治"看不见她在说"。回的是后端那份
   *    进程内在飞登记，不做检查点反序列化，所以敢 0.8 秒问一次 —— 发现、跟字、落地三个
   *    时刻的上限都是 0.8 秒。
   *  * `/messages?limit=1`（每 `FULL_PROBE_EVERY_TICKS` 拍一次，贵）：治"看不见已落地的"。
   *    每读一次要把整份检查点快照反序列化回来，不该当高频探针用 —— 但**她那句落地那一拍
   *    例外**：那时立刻补一次贵读，让真消息换掉镜像气泡，而不是再等 4.8 秒。
   *
   * 两道闸：正在流不抢（那轮的屏幕内容还没落库，抢了就是"我发一条她回两条"那个重影），
   * 条数没变一次 set 都不发（否则每几秒把滚动位置与勾选状态清一遍，比"看不到新的"更烦人）。
   *
   * **不看 `document.hidden`**：这是桌面 app，"控制台被别的窗盖住"是常态而不是后台标签页，
   * 而铃铛那侧的 3 秒轮询本来也不看可见性 —— 加一道只有这一半有的闸，只会造出
   * "一处会同步一处不会"这种查不出来的差别。`focus` 那一下直接跳到一次贵读：切回这一扇窗
   * 想立刻看到的正是已经落地的部分。
   *
   * 只有一个 `setTimeout` 自续，不是两个 `setInterval` 来回切：要的是任何时刻只有一拍在飞
   * （两拍并存时探针会以两种间隔之和的节拍打出去，看不出来也测不到）。
   */
  useEffect(() => {
    if (!sessionId) return;
    const tid = sessionId; // 收窄成 string：闭包里 TS 不认 state 的那道判空
    let cancelled = false;
    let timer = 0;
    let ticks = 0; // 距离上一次全量探针过了几拍
    const arm = () => {
      timer = window.setTimeout(() => void tick(), TICK_MS);
    };
    const setMirrorNow = (next: string | null) => {
      if (next === mirrorRef.current) return;
      mirrorRef.current = next;
      setMirror(next);
    };
    /** 贵的那一读：带 `total`，看得见**已经落地**的东西（别处的编辑、主动开口、跑完的那一轮）。 */
    async function heavyProbe(): Promise<void> {
      const page = await api.get<MessagePage>(`/api/session/${tid}/messages?limit=1`);
      if (cancelled) return;
      const seen = seenTotalRef.current;
      seenTotalRef.current = page.total;
      setMirrorNow(page.inflight ? page.inflight.text : null);
      ticks = 0;
      if (seen !== null && seen !== page.total) await selectSession(tid);
    }
    const tick = async () => {
      try {
        if (sendingRef.current) {
          // 这一扇窗自己在流：屏幕上已经有 `live` 那个气泡，镜像必须让位
          setMirrorNow(null);
        } else if (ticks >= FULL_PROBE_EVERY_TICKS) {
          await heavyProbe();
        } else {
          const cheap = await api.get<TurnProbe>(`/api/session/${tid}/turn`);
          if (!cancelled) {
            ticks += 1;
            if (cheap.inflight) {
              setMirrorNow(cheap.inflight.text);
            } else if (mirrorRef.current !== null) {
              // 她那句刚落地（登记清了而这边还画着气泡）：立刻补一次贵读把真消息换进来，
              // 不等下一拍 —— 用户等的那一下就是这个时刻。
              setMirrorNow(null);
              await heavyProbe();
            }
          }
        }
      } catch {
        /* 探针失败就等下一次节拍：为一次网络抖动改界面无意义 */
      }
      if (!cancelled) arm();
    };
    const onVisible = () => {
      clearTimeout(timer);
      ticks = FULL_PROBE_EVERY_TICKS; // 切回来这一次就读贵的
      void tick();
    };
    arm();
    window.addEventListener("focus", onVisible);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      cancelled = true;
      clearTimeout(timer);
      window.removeEventListener("focus", onVisible);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [sessionId]); // eslint-disable-line react-hooks/exhaustive-deps

  async function selectSession(threadId: string) {
    if (sendingRef.current) return;
    setSessionId(threadId);
    clearSelection(); // 勾选 / 编辑态属于上一个对话，不能跟着过来
    setLive(null);
    setModelMenuOpen(false);
    setSessionsOpen(false); // 移动端选中后收起抽屉
    try {
      const [page, detail, ctxInfo] = await Promise.all([
        // 分页响应：只取最近 N 条（默认 500），太长的一次性全量返回既慢也没用。
        api.get<MessagePage>(`/api/session/${threadId}/messages`),
        api.get<{ model_name: string | null; agent_mode: string }>(`/api/session/${threadId}`),
        // 上下文预算事实：刷新页面后「早期对话已折叠」这条提示同样要能显示出来
        // （它不是一次性的 SSE 事件，而是一个持续为真的状态）。
        api
          .get<SessionContext>(`/api/session/${threadId}/context`)
          .catch(() => ({ trimmed: 0, kept: 0, budget: 0 }) as SessionContext),
      ]);
      setMessages(page.messages);
      // 被截断时要如实说明：否则用户以为看到的是全部历史（审查报告 P2）。
      setHistoryTruncated(page.truncated ? page.total - page.messages.length : 0);
      seenTotalRef.current = page.total; // 记账：别处的写入靠这个数与探针比对
      setSessionModel(detail.model_name);
      setSessionMode(detail.agent_mode || "chat");
      setTrim(ctxInfo.trimmed > 0 ? { dropped: ctxInfo.trimmed, kept: ctxInfo.kept } : null);
      setCtxBudget(ctxInfo.budget);
      setStatus("");
    } catch (e) {
      setStatus(`加载历史失败：${(e as Error).message}`, "warn");
    }
  }

  async function newSession() {
    if (sendingRef.current) return;
    await createSession();
  }

  // 深链打开会话：收件箱「打开对话并回复」（将来桌宠壳的系统通知点击也走这一个入口）。
  // 放在 selectSession 之后，是因为它要复用同一条载入路径（历史 / 角色 / 模型 / 上下文预算）。
  useEffect(() => {
    if (!deepThread) return;
    if (deepThread === sessionId) {
      onDeepThreadUsed?.();
      return;
    }
    if (sendingRef.current) {
      // 生成中切会话会被 selectSession 挡下（防半截回答串台）。与其"点了没反应"，说清原因。
      setStatus("这条回答还没生成完，等它结束再点一次就切过去", "warn");
      onDeepThreadUsed?.();
      return;
    }
    void selectSession(deepThread);
    onDeepThreadUsed?.();
    inputRef.current?.focus(); // 跳进来的目的是回话：光标直接落在输入框
  }, [deepThread, sessionId]); // eslint-disable-line react-hooks/exhaustive-deps

  /** 创建会话；上一个会话还没发过消息（无标题 = 空白）→ 直接打开它，不堆叠空会话。
   *  角色是可选的：不指定即用默认角色（内置「通用助手」），之后随时在功能行切换。
   *  返回可用的 thread_id（新建或复用的），失败返回 null。 */
  async function createSession(): Promise<string | null> {
    const empty = sessions.find((s) => s.is_blank && !s.is_proactive);
    if (empty) {
      setStatus("上一次的对话还是空白，已直接为你打开");
      if (sessionId !== empty.thread_id) await selectSession(empty.thread_id);
      return empty.thread_id;
    }
    try {
      const s = await api.post<SessionRow>("/api/session", {});
      setSessionId(s.thread_id);
      clearSelection();
      setMessages([]);
      setHistoryTruncated(0);
      setLive(null);
      liveRef.current = null;
      setTrim(null); // 新会话没有历史，也就谈不上"折叠"
      setSessionMode("chat"); // 新会话先按对话档渲染；首次加载明细时会刷新为后端的有效值
      setStatus("");
      await refreshSessions();
      return s.thread_id;
    } catch (e) {
      setStatus(`临时话题创建失败：${(e as Error).message}`, "warn");
      return null;
    }
  }

  /** 拿到一个会话 id：已选就用，没有就先建一个（默认角色 = 通用助手）。
   *  让「进入时就能选角色/模型」成为可能 —— 选中即开会话，不用先点「＋ 开一个临时话题」。 */
  async function ensureSession(): Promise<string | null> {
    if (sessionId) return sessionId;
    return createSession();
  }

  /**
   * 打开**那个角色的固定线**（`s_proactive_<role>`）：不存在就幂等 ensure 出来。
   * 这是"选角色"与侧栏「她们」那一栏共用的唯一入口 —— 两处各写一遍就会有一处忘了 ensure。
   *
   * 为什么不再用"把当前线程改挂到另一个角色名下"（旧写法，`PATCH role_id`）：一条线程的
   * 历史只属于一个说话人。旧实现把"历史保留"当卖点，实际是把她的话和别人的话混进同一份
   * 上下文 —— 模型下一轮读到的是前一个角色说的句子，而记忆按新角色写，两头都错。
   * 现在控制台、桌宠、收件箱点条目，三个入口落的是同一条线（用户 09-26："这应该是一起的啊"）。
   *
   * **这一轮还在跑就不切**：切线程会把 live 气泡清掉，那一轮在屏幕上就什么都没有了
   *（库里其实有），那是"话丢了"的错觉，不该由一次换人制造。
   */
  async function openLane(roleId: string): Promise<string | null> {
    if (sendingRef.current) {
      setStatus("这一轮还在跑 —— 先按「停止」或等它说完再换人", "warn");
      return null;
    }
    try {
      const ensured = await api.post<{ thread_id: string }>("/api/session/proactive", {
        role_id: roleId,
      });
      await selectSession(ensured.thread_id);
      await refreshSessions();
      return ensured.thread_id;
    } catch (e) {
      setStatus(`打开她的对话失败：${(e as Error).message}`, "warn");
      return null;
    }
  }

  async function switchRole(roleId: string) {
    const name = roles.find((r) => r.role_id === roleId)?.role_name || roleId;
    const tid = await openLane(roleId);
    if (tid) setStatus(`已切到${name}那条对话（原来那条留在左侧）`, "ok");
  }

  /**
   * 清空她这条线的消息，**线程留着**。固定栏只给清空、不给删除（用户 09-26 拍的）：
   * 删线程会让收件箱里那些行的跳转目标变空（§7.2.2 那张表就是为这件事写的），
   * 而"清空"抹掉的只是这段对话本身 —— 她的角色、她的记忆、她哪天找过你都照旧。
   */
  async function clearLane(threadId: string, name: string) {
    const ok = await confirm({
      title: `清空与${name}的对话？`,
      body: "这条对话的消息会全部删除，不可恢复。她这个角色、她的记忆、以及收件箱里"
        + "「她哪天主动找过我」那些记录都不动。",
      confirmText: "清空",
      danger: true,
    });
    if (!ok) return;
    try {
      const page = await api.get<MessagePage>(`/api/session/${threadId}/messages`);
      const ids = page.messages
        .map((m) => m.id)
        .filter((x): x is string => typeof x === "string" && x !== "");
      if (!ids.length) {
        setStatus("这条对话本来就是空的", "ok");
        return;
      }
      const r = await api.post<{ deleted: number }>(
        `/api/session/${threadId}/messages/delete`,
        { message_ids: ids },
      );
      setStatus(`已清空（删掉 ${r.deleted} 条消息）`, "ok");
      if (sessionId === threadId) await selectSession(threadId);
      await refreshSessions();
    } catch (e) {
      setStatus(`清空失败：${(e as Error).message}`, "warn");
    }
  }

  /**
   * 批量删临时话题：**逐条走已有的 `DELETE /api/session/{tid}`**，不新开后端口子。
   * 那条路径已经把该做的事做了（checkpoint 一起清、按 §7.2.2 留下收件箱那几行、进审计），
   * 批量只是省用户的手，不该顺手换一套语义。
   */
  async function deleteTemporaries(ids: string[]) {
    if (!ids.length) return;
    const ok = await confirm({
      title: `删除 ${ids.length} 个临时话题？`,
      body: "这些对话及其全部消息会被永久删除，不可恢复。「她们」那一栏里每个角色的固定对话"
        + "不在这批里，删不到。",
      confirmText: "删除",
      danger: true,
    });
    if (!ok) return;
    let done = 0;
    const failed: string[] = [];
    for (const id of ids) {
      try {
        await api.del(`/api/session/${id}`);
        done += 1;
      } catch {
        failed.push(id);
      }
    }
    setStatus(
      failed.length ? `删了 ${done} 条，${failed.length} 条没删掉` : `已删除 ${done} 个临时话题`,
      failed.length ? "warn" : "ok",
    );
    // 正在看的那条被删掉了：清空对话区，别把屏幕上还留着的历史当成它还存在于库里
    if (sessionId && ids.includes(sessionId)) {
      setSessionId(null);
      setMessages([]);
    }
    setTempPick(null);
    await refreshSessions();
  }

  /** 批量模式下的勾选。独立成一个函数是因为点击整行有两个意思：没开批量 = 打开这条，
   *  开了批量 = 选中它（用户要的是"少点几次"，不是"多一层菜单"）。 */
  function togglePick(threadId: string) {
    setTempPick((prev) => {
      const next = new Set(prev ?? []);
      if (next.has(threadId)) next.delete(threadId);
      else next.add(threadId);
      return next;
    });
  }

  async function doDelete(threadId: string) {
    try {
      await api.del(`/api/session/${threadId}`);
      if (sessionId === threadId) {
        setSessionId(null);
        setMessages([]);
      setHistoryTruncated(0);
      }
      await refreshSessions();
    } catch (e) {
      setStatus(`删除失败：${(e as Error).message}`, "warn");
    }
  }

  // 按轮分组（用户提问 → 过程步骤 → 最终回答）：切分逻辑在 lib/turns（与后端同规则）。
  // memo：数百条历史时每个 token 都会触发渲染，重跑切分是纯浪费（审查报告 P2）。
  const turns = useMemo(() => buildTurns(messages), [messages]);

  function copyContent(text: string, key: string) {
    navigator.clipboard?.writeText(text).then(
      () => {
        setCopiedKey(key);
        setTimeout(() => setCopiedKey((c) => (c === key ? null : c)), 1500);
      },
      () => undefined,
    );
  }

  /** 重新生成：丢弃该回答及其后的历史，用触发本轮的用户消息原样重问（WorkBuddy 式）。 */
  function regenerate(turn: BuiltTurn<MessageRow>) {
    const uid = turn.user?.id;
    const content = turn.user?.content ?? "";
    if (!uid || busy || (!content && !turn.user?.image)) return;
    // 多模态：原消息带图时随重问一起保图（重新生成保留上下文完整性，2026-09-18）
    saveEdit(uid, content, turn.user?.image ?? null);
  }

  // 上下文使用率：已用字符按当前消息估算（展示口径，随消息实时更新），上限来自后端配置。
    const ctxUsed = useMemo(
    () =>
      messages.reduce((n, m) => n + (m.content?.length ?? 0) + (m.reasoning?.length ?? 0), 0),
    [messages],
  );
  const ctxPct = ctxBudget > 0 ? Math.min(100, Math.round((ctxUsed / ctxBudget) * 100)) : 0;

  /** 设置某后端的上下文窗口（本地模型 num_ctx），保存后热重建、下一轮生效。 */
  async function setModelCtx(name: string, numCtx: number | null) {
    try {
      await api.setModelContext(name, numCtx);
      const ms = await api.get<ModelSettings>("/api/settings/models");
      setBackends(chatRows(ms));
    } catch (e) {
      setStatus(`设置上下文窗口失败：${(e as Error).message}`, "warn");
    }
  }

  /** 改一栏采样惩罚（一次只改一栏）。后端保存时已经热重建 —— 惩罚项与 temperature 同一条
   *  铁律，只能在构造期传进客户端，所以"下一轮生效"不需要重启，也不需要前端再猜。 */
  async function setModelSampling(
    name: string,
    field: "repeat_penalty" | "frequency_penalty" | "presence_penalty",
    value: number | null,
  ) {
    try {
      const stored = await api.setModelSampling(name, { [field]: value });
      // 用后端回来的那份现值更新，而不是本地假设：它同时管住了"没提交的那栏保持原样"。
      setBackends((rows) =>
        rows.map((r) =>
          r.name === stored.name
            ? {
                ...r,
                repeat_penalty: stored.repeat_penalty,
                frequency_penalty: stored.frequency_penalty,
                presence_penalty: stored.presence_penalty,
              }
            : r,
        ),
      );
    } catch (e) {
      setStatus(`设置采样惩罚失败：${(e as Error).message}`, "warn");
    }
  }

  /** 增强提示词：一次纯改写模型调用，结果替换草稿（对齐 WorkBuddy）。 */
  async function enhance() {
    const draft = input.trim();
    if (!draft || busy || enhancing) return;
    setEnhancing(true);
    try {
      const r = await api.enhancePrompt(draft);
      setInput(r.text);
    } catch (e) {
      setStatus(`增强提示词失败：${(e as Error).message}`, "warn");
    } finally {
      setEnhancing(false);
    }
  }

  /**
   * 提取精华：把**这段对话**抽成该角色的记忆条目（一次真模型调用，用户点才跑）。
   *
   * 结果只说条数与成本，不把抽出来的事实复读一遍 —— 那是模型对用户的理解，界面上要看的
   * 地方是记忆卡（那里还能逐条钉住/删除）。记忆总闸关着时后端回 400，文案里带着"去哪开"。
   */
  async function distillNow() {
    if (!sessionId || busy || distilling) return;
    setDistilling(true);
    try {
      const { report } = await api.distillSession(sessionId);
      const parts = [
        report.added && `新增 ${report.added} 条`,
        report.updated && `更新 ${report.updated} 条`,
        report.skipped && `忽略 ${report.skipped} 行看不懂的输出`,
      ].filter(Boolean);
      const cost = report.tokens ? ` · 用去 ${report.tokens} tokens` : "";
      // 像重复的只**报数**、不自动合并：字面度量分不清"换个说法"与"换个值"（实测
      // 「住在上海」与「住在苏州」比两条真同义还像），所以合并留在记忆卡的「整理记忆」里。
      const hint = report.similar
        ? `（另有 ${report.similar} 条字面上看着像同一件事，可在记忆卡点「整理记忆」核对合并）`
        : "";
      if (parts.length) setStatus(`已提取进${roleLabel}的记忆：${parts.join("、")}${cost}${hint}`);
      else setStatus(report.detail || "这段对话里没有值得新记的事实。", "info");
    } catch (e) {
      setStatus(`提取失败：${(e as Error).message}`, "warn");
    } finally {
      setDistilling(false);
    }
  }

  function pickImage(file: File | undefined) {
    if (!file) return;
    if (!file.type.startsWith("image/")) {
      setStatus("只支持图片文件", "warn");
      return;
    }
    if (file.size > MAX_IMAGE_BYTES) {
      setStatus(`图片超过 15MB 上限（当前 ${Math.round(file.size / 1024 / 1024)}MB）`, "warn");
      return;
    }
    const reader = new FileReader();
    reader.onload = () => setPendingImage(String(reader.result));
    reader.onerror = () => setStatus("图片读取失败", "warn");
    reader.readAsDataURL(file);
  }

  async function send(preset?: string) {
    const text = (preset ?? input).trim();
    if ((!text && !pendingImage) || sendingRef.current) return;
    sendingRef.current = true;
    setBusy(true);
    setInput("");
    const image = pendingImage;
    setPendingImage(null);
    // 没有会话就先建一个（角色可选，用默认）；用局部 tid 而非 state（setState 异步）
    let tid = sessionId;
    if (!tid) {
      tid = await createSession();
      if (!tid) {
        sendingRef.current = false;
        setBusy(false);
        return;
      }
    }
    setMessages((m) => [...m, { role: "user", content: text || "（图片）", ...(image ? { image } : {}) }]);
    // 在控制台开口回话 = 她那些主动开口"我看见了"（用户 09-23 定的口径）。桌宠面板、点气泡、
    // 抽屉里点任意一条早就都走这一条了，只有这一处漏接 —— 漏的症状是"我明明在回话，
    // 铃铛上别的角色还在闪"。清失败不碍这一轮：红点没清是可恢复的小毛病，答不回来才是大事。
    void api.markAllReachoutsRead().catch(() => undefined);
    setStoppedHint(false); // 上一轮的"被叫停"不该跟着这一轮
    const controller = startBubble(tid);
    await streamChat(tid, text, onEvent, controller.signal, image);
    const aborted = controller.signal.aborted;
    abortRef.current = null;
    // 流结束：checkpoint 是唯一真相，回放覆盖乐观状态（中断时同样回放，拿到已生成的部分）
    try {
      await reloadMessages(tid);
    } catch {
      /* 会话已被删等极端情况：保留现有气泡 */
    }
    setLive(null);
    liveRef.current = null;
    // 出错必须让用户看到：气泡被回放替换后，挂在气泡上的错误文案会一起消失。
    if (errorRef.current) {
      setStatus(`回答中断：${errorRef.current}`, "warn");
      errorRef.current = "";
    }
    sendingRef.current = false;
    setBusy(false);
    if (aborted) setStatus("已停止生成（已生成的内容已保留）", "warn");
    await refreshSessions();
  }

  // 停止生成（abortRef.current?.abort()）与流式生命周期一并由 hooks/useChatStream 的 stop() 提供。

  const { uploading, handleUpload } = useUploadFlow({
    sessionId,
    ensureSession: createSession,
    reloadMessages: async (tid) => {
      try {
        await reloadMessages(tid);
      } catch {
        /* 会话可能已被删除 */
      }
    },
    onStatus: setStatus,
  });

  function startEdit(id: string, text: string, image?: string) {
    setEditing({ id, text, image });
  }

  /** 编辑保存 = 从该条重新生成：SSE 与普通对话完全一致，结束后回放刷新历史。
   *  override* 供「重新生成」按钮复用同一通道（不经过编辑表单）。 */
  async function saveEdit(overrideId?: string, overrideText?: string, overrideImage?: string | null) {
    // override：直接指定要重新生成的用户消息（消息行「重新生成」按钮复用同一通道）
    const mid = overrideId ?? editing?.id;
    const content = (overrideText ?? editing?.text ?? "").trim();
    if (!mid || !sessionId || sendingRef.current) return;
    const image = overrideImage ?? editing?.image ?? null;
    if (!content && !image) return;
    sendingRef.current = true;
    setBusy(true);
    setStoppedHint(false); // 同上：重新生成是新一轮
    const controller = startBubble(sessionId);
    setEditing(null);
    await streamEdit(sessionId, mid, content, onEvent, controller.signal, image);
    setLive(null);
    liveRef.current = null;
    if (errorRef.current) {
      setStatus(`重新生成失败：${errorRef.current}`, "warn");
      errorRef.current = "";
    }
    sendingRef.current = false;
    setBusy(false);
    if (sessionId) {
      try {
        await reloadMessages(sessionId);
      } catch {
        /* 会话可能已删除 */
      }
    }
  }

  /** 删除所选（后端按整轮扩展并写审计语义上的不可恢复操作）。 */
  async function deleteSelected() {
    if (!sessionId || selected.length === 0) return;
    try {
      await api.post<{ deleted: number }>(`/api/session/${sessionId}/messages/delete`, {
        message_ids: selected,
      });
      setSelected([]);
      setSelectMode(false);
      setStatus(`已删除所选对话`, "ok");
      await reloadMessages(sessionId);
    } catch (e) {
      setStatus(`删除失败：${(e as Error).message}`, "warn");
    }
  }

  async function switchModel(name: string | null) {
    const tid = await ensureSession();
    if (!tid) return;
    try {
      await api.patch(`/api/session/${tid}`, { model_name: name });
      setSessionModel(name);
      setModelMenuOpen(false);
      setStatus(
        name ? `本对话已切换模型 → ${name}（下一轮生效）` : "已清除本对话的模型覆盖（下一轮生效）",
        "ok",
      );
      await refreshSessions();
    } catch (e) {
      setStatus(`切换模型失败：${(e as Error).message}`, "warn");
    }
  }

  /** 会话级切换「对话 / 智能体」：agent = 多步自主任务（规划指令 + 步数上限放大）。 */
  async function switchMode(mode: "chat" | "agent") {
    const tid = await ensureSession();
    if (!tid) return;
    try {
      await api.patch(`/api/session/${tid}`, { agent_mode: mode });
      setSessionMode(mode);
      setStatus(
        mode === "agent"
          ? "已切换为智能体模式：多步自主任务，规划 + 反复调用工具（下一轮生效）"
          : "已切换为对话模式（下一轮生效）",
        "ok",
      );
    } catch (e) {
      setStatus(`切换模式失败：${(e as Error).message}`, "warn");
    }
  }

  const current = sessions.find((s) => s.thread_id === sessionId);
  const roleBackend = roles.find((r) => r.role_id === (current?.role_id || ""))?.model_name || null;
  const effectiveBackend = sessionModel || roleBackend || defaultBackend;
  // 进入时还没有会话：角色下拉默认停在「通用助手」，让用户一眼看到默认角色且可直接选。
  const defaultRoleId =
    roles.find((r) => r.role_id === "general_assistant")?.role_id || roles[0]?.role_id || "";
  const displayRole = currentRole || defaultRoleId;
  // 提取精华的归属说明：写进的是**这个角色**的记忆桶，所以提示里要念出它的名字。
  const roleLabel =
    roles.find((r) => r.role_id === (current?.role_id || displayRole))?.role_name || "当前角色";
  const grouped = useMemo(() => {
    const g: Record<string, BackendRow[]> = {};
    // state 里已经只有**对话**后端（`chatRows` 在拉取边界就筛掉了），这里不再重复过滤。
    for (const b of backends) (g[b.provider] ||= []).push(b);
    return Object.entries(g).sort(([a], [z]) => a.localeCompare(z));
  }, [backends]);

  async function renameSession() {
    const title = titleDraft.trim();
    setEditingTitle(false);
    if (!sessionId || !title) return;
    try {
      await api.patch(`/api/session/${sessionId}`, { title });
      setStatus("已重命名", "ok");
      await refreshSessions();
    } catch (e) {
      setStatus(`重命名失败：${(e as Error).message}`, "warn");
    }
  }

  /**
   * 侧栏分两段。固定的那一栏**列的是角色，不是线程**（用户 09-26 的提法）：每个角色一行，
   * 从没被找过也照样在，点一下才把那条线 ensure 出来 —— "她有没有一条对话"不该取决于
   * 用户有没有先收到过主动消息。
   *
   * 两个旗标都来自后端（`is_proactive` / `is_blank`），前端不猜线程 id 的形状，也不拿
   * "有没有标题"猜空不空：前者是 `core/reachout.py` 的事实，后者会被重命名过的空线程与
   * 深链刚建出来的线程一起骗过去。
   */
  const laneRows = roles.map((r) => ({
    role: r,
    row: sessions.find((s) => s.is_proactive && s.role_id === r.role_id) ?? null,
  }));
  const laneThreadIds = new Set(laneRows.map((l) => l.row?.thread_id));
  /** 角色被删了但那条线还在：不能让它从侧栏消失，否则那段对话就找不回来了。 */
  const orphanLanes = sessions.filter((s) => s.is_proactive && !laneThreadIds.has(s.thread_id));
  const tempSessions = sessions.filter((s) => !s.is_proactive && !s.is_blank);
  /** 抽屉里当前列得出来的角色：按名字子串过滤（大小写不敏感）。没有搜索词时就是全量。 */
  const roleQuery = roleFilter.trim().toLowerCase();
  const shownRoles = roleQuery
    ? roles.filter((r) => r.role_name.toLowerCase().includes(roleQuery))
    : roles;

  return (
    <div className="relative flex h-full">
      {/* 会话列表面板 */}
      {/* 会话列表面板：桌面常驻，移动端 off-canvas 抽屉 */}
      <aside
        className={`flex w-64 shrink-0 flex-col border-r border-slate-200 bg-white transition-transform dark:border-slate-700 dark:bg-slate-800 max-md:fixed max-md:inset-y-0 max-md:left-0 max-md:z-40 max-md:pt-[41px] ${
          sessionsOpen ? "max-md:translate-x-0" : "max-md:-translate-x-full"
        }`}
      >
        <div className="border-b border-slate-100 dark:border-slate-800 p-3">
          <button
            onClick={newSession}
            title="开一条不带角色的临时话题（试个东西、测张图用）。每个角色那条固定对话不受影响。"
            className="w-full rounded-lg bg-blue-600 px-3 py-2 text-sm font-medium text-white hover:bg-blue-700"
          >
            ＋ 开一个临时话题
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto p-2">
          {laneRows.length === 0 && tempSessions.length === 0 && orphanLanes.length === 0 && (
            <p className="px-2 py-4 text-xs text-slate-400 dark:text-slate-500">
              还没有角色，也还没有对话
            </p>
          )}
          {/* 固定的那一栏：一行一个角色。没被找过也照样列着 —— 点一下才 ensure 出那条线。 */}
          {laneRows.length > 0 && (
            <p className="px-2 pb-1 pt-1 text-[11px] font-medium text-slate-400 dark:text-slate-500">
              她们
            </p>
          )}
          {laneRows.map(({ role, row }) => {
            const unread = unreadByRole[role.role_id] ?? 0;
            return (
              <div
                key={role.role_id}
                onClick={() => void openLane(role.role_id)}
                title={`与${role.role_name}的那条对话 —— 桌宠显示的就是这一条`}
                className={`group mb-1 flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-2 text-sm ${
                  row?.thread_id === sessionId
                    ? "bg-blue-50 dark:bg-blue-900/30 text-blue-800"
                    : "hover:bg-slate-50 dark:bg-slate-800/50 dark:hover:bg-slate-700/60"
                }`}
              >
                <div className="min-w-0 flex-1">
                  <div className="truncate">{role.role_name}</div>
                  <div className="truncate text-[11px] text-slate-400 dark:text-slate-500">
                    {row ? row.title || "你们的对话" : "还没开始 · 点一下就在这里"}
                  </div>
                </div>
                {unread > 0 && (
                  <span
                    className="shrink-0 rounded-full bg-blue-600 px-1.5 text-[10px] text-white"
                    title={`${unread} 条她主动找你，还没读`}
                  >
                    {unread}
                  </span>
                )}
                {/* 固定栏只给「清空」，不给删除（用户 09-26 拍）：删线程会把收件箱里那些行的
                    跳转目标掏空（§7.2.2 那张表），而清空抹掉的只是这段对话本身。 */}
                {row && (
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      void clearLane(row.thread_id, role.role_name);
                    }}
                    className="shrink-0 rounded px-1 py-0.5 text-[11px] text-slate-300 opacity-0 transition-opacity hover:bg-slate-100 hover:text-red-500 group-hover:opacity-100 dark:text-slate-600 dark:hover:bg-slate-700/50"
                    title={`清空与${role.role_name}的对话（她的角色、记忆与收件箱记录都不动）`}
                  >
                    清空
                  </button>
                )}
              </div>
            );
          })}
          {/* 角色被删了而那条线还在：留在栏里，否则那段对话没有任何入口了。 */}
          {orphanLanes.map((s) => (
            <div
              key={s.thread_id}
              onClick={() => void selectSession(s.thread_id)}
              className="group mb-1 flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-2 text-sm hover:bg-slate-50 dark:bg-slate-800/50 dark:hover:bg-slate-700/60"
              title="这个角色已经删掉了，而那段对话还在 —— 要清掉就删这一条"
            >
              <div className="min-w-0 flex-1">
                <div className="truncate">{s.title || "旧对话"}</div>
                <div className="truncate text-[11px] text-slate-400 dark:text-slate-500">
                  {s.role_name || s.role_id} · 角色已删
                </div>
              </div>
              <button
                onClick={async (e) => {
                  e.stopPropagation();
                  if (await confirm({ title: "删除这条对话？", body: "对话及其全部消息将被永久删除，不可恢复。", confirmText: "删除", danger: true })) doDelete(s.thread_id);
                }}
                className="shrink-0 rounded px-1 py-0.5 text-xs text-slate-300 opacity-0 transition-opacity hover:bg-slate-100 hover:text-red-500 group-hover:opacity-100 dark:text-slate-600 dark:hover:bg-slate-700/50"
                title="删除对话"
              >
                ✕
              </button>
            </div>
          ))}
          {/* 临时话题那一组：批量清理在这里，一条条删太麻烦（用户 09-26 实测 23 条）。 */}
          {tempSessions.length > 0 && (
            <>
              <div className="flex items-center justify-between gap-2 px-2 pb-1 pt-3">
                <p className="text-[11px] font-medium text-slate-400 dark:text-slate-500">
                  临时话题 · {tempSessions.length}
                </p>
                {tempPick === null ? (
                  <button
                    onClick={() => setTempPick(new Set())}
                    className="text-[11px] text-slate-400 hover:text-blue-600 dark:text-slate-500 dark:hover:text-blue-400"
                    title="勾着删，省得一条条点"
                  >
                    批量清理
                  </button>
                ) : (
                  <span className="flex items-center gap-1.5 text-[11px]">
                    <button
                      onClick={() => setTempPick(new Set(tempSessions.map((s) => s.thread_id)))}
                      className="text-slate-400 hover:text-blue-600 dark:text-slate-500"
                    >
                      全选
                    </button>
                    <button
                      disabled={tempPick.size === 0}
                      onClick={() => void deleteTemporaries([...tempPick])}
                      className="rounded bg-red-600 px-1.5 py-0.5 text-white disabled:bg-slate-300 dark:disabled:bg-slate-700"
                    >
                      删除 {tempPick.size}
                    </button>
                    <button
                      onClick={() => setTempPick(null)}
                      className="text-slate-400 hover:text-slate-600 dark:text-slate-500"
                    >
                      退出
                    </button>
                  </span>
                )}
              </div>
              {tempSessions.map((s) => (
                <div
                  key={s.thread_id}
                  className={`group mb-1 flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-2 text-sm ${
                    s.thread_id === sessionId ? "bg-blue-50 dark:bg-blue-900/30 text-blue-800" : "hover:bg-slate-50 dark:bg-slate-800/50 dark:hover:bg-slate-700/60"
                  }`}
                  onClick={() => (tempPick === null ? void selectSession(s.thread_id) : togglePick(s.thread_id))}
                >
                  {tempPick !== null && (
                    <input
                      type="checkbox"
                      checked={tempPick.has(s.thread_id)}
                      onChange={() => togglePick(s.thread_id)}
                      aria-label={`选中「${s.title || "新对话"}」`}
                      className="shrink-0"
                    />
                  )}
                  <div className="min-w-0 flex-1">
                    <div className="truncate">{s.title || "新对话"}</div>
                    <div className="truncate text-[11px] text-slate-400 dark:text-slate-500">
                      {s.role_name || s.role_id}
                    </div>
                  </div>
                  <button
                    onClick={async (e) => {
                      e.stopPropagation();
                      if (await confirm({ title: "删除这个对话？", body: "对话及其全部消息将被永久删除，不可恢复。", confirmText: "删除", danger: true })) doDelete(s.thread_id);
                    }}
                    className="shrink-0 rounded px-1 py-0.5 text-xs text-slate-300 dark:text-slate-600 opacity-0 transition-opacity hover:bg-slate-100 dark:bg-slate-700/50 dark:hover:bg-slate-700 hover:text-red-500 group-hover:opacity-100"
                    title="删除对话"
                  >
                    ✕
                  </button>
                </div>
              ))}
            </>
          )}
        </div>
      </aside>

      {/* 移动端：会话抽屉的遮罩 */}
      {sessionsOpen && (
        <button
          aria-label="关闭对话列表"
          onClick={() => setSessionsOpen(false)}
          className="absolute inset-0 z-30 bg-slate-900/40 md:hidden"
        />
      )}

      {/* 对话区 */}
      <section className="flex min-w-0 flex-1 flex-col">
        <header className="border-b border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 px-5 py-3">
          {editingTitle && current ? (
            <div className="flex items-center gap-2">
              <input
                autoFocus
                value={titleDraft}
                onChange={(e) => setTitleDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.nativeEvent.isComposing) renameSession();
                  if (e.key === "Escape") setEditingTitle(false);
                }}
                maxLength={100}
                className="w-72 rounded-lg border border-blue-300 px-2.5 py-1 text-sm outline-none"
              />
              <button
                onClick={renameSession}
                className="rounded bg-blue-600 px-2.5 py-1 text-xs text-white hover:bg-blue-700"
              >
                保存
              </button>
              <button
                onClick={() => setEditingTitle(false)}
                className="rounded px-2 py-1 text-xs text-slate-500 dark:text-slate-400 hover:bg-slate-100 dark:bg-slate-700/50 dark:hover:bg-slate-700"
              >
                取消
              </button>
            </div>
          ) : (
            <div className="group/title flex items-center gap-2">
              <button
                onClick={() => setSessionsOpen(true)}
                aria-label="打开对话列表"
                className="rounded-lg border border-slate-200 px-2 py-0.5 text-xs text-slate-600 dark:border-slate-700 dark:text-slate-300 md:hidden"
              >
                对话
              </button>
              <h2 className="truncate text-sm font-medium text-slate-900 dark:text-slate-100">
                {current ? current.title || "新对话" : "对话"}
              </h2>
              {current && (
                <button
                  onClick={() => {
                    setTitleDraft(current.title || "");
                    setEditingTitle(true);
                  }}
                  className="rounded px-1.5 py-0.5 text-xs text-slate-300 dark:text-slate-600 opacity-0 transition-opacity hover:bg-slate-100 dark:bg-slate-700/50 dark:hover:bg-slate-700 hover:text-slate-600 dark:hover:text-slate-300 group-hover/title:opacity-100"
                  title="重命名对话"
                >
                  ✎
                </button>
              )}
              {/* 提取精华：输入是**这段对话**，所以按钮在对话页；记忆卡上那个是另一件事
                  （「整理记忆」，输入是已有条目）。两个按钮各自只需要自己那份输入。 */}
              {current && (
                <div className="ml-auto shrink-0">
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={distillNow}
                    disabled={busy || distilling || messages.length === 0}
                    disabledHint={
                      messages.length === 0
                        ? "先聊几句再提取"
                        : distilling
                          ? "正在提取（本地模型可能要几十秒）"
                          : "等这轮回答完再提取"
                    }
                    title={`把这段对话里关于你的事实抽进「${roleLabel}」的记忆（一次模型调用；逐条核对在 设置 → 跨会话记忆）`}
                  >
                    {distilling ? "提取中…" : "提取精华"}
                  </Button>
                </div>
              )}
            </div>
          )}
          <p className="mt-0.5 text-xs text-slate-400 dark:text-slate-500">
            {current
              ? `当前角色：${currentRole ? roles.find((r) => r.role_id === currentRole)?.role_name || currentRole : "默认"} · 切换角色后下一轮生效`
              : "新建或从左侧选择一个对话开始"}
          </p>
        </header>

        <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto px-6 py-5">
          {!sessionId && messages.length === 0 && !live && (
            <div className="mx-auto mt-8 max-w-xl rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 p-5">
              <h3 className="text-sm font-medium text-slate-800 dark:text-slate-100">开始一次对话</h3>
              <p className="mt-1 text-xs text-slate-400 dark:text-slate-500">
                直接在下方输入即可（会自动创建对话），或点左上角「＋ 开一个临时话题」。
              </p>
              <ul className="mt-3 space-y-1.5 text-xs leading-relaxed text-slate-500 dark:text-slate-400">
                <li>
                  · <b>上传报告 / 图片</b> —— 自动解析并入检索索引（.pdf/.docx/.pptx/.xlsx + 图片 OCR）
                </li>
                <li>
                  · <b>需要某类专业能力时</b> —— 切换角色；每个角色只调用自己白名单内的
                  工具，回答自带来源
                </li>
                <li>· 下方功能行可切换角色与模型（下一轮生效，历史保留）</li>
              </ul>
            </div>
          )}
          <div className="mx-auto flex max-w-3xl flex-col gap-6">
            {/* 按轮渲染（WorkBuddy 式）：用户气泡 → 一个「过程」折叠面板（思考/工具同框）
                → 最终回答 + 操作行。逐条渲染会把一轮散成三个突兀的框（用户反馈）。 */}
            {turns.map((turn) => {
              const userMid = turn.user?.id ?? "";
              const answerMid = turn.answer?.id ?? "";
              const selectId = userMid || answerMid;
              const checked = selectMode && !!selectId && selected.includes(selectId);
              const rowTone = checked ? "opacity-60 ring-1 ring-amber-400" : "";
              const isEditingThis = !!userMid && editing?.id === userMid;
              const dur =
                turn.user && turn.answer
                  ? fmtDuration(turn.user.ts ?? "", turn.answer.ts ?? "")
                  : null;
              return (
                <div key={turn.key} className={`group relative w-full ${rowTone}`}>
                  {selectMode && !!selectId && (
                    <input
                      type="checkbox"
                      aria-label={`选择这一轮：${(turn.user?.content ?? turn.answer?.content ?? "").slice(0, 12)}`}
                      checked={checked}
                      onChange={() => toggleSelect(selectId)}
                      className="absolute -left-7 top-1 h-3.5 w-3.5 accent-amber-500"
                    />
                  )}
                  {turn.user && (
                    <div
                      className={
                        (isEditingThis
                          ? "ml-auto w-full max-w-[80%]"
                          : "ml-auto w-fit max-w-[80%]") + " mb-3"
                      }
                    >
                      {isEditingThis ? (
                        <div className="rounded-2xl rounded-br-sm border border-blue-300 bg-blue-50 dark:bg-slate-800/70 p-2.5">
                          <textarea
                            autoFocus
                            value={editing.text}
                            onChange={(e) =>
                              setEditing({ id: editing.id, text: e.target.value, image: editing.image })
                            }
                            onKeyDown={(e) => {
                              if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) saveEdit();
                              if (e.key === "Escape") setEditing(null);
                            }}
                            rows={3}
                            className="w-full resize-y rounded-lg border border-blue-200 bg-white px-2.5 py-1.5 text-sm outline-none focus:border-blue-400"
                          />
                          <div className="mt-1.5 flex items-center justify-end gap-2 text-[11px]">
                            <span className="text-slate-400 dark:text-slate-500">
                              发送后此条之后的历史将作废并重新生成（Ctrl+Enter 发送）
                            </span>
                            <button
                              onClick={() => setEditing(null)}
                              className="rounded px-2 py-1 text-slate-500 hover:bg-slate-100 dark:text-slate-400 dark:hover:bg-slate-700"
                            >
                              取消
                            </button>
                            <button
                              onClick={() => saveEdit()}
                              className="rounded bg-blue-600 px-2.5 py-1 text-white hover:bg-blue-700"
                            >
                              保存并重新生成
                            </button>
                          </div>
                        </div>
                      ) : (
                        <>
                          {/* 悬浮铅笔：absolute 不占布局（占位会把气泡挤到换行） */}
                          {!busy && !!userMid && (
                            <button
                              onClick={() => startEdit(userMid, turn.user!.content, turn.user?.image)}
                              aria-label="编辑并重答"
                              title="编辑这条消息并重新生成（之后的对话会被作废）"
                              className="absolute -left-9 top-2 rounded-full p-1.5 text-slate-400 opacity-0 transition-opacity hover:bg-blue-50 hover:text-blue-600 group-hover:opacity-100 dark:text-slate-500 dark:hover:bg-slate-700/60 dark:hover:text-blue-400"
                            >
                              <svg viewBox="0 0 20 20" fill="currentColor" className="h-3.5 w-3.5" aria-hidden="true">
                                <path d="M13.586 3.586a2 2 0 112.828 2.828l-.793.793-2.828-2.828.793-.793zM11.379 5.793L3 14.172V17h2.828l8.38-8.379-2.83-2.828z" />
                              </svg>
                            </button>
                          )}
                          <div className="rounded-2xl rounded-br-sm bg-slate-200/90 px-4 py-2.5 text-slate-900 dark:bg-slate-700 dark:text-slate-100">
                            {turn.user.image && (
                              <img
                                src={turn.user.image}
                                alt="对话附图"
                                className="mb-2 max-h-48 w-full rounded-lg object-contain"
                              />
                            )}
                            {turn.user.content && (
                              <p className="whitespace-pre-wrap">{turn.user.content}</p>
                            )}
                          </div>
                          {turn.user.ts && (
                            <p className="mt-1 text-right text-[10px] text-slate-500 dark:text-slate-400">
                              {turn.user.ts}
                            </p>
                          )}
                        </>
                      )}
                    </div>
                  )}
                  {turn.steps.length > 0 && <ProcessPanel steps={turn.steps} />}
                  {turn.answer && (
                    <>
                      <Markdown text={turn.answer.content} />
                      <div className="mt-1 flex items-center gap-3 text-[11px] text-slate-400 dark:text-slate-500">
                        {dur && <span>耗时 {dur}</span>}
                        <button
                          onClick={() => copyContent(turn.answer!.content, turn.key)}
                          className="hover:text-slate-600 dark:hover:text-slate-300"
                        >
                          {copiedKey === turn.key ? "已复制" : "复制"}
                        </button>
                        {turn.user?.id && !busy && (
                          <button
                            onClick={() => regenerate(turn)}
                            className="hover:text-slate-600 dark:hover:text-slate-300"
                          >
                            重新生成
                          </button>
                        )}
                        {turn.answer.ts && <span>{turn.answer.ts}</span>}
                      </div>
                    </>
                  )}
                </div>
              );
            })}
            {live && (
              <div className="w-full">
                <ThinkingPanel text={live.thinking} />
                {live.tools.length > 0 && (
                  <div className="mb-2 space-y-1.5">
                    {live.tools.map((t) => (
                      <ToolStepCard key={t.id} step={t} />
                    ))}
                  </div>
                )}
                <div className={live.streaming ? "caret" : ""}>
                  {live.text ? <Markdown text={live.text} /> : "思考中…"}
                </div>
              </div>
            )}
            {/* 别处（桌宠）那一轮**正在生成**的那半句（R26-38）。
                判据只有一个：后端的在飞登记非空。不在这扇窗自己流的时候才画 —— 那时
                `live` 已经承载同一段字了，两处都画就是重影。
                空串显的是"她在说"而不是空白：那一段里唯一的事实就是她在打字。 */}
            {!live && mirror !== null && (
              <div className="w-full" data-testid="inflight-mirror">
                <div className="caret">
                  {mirror ? <Markdown text={mirror} /> : "她在说…"}
                </div>
                <p className="mt-1 text-xs text-slate-400 dark:text-slate-500">
                  正在生成 · 不是这一扇窗发的
                </p>
              </div>
            )}
            {/* 这一轮是被叫停的（后端 `End(stopped)`）：屏幕上那半截不是"说完了"。
                说它必须**在气泡之外** —— 收尾时气泡会被 checkpoint 回放整体换掉，挂在里面的字
                跟着消失，用户其实看不到（与 `errorRef` 那笔账同一个理由）。
                而"别的窗口按的停"（桌宠那个「停止」）正是本地 `signal.aborted` 覆盖不到的形态，
                判据只能来自后端那句 stopped。在**这一扇窗**按的停另有那句
                "已停止生成（已生成的内容已保留）"的提示，两边不重复说同一件事。 */}
            {stoppedHint && (
              <p className="mt-2 text-xs text-slate-400 dark:text-slate-500">
                这一轮是被叫停的 —— 上面那半截停在哪儿就是哪儿，没有说完。
              </p>
            )}
          </div>
        </div>

        <div className="border-t border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 p-4">
          {/* 多选删除：确认条出现时输入框让位，避免"边打字边误删" */}
          {selectMode && selected.length > 0 && (
            <div className="mx-auto mb-2 flex max-w-3xl items-center gap-2 rounded-lg border border-amber-200 dark:border-amber-800 bg-amber-50 dark:bg-amber-900/20 px-3 py-2 text-xs text-amber-700 dark:text-amber-300">
              <span className="flex-1">
                已选 <b>{selected.length}</b> 条消息（勾选一侧会带上配对的问答）。删除不可恢复。
              </span>
              <button
                onClick={async () => {
                  if (await confirm({ title: "删除选中的消息？", body: `将删除 ${selected.length} 条消息（整轮），不可恢复。`, confirmText: "确认删除", danger: true })) deleteSelected();
                }}
                className="rounded bg-red-500 px-2.5 py-1 text-white hover:bg-red-600"
              >
                删除所选
              </button>
              <button
                onClick={clearSelection}
                className="rounded px-2 py-1 hover:bg-amber-100 dark:hover:bg-amber-900/40"
              >
                退出选择
              </button>
            </div>
          )}
          {selectMode && selected.length === 0 && (
            <div className="mx-auto mb-2 max-w-3xl rounded-lg border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800/50 px-3 py-2 text-xs text-slate-500 dark:text-slate-400">
              删除模式：勾选任意一问或一答，会自动带上配对的另一侧；选好后点右下「删除所选」。
            </div>
          )}
          {historyTruncated > 0 && (
            <div className="mx-auto mb-2 max-w-3xl rounded-lg border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800/50 px-3 py-2 text-xs text-slate-500 dark:text-slate-400">
              当前只显示这个对话的<b>最近 {messages.length} 条</b>消息
              （还有 {historyTruncated} 条更早的未加载）。
            </div>
          )}
          {/* 上下文预算提示（H3）：模型这轮只看到了最近 N 条历史。
              为什么值得占一行位置：不说的话，用户遇到"它怎么忘了我前面说的"时只会
              归因于"模型不行"，而实际原因是可解释、可预期的行为。措辞在 lib/stream.ts
              里与测试共用一份（describeTrim），避免界面文案与断言各说各话。 */}
          {trim && trim.dropped > 0 && (
            <div className="mx-auto mb-2 flex max-w-3xl items-start gap-2 rounded-lg border border-amber-200 dark:border-amber-800 bg-amber-50 dark:bg-amber-900/20 px-3 py-2 text-xs leading-relaxed text-amber-700 dark:text-amber-300">
              <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-amber-400" />
              <span className="flex-1">{describeTrim(trim.dropped, trim.kept)}</span>
              <button
                onClick={() => setTrim(null)}
                title="知道了（下次仍会在需要时提示）"
                className="shrink-0 rounded px-1 text-amber-500 hover:bg-amber-100 dark:hover:bg-amber-900/40"
              >
                知道了
              </button>
            </div>
          )}
          {/* 输入框（WorkBuddy 式）：发送/暂停是嵌在框内的图标按钮；左下「增强提示词」，
              右下上下文使用率（悬停看明细）。 */}
          <div className="mx-auto max-w-3xl">
            <div className="rounded-2xl border border-slate-200 bg-white focus-within:border-blue-400 dark:border-slate-700 dark:bg-slate-800">
              {pendingImage && (
                <div className="flex items-center gap-2 border-b border-slate-100 px-3 py-2 dark:border-slate-700">
                  <img
                    src={pendingImage}
                    alt="待发送图片"
                    className="h-16 w-16 rounded-lg border border-slate-200 object-cover dark:border-slate-600"
                  />
                  <span className="text-[11px] text-slate-500 dark:text-slate-400">图片已附加，将随消息一起发送</span>
                  <button
                    onClick={() => setPendingImage(null)}
                    disabled={busy}
                    title="移除图片"
                    className="ml-auto rounded px-2 py-1 text-[11px] text-red-400 hover:bg-red-50 hover:text-red-600 disabled:opacity-40 dark:hover:bg-red-900/30"
                  >
                    移除
                  </button>
                </div>
              )}
              <textarea
                ref={inputRef}
                value={input}
                rows={1}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  // Enter 发送、Shift+Enter 换行；输入法组词中（isComposing）不触发。
                  if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                    e.preventDefault();
                    send();
                  }
                }}
                placeholder={
                  busy
                    ? "正在生成…（可点右下角停止）"
                    : "输入消息，Enter 发送 / Shift+Enter 换行（没有对话会自动创建）"
                }
                disabled={busy}
                className="max-h-40 w-full resize-none bg-transparent px-4 pt-3 pb-1 leading-relaxed outline-none disabled:bg-slate-50 dark:disabled:bg-slate-800/50"
              />
              <div className="flex items-center justify-between gap-2 px-2.5 pb-2">
                <div className="flex items-center gap-1">
                  <input
                    ref={imageInputRef}
                    type="file"
                    accept="image/*"
                    className="hidden"
                    onChange={(e) => {
                      pickImage(e.target.files?.[0]);
                      e.target.value = ""; // 允许连续选同一文件
                    }}
                  />
                  <button
                    onClick={() => imageInputRef.current?.click()}
                    disabled={busy}
                    title="附加图片（发给当前模型识别；需视觉模型支持）"
                    className="flex items-center gap-1 rounded-lg px-2 py-1 text-[11px] text-slate-500 hover:bg-slate-100 disabled:opacity-40 dark:text-slate-400 dark:hover:bg-slate-700/60"
                  >
                    <IconImage />
                    图片
                  </button>
                  <button
                    onClick={enhance}
                    disabled={busy || enhancing || !input.trim()}
                    title="增强提示词：把草稿改写得更清晰、具体（一次模型调用）"
                    className="flex items-center gap-1 rounded-lg px-2 py-1 text-[11px] text-slate-500 hover:bg-slate-100 disabled:opacity-40 dark:text-slate-400 dark:hover:bg-slate-700/60"
                  >
                    <IconSparkle />
                    {enhancing ? "增强中…" : "增强提示词"}
                  </button>
                </div>
                <div className="flex items-center gap-2">
                  {ctxBudget > 0 && (
                    <span
                      title={`上下文约 ${ctxUsed} 字 / 上限 ${ctxBudget} 字（${ctxPct}%）${
                        trim ? ` · 本轮已裁 ${trim.dropped} 条` : ""
                      }`}
                      className="flex cursor-default items-center gap-1 text-[11px] text-slate-400 dark:text-slate-500"
                    >
                      <span
                        className={`h-1.5 w-1.5 rounded-full ${
                          ctxPct >= 80 ? "bg-amber-400" : "bg-slate-300 dark:bg-slate-600"
                        }`}
                      />
                      {ctxPct}%
                    </span>
                  )}
                  {busy ? (
                    <button
                      onClick={stop}
                      title="停止生成"
                      aria-label="停止生成"
                      className="flex h-8 w-8 items-center justify-center rounded-full bg-slate-700 text-white hover:bg-slate-800 dark:bg-slate-600 dark:hover:bg-slate-500"
                    >
                      <IconStop />
                    </button>
                  ) : (
                    <button
                      onClick={() => send()}
                      disabled={!input.trim()}
                      title="发送（Enter）"
                      aria-label="发送"
                      className="flex h-8 w-8 items-center justify-center rounded-full bg-blue-600 text-white hover:bg-blue-700 disabled:bg-slate-300 dark:disabled:bg-slate-700"
                    >
                      <IconSend />
                    </button>
                  )}
                </div>
              </div>
            </div>
          </div>
          {/* 功能行（对齐 WorkBuddy：输入框下方一排功能）—— 全部对接真实后端能力 */}
          <div className="mx-auto mt-2 flex max-w-3xl items-center gap-2">
            <span
              className="relative"
              onMouseEnter={cancelMenuClose}
              onMouseLeave={() => armMenuClose(() => setRoleMenuOpen(false))}
              onKeyDown={(e) => {
                // 键盘用户的第二条退路：菜单靠鼠标移出关闭，Esc 必须也能关。
                if (e.key === "Escape") closeAllMenus();
              }}
            >
              {/* 角色切换（WorkBuddy 式自定义菜单）：原生 select 的弹层系统绘制、样式突兀，
                  换成与模型菜单同款的面板——角色名 + 内置徽标 + 当前项勾选。 */}
              <button
                onClick={() => {
                  setModelMenuOpen(false); // 两个菜单互斥
                  if (!roleMenuOpen) setRoleFilter(""); // 每次打开都是全量：上次的过滤词留着会让人以为角色变少了
                  setRoleMenuOpen((o) => !o);
                }}
                aria-haspopup="true"
                aria-expanded={roleMenuOpen}
                title="换个说话的人 —— 会打开她自己的那条对话（各人的话留在各自那条线里）"
                className="flex items-center gap-1.5 rounded-full border border-slate-200 bg-white px-3 py-1 text-xs text-slate-600 hover:border-blue-300 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300 dark:hover:border-blue-700"
              >
                <IconUser />
                {roles.find((r) => r.role_id === displayRole)?.role_name ?? "角色"} ▾
              </button>
              {roleMenuOpen && (
                <>
                  {/* 抽屉式而非"全量浮层"（用户 09-26："角色多了咋办"）：列表封顶 60vh 内部滚动，
                      角色一多再给一个搜索框 —— 少了这个封顶，十几个角色就能把浮层顶出屏幕，
                      而它往上长是会被头部截掉的。 */}
                  <div className="absolute bottom-full left-0 z-20 mb-2 flex max-h-[min(60vh,420px)] w-64 flex-col overflow-hidden rounded-xl border border-slate-200 bg-white shadow-lg dark:border-slate-600 dark:bg-slate-800">
                    <p className="shrink-0 bg-slate-50 px-3 py-1.5 text-[11px] font-medium text-slate-400 dark:bg-slate-800/60 dark:text-slate-500">
                      选角色 = 进她那条对话（原来那条留在左侧）
                    </p>
                    {roles.length > ROLE_SEARCH_FROM && (
                      <input
                        value={roleFilter}
                        onChange={(e) => setRoleFilter(e.target.value)}
                        placeholder={`搜角色（${roles.length} 个）`}
                        aria-label="搜角色"
                        className="mx-2 mt-2 shrink-0 rounded-lg border border-slate-200 px-2 py-1 text-xs outline-none focus:border-blue-400 dark:border-slate-600 dark:bg-slate-900"
                      />
                    )}
                    <div className="min-h-0 flex-1 overflow-y-auto py-1">
                      {shownRoles.map((r) => {
                        const unread = unreadByRole[r.role_id] ?? 0;
                        return (
                          <button
                            key={r.role_id}
                            onClick={() => {
                              setRoleMenuOpen(false);
                              switchRole(r.role_id);
                            }}
                            className="flex w-full items-center justify-between gap-2 px-3 py-2 text-xs hover:bg-blue-50 dark:hover:bg-blue-900/30"
                          >
                            <span className="truncate text-slate-700 dark:text-slate-200">{r.role_name}</span>
                            <span className="ml-auto flex shrink-0 items-center gap-1.5">
                              {unread > 0 && (
                                <span
                                  className="rounded-full bg-blue-600 px-1.5 text-[10px] text-white"
                                  title={`${unread} 条她主动找你，还没读`}
                                >
                                  {unread}
                                </span>
                              )}
                              {r.is_builtin && (
                                <span className="rounded bg-slate-100 px-1 text-[10px] text-slate-400 dark:bg-slate-700/60 dark:text-slate-400">
                                  内置
                                </span>
                              )}
                              {displayRole === r.role_id && (
                                <span className="text-blue-600 dark:text-blue-400">✓</span>
                              )}
                            </span>
                          </button>
                        );
                      })}
                      {shownRoles.length === 0 && (
                        <p className="px-3 py-2 text-xs text-slate-400 dark:text-slate-500">
                          没有匹配「{roleFilter}」的角色
                        </p>
                      )}
                    </div>
                  </div>
                </>
              )}
            </span>
            {/* 对话/智能体 模式切换（会话级，PATCH /api/session）：agent = 多步自主任务
                —— 注入规划指令、步数上限自动翻倍。与角色/模型同款"下一轮生效"。 */}
            <div
              className="flex items-center rounded-full border border-slate-200 bg-white p-0.5 text-xs dark:border-slate-700 dark:bg-slate-800"
              title={sessionMode === "agent" ? "智能体模式：多步自主任务" : "对话模式：一问一答"}
            >
              <button
                onClick={() => switchMode("chat")}
                className={`rounded-full px-2.5 py-1 transition-colors ${
                  sessionMode !== "agent" ? "bg-blue-600 text-white" : "text-slate-500 dark:text-slate-400"
                }`}
              >
                对话
              </button>
              <button
                onClick={() => switchMode("agent")}
                className={`rounded-full px-2.5 py-1 transition-colors ${
                  sessionMode === "agent" ? "bg-blue-600 text-white" : "text-slate-500 dark:text-slate-400"
                }`}
              >
                智能体
              </button>
            </div>
            <div
              className="relative"
              onMouseEnter={cancelMenuClose}
              onMouseLeave={() => armMenuClose(() => setModelMenuOpen(false))}
            >
              <button
                onClick={() => {
                  setRoleMenuOpen(false); // 两个菜单互斥
                  setModelMenuOpen((o) => !o);
                }}
                aria-haspopup="true"
                aria-expanded={modelMenuOpen}
                title="切换本对话使用的模型（按供应商分组；选中即开对话）"
                className="flex items-center gap-1.5 rounded-full border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 px-3 py-1 text-xs text-slate-600 dark:text-slate-300 hover:border-blue-300 dark:hover:border-blue-700"
              >
                <IconModel />
                {backends.find((b) => b.name === effectiveBackend)?.model || effectiveBackend || "模型"} ▾
              </button>
              {modelMenuOpen && (
                <>
                  <div className="absolute bottom-full left-0 z-20 mb-2 max-h-72 w-72 overflow-y-auto rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 shadow-lg">
                  <button
                    onClick={() => switchModel(null)}
                    className="flex w-full items-center justify-between px-3 py-2 text-xs hover:bg-blue-50 dark:bg-blue-900/30"
                  >
                    <span>默认后端（跟随设置）</span>
                    {sessionModel === null && <span className="text-blue-600 dark:text-blue-400">✓</span>}
                  </button>
                  {grouped.map(([provider, list]) => (
                    <div key={provider}>
                      <p className="bg-slate-50 dark:bg-slate-800/50 px-3 py-1 text-[11px] font-medium text-slate-400 dark:text-slate-500">
                        {providerLabels[provider] ?? provider}
                      </p>
                      {list.map((b) => (
                        <div key={b.name} className="relative">
                          {/* 选择按钮与上下文按钮是**兄弟**：嵌在 button 内部的徽章点击会被
                              父按钮的激活吞掉（实测），拆开才互不影响。 */}
                          <div className="flex items-center">
                            <button
                              onClick={() => switchModel(b.name)}
                              className="flex min-w-0 flex-1 items-center justify-between px-3 py-1.5 text-xs hover:bg-blue-50 dark:hover:bg-blue-900/30"
                            >
                              <span className="flex min-w-0 items-center gap-1.5">
                                <span className="font-mono truncate">{b.model}</span>
                                {b.supports_vision && (
                                  <span className="shrink-0 rounded bg-emerald-100 px-1 py-0.5 text-[9px] font-medium text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300">
                                    视觉
                                  </span>
                                )}
                                {b.supports_tools && (
                                  <span className="shrink-0 rounded bg-sky-100 px-1 py-0.5 text-[9px] font-medium text-sky-700 dark:bg-sky-900/40 dark:text-sky-300">
                                    工具
                                  </span>
                                )}
                              </span>
                              <span className="ml-2 flex min-w-0 items-center gap-1.5">
                                <span className="truncate text-slate-400 dark:text-slate-500">{b.name}</span>
                              </span>
                              {effectiveBackend === b.name && (
                                <span className="ml-1 text-blue-600 dark:text-blue-400">✓</span>
                              )}
                            </button>
                            {(b.style === "native") && (
                              <button
                                onClick={() => setCtxOpen((c) => (c === b.name ? null : b.name))}
                                title="设置该模型的上下文窗口（num_ctx）"
                                className="mr-2 shrink-0 rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-500 hover:bg-slate-200 dark:bg-slate-700/60 dark:text-slate-400 dark:hover:bg-slate-600"
                              >
                                {b.num_ctx ? `${Math.round(b.num_ctx / 1024)}k ▾` : "上下文 ▾"}
                              </button>
                            )}
                            {/* 采样惩罚：两类客户端都有这一栏（重复惩罚只对本地，见
                                `SAMPLING_FIELDS`）。徽章上的"·已设"只说"至少一栏不是默认"，
                                具体数值在面板里逐栏回显 —— 徽章上摆三个数是给人在菜单里读表格。 */}
                            <button
                              onClick={() => setSampOpen((s) => (s === b.name ? null : b.name))}
                              title="采样惩罚（重复 / 频率 / 存在）：保存即热重建，下一轮生效"
                              className="mr-2 shrink-0 rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-500 hover:bg-slate-200 dark:bg-slate-700/60 dark:text-slate-400 dark:hover:bg-slate-600"
                            >
                              {b.repeat_penalty != null ||
                              b.frequency_penalty != null ||
                              b.presence_penalty != null
                                ? "采样 ·已设 ▾"
                                : "采样 ▾"}
                            </button>
                          </div>
                          {/* 上下文选项：点行内「上下文」徽章展开（inline，触屏可用） */}
                          {(b.style === "native") &&
                            ctxOpen === b.name && (
                            <div className="border-t border-slate-100 px-3 py-2 dark:border-slate-700/60">
                              <p className="pb-1.5 text-[10px] font-medium text-slate-400 dark:text-slate-500">
                                上下文窗口 · {b.model}
                              </p>
                              <div className="grid grid-cols-3 gap-1">
                                {[
                                  { label: "引擎默认", value: null },
                                  { label: "2048", value: 2048 },
                                  { label: "4096", value: 4096 },
                                  { label: "8192", value: 8192 },
                                  { label: "16384", value: 16384 },
                                  { label: "32768", value: 32768 },
                                ].map((opt) => (
                                  <button
                                    key={opt.label}
                                    onClick={() => setModelCtx(b.name, opt.value)}
                                    className={`rounded px-2 py-1 text-[11px] ${
                                      (b.num_ctx ?? null) === opt.value
                                        ? "bg-blue-600 text-white"
                                        : "text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-700/60"
                                    }`}
                                  >
                                    {opt.label}
                                  </button>
                                ))}
                              </div>
                              <p className="mt-1.5 text-[10px] leading-relaxed text-slate-400 dark:text-slate-500">
                                Ollama 默认仅 2048 tokens，调大才能真正用上模型窗口
                              </p>
                            </div>
                          )}
                          {/* 采样惩罚面板：一栏一行、行内点档位，第一档永远是「未设置」。 */}
                          {sampOpen === b.name && (
                            <div className="border-t border-slate-100 px-3 py-2 dark:border-slate-700/60">
                              {SAMPLING_FIELDS.filter(
                                (f) =>
                                  !f.nativeOnly || b.style === "native",
                              ).map((f) => (
                                <div key={f.key} className="flex items-start gap-1.5 py-0.5">
                                  <span className="w-16 shrink-0 pt-1 text-[10px] text-slate-400 dark:text-slate-500">
                                    {f.label}
                                  </span>
                                  <div className="flex flex-wrap gap-1">
                                    {f.options.map((opt) => (
                                      <button
                                        key={opt.label}
                                        onClick={() => void setModelSampling(b.name, f.key, opt.value)}
                                        className={`rounded px-2 py-0.5 text-[11px] ${
                                          (b[f.key] ?? null) === opt.value
                                            ? "bg-blue-600 text-white"
                                            : "text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-700/60"
                                        }`}
                                      >
                                        {opt.label}
                                      </button>
                                    ))}
                                  </div>
                                </div>
                              ))}
                              <p className="mt-1 text-[10px] leading-relaxed text-slate-400 dark:text-slate-500">
                                「未设置」= 不传这项、听引擎的（Ollama 出厂重复惩罚就是 1.1）。
                                上调能压复读，但小模型上更容易伤连贯 —— 拿不准就留未设置。
                              </p>
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                  ))}
                  </div>
                </>
              )}
            </div>
            <button
              onClick={() => fileRef.current?.click()}
              disabled={uploading}
              title="上传报告 / 图片，自动解析并入检索索引（.txt/.md/.pdf/.docx/.pptx/.xlsx + 图片 OCR）"
              className="flex items-center gap-1.5 rounded-full border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 px-3 py-1 text-xs text-slate-600 dark:text-slate-300 hover:border-blue-300 dark:hover:border-blue-700 disabled:opacity-50"
            >
              <IconClip />
              {uploading ? "上传中…" : "上传报告"}
            </button>
            <button
              onClick={() => {
                // 进出删除模式都要清掉勾选：退出去再进来时，上一轮的勾选不该还留着。
                if (selectMode) clearSelection();
                else setSelectMode(true);
              }}
              disabled={busy || !sessionId}
              title="删除历史里的某几段问答：勾选任意一问或一答，会自动带上配对的另一侧"
              className={`flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs disabled:opacity-50 ${
                selectMode
                  ? "border-amber-300 bg-amber-50 text-amber-700 dark:border-amber-700 dark:bg-amber-900/30 dark:text-amber-300"
                  : "border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 text-slate-600 dark:text-slate-300 hover:border-amber-300 hover:text-amber-600"
              }`}
            >
              {selectMode ? "退出删除模式" : "删除对话"}
            </button>
            <input
              ref={fileRef}
              type="file"
              className="hidden"
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) handleUpload(f);
                e.target.value = "";
              }}
            />
            <span className="ml-auto text-[11px] text-slate-300 dark:text-slate-600">
              Enter 发送 · 生成中可停止 · 停用插件即刻生效
            </span>
          </div>
        </div>
      </section>
    </div>
  );
}
