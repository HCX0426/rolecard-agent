// @vitest-environment jsdom
// 外观那条链的**方向**与**单一真值**契约（2026-11-02 立，2026-11-03 归一化后扩充）。
//
// 为什么专门钉这些：
//  1) 滑块语义反了能存活到用户当面报出来，唯一原因是这条链上一个断言都没有 —— tsc 看不出
//     "减法漏了"，eslint 看不出，界面在 alpha=0 那一头看着还挺正常，于是谁都没往反了想。
//  2) 透明度从前在 JS 里算成**三档**并写成三条内联样式，内联压过 `.dark`，于是 CSS 里深色
//     那组档位（92%/97%）**从写下来一次都没生效过**。真浏览器实测才抓出来（清掉内联才露出
//     92%/97%）。这类"JS 把 CSS 盖死"的漂移，纯 CSS 侧看不出、纯 JS 侧也看不出，只有断言
//     "JS 到底写了哪几条内联属性"能看出来 —— 所以下面第一组用例是**枚举**，不是抽查。
import { beforeEach, describe, expect, it } from "vitest";

import {
  ALPHA_DEFAULT,
  ALPHA_MAX,
  ALPHA_MIN,
  BG_BLUR_DEFAULT,
  BG_DIM_DEFAULT,
  SOLID_DEFAULT,
  bgSolidity,
  initAppearance,
  readTheme,
} from "./appearance";

/** 当前 <html> 上**由 JS 写上去的**自定义属性名（不含 CSS 计算值）。 */
function jsVars(): string[] {
  const s = document.documentElement.style;
  const out: string[] = [];
  for (let i = 0; i < s.length; i++) {
    const n = s.item(i);
    if (n.startsWith("--")) out.push(n);
  }
  return out.sort();
}

describe("bgSolidity：透明度 → 实心度", () => {
  it("方向不许反：透明度拉满 ⇒ 底色全透（实心度 0）", () => {
    expect(bgSolidity(ALPHA_MAX)).toBe(0);
  });

  it("方向不许反：透明度归零 ⇒ 底色全实（实心度 100）", () => {
    expect(bgSolidity(ALPHA_MIN)).toBe(ALPHA_MAX);
  });

  it("单调：透明度越高，实心度越低（不许有反向段）", () => {
    let prev = bgSolidity(ALPHA_MIN);
    for (let a = ALPHA_MIN + 1; a <= ALPHA_MAX; a++) {
      const now = bgSolidity(a);
      expect(now).toBeLessThanOrEqual(prev);
      prev = now;
    }
  });

  it("越界与 NaN 都夹回 0..100（喂给 CSS 的数越界等于静默失效）", () => {
    for (const [v, want] of [
      [-50, ALPHA_MAX],
      [999, 0],
      [Number.NaN, SOLID_DEFAULT],
      [37, 63],
    ] as Array<[number, number]>) {
      expect(bgSolidity(v)).toBe(want);
    }
  });

  it("出厂档（alpha=0）的实心度 = index.css 里 --bg-solid 的字面值", () => {
    expect(bgSolidity(ALPHA_DEFAULT)).toBe(SOLID_DEFAULT);
  });
});

