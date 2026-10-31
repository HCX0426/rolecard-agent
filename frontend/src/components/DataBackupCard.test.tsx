// @vitest-environment jsdom
// DataBackupCard 的渲染判据（R1031-15 应用内入口那一半）。
//
// 与《诊断》那张卡同一条四态：空闲 → 打包中（禁用防双击）→ 成功（报文件名）→ 失败（中文理由）。
// 文案里必须带着"包里含模型 API key"这句（R1031-23 的界面侧：知道有入口的人得同时知道包是敏感的），
// 所以文案断言也钉一条 —— 它不是装饰，是安全声明的一部分。
// mock 边界选在 api.downloadDataBackup：fetch+blob 的浏览器机制不是这张卡要证明的事。

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DataBackupCard } from "./DataBackupCard";

const downloadDataBackup = vi.fn();

/** getByRole 回的是 HTMLElement；本卡的按钮是唯一交互元素，disabled 收窄一处转。 */
function backupButton(): HTMLButtonElement {
  return screen.getByRole("button", { name: /导出全量备份/ }) as HTMLButtonElement;
}

// mock 要补齐 errors.ts 也从 "../api" 拿的那个符号（同 DiagnosticsCard 那条理由）。
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
    get downloadDataBackup() {
      return downloadDataBackup;
    },
  },
}));

// jsdom 没有 createObjectURL —— 下载动作的"落地"在浏览器里，测试只钉"调用过"。
beforeEach(() => {
  downloadDataBackup.mockReset();
  Object.defineProperty(URL, "createObjectURL", { value: vi.fn(() => "blob:x"), configurable: true });
  Object.defineProperty(URL, "revokeObjectURL", { value: vi.fn(), configurable: true });
});

afterEach(() => {
  cleanup();
});

describe("DataBackupCard", () => {
  it("卡上带着敏感物警示（API key 在包里这句不许被顺手删掉）", () => {
    render(<DataBackupCard />);
    expect(screen.getByText(/API key/)).toBeTruthy();
  });

  it("点击后走下载并回成功态", async () => {
    downloadDataBackup.mockResolvedValue({ blob: new Blob(["zip"]), filename: "backup-liveroot-x.zip" });
    render(<DataBackupCard />);
    fireEvent.click(backupButton());
    await waitFor(() => expect(downloadDataBackup).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByText(/backup-liveroot-x\.zip/)).toBeTruthy());
  });

  it("失败时把中文理由摆在卡上，按钮回空闲", async () => {
    downloadDataBackup.mockRejectedValue(new Error("这台实例的库不在数据根里，拒绝导出"));
    render(<DataBackupCard />);
    fireEvent.click(backupButton());
    await waitFor(() =>
      expect(screen.getByText(/库不在数据根里/)).toBeTruthy(),
    );
    expect(backupButton().disabled).toBe(false);
  });

  it("打包中禁用按钮（双击不重复打包）", async () => {
    let resolve: (v: { blob: Blob; filename: string }) => void = () => {};
    downloadDataBackup.mockImplementation(
      () => new Promise((r) => (resolve = r)),
    );
    render(<DataBackupCard />);
    const btn = screen.getByRole("button", { name: /导出全量备份/ });
    fireEvent.click(btn);
    expect(backupButton().disabled).toBe(true);
    resolve({ blob: new Blob(["zip"]), filename: "backup-liveroot-y.zip" });
    await waitFor(() => expect(screen.getByText(/backup-liveroot-y\.zip/)).toBeTruthy());
  });
});
