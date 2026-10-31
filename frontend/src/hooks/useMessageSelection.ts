/** 多选删除模式：勾选（自动扩展到整轮）与清理（从 ChatPage 抽出）。 */
import { useCallback, useEffect, useRef, useState } from "react";

import type { MessageRow } from "../api";
import { expandSelection } from "../lib/turns";

/**
 * 为什么抽出来：这三个状态 + 扩展规则是一组自洽的"选择语义"，与流式/上传无关。
 *
 * `onClear`：切对话 / 新建对话时调用方还要清掉别的编辑态（如正在编辑的消息），
 * 由调用方传进来 —— 避免这个 hook 反过来依赖 ChatPage 的具体状态。
 */
export function useMessageSelection(messages: MessageRow[], onClear?: () => void) {
  const [selectMode, setSelectMode] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);

  /** `messages` 的活引用：`toggleSelect` 要用**最新**那份消息做整轮扩展，但它必须
   * 身份稳定（否则每一行 TurnRow 的 memo 都会被这个回调打穿 —— R1031-31）。
   * 用 ref 装消息、回调读 ref：回调本身只创建一次，语义仍是"按当下的消息扩展"。
   * 赋值放 effect 而非渲染期：渲染期改 ref 是 React 明令禁止的（react-hooks/refs）。 */
  const messagesRef = useRef(messages);
  useEffect(() => {
    messagesRef.current = messages;
  });

  /** 退出多选删除模式（切会话、新建会话、进出删除模式都必须调）。
   *
   * 为什么不能只靠"退出选择"按钮：勾选的是**消息 id**，而 id 属于某一个对话 —— 在 A 里
   * 勾两条再切到 B，顶部横幅还写着"已选 2 条"、复选框却全空；点"删除所选"会把 A 的 id
   * 发给 B（后端 404，审查报告 P1-9）。 */
  function clearSelection() {
    setSelectMode(false);
    setSelected([]);
    onClear?.();
  }

  /** 勾选/取消一条消息：自动扩展到整轮（与后端 expand_to_turns 同一规则）。 */
  const toggleSelect = useCallback((id: string) => {
    setSelected((cur) => {
      const next = new Set(cur);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return expandSelection(messagesRef.current, [...next]);
    });
  }, []);

  return {
    selectMode,
    setSelectMode,
    selected,
    setSelected,
    clearSelection,
    toggleSelect,
  };
}
