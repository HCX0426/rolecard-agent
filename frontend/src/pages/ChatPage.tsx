import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  streamChat,
  streamEdit,
  type ExtractResult,
  type MessageRow,
  type ModelProvider,
  type ModelSettings,
  type RoleCard,
  type SessionContext,
  type SessionRow,
} from "../api";
import { ToastStack, useToasts, type Tone } from "../components/Toast";
import {
  describeTrim,
  newLiveBubble,
  reduceChatEvent,
  type LiveBubble,
  type StreamMeta,
} from "../lib/stream";
import { describeExtract, describeUpload } from "../lib/uploadOutcome";
import { buildTurns, expandSelection, type BuiltTurn } from "../lib/turns";
import ProcessPanel from "../components/chat/ProcessPanel";
import ToolStepCard from "../components/chat/ToolStepCard";
import {
  IconClip,
  IconModel,
  IconSend,
  IconSparkle,
  IconStop,
  IconUser,
} from "../components/chat/icons";
import ThinkingPanel from "../components/chat/ThinkingPanel";
import { Markdown } from "../components/Markdown";

/** 回答耗时：created_at 配对（用户 → 助手）换算成可读时长；无时间戳的旧消息返回 null。 */
function fmtDuration(from: string, to: string): string | null {
  if (!from || !to) return null;
  const a = new Date(from.replace(" ", "T"));
  const b = new Date(to.replace(" ", "T"));
  if (isNaN(a.getTime()) || isNaN(b.getTime())) return null;
  const s = Math.max(0, Math.round((b.getTime() - a.getTime()) / 1000));
  if (s < 60) return `${s} 秒`;
  return `${Math.floor(s / 60)} 分 ${s % 60} 秒`;
}

