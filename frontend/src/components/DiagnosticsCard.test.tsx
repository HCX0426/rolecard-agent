// @vitest-environment jsdom
// DiagnosticsCard 的渲染判据（P2-② 的界面那一半）。
//
// 四态（前端设计矩阵钉的）：空闲（按钮）→ 加载中（禁用+文案）→ 成功（已导出 + 文件名）
// / 失败（describeError 的中文理由，按钮回空闲）。下载走 fetch+blob（api 层带鉴权头，
// 裸 href 在云端数据源会丢凭据）—— mock 的边界选在 api.downloadDiagnostics 上：
// fetch 与 blob 的浏览器机制不是这张卡要证明的事。

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DiagnosticsCard } from "./DiagnosticsCard";

const downloadDiagnostics = vi.fn();

/** getByRole 回的是 HTMLElement；本卡的按钮是唯一交互元素，disabled 收窄一处转。 */
function diagButton(): HTMLButtonElement {
  return screen.getByRole("button", { name: /导出诊断包/ }) as HTMLButtonElement;
}

// mock 要补齐 errors.ts 也从 "../api" 拿的那个符号：describeError 会 instanceof ApiError，
// 缺这个导出时 mock 的惰性 getter 直接抛 "No ... export" —— 替身要替整个被 import 的面。
vi.mock("../api", () => ({
  ApiError: class ApiError extends Error {
    constructor(
      public status: number,
      detail: string,
    ) {
      super(detail);
    }
  },
  api: {
    get downloadDiagnostics() {
      return downloadDiagnostics;
    },
  },
}));

// jsdom 没有 createObjectURL —— 下载动作的"落地"在浏览器里，测试只钉"调用过"。
beforeEach(() => {
  downloadDiagnostics.mockReset();
  Object.defineProperty(URL, "createObjectURL", { value: vi.fn(() => "blob:x"), configurable: true });
  Object.defineProperty(URL, "revokeObjectURL", { value: vi.fn(), configurable: true });
});

afterEach(() => {
  cleanup();
});

describe("DiagnosticsCard", () => {
  it("点击后走下载并回成功态", async () => {
    downloadDiagnostics.mockResolvedValue({ blob: new Blob(["zip"]), filename: "diag.zip" });
    render(<DiagnosticsCard />);
    fireEvent.click(diagButton());
    await waitFor(() => expect(downloadDiagnostics).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByText(/diag\.zip/)).toBeTruthy());
  });

  it("失败时把中文理由摆在卡上，按钮回空闲", async () => {
    downloadDiagnostics.mockRejectedValue(new Error("服务端出了没预料的错，现场已记录"));
    render(<DiagnosticsCard />);
    fireEvent.click(screen.getByRole("button", { name: /导出诊断包/ }));
    await waitFor(() =>
      expect(screen.getByText(/服务端出了没预料的错/)).toBeTruthy(),
    );
    expect(diagButton().disabled).toBe(false);
  });

  it("加载中禁用按钮（双击不重复导出）", async () => {
    let resolve: (v: { blob: Blob; filename: string }) => void = () => {};
    downloadDiagnostics.mockImplementation(
      () => new Promise((r) => (resolve = r)),
    );
    render(<DiagnosticsCard />);
    const btn = screen.getByRole("button", { name: /导出诊断包/ });
    fireEvent.click(btn);
    expect(diagButton().disabled).toBe(true);
    resolve({ blob: new Blob(["zip"]), filename: "diag.zip" });
    await waitFor(() => expect(screen.getByText(/diag\.zip/)).toBeTruthy());
  });
});
