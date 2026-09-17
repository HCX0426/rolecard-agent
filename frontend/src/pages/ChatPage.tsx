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
  type ToolStep,
} from "../lib/stream";
import { describeExtract, describeUpload, type UploadResponse } from "../lib/uploadOutcome";
import { expandSelection } from "../lib/turns";
import { ThinkingPanel } from "../lib/ThinkingPanel";
import { Markdown } from "../components/Markdown";

// 快捷问题：空会话时直接点着问（对齐 WorkBuddy 输入框上方的建议 chips）
const QUICK_PROMPTS = ["帮我查一下结石直径的变化", "我有哪些报告？"];

// 功能行图标同样用内联 SVG（emoji 在缺彩色字体的环境会变方框，见 App.tsx 的说明）。
const ICON = {
  width: 13,
  height: 13,
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 2,
  strokeLinecap: "round",
  strokeLinejoin: "round",
} as const;

const IconUser = () => (
  <svg {...ICON}>
    <path d="M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM4 21c0-4 3.6-6 8-6s8 2 8 6" />
  </svg>
);

const IconModel = () => (
  <svg {...ICON}>
    <rect x="6" y="6" width="12" height="12" rx="2" />
    <path d="M10 3v3M14 3v3M10 18v3M14 18v3M3 10h3M3 14h3M18 10h3M18 14h3" />
  </svg>
);

const IconClip = () => (
  <svg {...ICON}>
    <path d="M8 12l6.5-6.5a3 3 0 0 1 4.2 4.2L11 17.4a5 5 0 0 1-7.1-7.1L11 3.2" />
  </svg>
);