export default function ChatPage() {
  const [sessions, setSessions] = useState<SessionRow[]>([]);
  const [roles, setRoles] = useState<RoleCard[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [currentRole, setCurrentRole] = useState<string>("");
  const [messages, setMessages] = useState<MessageRow[]>([]);
  const [live, setLive] = useState<LiveBubble | null>(null);
  const [input, setInput] = useState("");
  const inputRef = useRef<HTMLTextAreaElement>(null);
  // 输入框自适应高度：内容多时长高（封顶 160px 后内部滚动），发送/清空后缩回一行。
  useEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  }, [input]);
  const [confirmDel, setConfirmDel] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [editingTitle, setEditingTitle] = useState(false);
  const [titleDraft, setTitleDraft] = useState("");
  const [modelMenuOpen, setModelMenuOpen] = useState(false);
  const [roleMenuOpen, setRoleMenuOpen] = useState(false);
  const [ctxOpen, setCtxOpen] = useState<string | null>(null); // 展开上下文选项的模型行名
  // 菜单「鼠标移出后关闭」的短延时：留出从按钮移到面板的过渡时间，防抖动。
  const menuCloseTimer = useRef<number | null>(null);
  function armMenuClose(close: () => void) {
    if (menuCloseTimer.current) window.clearTimeout(menuCloseTimer.current);
    menuCloseTimer.current = window.setTimeout(close, 250);
  }
  function cancelMenuClose() {
    if (menuCloseTimer.current) {
      window.clearTimeout(menuCloseTimer.current);
      menuCloseTimer.current = null;
    }
  }
  const [backends, setBackends] = useState<
    { name: string; provider: string; model: string; usage: string; num_ctx: number | null }[]
  >([]);
  // 供应商 id → 中文档称（分组标题显示"硅基流动"而非原始 id）
  const [providerLabels, setProviderLabels] = useState<Record<string, string>>({});
  const [defaultBackend, setDefaultBackend] = useState("");
  const [sessionModel, setSessionModel] = useState<string | null>(null);
  const [busy, setBusy] = useState(false); // 流式进行中：驱动「停止」按钮与输入禁用
  const [sessionsOpen, setSessionsOpen] = useState(false); // 移动端会话栏抽屉
  // 上下文预算事实（H3 的界面部分）：>0 时提示"早期对话已折叠"。
  // 单独放在 state 而不是气泡里，是因为气泡在流结束时会被 checkpoint 回放**整体替换** ——
  // 挂在气泡上的提示会在回答刚结束时消失，用户根本来不及看到。
  const [trim, setTrim] = useState<{ dropped: number; kept: number } | null>(null);
  // 编辑重生成：正在编辑的那条消息（id + 草稿）
  const [editing, setEditing] = useState<{ id: string; text: string } | null>(null);
  const [copiedKey, setCopiedKey] = useState<string | null>(null); // 复制反馈（按轮 key）
  const [enhancing, setEnhancing] = useState(false); // 增强提示词进行中
  const [ctxBudget, setCtxBudget] = useState(0); // 上下文字符预算（后端 context 端点）
  // 多选删除模式：勾选若干消息（勾一侧自动带上整轮）
  const [selectMode, setSelectMode] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const [confirmDelete, setConfirmDelete] = useState(false);

  /** 退出多选删除模式（切会话、新建会话时必须调）。
   *
   * 为什么不能只靠「退出选择」按钮：勾选的是**消息 id**，而 id 属于某一个对话 —— 在 A 里
   * 勾两条再切到 B，顶部横幅还写着「已选 2 条」、复选框却全空；点「删除所选」会把 A 的 id
   * 发给 B，后端 404（审查报告 P1-9）。
   */
  function clearSelection() {
    setSelectMode(false);
    setSelected([]);
    setConfirmDelete(false);
    setEditing(null);
  }
  const fileRef = useRef<HTMLInputElement>(null);
  const sendingRef = useRef(false);
  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  // 气泡的"当前值"镜像：事件回调里需要读到最新气泡才能归约，而 setState 的更新函数
  // 可能在渲染期被调用（在更新函数里做副作用在 StrictMode 下会执行两次）。用 ref 明确持有。
  const liveRef = useRef<LiveBubble | null>(null);
  // 本轮是否收到过 error 事件（详情留到收尾时统一提示，见 applyMeta 的说明）。
  const errorRef = useRef<string>("");

  /** 把事件的旁路信息落到对应的界面状态上（气泡正文之外的信息）。 */
  const applyMeta = useCallback((meta: StreamMeta) => {
    if (meta.trimmed && meta.trimmed.dropped > 0) setTrim(meta.trimmed);
    // 错误详情必须**攒起来留到流结束后再说**：气泡会在收尾时被 checkpoint 回放整体替换，
    // 挂在气泡上的 `[错误] …` 跟着一起消失 —— 用户实际上看不到任何提示。
    if (meta.errored) errorRef.current = meta.errorDetail || "模型调用失败";
  }, []);

  // 状态提示统一走 toast（可叠加、自动消失、带语气）—— 一行 status 会被后来的消息覆盖，
  // 上一个操作的结果还没看清就没了。保留 setStatus 这个名字，既有调用点无需改动。
  const { toasts, push: pushToast, dismiss } = useToasts();
  const setStatus = (text: string, tone: Tone = "info") => {
    if (text) pushToast(text, tone);
  };

  const refreshSessions = useCallback(async () => {
    setSessions(await api.get<SessionRow[]>("/api/sessions"));
  }, []);

  useEffect(() => {
    refreshSessions().catch((e) => setStatus(`加载对话失败：${e.message}`, "warn"));
    api.get<RoleCard[]>("/api/roles").then(setRoles).catch(() => {});
    api.get<ModelSettings>("/api/settings/models").then((s) => {
      setBackends(
        s.backends.map((b) => ({
          name: b.name,
          provider: b.provider,
          model: b.model,
          usage: b.usage ?? "chat",
          num_ctx: b.num_ctx ?? null,
        })),
      );
      setDefaultBackend(s.default || s.backends[0]?.name || "");
    }).catch(() => {});
    api.get<{ providers: ModelProvider[] }>("/api/settings/model-providers")
      .then((s) => setProviderLabels(Object.fromEntries(s.providers.map((p) => [p.id, p.label]))))
      .catch(() => {});
  }, [refreshSessions]);

  // 头部的角色选择跟随当前会话（切会话时显示该会话自己的角色）
  useEffect(() => {
    const cur = sessions.find((s) => s.thread_id === sessionId);
    setCurrentRole(cur?.role_id || "");
  }, [sessionId, sessions]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [messages, live]);

  async function selectSession(threadId: string) {
    if (sendingRef.current) return;
    setSessionId(threadId);
    clearSelection(); // 勾选 / 编辑态属于上一个对话，不能跟着过来
    setConfirmDel(null);
    setLive(null);
    setModelMenuOpen(false);
    setSessionsOpen(false); // 移动端选中后收起抽屉
    try {
      const [msgs, detail, ctxInfo] = await Promise.all([
        api.get<MessageRow[]>(`/api/session/${threadId}/messages`),
        api.get<{ model_name: string | null }>(`/api/session/${threadId}`),
        // 上下文预算事实：刷新页面后「早期对话已折叠」这条提示同样要能显示出来
        // （它不是一次性的 SSE 事件，而是一个持续为真的状态）。
        api
          .get<SessionContext>(`/api/session/${threadId}/context`)
          .catch(() => ({ trimmed: 0, kept: 0, budget: 0 }) as SessionContext),
      ]);
      setMessages(msgs);
      setSessionModel(detail.model_name);
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

  /** 创建会话；上一个会话还没发过消息（无标题 = 空白）→ 直接打开它，不堆叠空会话。
   *  角色是可选的：不指定即用默认角色（内置「通用助手」），之后随时在功能行切换。
   *  返回可用的 thread_id（新建或复用的），失败返回 null。 */
  async function createSession(): Promise<string | null> {
    const empty = sessions.find((s) => !s.title);
    if (empty) {
      setStatus("上一次的对话还是空白，已直接为你打开");
      if (sessionId !== empty.thread_id) await selectSession(empty.thread_id);
      else setConfirmDel(null);
      return empty.thread_id;
    }
    try {
      const s = await api.post<SessionRow>("/api/session", {});
      setSessionId(s.thread_id);
      clearSelection();
      setConfirmDel(null);
      setMessages([]);
      setLive(null);
      liveRef.current = null;
      setTrim(null); // 新会话没有历史，也就谈不上"折叠"
      setStatus("");
      await refreshSessions();
      return s.thread_id;
    } catch (e) {
      setStatus(`新建对话失败：${(e as Error).message}`, "warn");
      return null;
    }
  }

  /** 拿到一个会话 id：已选就用，没有就先建一个（默认角色 = 通用助手）。
   *  让「进入时就能选角色/模型」成为可能 —— 选中即开会话，不用先点「新建对话」。 */
  async function ensureSession(): Promise<string | null> {
    if (sessionId) return sessionId;
    return createSession();
  }

  async function switchRole(roleId: string) {
    if (sessionId && roleId === currentRole) return;
    const tid = await ensureSession();
    if (!tid) return;
    try {
      const r = await api.patch<{ role_name: string }>(`/api/session/${tid}`, {
        role_id: roleId,
      });
      setCurrentRole(roleId);
      setStatus(`已切换角色 → ${r.role_name}（下一轮生效，历史保留）`, "ok");
      await refreshSessions();
    } catch (e) {
      setStatus(`切换角色失败：${(e as Error).message}`, "warn");
    }
  }

  async function doDelete(threadId: string) {
    try {
      await api.del(`/api/session/${threadId}`);
      setConfirmDel(null);
      if (sessionId === threadId) {
        setSessionId(null);
        setMessages([]);
      }
      await refreshSessions();
    } catch (e) {
      setStatus(`删除失败：${(e as Error).message}`, "warn");
    }
  }

  // 按轮分组（用户提问 → 过程步骤 → 最终回答）：切分逻辑在 lib/turns（与后端同规则）。
  const turns = buildTurns(messages);

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
    if (!uid || busy || !content) return;
    saveEdit(uid, content);
  }

  // 上下文使用率：已用字符按当前消息估算（展示口径，随消息实时更新），上限来自后端配置。
  const ctxUsed = messages.reduce((n, m) => n + (m.content?.length ?? 0) + (m.reasoning?.length ?? 0), 0);
  const ctxPct = ctxBudget > 0 ? Math.min(100, Math.round((ctxUsed / ctxBudget) * 100)) : 0;

  /** 设置某后端的上下文窗口（本地模型 num_ctx），保存后热重建、下一轮生效。 */
  async function setModelCtx(name: string, numCtx: number | null) {
    try {
      await api.setModelContext(name, numCtx);
      const ms = await api.get<ModelSettings>("/api/settings/models");
      setBackends(ms.backends.filter((b) => b.usage === "chat"));
    } catch (e) {
      setStatus(`设置上下文窗口失败：${(e as Error).message}`, "warn");
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

  async function send(preset?: string) {
    const text = (preset ?? input).trim();
    if (!text || sendingRef.current) return;
    sendingRef.current = true;
    setBusy(true);
    setInput("");
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
    setMessages((m) => [...m, { role: "user", content: text }]);
    const bubble = newLiveBubble();
    liveRef.current = bubble;
    setLive(bubble);

    const controller = new AbortController();
    abortRef.current = controller;
    await streamChat(
      tid,
      text,
      (ev) => {
        // 气泡归约是"事件 → 新气泡"的纯函数，逻辑在 lib/stream.ts 里被单测覆盖；
        // meta 是旁路信息（上下文裁剪 / 角色摘要 / 是否出错），不进入气泡正文。
        const prev = liveRef.current ?? newLiveBubble();
        const reduced = reduceChatEvent(prev, ev);
        liveRef.current = reduced.bubble;
        setLive(reduced.bubble);
        applyMeta(reduced.meta);
      },
      controller.signal,
    );
    const aborted = controller.signal.aborted;
    abortRef.current = null;
    // 流结束：checkpoint 是唯一真相，回放覆盖乐观状态（中断时同样回放，拿到已生成的部分）
    try {
      setMessages(await api.get<MessageRow[]>(`/api/session/${tid}/messages`));
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

  /** 停止生成：中断 SSE 连接。服务端已落 checkpoint 的部分会在收尾回放中显示出来。 */
  function stop() {
    abortRef.current?.abort();
  }

  async function handleUpload(file: File) {
    if (uploading) return;
    let tid = sessionId;
    if (!tid) {
      tid = await createSession();
      if (!tid) return;
    }
    setUploading(true);
    try {
      const fd = new FormData();
      fd.append("file", file);
      // 走 api.upload（长超时）：OCR 子进程本身允许 120s，30s 会把界面切成「假失败」，
      // 而后端其实已经把文件落盘并入索引了（审查报告 P1-4）。
      const r = await api.upload(tid, fd);

      // 三态反馈（登记但读不了 / 解析了没文本 / 已入索引）：判断逻辑抽到
      // lib/uploadOutcome.ts 并被单测覆盖 —— 这段分支以前只能靠人工点页面验。
      const uploadOutcome = describeUpload(r);
      if (uploadOutcome) {
        setStatus(uploadOutcome.text, uploadOutcome.tone);
        return;
      }

      // v2.3：已入索引 → 自动触发结构化抽取（独立请求 + 进度提示，不拖慢上传本身）。
      // 抽取失败不算上传失败：原文已可提问，指标提取可以重试。
      setStatus(`「${r.file}」已入检索索引，AI 识别指标中…`, "info");
      let result: ExtractResult | null = null;
      let extractError = "";
      try {
        result = await api.extractRecord(r.task_id);
      } catch (e) {
        extractError = (e as Error).message;
      }
      const outcome = describeExtract(result, extractError, r.file);
      setStatus(outcome.text, outcome.tone);
    } catch (e) {
      setStatus(`上传失败：${(e as Error).message}`, "warn");
    } finally {
      setUploading(false);
      // 注入的说明消息已进 checkpoint，回放让用户看到
      try {
        setMessages(await api.get<MessageRow[]>(`/api/session/${tid}/messages`));
      } catch {
        /* 会话可能已被删除 */
      }
    }
  }

  /** 进入/退出编辑态。 */
  function startEdit(id: string, text: string) {
    setEditing({ id, text });
  }

  /** 编辑保存 = 从该条重新生成：SSE 与普通对话完全一致，结束后回放刷新历史。 */
  async function saveEdit(overrideId?: string, overrideText?: string) {
    // override：直接指定要重新生成的用户消息（消息行「重新生成」按钮复用同一通道）
    const mid = overrideId ?? editing?.id;
    const content = (overrideText ?? editing?.text ?? "").trim();
    if (!mid || !sessionId || sendingRef.current || !content) return;
    sendingRef.current = true;
    setBusy(true);
    const controller = new AbortController();
    abortRef.current = controller;
    const bubble = newLiveBubble();
    liveRef.current = bubble;
    setLive(bubble);
    setEditing(null);
    await streamEdit(
      sessionId,
      mid,
      content,
      (ev) => {
        const prev = liveRef.current ?? newLiveBubble();
        const reduced = reduceChatEvent(prev, ev);
        liveRef.current = reduced.bubble;
        setLive(reduced.bubble);
        applyMeta(reduced.meta);
      },
      controller.signal,
    );
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
        setMessages(await api.get<MessageRow[]>(`/api/session/${sessionId}/messages`));
      } catch {
        /* 会话可能已删除 */
      }
    }
  }

  /** 勾选/取消一条消息：自动扩展到整轮（与后端 expand_to_turns 同一规则）。 */
  function toggleSelect(id: string) {
    setSelected((cur) => {
      const next = new Set(cur);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return expandSelection(messages, [...next]);
    });
  }

  /** 删除所选（后端按整轮扩展并写审计语义上的不可恢复操作）。 */
  async function deleteSelected() {
    if (!sessionId || selected.length === 0) return;
    setConfirmDelete(false);
    try {
      await api.post<{ deleted: number }>(`/api/session/${sessionId}/messages/delete`, {
        message_ids: selected,
      });
      setSelected([]);
      setSelectMode(false);
      setStatus(`已删除所选对话`, "ok");
      setMessages(await api.get<MessageRow[]>(`/api/session/${sessionId}/messages`));
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

  const current = sessions.find((s) => s.thread_id === sessionId);
  const roleBackend = roles.find((r) => r.role_id === (current?.role_id || ""))?.model_name || null;
  const effectiveBackend = sessionModel || roleBackend || defaultBackend;
  // 进入时还没有会话：角色下拉默认停在「通用助手」，让用户一眼看到默认角色且可直接选。
  const defaultRoleId =
    roles.find((r) => r.role_id === "general_assistant")?.role_id || roles[0]?.role_id || "";
  const displayRole = currentRole || defaultRoleId;
  const grouped = useMemo(() => {
    const g: Record<string, typeof backends> = {};
    // 模型菜单只显示**对话**后端（usage=chat）；嵌入/重排/OCR 凭据行在服务页按用途引用。
    for (const b of backends.filter((x) => x.usage === "chat")) (g[b.provider] ||= []).push(b);
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
            className="w-full rounded-lg bg-blue-600 px-3 py-2 text-sm font-medium text-white hover:bg-blue-700"
          >
            ＋ 新建对话
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto p-2">
          {sessions.length === 0 && (
            <p className="px-2 py-4 text-xs text-slate-400 dark:text-slate-500">还没有对话</p>
          )}
          {sessions.map((s) => (
            <div
              key={s.thread_id}
              className={`group mb-1 flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-2 text-sm ${
                s.thread_id === sessionId ? "bg-blue-50 dark:bg-blue-900/30 text-blue-800" : "hover:bg-slate-50 dark:bg-slate-800/50 dark:hover:bg-slate-700/60"
              }`}
              onClick={() => selectSession(s.thread_id)}
            >
              <div className="min-w-0 flex-1">
                <div className="truncate">{s.title || "新对话"}</div>
                <div className="truncate text-[11px] text-slate-400 dark:text-slate-500">
                  {s.role_name || s.role_id}
                </div>
              </div>
              {confirmDel === s.thread_id ? (
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    doDelete(s.thread_id);
                  }}
                  className="shrink-0 rounded bg-red-500 px-1.5 py-0.5 text-[11px] text-white hover:bg-red-600"
                >
                  删除
                </button>
              ) : (
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    setConfirmDel(s.thread_id);
                  }}
                  className="shrink-0 rounded px-1 py-0.5 text-xs text-slate-300 dark:text-slate-600 opacity-0 transition-opacity hover:bg-slate-100 dark:bg-slate-700/50 dark:hover:bg-slate-700 hover:text-red-500 group-hover:opacity-100"
                  title="删除对话"
                >
                  ✕
                </button>
              )}
            </div>
          ))}
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
                className="rounded px-2 py-1 text-xs text-slate-500 dark:text-slate-400 dark:text-slate-500 hover:bg-slate-100 dark:bg-slate-700/50 dark:hover:bg-slate-700"
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
                  className="rounded px-1.5 py-0.5 text-xs text-slate-300 dark:text-slate-600 opacity-0 transition-opacity hover:bg-slate-100 dark:bg-slate-700/50 dark:hover:bg-slate-700 hover:text-slate-600 dark:text-slate-300 dark:text-slate-600 group-hover/title:opacity-100"
                  title="重命名对话"
                >
                  ✎
                </button>
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
                直接在下方输入即可（会自动创建对话），或点左上角「＋ 新建对话」。
              </p>
              <ul className="mt-3 space-y-1.5 text-xs leading-relaxed text-slate-500 dark:text-slate-400 dark:text-slate-500">
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
                            onChange={(e) => setEditing({ id: editing.id, text: e.target.value })}
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
                              onClick={() => startEdit(userMid, turn.user!.content)}
                              aria-label="编辑并重答"
                              title="编辑这条消息并重新生成（之后的对话会被作废）"
                              className="absolute -left-9 top-2 rounded-full p-1.5 text-slate-400 opacity-0 transition-opacity hover:bg-blue-50 hover:text-blue-600 group-hover:opacity-100 dark:text-slate-500 dark:hover:bg-slate-700/60 dark:hover:text-blue-400"
                            >
                              <svg viewBox="0 0 20 20" fill="currentColor" className="h-3.5 w-3.5" aria-hidden="true">
                                <path d="M13.586 3.586a2 2 0 112.828 2.828l-.793.793-2.828-2.828.793-.793zM11.379 5.793L3 14.172V17h2.828l8.38-8.379-2.83-2.828z" />
                              </svg>
                            </button>
                          )}
                          <div className="rounded-2xl rounded-br-sm bg-slate-200/90 px-4 py-2.5 whitespace-pre-wrap text-slate-900 dark:bg-slate-700 dark:text-slate-100">
                            {turn.user.content}
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
                  {live.text ? <Markdown text={live.text} /> : "…"}
                </div>
              </div>
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
              {confirmDelete ? (
                <>
                  <button
                    onClick={deleteSelected}
                    className="rounded bg-red-500 px-2.5 py-1 text-white hover:bg-red-600"
                  >
                    确认删除
                  </button>
                  <button
                    onClick={() => setConfirmDelete(false)}
                    className="rounded px-2 py-1 hover:bg-amber-100 dark:hover:bg-amber-900/40"
                  >
                    再想想
                  </button>
                </>
              ) : (
                <button
                  onClick={() => setConfirmDelete(true)}
                  className="rounded bg-red-500 px-2.5 py-1 text-white hover:bg-red-600"
                >
                  删除所选
                </button>
              )}
              <button
                onClick={clearSelection}
                className="rounded px-2 py-1 hover:bg-amber-100 dark:hover:bg-amber-900/40"
              >
                退出选择
              </button>
            </div>
          )}
          {selectMode && selected.length === 0 && (
            <div className="mx-auto mb-2 max-w-3xl rounded-lg border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800/50 px-3 py-2 text-xs text-slate-500 dark:text-slate-400 dark:text-slate-500">
              删除模式：勾选任意一问或一答，会自动带上配对的另一侧；选好后点右下「删除所选」。
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
                <button
                  onClick={enhance}
                  disabled={busy || enhancing || !input.trim()}
                  title="增强提示词：把草稿改写得更清晰、具体（一次模型调用）"
                  className="flex items-center gap-1 rounded-lg px-2 py-1 text-[11px] text-slate-500 hover:bg-slate-100 disabled:opacity-40 dark:text-slate-400 dark:hover:bg-slate-700/60"
                >
                  <IconSparkle />
                  {enhancing ? "增强中…" : "增强提示词"}
                </button>
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
            >
              {/* 角色切换（WorkBuddy 式自定义菜单）：原生 select 的弹层系统绘制、样式突兀，
                  换成与模型菜单同款的面板——角色名 + 内置徽标 + 当前项勾选。 */}
              <button
                onClick={() => {
                  setModelMenuOpen(false); // 两个菜单互斥
                  setRoleMenuOpen((o) => !o);
                }}
                title="切换当前对话的角色（下一轮生效）"
                className="flex items-center gap-1.5 rounded-full border border-slate-200 bg-white px-3 py-1 text-xs text-slate-600 hover:border-blue-300 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300 dark:hover:border-blue-700"
              >
                <IconUser />
                {roles.find((r) => r.role_id === displayRole)?.role_name ?? "角色"} ▾
              </button>
              {roleMenuOpen && (
                <>
                  <div className="absolute bottom-full left-0 z-20 mb-2 w-56 overflow-hidden rounded-xl border border-slate-200 bg-white shadow-lg dark:border-slate-600 dark:bg-slate-800">
                  <p className="bg-slate-50 px-3 py-1.5 text-[11px] font-medium text-slate-400 dark:bg-slate-800/60 dark:text-slate-500">
                    切换角色（下一轮生效，历史保留）
                  </p>
                  {roles.map((r) => (
                    <button
                      key={r.role_id}
                      onClick={() => {
                        setRoleMenuOpen(false);
                        switchRole(r.role_id);
                      }}
                      className="flex w-full items-center justify-between px-3 py-2 text-xs hover:bg-blue-50 dark:hover:bg-blue-900/30"
                    >
                      <span className="truncate text-slate-700 dark:text-slate-200">{r.role_name}</span>
                      <span className="ml-2 flex shrink-0 items-center gap-1.5">
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
                  ))}
                  </div>
                </>
              )}
            </span>
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
                title="切换本对话使用的模型（按供应商分组；选中即开对话）"
                className="flex items-center gap-1.5 rounded-full border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 px-3 py-1 text-xs text-slate-600 dark:text-slate-300 dark:text-slate-600 hover:border-blue-300 dark:hover:border-blue-700"
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
                              <span className="font-mono">{b.model}</span>
                              <span className="ml-2 flex min-w-0 items-center gap-1.5">
                                <span className="truncate text-slate-400 dark:text-slate-500">{b.name}</span>
                              </span>
                              {effectiveBackend === b.name && (
                                <span className="ml-1 text-blue-600 dark:text-blue-400">✓</span>
                              )}
                            </button>
                            {(b.provider === "ollama" || b.provider === "local") && (
                              <button
                                onClick={() => setCtxOpen((c) => (c === b.name ? null : b.name))}
                                title="设置该模型的上下文窗口（num_ctx）"
                                className="mr-2 shrink-0 rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-500 hover:bg-slate-200 dark:bg-slate-700/60 dark:text-slate-400 dark:hover:bg-slate-600"
                              >
                                {b.num_ctx ? `${Math.round(b.num_ctx / 1024)}k ▾` : "上下文 ▾"}
                              </button>
                            )}
                          </div>
                          {/* 上下文选项：点行内「上下文」徽章展开（inline，触屏可用） */}
                          {(b.provider === "ollama" || b.provider === "local") &&
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
              className="flex items-center gap-1.5 rounded-full border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 px-3 py-1 text-xs text-slate-600 dark:text-slate-300 dark:text-slate-600 hover:border-blue-300 dark:hover:border-blue-700 disabled:opacity-50"
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
                  : "border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 text-slate-600 dark:text-slate-300 dark:text-slate-600 hover:border-amber-300 hover:text-amber-600"
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

      <ToastStack toasts={toasts} onDismiss={dismiss} />
    </div>
  );
}