describe("JS 写进 <html> 的内联属性：只许一条 alpha 相关的", () => {
  beforeEach(() => {
    localStorage.clear();
    document.documentElement.removeAttribute("style");
    document.documentElement.removeAttribute("data-has-bg");
    document.documentElement.className = "";
  });

  // 这一组是**归一化本身**：从前这里有三条（--bg-alpha / --glass-alpha /
  // --glass-alpha-soft），后两条把 CSS 里 .dark 的深色档位永久盖死。
  // 断言写成"等于这一个数组"，将来谁再加一条内联 alpha，这里立刻红 ——
  // 抽查一条挡不住，因为要挡的正是"多写一条"。
  it("透明度只写 --bg-solid，层次差归 CSS", () => {
    localStorage.setItem("rolecard.alpha", "30");
    initAppearance();
    expect(jsVars().filter((v) => v.includes("alpha") || v.includes("solid"))).toEqual([
      "--bg-solid",
    ]);
  });

  it("没设过背景图/模糊/暗化 ⇒ 这三条内联**一条都不写**（让 CSS 默认做主）", () => {
    initAppearance();
    expect(jsVars()).not.toContain("--bg-dim");
    expect(jsVars()).not.toContain("--bg-blur");
    expect(jsVars()).not.toContain("--bg-image");
  });

  it("设过暗化 ⇒ 写内联，且尊重显式的 0（别把「我就是要 0」又兜回默认）", () => {
    localStorage.setItem("rolecard.bgDim", "0");
    initAppearance();
    expect(document.documentElement.style.getPropertyValue("--bg-dim")).toBe("0");
  });

  it("设过背景图 ⇒ 挂 data-has-bg（选择器住在 html 上，首帧脚本够得着）", () => {
    localStorage.setItem("rolecard.bgImage", "/api/uploads/bg.png");
    initAppearance();
    expect(document.documentElement.hasAttribute("data-has-bg")).toBe(true);
    expect(document.documentElement.style.getPropertyValue("--bg-image")).toBe(
      'url("/api/uploads/bg.png")',
    );
  });

  it("移除背景图 ⇒ 属性一起摘掉（否则图上加深那条规则一直挂着）", () => {
    localStorage.setItem("rolecard.bgImage", "/api/uploads/bg.png");
    initAppearance();
    localStorage.setItem("rolecard.bgImage", "");
    initAppearance();
    expect(document.documentElement.hasAttribute("data-has-bg")).toBe(false);
  });

  it("alpha=100 落进 DOM 的是全透那一头，alpha=0 是全实", () => {
    localStorage.setItem("rolecard.alpha", "100");
    initAppearance();
    expect(document.documentElement.style.getPropertyValue("--bg-solid")).toBe("0");
    localStorage.setItem("rolecard.alpha", "0");
    initAppearance();
    expect(document.documentElement.style.getPropertyValue("--bg-solid")).toBe("100");
  });

  it("键**不存在**落默认档：不能把「没设过」读成「设了 0」", () => {
    // 旧写法 Number(localStorage.getItem(k)) 在键缺失时给出 0（Number(null)===0 且**有限**），
    // 于是那句 `isFinite ? clamp : 100` 的兜底一路是死代码，默认档形同虚设。
    expect(localStorage.getItem("rolecard.alpha")).toBeNull();
    initAppearance();
    expect(document.documentElement.style.getPropertyValue("--bg-solid")).toBe(
      String(ALPHA_MAX - ALPHA_DEFAULT),
    );
  });
});

describe("明暗的判据", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  // 键存在 = 用户做过选择，以它为准；缺键才读系统偏好。这条判据从前只住在
  // components/useTheme.ts，index.html 的首帧脚本压根不管明暗（现在按同一条抄）。
  it("缺键 ⇒ 跟随系统偏好，且**不写盘**（写了就等于把跟随系统钉死）", () => {
    expect(localStorage.getItem("theme")).toBeNull();
    expect(readTheme()).toBe("light"); // jsdom 的 matchMedia 桩给 light，见 test-setup.ts
  });

  it("显式 light / dark 覆盖系统偏好", () => {
    localStorage.setItem("theme", "dark");
    expect(readTheme()).toBe("dark");
    localStorage.setItem("theme", "light");
    expect(readTheme()).toBe("light");
  });

  it("认不得的值不当作用户的选择（脏值回落到跟随系统）", () => {
    localStorage.setItem("theme", "auto");
    expect(readTheme()).toBe("light");
  });
});

describe("启动那一趟只挂 DOM，不落盘", () => {
  beforeEach(() => {
    localStorage.clear();
    document.documentElement.removeAttribute("style");
    document.documentElement.removeAttribute("data-has-bg");
    document.documentElement.className = "";
  });

  // 从前的 apply* 把"挂 DOM"和"写 localStorage"绑在一起，于是 initAppearance 一跑，
  // 盘上凭空多出 material/palette/alpha/theme 四个值。后果不是"多占一格"：
  // readMaterial() 写的是 `=== "panel" ? panel : glass`，盘上有了 "glass" 之后，
  // 「用户从没表态」与「用户选了 glass」在数据上不可分 —— 跟 theme 那一路同一个病。
  it("全新机器跑一次 initAppearance ⇒ 盘上一个键都不该多", () => {
    initAppearance();
    expect(Object.keys(localStorage)).toEqual([]);
  });

  it("已有值时启动也不改写它", () => {
    localStorage.setItem("rolecard.alpha", "40");
    initAppearance();
    expect(localStorage.getItem("rolecard.alpha")).toBe("40");
  });
});

describe("出厂默认值与 index.css 字面值同组（断言在 python 侧）", () => {
  it("JS 侧的三个常量是有限数（值本身由 scripts/consistency 对 CSS 核）", () => {
    // 为什么不在这儿 import CSS 比字面值：vitest 把 .css 导入桩成空字符串，
    // 那样写的断言**永远拿到空串然后永远通过** —— 一个从不失败的检查比没有检查更坏。
    // 真比对在 scripts/consistency/checks_runtime.py 的「外观默认值单一真值」，
    // 它直接读 index.css 原文，JS 侧读不到就报错。
    expect(BG_DIM_DEFAULT).toBe(0.25);
    expect(BG_BLUR_DEFAULT).toBe(0);
    expect(SOLID_DEFAULT).toBe(100);
  });
});
