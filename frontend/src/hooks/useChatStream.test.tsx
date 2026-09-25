// @vitest-environment jsdom
//
// `useChatStream.stop()` 的那一下点击（#18）。
//
// 为什么单独钉这个 hook：「停止生成」以前只做一件事 —— 掐掉这条 fetch。而掐连接**不会**中断
// 服务端线程池里已经在跑的那次模型调用（审计 §12.12② 实测），于是用户看到界面停了、后端还在
// 往那条流里写：云端白花一轮，本地（串行推理）把下一个请求排在它后面。现在 stop 先通知后端
// 立旗，再掐连接 —— 顺序也要钉，反过来就等于把通知挤在那次中断之后。

import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { stopTurn } = vi.hoisted(() => ({ stopTurn: vi.fn() }));

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, api: { ...actual.api, stopTurn } };
});

import { useChatStream } from "./useChatStream";

describe("useChatStream 的停止", () => {
  beforeEach(() => {
    // 真实的 `api.stopTurn` 永远回一个 Promise，所以 hook 不必防"返回 undefined"；
    // mock 复位后要自己补上这个默认值，否则测的是桩的形状而不是接线的形状。
    stopTurn.mockReset().mockResolvedValue({ requested: true });
  });

  it("开过一轮：先通知后端（此时连接还没掐），再 abort", async () => {
    let abortedWhenNotified: boolean | null = null;
    stopTurn.mockImplementation((tid: string) => {
      // 同步执行到这里时 `stop()` 还没走到 abort —— 这就是"先通知"的证据。
      abortedWhenNotified = controller.signal.aborted;
      return Promise.resolve({ thread_id: tid, requested: true });
    });
    const { result } = renderHook(() => useChatStream(() => {}));
    const controller = result.current.startBubble("s7");
    act(() => {
      result.current.stop();
    });
    expect(stopTurn).toHaveBeenCalledWith("s7");
    expect(abortedWhenNotified).toBe(false);
    expect(controller.signal.aborted).toBe(true);
  });

  it("通知失败也照样掐连接：界面不能因为一次失败的 POST 卡在生成中", async () => {
    stopTurn.mockRejectedValue(new Error("后端没在跑"));
    const { result } = renderHook(() => useChatStream(() => {}));
    const controller = result.current.startBubble("s8");
    await act(async () => {
      result.current.stop();
      await Promise.resolve();
    });
    expect(controller.signal.aborted).toBe(true);
  });

  it("没开轮时点停止：一个请求都不发，也不炸", () => {
    const { result } = renderHook(() => useChatStream(() => {}));
    act(() => {
      result.current.stop();
    });
    expect(stopTurn).not.toHaveBeenCalled();
  });

  it("新一轮把会话记成这一轮的：上一轮的停止不会顺延通知", () => {
    const { result } = renderHook(() => useChatStream(() => {}));
    result.current.startBubble("old");
    result.current.startBubble("new");
    act(() => {
      result.current.stop();
    });
    expect(stopTurn).toHaveBeenCalledWith("new");
  });
});
