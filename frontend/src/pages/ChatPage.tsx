import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  streamChat,
  type MessageRow,
  type ModelSettings,
  type RoleCard,
  type SessionRow,
} from "../api";
import { ToastStack, useToasts, type Tone } from "../components/Toast";

// 一次工具调用：状态 + 名称 + 结果。结构化展示，而不是一行被截断的原始文本。
interface ToolStep {
  name: string;
  status: "running" | "ok" | "error";
  content: string;
}

// 流式回答的临时气泡：token 逐段进入，message_replace 用权威文本覆盖，
// 流结束后用服务端 checkpoint 回放覆盖整个消息列表（前后端唯一真相在 checkpoint）。
interface LiveBubble {
  text: string;
  tools: ToolStep[];
  streaming: boolean;
}

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
        : "bg-green-500";
  const body = step.content.trim();
  return (
    <div className="rounded-lg border border-slate-200 bg-slate-50 px-2.5 py-1.5">
      <button
        onClick={() => body && setOpen((o) => !o)}
        className={`flex w-full items-center gap-2 text-left font-mono text-xs text-slate-600 ${
          body ? "cursor-pointer" : "cursor-default"
        }`}
      >
        <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${dot}`} />
        <span className="shrink-0">{step.name}</span>
        {step.status === "running" && <span className="text-slate-400">执行中…</span>}
        {body && (
          <span className="ml-auto shrink-0 text-slate-300">
            {open ? "收起 ▴" : `${body.length} 字 ▾`}
          </span>
        )}
      </button>
      {open && body && (
        <pre className="mt-1.5 max-h-56 overflow-auto rounded bg-white p-2 text-[11px] whitespace-pre-wrap text-slate-600">
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
  const [confirmDel, setConfirmDel] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [editingTitle, setEditingTitle] = useState(false);
  const [titleDraft, setTitleDraft] = useState("");
  const [modelMenuOpen, setModelMenuOpen] = useState(false);
  const [backends, setBackends] = useState<{ name: string; provider: string; model: string }[]>([]);
  const [defaultBackend, setDefaultBackend] = useState("");
  const [sessionModel, setSessionModel] = useState<string | null>(null);
  const [busy, setBusy] = useState(false); // 流式进行中：驱动「停止」按钮与输入禁用
  const fileRef = useRef<HTMLInputElement>(null);
  const sendingRef = useRef(false);
  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

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
      setBackends(s.backends.map((b) => ({ name: b.name, provider: b.provider, model: b.model })));
      setDefaultBackend(s.default || s.backends[0]?.name || "");
    }).catch(() => {});
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
    try {
      const [msgs, detail] = await Promise.all([
        api.get<MessageRow[]>(`/api/session/${threadId}/messages`),
        api.get<{ model_name: string | null }>(`/api/session/${threadId}`),
      ]);
      setMessages(msgs);
      setSessionModel(detail.model_name);
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
   *  角色是可选的：不指定即用默认角色（内置健康档案管理员），之后随时在功能行切换。
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
      setStatus("");
      await refreshSessions();
      return s.thread_id;
    } catch (e) {
      setStatus(`新建会话失败：${(e as Error).message}`, "warn");
      return null;
    }
  }

  async function switchRole(roleId: string) {
    if (!sessionId || roleId === currentRole) return;
    try {
      const r = await api.patch<{ role_name: string }>(`/api/session/${sessionId}`, {
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
    setLive({ text: "", tools: [], streaming: true });

    const controller = new AbortController();
    abortRef.current = controller;
    await streamChat(
      tid,
      text,
      (ev) => {
        if (ev.type === "token") {
          setLive((s) => (s ? { ...s, text: s.text + ev.text } : s));
        } else if (ev.type === "tool_call") {
          // 同一轮可能连续调多个工具：每个都开一张卡片，状态"执行中"
          setLive((s) =>
            s ? { ...s, tools: [...s.tools, { name: ev.name, status: "running", content: "" }] } : s,
          );
        } else if (ev.type === "tool_result") {
          // 把最近一个同名且"执行中"的卡片标记完成（离线/拒绝也算结果，不再另开卡片）
          setLive((s) => {
            if (!s) return s;
            const tools = [...s.tools];
            for (let i = tools.length - 1; i >= 0; i -= 1) {
              if (tools[i].name === ev.name && tools[i].status === "running") {
                tools[i] = { name: ev.name, status: "ok", content: ev.content };
                return { ...s, tools };
              }
            }
            return {
              ...s,
              tools: [...tools, { name: ev.name, status: "ok", content: ev.content }],
            };
          });
        } else if (ev.type === "message_replace") {
          setLive((s) => (s ? { ...s, text: ev.text } : s));
        } else if (ev.type === "error") {
          setLive((s) =>
            s
              ? {
                  ...s,
                  text: `${s.text}\n[错误] ${ev.detail}`,
                  tools: s.tools.map((t) =>
                    t.status === "running" ? { ...t, status: "error" as const } : t,
                  ),
                }
              : s,
          );
        }
        // "end" 在下面统一收尾
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
      const r = await api.post<{
        task_id: string;
        reused: boolean;
        file: string;
        status?: string;
      }>(`/api/session/${tid}/upload`, fd);
      // 三态反馈：让用户知道这份文件到底读没读进去（v2.2 起解析已真实生效，不再是"登记一下"）
      if (r.status === "indexed") {
        setStatus(`「${r.file}」已解析并入检索索引 —— 现在就可以对它提问`, "ok");
      } else if (r.status === "parsed") {
        setStatus(
          `「${r.file}」已解析，但没有提取到文本（可能是扫描件），暂未入检索`,
          "warn",
        );
      } else {
        setStatus(
          `「${r.file}」已登记，但当前无法解析（类型不支持，或图片 OCR 未配置）${
            r.reused ? "（同一文件此前已登记）" : ""
          }`,
          "warn",
        );
      }
      // 注入的说明消息已进 checkpoint，回放让用户看到
      setMessages(await api.get<MessageRow[]>(`/api/session/${tid}/messages`));
    } catch (e) {
      setStatus(`上传失败：${(e as Error).message}`, "warn");
    } finally {
      setUploading(false);
    }
  }

  async function switchModel(name: string | null) {
    if (!sessionId) return;
    try {
      await api.patch(`/api/session/${sessionId}`, { model_name: name });
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
  const grouped = useMemo(() => {
    const g: Record<string, typeof backends> = {};
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

  return (
    <div className="relative flex h-full">
      {/* 会话列表面板 */}
      <aside className="flex w-64 shrink-0 flex-col border-r border-slate-200 bg-white">
        <div className="border-b border-slate-100 p-3">
          <button
            onClick={newSession}
            className="w-full rounded-lg bg-blue-600 px-3 py-2 text-sm font-medium text-white hover:bg-blue-700"
          >
            ＋ 新建对话
          </button>
          <p className="mt-1.5 text-[11px] leading-relaxed text-slate-400">
            角色可选：默认为「健康档案管理员」，对话中可随时在右上角切换
          </p>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto p-2">
          {sessions.length === 0 && (
            <p className="px-2 py-4 text-xs text-slate-400">还没有会话</p>
          )}
          {sessions.map((s) => (
            <div
              key={s.thread_id}
              className={`group mb-1 flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-2 text-sm ${
                s.thread_id === sessionId ? "bg-blue-50 text-blue-800" : "hover:bg-slate-50"
              }`}
              onClick={() => selectSession(s.thread_id)}
            >
              <div className="min-w-0 flex-1">
                <div className="truncate">{s.title || "新会话"}</div>
                <div className="truncate text-[11px] text-slate-400">
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
                  className="shrink-0 rounded px-1 py-0.5 text-xs text-slate-300 opacity-0 transition-opacity hover:bg-slate-100 hover:text-red-500 group-hover:opacity-100"
                  title="删除会话"
                >
                  ✕
                </button>
              )}
            </div>
          ))}
        </div>
      </aside>

      {/* 对话区 */}
      <section className="flex min-w-0 flex-1 flex-col">
        <header className="border-b border-slate-200 bg-white px-5 py-3">
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
                className="rounded px-2 py-1 text-xs text-slate-500 hover:bg-slate-100"
              >
                取消
              </button>
            </div>
          ) : (
            <div className="group/title flex items-center gap-2">
              <h2 className="truncate text-sm font-medium text-slate-900">
                {current ? current.title || "新会话" : "对话"}
              </h2>
              {current && (
                <button
                  onClick={() => {
                    setTitleDraft(current.title || "");
                    setEditingTitle(true);
                  }}
                  className="rounded px-1.5 py-0.5 text-xs text-slate-300 opacity-0 transition-opacity hover:bg-slate-100 hover:text-slate-600 group-hover/title:opacity-100"
                  title="重命名会话"
                >
                  ✎
                </button>
              )}
            </div>
          )}
          <p className="mt-0.5 text-xs text-slate-400">
            {current
              ? `当前角色：${currentRole ? roles.find((r) => r.role_id === currentRole)?.role_name || currentRole : "默认"} · 切换角色后下一轮生效`
              : "新建或从左侧选择一个会话开始"}
          </p>
        </header>

        <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto px-6 py-5">
          {!sessionId && messages.length === 0 && !live && (
            <div className="mx-auto mt-8 max-w-xl rounded-xl border border-slate-200 bg-white p-5">
              <h3 className="text-sm font-medium text-slate-800">开始一次对话</h3>
              <p className="mt-1 text-xs text-slate-400">
                直接在下方输入即可（会自动创建会话），或点左上角「＋ 新建对话」。
              </p>
              <ul className="mt-3 space-y-1.5 text-xs leading-relaxed text-slate-500">
                <li>
                  · <b>上传报告 / 图片</b> —— 自动解析并入检索索引（.pdf/.docx/.pptx/.xlsx + 图片 OCR）
                </li>
                <li>
                  · <b>提问档案相关问题</b> —— 角色会调用工具查询，结果带来源与「是否已校验」标记
                </li>
                <li>· 下方功能行可切换角色与模型（下一轮生效，历史保留）</li>
              </ul>
            </div>
          )}
          <div className="mx-auto flex max-w-3xl flex-col gap-4">
            {messages.map((m, i) =>
              m.role === "user" ? (
                <div key={i} className="flex justify-end">
                  <div className="max-w-[80%] rounded-2xl rounded-br-sm bg-blue-600 px-4 py-2.5 whitespace-pre-wrap text-white">
                    {m.content}
                  </div>
                </div>
              ) : m.role === "tool" ? (
                <div key={i} className="flex justify-start">
                  <div className="w-full max-w-[85%]">
                    <ToolStepCard
                      step={{ name: m.name || "tool", status: "ok", content: m.content }}
                    />
                  </div>
                </div>
              ) : (
                <div key={i} className="flex justify-start">
                  <div className="max-w-[85%] rounded-2xl rounded-bl-sm border border-slate-200 bg-white px-4 py-2.5 whitespace-pre-wrap">
                    {m.content}
                  </div>
                </div>
              ),
            )}
            {live && (
              <div className="flex justify-start">
                <div className="max-w-[85%] rounded-2xl rounded-bl-sm border border-slate-200 bg-white px-4 py-2.5">
                  {live.tools.length > 0 && (
                    <div className="mb-2 space-y-1.5">
                      {live.tools.map((t, i) => (
                        <ToolStepCard key={i} step={t} />
                      ))}
                    </div>
                  )}
                  <div
                    className={`whitespace-pre-wrap ${live.streaming ? "caret" : ""}`}
                  >
                    {live.text || "…"}
                  </div>
                </div>
              </div>
            )}
          </div>
        </div>

        <div className="border-t border-slate-200 bg-white p-4">
          {/* 快捷问题（WorkBuddy 式建议 chips）：空会话时出现，点一下直接问 */}
          {sessionId && messages.length === 0 && !live && (
            <div className="mx-auto mb-2 flex max-w-3xl flex-wrap gap-2">
              {QUICK_PROMPTS.map((q) => (
                <button
                  key={q}
                  onClick={() => send(q)}
                  className="rounded-full border border-slate-200 bg-white px-3 py-1.5 text-xs text-slate-600 hover:border-blue-300 hover:text-blue-700"
                >
                  {q}
                </button>
              ))}
            </div>
          )}
          <div className="mx-auto flex max-w-3xl gap-2">
            <input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.nativeEvent.isComposing) send();
              }}
              placeholder={
                busy ? "正在生成…（可点右侧「停止」）" : "输入消息，回车发送（没有会话会自动创建）"
              }
              disabled={busy}
              className="flex-1 rounded-xl border border-slate-200 px-4 py-2.5 outline-none focus:border-blue-400 disabled:bg-slate-50"
            />
            {busy ? (
              <button
                onClick={stop}
                className="rounded-xl border border-slate-300 bg-white px-5 text-sm font-medium text-slate-600 hover:border-red-300 hover:text-red-600"
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
              title="切换当前会话的角色（可选，默认健康档案管理员）"
              className="flex items-center gap-1.5 rounded-full border border-slate-200 bg-white px-3 py-1 text-xs text-slate-600 hover:border-blue-300"
            >
              <IconUser />
              <select
                value={currentRole}
                onChange={(e) => switchRole(e.target.value)}
                disabled={!sessionId}
                className="bg-transparent text-xs outline-none disabled:opacity-50"
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
                disabled={!sessionId}
                title="切换本会话使用的模型（按供应商分组）"
                className="flex items-center gap-1.5 rounded-full border border-slate-200 bg-white px-3 py-1 text-xs text-slate-600 hover:border-blue-300 disabled:opacity-50"
              >
                <IconModel />
                {effectiveBackend || "模型"} ▾
              </button>
              {modelMenuOpen && (
                <div className="absolute bottom-full left-0 z-20 mb-2 max-h-72 w-72 overflow-y-auto rounded-xl border border-slate-200 bg-white shadow-lg">
                  <button
                    onClick={() => switchModel(null)}
                    className="flex w-full items-center justify-between px-3 py-2 text-xs hover:bg-blue-50"
                  >
                    <span>默认后端（跟随设置）</span>
                    {sessionModel === null && <span className="text-blue-600">✓</span>}
                  </button>
                  {grouped.map(([provider, list]) => (
                    <div key={provider}>
                      <p className="bg-slate-50 px-3 py-1 text-[11px] font-medium text-slate-400">
                        {provider}
                      </p>
                      {list.map((b) => (
                        <button
                          key={b.name}
                          onClick={() => switchModel(b.name)}
                          className="flex w-full items-center justify-between px-3 py-1.5 text-xs hover:bg-blue-50"
                        >
                          <span className="font-mono">{b.name}</span>
                          <span className="ml-2 truncate text-slate-400">{b.model}</span>
                          {effectiveBackend === b.name && (
                            <span className="ml-1 text-blue-600">✓</span>
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
                    className="w-full border-t border-slate-100 px-3 py-2 text-[11px] text-slate-400 hover:text-blue-600"
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
              className="flex items-center gap-1.5 rounded-full border border-slate-200 bg-white px-3 py-1 text-xs text-slate-600 hover:border-blue-300 disabled:opacity-50"
            >
              <IconClip />
              {uploading ? "上传中…" : "上传报告"}
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
            <span className="ml-auto text-[11px] text-slate-300">
              Enter 发送 · 生成中可停止 · 停用插件即刻生效
            </span>
          </div>
        </div>
      </section>

      <ToastStack toasts={toasts} onDismiss={dismiss} />
    </div>
  );
}
