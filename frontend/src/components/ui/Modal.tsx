import { useEffect, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";

/** 受控模态框：ESC / 点遮罩关闭、基础焦点管理、暗色态一致。
 *  通过 portal 渲染到 document.body，调用方无需 relative 容器。 */
export default function Modal({
  open,
  onClose,
  title,
  children,
  footer,
  closeOnOverlay = true,
}: {
  open: boolean;
  onClose: () => void;
  title?: ReactNode;
  children?: ReactNode;
  footer?: ReactNode;
  closeOnOverlay?: boolean;
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

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      onClick={closeOnOverlay ? onClose : undefined}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
        className="relative w-full max-w-md rounded-xl border border-slate-200 bg-white p-5 shadow-xl outline-none dark:border-slate-700 dark:bg-slate-800"
      >
        {title != null && (
          <h3 className="mb-3 text-sm font-semibold text-slate-900 dark:text-slate-100">
            {title}
          </h3>
        )}
        <div className="text-sm leading-relaxed text-slate-600 dark:text-slate-300">
          {children}
        </div>
        {footer != null && <div className="mt-4 flex justify-end gap-2">{footer}</div>}
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
