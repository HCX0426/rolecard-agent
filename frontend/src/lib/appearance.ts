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

/** 透明度的取值范围与**出厂默认**。0 = 底色全实。
 *
 * 默认刻意取 0（全实）而不是"看起来很玻璃"的中间档：主窗是一扇**实心窗**
 * （`shell/main/windows.ts:createMainWindow` 没开 `transparent`），底下没有桌面可透 ——
 * 把底色调透只会把暖/冷那层雾底洗掉、露出窗体自带的白，用户读到的是"主题色没了"。
 * 真要玻璃质感，得先给背景图（`.glass` 糊的是**它身后画过的像素**，糊纯色等于没糊），
 * 所以顺序是"选图 → 再拉透明度"，默认那一格停在最不容易看错的那一头。
 */
export const ALPHA_MIN = 0;
export const ALPHA_MAX = 100;
export const ALPHA_DEFAULT = 0;

function clampAlpha(v: number): number {
  if (!Number.isFinite(v)) return ALPHA_DEFAULT;
  return Math.max(ALPHA_MIN, Math.min(ALPHA_MAX, Math.round(v)));
}

/**
 * 滑块值 → 三档**实心度**（喂给 CSS 的 `--bg-alpha` / `--glass-alpha` / `--glass-alpha-soft`）。
 *
 * ⚠ 这两件事方向相反，2026-11-02 之前就是栽在把它当同一件事。CSS 那三个变量被
 * `color-mix(in srgb, 色 var(--bg-alpha), transparent)` 消费 —— 那里的百分数是**留多少色**，
 * 即实心度；而界面滑块按 `AboutPanel` 的文案是**透明度**（0=全不透明、100=全透）。
 * 旧代码写 `--bg-alpha = ${alpha}%`，等于把透明度当实心度：拉满 ⇒ 一丝不透，
 * 就是用户那句「透明度百分百还是不咋透明」。
 *
 * 三档的**差值**沿用原作者的 -12 / -4：`index.css` 的 `:root` 默认就是 `100 / 88 / 96`，
 * 那正是 alpha=0（全不透明）这一头的样子 —— 面板比底色略透，层次由此而来。只补那一个
 * 缺失的减法，不去改作者定的层次关系。
 *
 * 纯函数、单点定义：`index.html` 里那份首帧脚本按同样算术抄一遍（它跑在打包前的裸 HTML 里，
 * import 不到这里），`appearance.test.ts` 钉住方向与这组默认读数，防再反、防再漂移。
 */
export function soliditiesFor(alpha: number): {
  bg: number;
  glass: number;
  glassSoft: number;
} {
  const bg = ALPHA_MAX - clampAlpha(alpha);
  return {
    bg,
    glass: Math.max(0, bg - 12),
    glassSoft: Math.max(0, bg - 4),
  };
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
  if (typeof localStorage === "undefined") return ALPHA_DEFAULT;
  // 键**不存在**必须落默认档，不能走 `Number(null)` —— 那是 0，而 0 是合法透明度，
  // 于是旧写法（`Number.isFinite(v) ? clampAlpha(v) : 100`）里"没设过"与"设成 0"不可分，
  // 默认回退那一路永远走不到。先判 null 再转换。
  const raw = localStorage.getItem(ALPHA_KEY);
  if (raw === null || raw === "") return ALPHA_DEFAULT;
  return clampAlpha(Number(raw));
}

function readNumber(key: string, fallback: number): number {
  if (typeof localStorage === "undefined") return fallback;
  // 与 readAlpha 同一个坑（2026-11-02 一并收）：`Number(null)` 是 0 且**有限**，
  // 于是"键不存在"会被读成"用户设了 0"，fallback 永远走不到。bgDim 那条的现形方式是
  // "选了背景图却没有暗化"—— 图上的字直接压在亮面上，正是可读性保障那一格失效。
  const raw = localStorage.getItem(key);
  if (raw === null || raw === "") return fallback;
  const v = Number(raw);
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
 *  换算的算术与方向都住在 `soliditiesFor`（那里记着为什么必须做一次减法）。 */
function applyAlpha(alpha: number) {
  const s = soliditiesFor(alpha);
  const root = document.documentElement;
  root.style.setProperty("--bg-alpha", `${s.bg}%`);
  root.style.setProperty("--glass-alpha", `${s.glass}%`);
  root.style.setProperty("--glass-alpha-soft", `${s.glassSoft}%`);
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
