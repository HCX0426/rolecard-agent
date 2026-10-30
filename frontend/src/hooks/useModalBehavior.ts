import { useEffect, useRef, type RefObject } from "react";
/** 模态行为（Esc 关闭 + 打开时聚焦面板）的唯一实现。
 *
 * 为什么抽出来（2026-10-30 交互审计 #12 的收法）：`ui/Modal` 的 docstring 立着
 * "模态行为只有一处实现"，而审批/收件箱两扇 w-80 自绘头部的抽屉整迁 Modal 会动
 * 布局（头一行塞着清空/筛选按钮）—— 行为与版式解耦：两边共用这个钩子，版式各留各的。
 *
 * 用法：面板根节点挂 `ref` + `tabIndex={-1}` + `role="dialog"`，调用处
 * `useModalBehavior(open, onClose, panelRef)`。焦点落在面板容器（`tabIndex=-1` 的
 * programmatic 容器，不是可聚焦控件 —— 与 Modal 同一口径，可聚焦控件各自带焦点反馈）。
 */
export function useModalBehavior(
  open: boolean,
  onClose: () => void,
): RefObject<HTMLDivElement> {
  const panelRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    panelRef.current?.focus();
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  return panelRef;
}
