// @vitest-environment jsdom
// PetSprite（waifu spritesheet 渲染器）接线测试。
// 钉三件事：无素材/加载失败 → 回退 GeometricPet（桌宠不能因为缺一张图消失）；
// 有素材 → 按 8×9 协议算 background 位置，且状态换行（说话/挂机不同行）；
// 帧推进靠时间（fake timers 下每帧换个列）。

import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { FRAME_MS, PetSprite } from "./PetSprite";

const SHEET = "/pets/lumina/sprite.webp";

function bg(node: HTMLElement): string {
  return node.style.backgroundPosition;
}

describe("PetSprite", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("没有素材时回退到 GeometricPet（形象本体不消失）", () => {
    render(<PetSprite status="idle" />);
    expect(screen.getByTestId("pet-figure")).toBeTruthy();
    expect(screen.queryByTestId("pet-sprite")).toBeNull();
  });

  it("有素材时按协议渲染：背景图 + 状态决定行、帧推进换列", () => {
    render(<PetSprite src={SHEET} status="idle" width={160} height={184} />);
    const sprite = screen.getByTestId("pet-sprite");
    expect(sprite.style.backgroundImage).toContain("lumina/sprite.webp");
    // idle 在第 0 行：列随帧走，但 y 始终是 0。
    expect(bg(sprite)).toContain("px 0px");
    const first = bg(sprite);
    act(() => {
      vi.advanceTimersByTime(FRAME_MS * 2);
    });
    const later = bg(sprite);
    expect(later).not.toBe(first); // 帧往前推进了
  });

  it("状态换行：speaking 不再停在 idle 行（y 落在第 3 行那一段）", () => {
    const { rerender } = render(<PetSprite src={SHEET} status="idle" height={184} />);
    rerender(<PetSprite src={SHEET} status="speaking" height={184} />);
    const sprite = screen.getByTestId("pet-sprite");
    expect(bg(sprite)).toContain("-552px"); // 3 × 208（× 缩放 184/208）
  });
});