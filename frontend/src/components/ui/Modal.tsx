import { useEffect, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";

/** 受控模态框：ESC / 点遮罩关闭、基础焦点管理、暗色态一致。
 *  通过 portal 渲染到 document.body，调用方无需 relative 容器。
 *
 *  `variant="drawer"` 是右侧抽屉（添加模型那种多步表单）：同样一套 ESC/遮罩/焦点逻辑，
 *  只是位置与宽度不同 —— 与其再造一个 Drawer 组件、把这套行为复制一遍，
 *  不如让"模态行为"只有一处实现（两处实现迟早各自漂移）。 */
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
          <button
            ref={cancelRef}
            type="button"
            onClick={onCancel}
            className="rounded-lg border border-slate-200 px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-50 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-700/40"
          >
            {cancelText}
          </button>
          <button
            type="button"
            onClick={onConfirm}
            className={
              danger
                ? "rounded-lg bg-red-600 px-3 py-1.5 text-sm text-white hover:bg-red-700"
                : "rounded-lg bg-blue-600 px-3 py-1.5 text-sm text-white hover:bg-blue-700"
            }
          >
            {confirmText}
          </button>
        </>
      }
    >
      {body}
    </Modal>
  );
}
