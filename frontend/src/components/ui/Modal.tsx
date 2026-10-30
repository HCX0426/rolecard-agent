import { useEffect, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useModalBehavior } from "../../hooks/useModalBehavior";

import Button from "./Button";

/** 受控模态框：ESC / 点遮罩关闭、基础焦点管理、暗色态一致。
 *  通过 portal 渲染到 document.body，调用方无需 relative 容器。
 *
 *  `variant="drawer"` 是右侧抽屉（添加模型那种多步表单）：同样一套 ESC/遮罩/焦点逻辑，
 *  只是位置与宽度不同 —— 与其再造一个 Drawer 组件、把这套行为复制一遍，
 *  不如让"模态行为"只有一处实现（两处实现迟早各自漂移）。
 *  那一处实现自 2026-10-30 起是 `hooks/useModalBehavior`：审批/收件箱两扇 w-80
 *  自绘头部的抽屉整迁 Modal 会动布局（头一行塞着清空/筛选按钮），但它们要的行为
 *  与这里逐字相同 —— 行为与版式解耦，两边共用钩子。 */
export default function Modal({
  open,
  onClose,
  title,
  children,
  footer,
  closeOnOverlay = true,
  variant = "center",
}: {
  open: boolean;
  onClose: () => void;
  title?: ReactNode;
  children?: ReactNode;
  footer?: ReactNode;
  closeOnOverlay?: boolean;
  variant?: "center" | "drawer";
}) {
  // 面板上是 `outline-none`：**故意的** —— 它是 `tabIndex={-1}` 的容器（只为 programmatic
  // focus），不是可聚焦控件；可聚焦的控件（按钮/输入/链接）在下面各自带 focus 反馈，
  // 模态里的 Tab 循环由浏览器原生行为承担。
  const panelRef = useModalBehavior(open, onClose);

  if (!open) return null;

  const drawer = variant === "drawer";
  return createPortal(
    <div
      className={`fixed inset-0 z-50 bg-black/40 ${drawer ? "" : "flex items-center justify-center p-4"}`}
      onClick={closeOnOverlay ? onClose : undefined}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
        className={
          drawer
            ? "absolute inset-y-0 right-0 flex w-full max-w-lg flex-col overflow-y-auto border-l border-slate-200 bg-white p-5 shadow-xl outline-none dark:border-slate-700 dark:bg-slate-800"
            : "relative w-full max-w-md rounded-xl border border-slate-200 bg-white p-5 shadow-xl outline-none dark:border-slate-700 dark:bg-slate-800"
        }
      >
        {title != null && (
          <h3 className="mb-3 text-sm font-semibold text-slate-900 dark:text-slate-100">
            {title}
          </h3>
        )}
        <div className="flex-1 text-sm leading-relaxed text-slate-600 dark:text-slate-300">
          {children}
        </div>
        {footer != null && (
          <div
            className={`flex justify-end gap-2 ${drawer ? "sticky bottom-0 mt-5 bg-white pt-3 dark:bg-slate-800" : "mt-4"}`}
          >
            {footer}
          </div>
        )}
      </div>
    </div>,
    document.body,
  );
}

/** 二次确认对话框：默认焦点在「取消」、文案为问句、danger 态可配。
 *  供 useConfirm 内部使用，也可直接当受控确认框用。 */
export function ConfirmDialog({
  open,
  title,
  body,
  confirmText = "确认",
  cancelText = "取消",
  danger = false,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: ReactNode;
  body?: ReactNode;
  confirmText?: string;
  cancelText?: string;
  danger?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const cancelRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (open) cancelRef.current?.focus();
  }, [open]);

  return (
    <Modal
      open={open}
      onClose={onCancel}
      title={title}
      footer={
        <>
          {/* 2026-10-24 UI 打磨：这对手搓按钮收编进 ui/Button（当初确认框自己长按钮，
              悬停/禁用/焦点样式就与全项目各漂一套 —— 设计文档"新代码必须复用 ui/"的
              首个违例就在这里）。size="sm" 与原 px-3 py-1.5 text-sm 观感等距。 */}
          <Button ref={cancelRef} variant="outline" size="sm" type="button" onClick={onCancel}>
            {cancelText}
          </Button>
          <Button variant={danger ? "danger" : "primary"} size="sm" type="button" onClick={onConfirm}>
            {confirmText}
          </Button>
        </>
      }
    >
      {body}
    </Modal>
  );
}
