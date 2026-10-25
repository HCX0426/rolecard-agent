/** 外观偏好（材质 × 配色 × 透明度 × 明暗）—— 四个正交开关（2026-10-25）。
 *
 *  为什么分四个：用户点破过两次——先是"毛玻璃透明 ≠ 暖色调"（材质与配色是两件事），
 * 再是"玻璃没有透明度设置吗"（"透多少"又是一件独立的事）。把它们混成一锅就谁也调不了。
 * 现在：材质（玻璃 ↔ 实板）、配色（暖可可 ↔ 冷中性）、透明度（5 档）、明暗各管各的，
 * 组合出任意多种样子，任意两个不互相影响。
 *
 *  落地方式：材质/配色写 <html> 的 data-material / data-palette，透明度写
 *  --glass-alpha / --glass-alpha-soft 两个 CSS 变量，明暗用 .dark 类（见 index.css）。
 *  持久化在 localStorage。
 */
import { useCallback, useEffect, useState } from "react";

export type Material = "glass" | "panel";
export type Palette = "warm" | "cool";

const MATERIAL_KEY = "rolecard.material";
const PALETTE_KEY = "rolecard.palette";
/** 玻璃透明度（百分比）。存整数，CSS 那边拼成百分比。 */
const ALPHA_KEY = "rolecard.glassAlpha";

/** 透明度的档位（%）：做成档位而不是连续滑杆，是为了让"常用值"可复现
 *  （连续值每次拖动都是新组合，存进 localStorage 后没人能回到同一个）。 */
export const ALPHA_STEPS = [30, 45, 62, 78, 90] as const;

function readMaterial(): Material {
  if (typeof localStorage === "undefined") return "glass";
  const v = localStorage.getItem(MATERIAL_KEY);
  return v === "panel" ? "panel" : "glass";
}

function readPalette(): Palette {
  if (typeof localStorage === "undefined") return "warm";
  const v = localStorage.getItem(PALETTE_KEY);
  return v === "cool" ? "cool" : "warm";
}

function readAlpha(): number {
  if (typeof localStorage === "undefined") return 62;
  const v = Number(localStorage.getItem(ALPHA_KEY));
  return (ALPHA_STEPS as readonly number[]).includes(v) ? v : 62;
}

function applyMaterial(m: Material) {
  document.documentElement.setAttribute("data-material", m);
  localStorage.setItem(MATERIAL_KEY, m);
}

function applyPalette(p: Palette) {
  document.documentElement.setAttribute("data-palette", p);
  localStorage.setItem(PALETTE_KEY, p);
}

function applyAlpha(alpha: number) {
  // 两档一起写：固定层（侧栏/页头/输入框）与滚动区内容，前者更透、后者略实
  // （滚动时透太多会晃眼）。
  document.documentElement.style.setProperty("--glass-alpha", `${alpha}%`);
  document.documentElement.style.setProperty("--glass-alpha-soft", `${Math.min(100, alpha + 12)}%`);
  localStorage.setItem(ALPHA_KEY, String(alpha));
}

/** 启动时把偏好写到 <html>（在 React 挂载前跑，避免第一帧闪错材质）。
 *  index.html 里已有一份等价的内联脚本（首帧前更早）；这里是 React 侧的权威 —— 两者
 *  读同一个 localStorage 键，所以值永远一致。 */
export function initAppearance() {
  applyMaterial(readMaterial());
  applyPalette(readPalette());
  applyAlpha(readAlpha());
}

/** 四个开关的当前值 + 切换器。 */
export function useAppearance() {
  const [material, setMaterial] = useState<Material>(readMaterial);
  const [palette, setPalette] = useState<Palette>(readPalette);
  const [alpha, setAlpha] = useState<number>(readAlpha);

  useEffect(() => {
    applyMaterial(material);
  }, [material]);
  useEffect(() => {
    applyPalette(palette);
  }, [palette]);
  useEffect(() => {
    applyAlpha(alpha);
  }, [alpha]);

  const toggleMaterial = useCallback(() => {
    setMaterial((m) => (m === "glass" ? "panel" : "glass"));
  }, []);
  const togglePalette = useCallback(() => {
    setPalette((p) => (p === "warm" ? "cool" : "warm"));
  }, []);
  const cycleAlpha = useCallback(() => {
    setAlpha((cur) => {
      const i = ALPHA_STEPS.indexOf(cur as (typeof ALPHA_STEPS)[number]);
      return ALPHA_STEPS[(i + 1) % ALPHA_STEPS.length];
    });
  }, []);

  return { material, palette, alpha, toggleMaterial, togglePalette, cycleAlpha };
}
