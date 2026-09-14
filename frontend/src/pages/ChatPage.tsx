import { useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  streamChat,
  type MessageRow,
  type RoleCard,
  type SessionRow,
} from "../api";

// 流式回答的临时气泡：token 逐段进入，message_replace 用权威文本覆盖，
// 流结束后用服务端 checkpoint 回放覆盖整个消息列表（前后端唯一真相在 checkpoint）。
interface LiveBubble {
  text: string;
  toolChips: string[];
  streaming: boolean;
}

export default function ChatPage() {
  const [sessions, setSessions] = useState<SessionRow[]>([]);
  const [roles, setRoles] = useState<RoleCard[]>([]);
  const [selectedRole, setSelectedRole] = useState<string>("");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<MessageRow[]>([]);
  const [live, setLive] = useState<LiveBubble | null>(null);
  const [input, setInput] = useState("");
  const [status, setStatus] = useState("");
  const sendingRef = useRef(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  const refreshSessions = useCallback(async () => {
    setSessions(await api.get<SessionRow[]>("/api/sessions"));
  }, []);

  useEffect(() => {
    refreshSessions().catch((e) => setStatus(`加载会话失败：${e.message}`));
    api
      .get<RoleCard[]>("/api/roles")
      .then((rows) => {
        setRoles(rows);
        if (rows.length > 0) setSelectedRole(rows[0].role_id);
      })
      .catch(() => {});
  }, [refreshSessions]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [messages, live]);

  async function newSession() {
    try {
      const s = await api.post<SessionRow>("/api/session", {
        role_id: selectedRole || null,
      });
      setSessionId(s.thread_id);
      setMessages([]);
      setLive(null);
      setStatus("");
      await refreshSessions();
    } catch (e) {
      setStatus(`新建会话失败：${(e as Error).message}`);
    }
  }

  async function selectSession(threadId: string) {
    if (sendingRef.current) return;
    setSessionId(threadId);
    setLive(null);
    try {
      setMessages(await api.get<MessageRow[]>(`/api/session/${threadId}/messages`));
      setStatus("");
    } catch (e) {
      setStatus(`加载历史失败：${(e as Error).message}`);
    }
  }

  async function deleteSession(threadId: string) {
    if (!confirm("删除这个会话？历史消息不可恢复。")) return;
    try {
      await api.del(`/api/session/${threadId}`);
      if (sessionId === threadId) {
        setSessionId(null);
        setMessages([]);
      }
      await refreshSessions();
    } catch (e) {
      setStatus(`删除失败：${(e as Error).message}`);
    }
  }

  async function switchRole(roleId: string) {
    setSelectedRole(roleId);
    if (!sessionId) return; // 尚无会话：仅记住选择，新会话时生效
    try {
      const r = await api.patch<{ role_name: string }>(`/api/session/${sessionId}`, {
        role_id: roleId,
      });
      setStatus(`已切换角色 → ${r.role_name}（下一轮生效，历史保留）`);
      await refreshSessions();
    } catch (e) {
      setStatus(`切换角色失败：${(e as Error).message}`);
    }
  }

  async function send() {
    const text = input.trim();
    if (!text || sendingRef.current) return;
    if (!sessionId) {
      setStatus("请先新建或选择一个会话");
      return;
    }
    sendingRef.current = true;
    setInput("");
    setStatus("");
    setMessages((m) => [...m, { role: "user", content: text }]);
    setLive({ text: "", toolChips: [], streaming: true });
    await streamChat(sessionId, text, (ev) => {
      if (ev.type === "token") {
        setLive((s) => (s ? { ...s, text: s.text + ev.text } : s));
      } else if (ev.type === "tool_call") {
        setLive((s) =>
          s ? { ...s, toolChips: [...s.toolChips, `🔧 调用 ${ev.name}`] } : s,
        );
      } else if (ev.type === "tool_result") {
        setLive((s) =>
          s
            ? {
                ...s,
                toolChips: [
                  ...s.toolChips,
                  `✅ ${ev.name} → ${ev.content.slice(0, 100)}`,
                ],
              }
            : s,
        );
      } else if (ev.type === "message_replace") {
        setLive((s) => (s ? { ...s, text: ev.text } : s));
      } else if (ev.type === "error") {
        setLive((s) => (s ? { ...s, text: `${s.text}\n[错误] ${ev.detail}` } : s));
      }
      // "end" 在下面统一收尾
    });
    // 流结束：checkpoint 是唯一真相，回放覆盖乐观状态
    try {
      setMessages(await api.get<MessageRow[]>(`/api/session/${sessionId}/messages`));
    } catch {
      /* 会话已被删等极端情况：保留现有气泡 */
      setLive(null);
    }
    setLive(null);
    sendingRef.current = false;
    await refreshSessions();
  }

  const current = sessions.find((s) => s.thread_id === sessionId);

  return (
    <div className="flex h-full">
      {/* 会话列表面板 */}
      <aside className="flex w-64 shrink-0 flex-col border-r border-slate-200 bg-white">
        <div className="border-b border-slate-100 p-3">
          <button
            onClick={newSession}
            className="w-full rounded-lg bg-blue-600 px-3 py-2 text-sm font-medium text-white hover:bg-blue-700"
          >
            ＋ 新建对话
          </button>
          <select
            value={selectedRole}
            onChange={(e) => switchRole(e.target.value)}
            className="mt-2 w-full rounded-lg border border-slate-200 px-2 py-1.5 text-xs"
          >
            {roles.map((r) => (
              <option key={r.role_id} value={r.role_id}>
                {r.role_name}
              </option>
            ))}
          </select>
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
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  deleteSession(s.thread_id);
                }}
                className="hidden shrink-0 rounded px-1 text-xs text-slate-300 hover:text-red-500 group-hover:block"
                title="删除会话"
              >
                ✕
              </button>
            </div>
          ))}
        </div>
      </aside>

      {/* 对话区 */}
      <section className="flex min-w-0 flex-1 flex-col">
        <header className="border-b border-slate-200 bg-white px-5 py-3">
          <h2 className="text-sm font-medium text-slate-900">
            {current ? current.title || "新会话" : "对话"}
          </h2>
          <p className="mt-0.5 text-xs text-slate-400">
            {current
              ? `当前角色：${current.role_name || current.role_id} · 切换角色后下一轮生效`
              : "新建或从左侧选择一个会话开始"}
          </p>
        </header>

        <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto px-6 py-5">
          {!sessionId && messages.length === 0 && !live && (
            <p className="mt-10 text-center text-sm text-slate-400">
              左上角「＋ 新建对话」开始
            </p>
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
                  <div className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-1.5 font-mono text-xs text-slate-500">
                    🛠 {m.name}：{m.content.slice(0, 120)}
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
                  {live.toolChips.map((c, i) => (
                    <div
                      key={i}
                      className="mb-1.5 rounded-md border border-slate-200 bg-slate-50 px-2 py-1 font-mono text-xs break-all text-slate-500"
                    >
                      {c}
                    </div>
                  ))}
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

        {status && (
          <div className="border-t border-amber-100 bg-amber-50 px-6 py-1.5 text-xs text-amber-700">
            {status}
          </div>
        )}

        <div className="border-t border-slate-200 bg-white p-4">
          <div className="mx-auto flex max-w-3xl gap-2">
            <input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.nativeEvent.isComposing) send();
              }}
              placeholder={sessionId ? "输入消息，回车发送" : "先新建或选择会话"}
              disabled={!sessionId || sendingRef.current}
              className="flex-1 rounded-xl border border-slate-200 px-4 py-2.5 outline-none focus:border-blue-400 disabled:bg-slate-50"
            />
            <button
              onClick={send}
              disabled={!sessionId || sendingRef.current}
              className="rounded-xl bg-blue-600 px-5 text-sm font-medium text-white hover:bg-blue-700 disabled:bg-slate-300"
            >
              发送
            </button>
          </div>
        </div>
      </section>
    </div>
  );
}
