/** 面板自动收起判据的两臂（`lib/petPanel.ts`）。 */

import { describe, expect, it } from "vitest";

import { PANEL_TICK_MS, panelCountdown, panelHeld, panelTimerArmed } from "./petPanel";

describe("托盘档位 → 要不要计时", () => {
  it("0 = 不自动收起：整条链不启动", () => {
    expect(panelTimerArmed(0)).toBe(false);
  });

  it("任何正数都启动（含 5 秒这一档）", () => {
    expect(panelTimerArmed(5000)).toBe(true);
    expect(panelTimerArmed(60000)).toBe(true);
  });

  it("坏值不当成「一直收」：NaN / 负数一律不启动", () => {
    expect(panelTimerArmed(Number.NaN)).toBe(false);
    expect(panelTimerArmed(-15000)).toBe(false);
  });
});

describe("占住的三种情况都不倒数", () => {
  const free = { pointerInside: false, focusInside: false, speaking: false };

  it("指针在面板上 ⇒ 剩余不动（这是「别抢我正在读的」那一半）", () => {
    expect(panelCountdown(1200, panelHeld({ ...free, pointerInside: true }))).toBe(1200);
  });

  it("焦点在面板里（正在打字）⇒ 剩余不动", () => {
    expect(panelCountdown(1200, panelHeld({ ...free, focusInside: true }))).toBe(1200);
  });

  it("她正在说话 ⇒ 剩余不动", () => {
    expect(panelCountdown(1200, panelHeld({ ...free, speaking: true }))).toBe(1200);
  });

  it("反向臂：谁都不占 ⇒ 每拍减一个 tick", () => {
    expect(panelCountdown(1200, panelHeld(free))).toBe(1200 - PANEL_TICK_MS);
  });
});

describe("到点与暂停之后继续", () => {
  it("15 秒这一档：30 拍之后到 0", () => {
    let left = 15000;
    for (let i = 0; i < 30; i += 1) left = panelCountdown(left, false);
    expect(left).toBeLessThanOrEqual(0);
  });

  it("中途占住 ⇒ 只是暂停，不重新计时（路过一下不该白得 15 秒）", () => {
    let left = 15000;
    for (let i = 0; i < 28; i += 1) left = panelCountdown(left, false); // 14 秒过去
    for (let i = 0; i < 20; i += 1) left = panelCountdown(left, true); // 手放上去 10 秒
    expect(left).toBe(1000);
    left = panelCountdown(left, false);
    left = panelCountdown(left, false);
    expect(left).toBeLessThanOrEqual(0);
  });
});
