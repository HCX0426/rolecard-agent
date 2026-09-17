/** 多选删除模式：勾选（自动扩展到整轮）与清理（从 ChatPage 抽出）。 */
import { useState } from "react";

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
  const [confirmDelete, setConfirmDelete] = useState(false);

  /** 退出多选删除模式（切会话、新建会话、进出删除模式都必须调）。
   *
   * 为什么不能只靠"退出选择"按钮：勾选的是**消息 id**，而 id 属于某一个对话 —— 在 A 里
   * 勾两条再切到 B，顶部横幅还写着"已选 2 条"、复选框却全空；点"删除所选"会把 A 的 id
   * 发给 B（后端 404，审查报告 P1-9）。 */
  function clearSelection() {
    setSelectMode(false);
    setSelected([]);
    setConfirmDelete(false);
    onClear?.();
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

  return {
    selectMode,
    setSelectMode,
    selected,
    setSelected,
    confirmDelete,
    setConfirmDelete,
    clearSelection,
    toggleSelect,
  };
}
