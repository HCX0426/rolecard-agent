// @vitest-environment jsdom
import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../api";
import { useAsyncAction } from "./useAsyncAction";

describe("useAsyncAction", () => {
  it("成功：动作执行、busy 起落、返回 true，出口不被打扰", async () => {
    const outlet = vi.fn();
    const { result } = renderHook(() => useAsyncAction(outlet));
    const action = vi.fn().mockResolvedValue(undefined);
    let ok!: boolean;
    await act(async () => {
      ok = await result.current.run(action);
    });
    expect(ok).toBe(true);
    expect(action).toHaveBeenCalledTimes(1);
    expect(result.current.busy).toBe(false);
    expect(outlet).not.toHaveBeenCalled();
  });

  it("失败：describeError 的文案进出口（带 warn 语气）、返回 false、busy 复位", async () => {
    const outlet = vi.fn();
    const { result } = renderHook(() => useAsyncAction(outlet));
    await act(async () => {
      await result.current.run(() => Promise.reject(new ApiError(404, "找不到这份角色卡")));
    });
    expect(outlet).toHaveBeenCalledWith("找不到这份角色卡", "warn");
    expect(result.current.busy).toBe(false);
  });

  it("不给出参：失败静默（返回 false），不炸调用方", async () => {
    const { result } = renderHook(() => useAsyncAction());
    let ok!: boolean;
    await act(async () => {
      ok = await result.current.run(() => Promise.reject(new Error("静默那一档")));
    });
    expect(ok).toBe(false);
  });
});
