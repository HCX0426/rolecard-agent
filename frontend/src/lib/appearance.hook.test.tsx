// @vitest-environment jsdom
// 外观这条链的**方向**与**单一真值**契约（2026-11-02 立，2026-11-03 归一化后扩充）。
//
// 分两个文件是本仓既有姿势：纯函数与启动那一路住 appearance.test.ts，
// **hook 的用户动作**住这里（要 renderHook，属组件级）。
//
// 为什么非钉不可：这轮把落盘时机从"apply 顺手写盘"改成"只有用户动作才写盘"，
// 而这批 setter 是**唯一的写路径**。它们没测试的话，"启动不落盘"那条断言只证明了
// initAppearance 一侧，另一侧（用户按了到底该不该落）仍是空的 —— 那正是本仓反对的
// "一半有尺子、一半凭感觉"。
import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { useTheme } from "../components/useTheme";
import { readTheme, useAppearance } from "./appearance";

function jsVars(): string[] {
  const s = document.documentElement.style;
  const out: string[] = [];
  for (let i = 0; i < s.length; i++) {
    const n = s.item(i);
    if (n.startsWith("--")) out.push(n);
  }
  return out.sort();
}

beforeEach(() => {
  // 属性也要清：这些用例断言的是"渲染本身不动 DOM"，而 renderHook 之间共享同一个
  // documentElement —— 前一条 toggle 过 material 的话，后一条读到的就是它的残留
  // （第一版就是这么红的：不是实现错了，是我的用例互相踩）。
  localStorage.clear();
  document.documentElement.removeAttribute("style");
  document.documentElement.removeAttribute("data-has-bg");
  document.documentElement.removeAttribute("data-material");
  document.documentElement.removeAttribute("data-palette");
  document.documentElement.className = "";
});

describe("useAppearance 的用户动作：落盘 + 挂 DOM", () => {
  it("透明度：设一次 ⇒ DOM 写 --bg-solid、盘上存滑块原值（不是实心度）", () => {
    const { result } = renderHook(() => useAppearance());
    act(() => result.current.setAlpha(60));
    expect(result.current.alpha).toBe(60);
    expect(localStorage.getItem("rolecard.alpha")).toBe("60");
    expect(document.documentElement.style.getPropertyValue("--bg-solid")).toBe("40");
  });

  it("透明度：越界与 NaN 夹回 0..100（滑块能输任意数）", () => {
    const { result } = renderHook(() => useAppearance());
    for (const [input, want] of [
      [-20, 0],
      [400, 100],
      [37.6, 38],
    ] as Array<[number, number]>) {
      act(() => result.current.setAlpha(input));
      expect(result.current.alpha).toBe(want);
    }
    act(() => result.current.setAlpha(Number.NaN));
    expect(result.current.alpha).toBe(0);
  });

  it("材质/配色：toggle 同时挂 DOM 与落盘（这两个键就是给 DOM 用的）", () => {
    const { result } = renderHook(() => useAppearance());
    expect(result.current.material).toBe("glass");
    act(() => result.current.toggleMaterial());
    expect(result.current.material).toBe("panel");
    expect(document.documentElement.getAttribute("data-material")).toBe("panel");
    expect(localStorage.getItem("rolecard.material")).toBe("panel");

    act(() => result.current.togglePalette());
    expect(result.current.palette).toBe("cool");
    expect(document.documentElement.getAttribute("data-palette")).toBe("cool");
    expect(localStorage.getItem("rolecard.palette")).toBe("cool");
  });

  it("背景图：设置 ⇒ 挂 data-has-bg + 写内联 + 落盘", () => {
    const { result } = renderHook(() => useAppearance());
    act(() => result.current.setBgImage("/api/uploads/bg.png"));
    expect(document.documentElement.hasAttribute("data-has-bg")).toBe(true);
    expect(document.documentElement.style.getPropertyValue("--bg-image")).toBe(
      'url("/api/uploads/bg.png")',
    );
    expect(localStorage.getItem("rolecard.bgImage")).toBe("/api/uploads/bg.png");
  });

  it("背景图：移除（空串）⇒ 属性摘掉、内联写回 none —— 与「从没设过」不同", () => {
    const { result } = renderHook(() => useAppearance());
    act(() => result.current.setBgImage("/api/uploads/bg.png"));
    act(() => result.current.setBgImage(""));
    expect(document.documentElement.hasAttribute("data-has-bg")).toBe(false);
    expect(document.documentElement.style.getPropertyValue("--bg-image")).toBe("none");
    // 键还在（值是空串）：那是"设过又移除"，下次启动仍要写内联把它按 none 钉住
    expect(localStorage.getItem("rolecard.bgImage")).toBe("");
  });

  it("模糊与暗化：写内联也写盘；暗化显式设 0 必须保住 0", () => {
    const { result } = renderHook(() => useAppearance());
    act(() => result.current.setBgBlur(12));
    act(() => result.current.setBgDim(0));
    expect(document.documentElement.style.getPropertyValue("--bg-blur")).toBe("12px");
    expect(document.documentElement.style.getPropertyValue("--bg-dim")).toBe("0");
    expect(localStorage.getItem("rolecard.bgBlur")).toBe("12");
    expect(localStorage.getItem("rolecard.bgDim")).toBe("0");
  });

  it("渲染这个 hook 本身不写盘 —— 「打开关于面板」不等于「用户做了选择」", () => {
    // 从前 hook 里每个开关挂一个 useEffect(apply+写盘)，挂载首拍就把五项全写进盘上。
    // 后果不是多占一格：material 的读法是 `=== "panel" ? panel : glass`，盘上一旦
    // 凭空有了 "glass"，「从没表态」与「表态选了 glass」在数据上就不可分了。
    const { result, rerender } = renderHook(() => useAppearance());
    rerender();
    // 值本身仍是各档的默认（界面照常显示），只是**不落到 DOM 与盘上**
    expect(result.current.material).toBe("glass");
    expect(result.current.palette).toBe("warm");
    expect(result.current.alpha).toBe(0);
    expect(Object.keys(localStorage)).toEqual([]);
    expect(jsVars()).toEqual([]);
    expect(document.documentElement.getAttribute("data-material")).toBeNull();
  });
});