/** 工具调用卡片：状态点 + 名称 + 可展开的完整结果（历史回放里的工具结果也用它）。 */
function ToolStepCard({ step }: { step: ToolStep }) {
  const [open, setOpen] = useState(false);
  const dot =
    step.status === "running"
      ? "bg-blue-400 animate-pulse"
      : step.status === "error"
        ? "bg-red-400"
        : "bg-green-500 dark:bg-green-600";
  const body = step.content.trim();
  // 入参摘要：让"过程"可见（搜了什么词 / 抓了哪个地址），截断到一行。
  const argsSummary = Object.entries(step.args ?? {})
    .filter(([, v]) => v !== null && v !== undefined && String(v).trim() !== "")
    .map(([k, v]) => `${k}=${String(v)}`)
    .join("  ")
    .slice(0, 80);
  return (
    <div className="rounded-lg border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800/50 px-2.5 py-1.5">
      <button
        onClick={() => body && setOpen((o) => !o)}
        className={`flex w-full items-center gap-2 text-left font-mono text-xs text-slate-600 dark:text-slate-300 dark:text-slate-600 ${
          body ? "cursor-pointer" : "cursor-default"
        }`}
      >
        <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${dot}`} />
        <span className="shrink-0">{step.name}</span>
        {argsSummary && (
          <span className="min-w-0 flex-1 truncate text-slate-400 dark:text-slate-500">
            {argsSummary}
          </span>
        )}
        {step.status === "running" && <span className="shrink-0 text-slate-400 dark:text-slate-500">执行中…</span>}
        {body && (
          <span className="ml-auto shrink-0 text-slate-300 dark:text-slate-600">
            {open ? "收起 ▴" : `${body.length} 字 ▾`}
          </span>
        )}
      </button>
      {open && body && (
        <pre className="mt-1.5 max-h-56 overflow-auto rounded bg-white dark:bg-slate-800 p-2 text-[11px] whitespace-pre-wrap text-slate-600 dark:text-slate-300 dark:text-slate-600">
          {body}
        </pre>
      )}
    </div>
  );
}

export default function ChatPage({
  onOpenSettings,
}: {
  onOpenSettings: () => void;
}) {
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
  const [backends, setBackends] = useState<{ name: string; provider: string; model: string; usage: string }[]>([]);
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
  // 多选删除模式：勾选若干消息（勾一侧自动带上整轮）
  const [selectMode, setSelectMode] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const [confirmDelete, setConfirmDelete] = useState(false);
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
    refreshSessions().catch((e) => setStatus(`加载会话失败：${e.message}`, "warn"));
    api.get<RoleCard[]>("/api/roles").then(setRoles).catch(() => {});
    api.get<ModelSettings>("/api/settings/models").then((s) => {
      setBackends(s.backends.map((b) => ({ name: b.name, provider: b.provider, model: b.model, usage: b.usage ?? "chat" })));
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
      setStatus("上一次的会话还是空白，已直接为你打开");
      if (sessionId !== empty.thread_id) await selectSession(empty.thread_id);
      else setConfirmDel(null);
      return empty.thread_id;
    }
    try {
      const s = await api.post<SessionRow>("/api/session", {});
      setSessionId(s.thread_id);
      setConfirmDel(null);
      setMessages([]);
      setLive(null);
      liveRef.current = null;
      setTrim(null); // 新会话没有历史，也就谈不上"折叠"
      setStatus("");
      await refreshSessions();
      return s.thread_id;
    } catch (e) {
      setStatus(`新建会话失败：${(e as Error).message}`, "warn");
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
      const r = await api.post<UploadResponse>(`/api/session/${tid}/upload`, fd);

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
  async function saveEdit() {
    if (!editing || !sessionId || sendingRef.current) return;
    const content = editing.text.trim();
    if (!content) return;
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
      editing.id,
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
        name ? `本会话已切换模型 → ${name}（下一轮生效）` : "已清除会话级模型覆盖（下一轮生效）",
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
          <p className="mt-1.5 text-[11px] leading-relaxed text-slate-400 dark:text-slate-500">
            默认「通用助手」＝纯对话（不接工具与档案）。需要其他能力时，在下方
            切换角色 —— 每个角色只暴露自己白名单内的工具。
          </p>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto p-2">
          {sessions.length === 0 && (
            <p className="px-2 py-4 text-xs text-slate-400 dark:text-slate-500">还没有会话</p>
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
                <div className="truncate">{s.title || "新会话"}</div>
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
                  title="删除会话"
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
          aria-label="关闭会话列表"
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
                aria-label="打开会话列表"
                className="rounded-lg border border-slate-200 px-2 py-0.5 text-xs text-slate-600 dark:border-slate-700 dark:text-slate-300 md:hidden"
              >
                会话
              </button>
              <h2 className="truncate text-sm font-medium text-slate-900 dark:text-slate-100">
                {current ? current.title || "新会话" : "对话"}
              </h2>
              {current && (
                <button
                  onClick={() => {
                    setTitleDraft(current.title || "");
                    setEditingTitle(true);
                  }}
                  className="rounded px-1.5 py-0.5 text-xs text-slate-300 dark:text-slate-600 opacity-0 transition-opacity hover:bg-slate-100 dark:bg-slate-700/50 dark:hover:bg-slate-700 hover:text-slate-600 dark:text-slate-300 dark:text-slate-600 group-hover/title:opacity-100"
                  title="重命名会话"
                >
                  ✎
                </button>
              )}
            </div>
          )}
          <p className="mt-0.5 text-xs text-slate-400 dark:text-slate-500">
            {current
              ? `当前角色：${currentRole ? roles.find((r) => r.role_id === currentRole)?.role_name || currentRole : "默认"} · 切换角色后下一轮生效`
              : "新建或从左侧选择一个会话开始"}
          </p>
        </header>

        <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto px-6 py-5">
          {!sessionId && messages.length === 0 && !live && (
            <div className="mx-auto mt-8 max-w-xl rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 p-5">
              <h3 className="text-sm font-medium text-slate-800 dark:text-slate-100">开始一次对话</h3>
              <p className="mt-1 text-xs text-slate-400 dark:text-slate-500">
                直接在下方输入即可（会自动创建会话），或点左上角「＋ 新建对话」。
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
          <div className="mx-auto flex max-w-3xl flex-col gap-4">
            {/* key 用消息的 checkpoint 寻址 id（M7）：流式 message_replace / 编辑重生成 /
                删除问答对时 React 按身份复用节点，编辑态与勾选才不会错位。
                仅乐观回显（发送瞬间本地追加、尚未刷新）没有 id，用序号兜底——
                它永远是列表末尾且存活只有一瞬，序号在这个窗口内是稳定的。 */}
            {messages.map((m, i) => {
              const mid = m.id ?? "";
              const isEditingThis = editing?.id === mid;
              const checked = selectMode && !!mid && selected.includes(mid);
              const rowTone = checked ? "opacity-60 ring-1 ring-amber-400" : "";
              return m.role === "user" ? (
                <div key={mid || `msg-${i}`} className={`group flex items-start justify-end gap-2 ${rowTone}`}>
                  {selectMode && !!mid && (
                    <input
                      type="checkbox"
                      aria-label={`选择这条消息：${m.content.slice(0, 12)}`}
                      checked={checked}
                      onChange={() => toggleSelect(mid)}
                      className="mt-3 h-3.5 w-3.5 accent-amber-500"
                    />
                  )}
                  <div className={isEditingThis ? "w-full max-w-[80%]" : "max-w-[80%]"}>
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
                            className="rounded px-2 py-1 text-slate-500 dark:text-slate-400 dark:text-slate-500 hover:bg-slate-100 dark:bg-slate-700/50 dark:hover:bg-slate-700"
                          >
                            取消
                          </button>
                          <button
                            onClick={saveEdit}
                            className="rounded bg-blue-600 px-2.5 py-1 text-white hover:bg-blue-700"
                          >
                            保存并重新生成
                          </button>
                        </div>
                      </div>
                    ) : (
                      <>
                        {/* AI IDE 式交互：悬停自己的消息时，气泡左侧浮现铅笔图标（absolute
                            悬浮，不占布局——此前 opacity-0 恒占 28px flex 空间，把气泡挤到
                            换行）。group-hover 而非常显——消息多时不干扰视线。 */}
                        {!busy && (
                          <button
                            onClick={() => startEdit(mid, m.content)}
                            aria-label="编辑并重答"
                            title="编辑这条消息并重新生成（之后的对话会被作废）"
                            className="absolute -left-9 top-2 rounded-full p-1.5 text-slate-400 opacity-0 transition-opacity hover:bg-blue-50 hover:text-blue-600 group-hover:opacity-100 dark:text-slate-500 dark:hover:bg-slate-700/60 dark:hover:text-blue-400"
                          >
                            <svg
                              viewBox="0 0 20 20"
                              fill="currentColor"
                              className="h-3.5 w-3.5"
                              aria-hidden="true"
                            >
                              <path d="M13.586 3.586a2 2 0 112.828 2.828l-.793.793-2.828-2.828.793-.793zM11.379 5.793L3 14.172V17h2.828l8.38-8.379-2.83-2.828z" />
                            </svg>
                          </button>
                        )}
                        <div className="w-fit rounded-2xl rounded-br-sm bg-blue-600 px-4 py-2.5 whitespace-pre-wrap text-white">
                          {m.content}
                          {m.ts && (
                            <p className="mt-1 text-right text-[10px] text-blue-200">{m.ts}</p>
                          )}
                        </div>
                      </>
                    )}
                  </div>
                </div>
              ) : m.role === "tool" ? (
                <div key={mid || `msg-${i}`} className={`flex items-start justify-start gap-2 ${rowTone}`}>
                  {selectMode && !!mid && (
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() => toggleSelect(mid)}
                      className="mt-3 h-3.5 w-3.5 accent-amber-500"
                    />
                  )}
                  <div className="w-full max-w-[85%]">
                    {/* id 仅 live 工具列表的 React key 用；回放卡片不在列表里，0 占位。
                        args：回放也显示"搜了什么"（serialize_message 按 tool_call_id 配对）。 */}
                    <ToolStepCard
                      step={{
                        id: 0,
                        name: m.name || "tool",
                        status: "ok",
                        content: m.content,
                        args: m.args,
                      }}
                    />
                    {m.ts && (
                      <p className="mt-1 text-[10px] text-slate-300 dark:text-slate-500">{m.ts}</p>
                    )}
                  </div>
                </div>
              ) : (
                <div key={mid || `msg-${i}`} className={`flex items-start justify-start gap-2 ${rowTone}`}>
                  {selectMode && !!mid && (
                    <input
                      type="checkbox"
                      aria-label={`选择这条回答：${m.content.slice(0, 12)}`}
                      checked={checked}
                      onChange={() => toggleSelect(mid)}
                      className="mt-3 h-3.5 w-3.5 accent-amber-500"
                    />
                  )}
                  <div className="max-w-[85%] rounded-2xl rounded-bl-sm border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 px-4 py-2.5">
                    {/* 回放的助手消息也可能带思考（后端 serialize_message 带 reasoning）：
                        轮次结束 live 气泡会被清掉，思考必须在这里再渲染一次才留得住。 */}
                    {/* 回放默认折叠：翻历史时主体是回答，推理按需展开 */}
                    <ThinkingPanel text={m.reasoning ?? ""} defaultOpen={false} />
                    <Markdown text={m.content} />
                    {m.ts && (
                      <p className="mt-1 text-right text-[10px] text-slate-300 dark:text-slate-500">{m.ts}</p>
                    )}
                  </div>
                </div>
              );
            })}
            {live && (
              <div className="flex justify-start">
                <div className="max-w-[85%] rounded-2xl rounded-bl-sm border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 px-4 py-2.5">
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
                onClick={() => {
                  setSelectMode(false);
                  setSelected([]);
                  setConfirmDelete(false);
                }}
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
          {/* 快捷问题（WorkBuddy 式建议 chips）：空会话时出现，点一下直接问 */}
          {sessionId && messages.length === 0 && !live && (
            <div className="mx-auto mb-2 flex max-w-3xl flex-wrap gap-2">
              {QUICK_PROMPTS.map((q) => (
                <button
                  key={q}
                  onClick={() => send(q)}
                  className="rounded-full border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 px-3 py-1.5 text-xs text-slate-600 dark:text-slate-300 dark:text-slate-600 hover:border-blue-300 dark:hover:border-blue-700 hover:text-blue-700 dark:text-blue-300"
                >
                  {q}
                </button>
              ))}
            </div>
          )}
          <div className="mx-auto flex max-w-3xl items-end gap-2">
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
                  ? "正在生成…（可点右侧「停止」）"
                  : "输入消息，Enter 发送 / Shift+Enter 换行（没有会话会自动创建）"
              }
              disabled={busy}
              className="max-h-40 flex-1 resize-none rounded-xl border border-slate-200 dark:border-slate-700 px-4 py-2.5 leading-relaxed outline-none focus:border-blue-400 disabled:bg-slate-50 dark:bg-slate-800/50"
            />
            {busy ? (
              <button
                onClick={stop}
                className="rounded-xl border border-slate-300 dark:border-slate-600 bg-white dark:bg-slate-800 px-5 text-sm font-medium text-slate-600 dark:text-slate-300 dark:text-slate-600 hover:border-red-300 dark:hover:border-red-700 hover:text-red-600 dark:text-red-400 dark:text-red-500"
              >
                停止
              </button>
            ) : (
              <button
                onClick={() => send()}
                className="rounded-xl bg-blue-600 px-5 text-sm font-medium text-white hover:bg-blue-700"
              >
                发送
              </button>
            )}
          </div>
          {/* 功能行（对齐 WorkBuddy：输入框下方一排功能）—— 全部对接真实后端能力 */}
          <div className="mx-auto mt-2 flex max-w-3xl items-center gap-2">
            <span
              title="切换当前会话的角色（可选，默认「通用助手」）"
              className="flex items-center gap-1.5 rounded-full border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 px-3 py-1 text-xs text-slate-600 dark:text-slate-300 dark:text-slate-600 hover:border-blue-300 dark:hover:border-blue-700"
            >
              <IconUser />
              <select
                value={displayRole}
                onChange={(e) => switchRole(e.target.value)}
                title="切换当前会话的角色（可选，默认通用助手；选中即开会话）"
                className="bg-transparent text-xs outline-none"
              >
                {roles.map((r) => (
                  <option key={r.role_id} value={r.role_id}>
                    {r.role_name}
                  </option>
                ))}
              </select>
            </span>
            <div className="relative">
              <button
                onClick={() => setModelMenuOpen((o) => !o)}
                title="切换本会话使用的模型（按供应商分组；选中即开会话）"
                className="flex items-center gap-1.5 rounded-full border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 px-3 py-1 text-xs text-slate-600 dark:text-slate-300 dark:text-slate-600 hover:border-blue-300 dark:hover:border-blue-700"
              >
                <IconModel />
                {backends.find((b) => b.name === effectiveBackend)?.model || effectiveBackend || "模型"} ▾
              </button>
              {modelMenuOpen && (
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
                        <button
                          key={b.name}
                          onClick={() => switchModel(b.name)}
                          className="flex w-full items-center justify-between px-3 py-1.5 text-xs hover:bg-blue-50 dark:bg-blue-900/30"
                        >
                          <span className="font-mono">{b.model}</span>
                          <span className="ml-2 truncate text-slate-400 dark:text-slate-500">{b.name}</span>
                          {effectiveBackend === b.name && (
                            <span className="ml-1 text-blue-600 dark:text-blue-400">✓</span>
                          )}
                        </button>
                      ))}
                    </div>
                  ))}
                  <button
                    onClick={() => {
                      setModelMenuOpen(false);
                      onOpenSettings?.();
                    }}
                    className="w-full border-t border-slate-100 dark:border-slate-800 px-3 py-2 text-[11px] text-slate-400 dark:text-slate-500 hover:text-blue-600 dark:text-blue-400"
                  >
                    管理后端与回退链 → 设置页
                  </button>
                </div>
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
                setSelectMode((v) => !v);
                setSelected([]);
                setConfirmDelete(false);
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
