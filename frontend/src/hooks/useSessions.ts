/** 会话列表（含移动端抽屉开关）与刷新逻辑（从 ChatPage 抽出）。 */
import { useCallback, useState } from "react";

import { api, type SessionRow } from "../api";

/**
 * 只管"有哪些会话"这一件事：选中某个会话会连带加载消息 / 角色 / 模型，那是
 * ChatPage 的职责（耦合在 selectSession 里），刻意不搬进来。
 *
 * 刷新失败会抛出：提示方式由调用方决定（ChatPage 用 toast）。
 */
export function useSessions() {
  const [sessions, setSessions] = useState<SessionRow[]>([]);
  /** 移动端会话栏抽屉 */
  const [sessionsOpen, setSessionsOpen] = useState(false);

  // 失败**抛出**由调用方决定怎么提示：这里只负责"有哪些会话"。
  const refresh = useCallback(async () => {
    setSessions(await api.get<SessionRow[]>("/api/sessions"));
  }, []);

  return {
    sessions,
    setSessions,
    sessionsOpen,
    setSessionsOpen,
    refreshSessions: refresh,
  };
}
