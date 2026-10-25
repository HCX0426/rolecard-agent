/** 外观偏好（材质 × 配色 × 透明度 × 背景图 × 明暗）—— 五个正交开关（2026-10-25）。
 *
 *  为什么分五个：用户连着点破三次——"毛玻璃透明 ≠ 暖色调"（材质与配色是两件事）、
 *  "玻璃没有透明度设置吗"（"透多少"又是一件独立的事）、"我要的是整个界面的透明度"
 *  （不只玻璃面板，**底色本身**也要能透）+ "要可输入的"（不是几个档位）。
 *  混成一锅就谁也调不了；现在每一件各管各的，任意组合都成立。
 *
 *  落地方式：
 *   材质/配色 → <html> 的 data-material / data-palette；
 *   透明度   → --glass-alpha（玻璃层）与 --bg-alpha（**底色**，整个界面的透光）；
 *   背景图   → --bg-image（url）+ --bg-blur / --bg-dim（可读性保障）；
 *   明暗     → .dark 类。全部持久化在 localStorage。
 */
import { useCallback, useEffect, useState } from "react";

export type Material = "glass" | "panel";
export type Palette = "warm" | "cool";

const MATERIAL_KEY = "rolecard.material";
const PALETTE_KEY = "rolecard.palette";
/** 透明度（百分比，0 = 全不透明、100 = 全透）。可输入任意整数，不是档位。 */
const ALPHA_KEY = "rolecard.alpha";
/** 背景图：本地图片的 URL（后端静态托管）或内置渐变的标识。 */
const BG_KEY = "rolecard.bgImage";
const BG_BLUR_KEY = "rolecard.bgBlur";
const BG_DIM_KEY = "rolecard.bgDim";

/** 透明度的取值范围。100 = 底色全透（背后只剩背景图/桌面）。 */
export const ALPHA_MIN = 0;
export const ALPHA_MAX = 100;

function clampAlpha(v: number): number {
  if (!Number.isFinite(v)) return 100;
  return Math.max(ALPHA_MIN, Math.min(ALPHA_MAX, Math.round(v)));
}

function readMaterial(): Material {
  if (typeof localStorage === "undefined") return "glass";
  return localStorage.getItem(MATERIAL_KEY) === "panel" ? "panel" : "glass";
}

function readPalette(): Palette {
  if (typeof localStorage === "undefined") return "warm";
  return localStorage.getItem(PALETTE_KEY) === "cool" ? "cool" : "warm";
}

function readAlpha(): number {
  if (typeof localStorage === "undefined") return 100;
  const v = Number(localStorage.getItem(ALPHA_KEY));
  return Number.isFinite(v) ? clampAlpha(v) : 100;
}

function readNumber(key: string, fallback: number): number {
  if (typeof localStorage === "undefined") return fallback;
  const v = Number(localStorage.getItem(key));
  return Number.isFinite(v) ? v : fallback;
}

function applyMaterial(m: Material) {
  document.documentElement.setAttribute("data-material", m);
  localStorage.setItem(MATERIAL_KEY, m);
}

function applyPalette(p: Palette) {
  document.documentElement.setAttribute("data-palette", p);
  localStorage.setItem(PALETTE_KEY, p);
}

/** 透明度一次写三处：底色（整个界面的透光）+ 玻璃层（面板自己的通透）。
 *  玻璃层比底色**更实**一点（alpha - 12），否则面板与底色一样透就没有层次了。 */
function applyAlpha(alpha: number) {
  const root = document.documentElement;
  root.style.setProperty("--bg-alpha", `${alpha}%`);
  root.style.setProperty("--glass-alpha", `${Math.max(0, alpha - 12)}%`);
  root.style.setProperty("--glass-alpha-soft", `${Math.max(0, alpha - 4)}%`);
  localStorage.setItem(ALPHA_KEY, String(alpha));
}

function applyBackground(image: string, blur: number, dim: number) {
  const root = document.documentElement;
  // 有图时给 body 挂一个类：次要文字在图上要加深（无图时不该动配色）
  document.body.classList.toggle("has-bg", !!image);
  root.style.setProperty("--bg-image", image ? `url("${image}")` : "none");
  root.style.setProperty("--bg-blur", `${blur}px`);
  root.style.setProperty("--bg-dim", String(dim));
  localStorage.setItem(BG_KEY, image);
  localStorage.setItem(BG_BLUR_KEY, String(blur));
  localStorage.setItem(BG_DIM_KEY, String(dim));
}

/** 启动时把偏好写到 <html>（在 React 挂载前跑，避免第一帧闪错材质）。
 *  index.html 里已有一份等价的内联脚本（首帧前更早）；这里是 React 侧的权威 —— 两者
 *  读同一个 localStorage 键，所以值永远一致。 */
export function initAppearance() {
  applyMaterial(readMaterial());
  applyPalette(readPalette());
  applyAlpha(readAlpha());
  applyBackground(
    readNumber(BG_BLUR_KEY, 0) >= 0 ? localStorage.getItem(BG_KEY) || "" : "",
    readNumber(BG_BLUR_KEY, 0),
    readNumber(BG_DIM_KEY, 0.25),
  );
}

/** 五个开关的当前值 + 切换器。 */
export function useAppearance() {
  const [material, setMaterial] = useState<Material>(readMaterial);
  const [palette, setPalette] = useState<Palette>(readPalette);
  const [alpha, setAlpha] = useState<number>(readAlpha);
  const [bgImage, setBgImage] = useState<string>(() =>
    typeof localStorage === "undefined" ? "" : localStorage.getItem(BG_KEY) || "",
  );
  const [bgBlur, setBgBlur] = useState<number>(() => readNumber(BG_BLUR_KEY, 0));
  const [bgDim, setBgDim] = useState<number>(() => readNumber(BG_DIM_KEY, 0.25));

  useEffect(() => {
    applyMaterial(material);
  }, [material]);
  useEffect(() => {
    applyPalette(palette);
  }, [palette]);
  useEffect(() => {
    applyAlpha(alpha);
  }, [alpha]);
  useEffect(() => {
    applyBackground(bgImage, bgBlur, bgDim);
  }, [bgImage, bgBlur, bgDim]);

  const toggleMaterial = useCallback(() => {
    setMaterial((m) => (m === "glass" ? "panel" : "glass"));
  }, []);
  const togglePalette = useCallback(() => {
    setPalette((p) => (p === "warm" ? "cool" : "warm"));
  }, []);
  const setAlphaClamped = useCallback((v: number) => {
    setAlpha(clampAlpha(v));
  }, []);

  return {
    material,
    palette,
    alpha,
    bgImage,
    bgBlur,
    bgDim,
    toggleMaterial,
    togglePalette,
    setAlpha: setAlphaClamped,
    setBgImage,
    setBgBlur,
    setBgDim,
  };
}
