import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  streamChat,
  streamEdit,
  type MessagePage,
  type MessageRow,
  type RoleCard,
  type SessionContext,
  type SessionRow,
} from "../api";
import { useConfirm } from "../hooks/useConfirm";
import { useToast, type Tone } from "../components/Toast";
import { describeTrim, type StreamMeta } from "../lib/stream";
import { buildTurns, type BuiltTurn } from "../lib/turns";
import { useAutoScroll } from "../hooks/useAutoScroll";
import { useChatStream } from "../hooks/useChatStream";
import { useMessageSelection } from "../hooks/useMessageSelection";
import { useSessions } from "../hooks/useSessions";
import { useSessionMirror } from "../hooks/useSessionMirror";
import { useUploadFlow } from "../hooks/useUploadFlow";
import ChatToolbar from "../components/chat/ChatToolbar";
import ToolStepCard from "../components/chat/ToolStepCard";
import SessionSidebar from "../components/chat/SessionSidebar";
import TurnRow from "../components/chat/TurnRow";
import {
  IconImage,
  IconSend,
  IconSparkle,
  IconStop,
} from "../components/chat/icons";
import ThinkingPanel from "../components/chat/ThinkingPanel";
import { Markdown } from "../components/Markdown";
import { Button } from "../components/ui";

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
  const [sessionId, setSessionId] = useState<string | null>(null);
  /** 选中会话的计数。工具条拿它当「换了一条，把菜单收掉」的扳机：菜单开合是它的私事，
   *  而**哪一次算换会话**是页面的事 —— 发送时顺手新建一条不该收菜单（见 ChatToolbar）。 */
  const [selectionNonce, setSelectionNonce] = useState(0);
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
  // 菜单开合与模型行（供应商分组 / 上下文窗口 / 采样惩罚）都在 ChatToolbar 里：那两个菜单
  // 互斥、共用一把「鼠标移出后延时关闭」的定时器，拆成两处就守不住这个不变式。
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
    // 模型行由 ChatToolbar 自己拉（hooks/useModelBackends）：这一页除了生效模型的名字不碰它。
  }, [refreshSessions]);

  // 头部的角色选择跟随当前会话（切会话时显示该会话自己的角色）
  useEffect(() => {
    const cur = sessions.find((s) => s.thread_id === sessionId);
    setCurrentRole(cur?.role_id || "");
  }, [sessionId, sessions]);

  // 「别处那一轮正在说的半句」与两路探针（跟着服务端走）抽到 hooks/useSessionMirror。
  // 调用必须留在 `useAutoScroll` 之前：滚动要跟的是镜像这一格涨字。
  const { mirror, seenTotalRef, reloadMessages } = useSessionMirror({
    sessionId,
    sendingRef,
    selectSession,
    setMessages,
  });

  const scrollRef = useAutoScroll(sessionId, [messages, live, mirror]);

  async function selectSession(threadId: string) {
    if (sendingRef.current) return;
    setSessionId(threadId);
    setSelectionNonce((n) => n + 1); // 收菜单：这一步原先直接写在这里
    clearSelection(); // 勾选 / 编辑态属于上一个对话，不能跟着过来
    setLive(null);
    setSessionsOpen(false); // 移动端选中后收起抽屉（菜单的开合由 ChatToolbar 跟着会话变化收掉）
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

  /** 切本对话的模型覆盖。返回**是否换成**：没换成时那一侧菜单要保持打开，让失败看得见。 */
  async function switchModel(name: string | null): Promise<boolean> {
    const tid = await ensureSession();
    if (!tid) return false;
    try {
      await api.patch(`/api/session/${tid}`, { model_name: name });
      setSessionModel(name);
      setStatus(
        name ? `本对话已切换模型 → ${name}（下一轮生效）` : "已清除本对话的模型覆盖（下一轮生效）",
        "ok",
      );
      await refreshSessions();
      return true;
    } catch (e) {
      setStatus(`切换模型失败：${(e as Error).message}`, "warn");
      return false;
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
  // 进入时还没有会话：角色下拉默认停在「通用助手」，让用户一眼看到默认角色且可直接选。
  const defaultRoleId =
    roles.find((r) => r.role_id === "general_assistant")?.role_id || roles[0]?.role_id || "";
  const displayRole = currentRole || defaultRoleId;
  // 提取精华的归属说明：写进的是**这个角色**的记忆桶，所以提示里要念出它的名字。
  const roleLabel =
    roles.find((r) => r.role_id === (current?.role_id || displayRole))?.role_name || "当前角色";

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

  return (
    <div className="relative flex h-full">
      <SessionSidebar
        roles={roles}
        sessions={sessions}
        unreadByRole={unreadByRole}
        sessionId={sessionId}
        sessionsOpen={sessionsOpen}
        onCloseDrawer={() => setSessionsOpen(false)}
        tempPick={tempPick}
        setTempPick={setTempPick}
        onNewSession={() => void newSession()}
        onOpenLane={(roleId) => void openLane(roleId)}
        onSelectSession={(threadId) => void selectSession(threadId)}
        onClearLane={(threadId, name) => void clearLane(threadId, name)}
        onDeleteOne={(threadId) => void doDelete(threadId)}
        onDeleteMany={(ids) => void deleteTemporaries(ids)}
      />

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
              // 勾选位：一问一答共用一个（后端也按整轮删），所以取用户那条优先。
              const selectId = turn.user?.id ?? turn.answer?.id ?? "";
              return (
                <TurnRow
                  key={turn.key}
                  turn={turn}
                  busy={busy}
                  selectMode={selectMode}
                  checked={selectMode && !!selectId && selected.includes(selectId)}
                  editing={editing}
                  copied={copiedKey === turn.key}
                  onToggleSelect={() => toggleSelect(selectId)}
                  onStartEdit={startEdit}
                  onEditChange={(text) => editing && setEditing({ ...editing, text })}
                  onEditCancel={() => setEditing(null)}
                  onSaveEdit={() => saveEdit()}
                  onRegenerate={() => regenerate(turn)}
                  onCopy={(text) => copyContent(text, turn.key)}
                />
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
          {/* 功能行（角色 / 模式 / 模型 / 上传 / 删除模式）—— 菜单开合与模型行的读写
              都在 components/chat/ChatToolbar 里，这里只给数据与回调。 */}
          <ChatToolbar
            roles={roles}
            currentRoleId={currentRole}
            displayRole={displayRole}
            unreadByRole={unreadByRole}
            sessionId={sessionId}
            selectionNonce={selectionNonce}
            busy={busy}
            uploading={uploading}
            selectMode={selectMode}
            sessionModel={sessionModel}
            sessionMode={sessionMode}
            onPickRole={switchRole}
            onSwitchModel={switchModel}
            onSwitchMode={(mode) => void switchMode(mode)}
            onUpload={handleUpload}
            onToggleSelectMode={() => {
              // 进出删除模式都要清掉勾选：退出去再进来时，上一轮的勾选不该还留着。
              if (selectMode) clearSelection();
              else setSelectMode(true);
            }}
            onStatus={setStatus}
          />
        </div>
      </section>
    </div>
  );
}
