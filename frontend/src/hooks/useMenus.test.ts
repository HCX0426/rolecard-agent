// @vitest-environment jsdom
//
// useMenus 的延时关闭与卸载清理（`armMenuClose` / `cancelMenuClose`）此前零覆盖 ——
// 它们是"鼠标移出 250ms 后关闭"的防抖底盘：关错了不是难看，是 Esc/卸载那两条
// 兜底路静默失效。fake timers 把延时钉死，不真等 250ms。

import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useMenus } from "./useMenus";

describe("useMenus 的移出延时与清理", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("armMenuClose：250ms 后才执行 close，重复 arm 重置计时", () => {
    const { result } = renderHook(() => useMenus());
    const close = vi.fn();
    act(() => result.current.armMenuClose(close));
    act(() => {
      vi.advanceTimersByTime(200);
      result.current.armMenuClose(close); // 第二次 arm：重置，不是再来一个定时器
    });
    act(() => vi.advanceTimersByTime(200));
    expect(close).not.toHaveBeenCalled(); // 重置后 200ms 还没到点
    act(() => vi.advanceTimersByTime(50));
    expect(close).toHaveBeenCalledTimes(1); // 恰好在重置后的 250ms 触发
  });

  it("cancelMenuClose：取消后 close 不再触发", () => {
    const { result } = renderHook(() => useMenus());
    const close = vi.fn();
    act(() => result.current.armMenuClose(close));
    act(() => result.current.cancelMenuClose());
    act(() => vi.advanceTimersByTime(500));
    expect(close).not.toHaveBeenCalled();
  });

  it("closeAllMenus：四个开关一起关", () => {
    const { result } = renderHook(() => useMenus());
    act(() => {
      result.current.setModelMenuOpen(true);
      result.current.setRoleMenuOpen(true);
      result.current.setCtxOpen("m1");
      result.current.setSampOpen("m1");
    });
    act(() => result.current.closeAllMenus());
    expect(result.current.modelMenuOpen).toBe(false);
    expect(result.current.roleMenuOpen).toBe(false);
    expect(result.current.ctxOpen).toBeNull();
    expect(result.current.sampOpen).toBeNull();
  });

  it("卸载时清掉在飞的定时器：延迟触发点不再回调已卸载闭包", () => {
    const { result, unmount } = renderHook(() => useMenus());
    const close = vi.fn();
    act(() => result.current.armMenuClose(close));
    unmount();
    act(() => vi.advanceTimersByTime(500));
    expect(close).not.toHaveBeenCalled();
  });
});