describe("useTheme 的用户动作", () => {
  it("切换 ⇒ 挂 .dark 且落盘", () => {
    const { result } = renderHook(() => useTheme());
    expect(result.current.theme).toBe("light");
    act(() => result.current.toggle());
    expect(result.current.theme).toBe("dark");
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    expect(localStorage.getItem("theme")).toBe("dark");
  });

  it("只渲染不写盘：系统深色时「跟随系统」这个状态必须能在盘上保持空缺", () => {
    // 从前是 useEffect(apply+setItem)，第一次启动就把从系统读来的 dark 钉进盘上；
    // 之后用户把系统改回浅色，应用再也不跟 —— 而他从没做过"选深色"这个动作。
    const { result } = renderHook(() => useTheme());
    expect(result.current.theme).toBe("light"); // jsdom 的 matchMedia 桩给 light
    expect(localStorage.getItem("theme")).toBeNull();
  });

  it("连点两下回到原值，DOM 与盘都跟着回到那一样", () => {
    const { result } = renderHook(() => useTheme());
    act(() => result.current.toggle());
    act(() => result.current.toggle());
    expect(result.current.theme).toBe("light");
    expect(document.documentElement.classList.contains("dark")).toBe(false);
    expect(localStorage.getItem("theme")).toBe("light");
  });

  it("环境没有 matchMedia ⇒ 落浅色（那条兜底分支必须真有人走）", () => {
    // 不留成"永不覆盖的防御分支"：仓里反对的正是"写了但没人验证会走到的兜底"。
    const original = window.matchMedia;
    // @ts-expect-error 故意摘掉：模拟没有这个 API 的运行环境（test-setup.ts 的桩也一并绕开）
    window.matchMedia = undefined;
    try {
      expect(readTheme()).toBe("light");
    } finally {
      window.matchMedia = original;
    }
  });
});
