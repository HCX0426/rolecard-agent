/** 菜单相关的状态与「鼠标移出后延时关闭」的小工具（从 ChatPage 抽出）。 */
import { useEffect, useRef, useState } from "react";

/**
 * 为什么抽出来：ChatPage 里菜单相关有 4 个开关 + 一个防抖定时器，混在对话逻辑里
 * 既难读也让组件行数失控。这里只管"开/关"，不含任何业务语义。
 *
 * `closeAllMenus` 是键盘退路：菜单靠鼠标移出关闭，Esc 必须也能关（a11y）。
 */
export function useMenus() {
  const [modelMenuOpen, setModelMenuOpen] = useState(false);
  const [roleMenuOpen, setRoleMenuOpen] = useState(false);
  /** 展开上下文选项（num_ctx）的模型行名。 */
  const [ctxOpen, setCtxOpen] = useState<string | null>(null);
  const menuCloseTimer = useRef<number | null>(null);

  // 菜单「鼠标移出后关闭」的短延时：留出从按钮移到面板的过渡时间，防抖动。
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
  function closeAllMenus() {
    setModelMenuOpen(false);
    setRoleMenuOpen(false);
    setCtxOpen(null);
  }

  // 卸载时清定时器：否则在延迟触发的瞬间组件已卸载（React 会警告状态更新到已卸载组件）。
  useEffect(
    () => () => {
      if (menuCloseTimer.current) window.clearTimeout(menuCloseTimer.current);
    },
    [],
  );

  return {
    modelMenuOpen,
    setModelMenuOpen,
    roleMenuOpen,
    setRoleMenuOpen,
    ctxOpen,
    setCtxOpen,
    armMenuClose,
    cancelMenuClose,
    closeAllMenus,
  };
}
