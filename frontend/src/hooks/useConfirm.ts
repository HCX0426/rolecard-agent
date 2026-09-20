import { useCallback, type ReactNode } from "react";
import { createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { ConfirmDialog } from "../components/ui/Modal";

let host: HTMLDivElement | null = null;
let root: Root | null = null;

/** 进程内唯一的确认弹窗根：首次调用时挂到 document.body，
 *  之后复用同一个 React root，避免每次确认都重建容器。 */
function ensureRoot(): Root {
  if (!host) {
    host = document.createElement("div");
    host.setAttribute("data-confirm-root", "");
    document.body.appendChild(host);
    root = createRoot(host);
  }
  return root!;
}

export interface ConfirmOptions {
  /** 问句（A11y：默认焦点在「取消」，标题应是问句而非陈述）。 */
  title: string;
  /** 可选的补充说明 / 待删清单等。 */
  body?: ReactNode;
  confirmText?: string;
  cancelText?: string;
  /** 危险操作（删除等）时渲染红色确认按钮。 */
  danger?: boolean;
}

/**
 * 全局二次确认：返回 `confirm(opts) => Promise<boolean>`。
 *
 * 内部通过 portal 在 document.body 挂一个全局 Modal，无需 Provider；
 * 组件树任意位置调用，点「确认」resolve(true)、点「取消」/Esc/点遮罩 resolve(false)。
 * 调用点须从同步 `if (!confirm(...)) return` 改为异步 `if (!(await confirm(...))) return`。
 */
export function useConfirm() {
  return useCallback((opts: ConfirmOptions): Promise<boolean> => {
    return new Promise<boolean>((resolve) => {
      const r = ensureRoot();
      const finish = (ok: boolean) => {
        r.render(
          createElement(ConfirmDialog, {
            open: false,
            title: "",
            onConfirm: () => {},
            onCancel: () => {},
          }),
        );
        resolve(ok);
      };
      r.render(
        createElement(ConfirmDialog, {
          open: true,
          title: opts.title,
          body: opts.body,
          confirmText: opts.confirmText ?? "确认",
          cancelText: opts.cancelText ?? "取消",
          danger: opts.danger,
          onConfirm: () => finish(true),
          onCancel: () => finish(false),
        }),
      );
    });
  }, []);
}
