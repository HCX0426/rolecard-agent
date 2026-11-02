// @vitest-environment jsdom
// 透明度那条链的**方向**契约（2026-11-02）。
//
// 为什么专门钉这个：滑块语义反了能存活到用户当面报出来，唯一原因就是这条链上一个断言
// 都没有 —— tsc 看不出"减法漏了"，eslint 看不出，界面在 alpha=0 那一头看着还挺正常
// （0 → 实心 100%，正是大家习惯的样子），于是谁都没往反了想。
// 补的这条不求覆盖五个开关，只求"再反一次立刻红"。
import { beforeEach, describe, expect, it } from "vitest";

import { initAppearance, soliditiesFor, ALPHA_DEFAULT, ALPHA_MAX, ALPHA_MIN } from "./appearance";

describe("soliditiesFor：透明度 → 实心度", () => {
  it("方向不许反：透明度拉满 ⇒ 底色全透（实心度 0）", () => {
    expect(soliditiesFor(ALPHA_MAX).bg).toBe(0);
  });

  it("方向不许反：透明度归零 ⇒ 底色全实（实心度 100）", () => {
    expect(soliditiesFor(ALPHA_MIN).bg).toBe(ALPHA_MAX);
  });

  it("单调：透明度越高，三档实心度都越低或持平（不许有一档反向）", () => {
    let prev = soliditiesFor(ALPHA_MIN);
    for (let a = ALPHA_MIN + 1; a <= ALPHA_MAX; a++) {
      const now = soliditiesFor(a);
      expect(now.bg).toBeLessThanOrEqual(prev.bg);
      expect(now.glass).toBeLessThanOrEqual(prev.glass);
      expect(now.glassSoft).toBeLessThanOrEqual(prev.glassSoft);
      prev = now;
    }
  });

  it("玻璃层始终不比底色更实：层次由「面板略透于底色」撑着（沿用作者 -12/-4）", () => {
    for (let a = ALPHA_MIN; a <= ALPHA_MAX; a++) {
      const s = soliditiesFor(a);
      expect(s.glass).toBeLessThanOrEqual(s.bg);
      expect(s.glassSoft).toBeLessThanOrEqual(s.bg);
      expect(s.glassSoft).toBeGreaterThanOrEqual(s.glass);
    }
  });

  it("默认档（alpha=0）复现 index.css :root 那三个出厂读数 100/88/96", () => {
    // 这条把换算钉在作者定义的外观上：改了差值或方向，这里立刻红。
    expect(soliditiesFor(ALPHA_DEFAULT)).toEqual({ bg: 100, glass: 88, glassSoft: 96 });
  });

  it("三档都夹在 0..100（喂给 color-mix 的百分数越界等于静默失效）", () => {
    for (const a of [ALPHA_MIN, 37, ALPHA_MAX, -50, 999, Number.NaN]) {
      const s = soliditiesFor(a);
      for (const v of [s.bg, s.glass, s.glassSoft]) {
        expect(v).toBeGreaterThanOrEqual(0);
        expect(v).toBeLessThanOrEqual(ALPHA_MAX);
      }
    }
  });
});

describe("写进 <html> 的那三个变量", () => {
  beforeEach(() => {
    localStorage.clear();
    document.documentElement.removeAttribute("style");
  });

  const readVars = () => ({
    bg: document.documentElement.style.getPropertyValue("--bg-alpha"),
    glass: document.documentElement.style.getPropertyValue("--glass-alpha"),
    soft: document.documentElement.style.getPropertyValue("--glass-alpha-soft"),
  });

  it("alpha=100 落进 DOM 的是全透那一头", () => {
    localStorage.setItem("rolecard.alpha", "100");
    initAppearance();
    expect(readVars().bg).toBe("0%");
  });

  it("alpha=0 落进 DOM 的是全实那一头", () => {
    localStorage.setItem("rolecard.alpha", "0");
    initAppearance();
    expect(readVars().bg).toBe("100%");
  });

  it("键**不存在**落默认档：不能把「没设过」读成「设了 0」", () => {
    // 旧写法 Number(localStorage.getItem(k)) 在键缺失时给出 0（Number(null)===0 且有限），
    // 于是那句 `isFinite ? clamp : 100` 的兜底一路是死代码，默认档形同虚设。
    expect(localStorage.getItem("rolecard.alpha")).toBeNull();
    initAppearance();
    const expected = 100 - ALPHA_DEFAULT;
    expect(readVars().bg).toBe(`${expected}%`);
  });

  it("bgDim 缺键时落兜底暗化，而不是 0（0 = 图上没有暗化层，字压在亮面）", () => {
    localStorage.setItem("rolecard.bgImage", "/api/uploads/bg.png");
    initAppearance();
    expect(document.documentElement.style.getPropertyValue("--bg-dim")).toBe("0.25");
  });

  it("用户显式设过的 0 必须被尊重（别把「我就是要 0」又兜回默认）", () => {
    localStorage.setItem("rolecard.bgDim", "0");
    initAppearance();
    expect(document.documentElement.style.getPropertyValue("--bg-dim")).toBe("0");
  });
});
