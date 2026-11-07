// @vitest-environment jsdom
//
// 上传链路 hook 的分支此前只能靠人工点页面验（hook 抽出去的那条注释自己写着）：
// 在飞防重入 / ensureSession 失败 / 三态早退 / 上传失败。判定函数 describeUpload
// 已有单测，这里钉的是**接线** —— 每个分支真的走到它该走的 onStatus 与 reloadMessages。
//
// 2026-11-07：结构化抽取那一步随它的域包退役，链路到"已入索引"为止 —— 原先那两条
// "自动抽取成功 / 抽取抛错"的用例随之删掉（它们测的分支不存在了）。
import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { api, reloadMessages } = vi.hoisted(() => ({
  api: { upload: vi.fn() },
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

describe("useUploadFlow 的分支接线", () => {
  beforeEach(() => {
    api.upload.mockReset();
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

  it("登记但读不了（三态早退）：报状态即收工", async () => {
    api.upload.mockResolvedValue({ task_id: "k1", reused: false, file: "a.pdf", status: "pending" });
    const u = setup("t1");
    await act(() => u.result.current.handleUpload(new File(["x"], "a.pdf")));
    expect(u.onStatus).toHaveBeenCalledTimes(1);
    expect(u.onStatus.mock.calls[0][1]).toBe("warn");
    expect(reloadMessages).toHaveBeenCalledWith("t1");
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
