// @vitest-environment jsdom
//
// 上传链路 hook 的五个分支此前只能靠人工点页面验（hook 抽出去的那条注释自己写着）：
// 在飞防重入 / ensureSession 失败 / 三态早退 / 抽取成败 / 上传失败。判定函数
// describeUpload/describeExtract 已有单测，这里钉的是**接线** —— 每个分支真的
// 走到它该走的 onStatus 与 reloadMessages。
import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { api, reloadMessages } = vi.hoisted(() => ({
  api: { upload: vi.fn(), extractRecord: vi.fn() },
  reloadMessages: vi.fn(),
}));

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, api };
});

import { useUploadFlow } from "./useUploadFlow";

function setup(sessionId: string | null, ensureSession = vi.fn()) {
  const onStatus = vi.fn();
  const utils = renderHook(() =>
    useUploadFlow({
      sessionId,
      ensureSession,
      reloadMessages,
      onStatus,
    }),
  );
  return { onStatus, ensureSession, ...utils };
}

/** describeUpload 认作「已入索引」的最小形状（status 非空且非 pending/parsed）。 */
const indexed = { task_id: "k1", reused: false, file: "a.pdf", status: "indexed" };

describe("useUploadFlow 的分支接线", () => {
  beforeEach(() => {
    api.upload.mockReset();
    api.extractRecord.mockReset();
    reloadMessages.mockReset().mockResolvedValue(undefined);
  });

  it("在飞时防重入：重渲染后的第二次调用不上传", async () => {
    let release: (v: unknown) => void = () => {};
    api.upload.mockReturnValue(new Promise((r) => (release = r)));
    const u = setup("t1");
    let first!: Promise<void>;
    act(() => {
      first = u.result.current.handleUpload(new File(["x"], "a.txt"));
    });
    // 让 setUploading(true) 的重渲染落地 —— 防重入判的是渲染后的状态。
    await act(async () => {
      await Promise.resolve();
    });
    await act(() => u.result.current.handleUpload(new File(["y"], "b.txt")));
    await act(async () => {
      release(undefined);
      await first;
    });
    expect(api.upload).toHaveBeenCalledTimes(1);
    expect(reloadMessages).toHaveBeenCalledTimes(1);
  });

  it("无会话且 ensureSession 失败：直接放弃，不 upload 也不 reload", async () => {
    const ensureSession = vi.fn().mockResolvedValue(null);
    const u = setup(null, ensureSession);
    await act(() => u.result.current.handleUpload(new File(["x"], "a.pdf")));
    expect(api.upload).not.toHaveBeenCalled();
    expect(reloadMessages).not.toHaveBeenCalled();
  });

  it("登记但读不了（三态早退）：报状态后不再触发抽取", async () => {
    api.upload.mockResolvedValue({ task_id: "k1", reused: false, file: "a.pdf", status: "pending" });
    const u = setup("t1");
    await act(() => u.result.current.handleUpload(new File(["x"], "a.pdf")));
    expect(api.extractRecord).not.toHaveBeenCalled();
    expect(u.onStatus).toHaveBeenCalledTimes(1);
    expect(u.onStatus.mock.calls[0][1]).toBe("warn");
    expect(reloadMessages).toHaveBeenCalledWith("t1");
  });

  it("已入索引 → 自动抽取成功：抽取结果进 onStatus，失败不算上传失败", async () => {
    api.upload.mockResolvedValue(indexed);
    api.extractRecord.mockResolvedValue({
      skipped: null,
      written: [{ index_name: "uric-acid" }],
      conflicts: [],
      notes: [],
    });
    const u = setup("t1");
    await act(() => u.result.current.handleUpload(new File(["x"], "a.pdf")));
    expect(api.extractRecord).toHaveBeenCalledWith("k1");
    expect(u.onStatus).toHaveBeenCalledTimes(2); // 入索引提示 + 抽取结果
    expect(u.onStatus.mock.calls[1][1]).toBe("ok");
  });

  it("抽取抛错：原文仍在，状态给出抽取错误而非上传失败", async () => {
    api.upload.mockResolvedValue(indexed);
    api.extractRecord.mockRejectedValue(new Error("模型没响应"));
    const u = setup("t1");
    await act(() => u.result.current.handleUpload(new File(["x"], "a.pdf")));
    expect(u.onStatus).toHaveBeenCalledTimes(2);
    expect(String(u.onStatus.mock.calls[1][0])).toContain("模型没响应");
    expect(u.onStatus.mock.calls[1][0]).not.toContain("上传失败");
  });

  it("upload 本身抛错：报「上传失败」且 uploading 复位", async () => {
    api.upload.mockRejectedValue(new Error("网络断了"));
    const u = setup("t1");
    await act(() => u.result.current.handleUpload(new File(["x"], "a.pdf")));
    expect(u.onStatus).toHaveBeenCalledTimes(1);
    expect(u.onStatus.mock.calls[0][0]).toContain("上传失败");
    expect(u.result.current.uploading).toBe(false);
  });
});
