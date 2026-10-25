/** 外观偏好（材质 × 配色 × 主题）—— 三个正交开关（2026-10-25）。
 *
 *  为什么分三个：用户点破过"毛玻璃透明 ≠ 暖色调"——把它们混成一锅就谁也调不了。
 * 现在：材质（玻璃 ↔ 实板）、配色（暖可可 ↔ 冷中性）、明暗（浅 ↔ 深）各管各的，
 * 组合出八种样子，任意两个不互相影响。
 *
 *  落地方式：写 <html> 的 data-material / data-palette + .dark 类（材质与配色层
 *  读这两个属性，见 index.css）。持久化在 localStorage。
 */
import { useCallback, useEffect, useState } from "react";

export type Material = "glass" | "panel";
export type Palette = "warm" | "cool";

const MATERIAL_KEY = "rolecard.material";
const PALETTE_KEY = "rolecard.palette";

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

function applyMaterial(m: Material) {
  document.documentElement.setAttribute("data-material", m);
  localStorage.setItem(MATERIAL_KEY, m);
}

function applyPalette(p: Palette) {
  document.documentElement.setAttribute("data-palette", p);
  localStorage.setItem(PALETTE_KEY, p);
}

/** 启动时把偏好写到 <html>（在 React 挂载前跑，避免第一帧闪错材质）。
 *  index.html 里已有一份等价的内联脚本（首帧前更早）；这里是 React 侧的权威 —— 两者
 *  读同一个 localStorage 键，所以值永远一致。 */
export function initAppearance() {
  applyMaterial(readMaterial());
  applyPalette(readPalette());
}

/** 材质与配色的当前值 + 切换器。 */
export function useAppearance() {
  const [material, setMaterial] = useState<Material>(readMaterial);
  const [palette, setPalette] = useState<Palette>(readPalette);

  useEffect(() => {
    applyMaterial(material);
  }, [material]);
  useEffect(() => {
    applyPalette(palette);
  }, [palette]);

  const toggleMaterial = useCallback(() => {
    setMaterial((m) => (m === "glass" ? "panel" : "glass"));
  }, []);
  const togglePalette = useCallback(() => {
    setPalette((p) => (p === "warm" ? "cool" : "warm"));
  }, []);

  return { material, palette, toggleMaterial, togglePalette };
}
