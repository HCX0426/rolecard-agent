/** 外观偏好（材质 × 配色 × 透明度 × 背景图 × 明暗）—— 五个正交开关（2026-10-25）。
 *
 *  为什么分五个：用户连着点破三次——"毛玻璃透明 ≠ 暖色调"（材质与配色是两件事）、
 *  "玻璃没有透明度设置吗"（"透多少"又是一件独立的事）、"我要的是整个界面的透明度"
 *  （不只玻璃面板，**底色本身**也要能透）+ "要可输入的"（不是几个档位）。
 *  混成一锅就谁也调不了；现在每一件各管各的，任意组合都成立。
 *
 *  落地方式：
 *   材质/配色 → <html> 的 data-material / data-palette；
 *   透明度   → --bg-solid（底色实心度，一个数）；玻璃层的通透由 CSS 从这个数**派生**
 *              （--glass-alpha / --glass-alpha-soft 是 calc 出来的，见 index.css :root），
 *              JS 不再写它们 —— 从前写，于是把 .dark 里那组深色档位永远盖死；
 *   背景图   → --bg-image（url）+ --bg-blur / --bg-dim（可读性保障）+ data-has-bg；
 *   明暗     → .dark 类。全部持久化在 localStorage。
 */
import { useCallback, useState } from "react";

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

/** 出厂的**实心度**（= ALPHA_MAX - ALPHA_DEFAULT）：alpha=0 那一头，底色全实。
 *  必须与 index.css 里 `--bg-solid:` 的字面值相同，由一致性断言把守。 */
export const SOLID_DEFAULT = ALPHA_MAX - ALPHA_DEFAULT;

/** 模糊的出厂值，必须与 index.css 里 `--bg-blur:` 逐字相同（断言把守）。 */
export const BG_BLUR_DEFAULT = 0;

/** 暗化的出厂值，必须与 index.css 里 `--bg-dim:` 逐字相同（断言把守）。
 *  从前 CSS 写 0.35、这里写 0.25，而 JS 总把兜底值写成内联样式盖住 CSS —— 于是
 *  历史上用户看到的**一直是 0.25**，那行 0.35 是死码。取真正生效的那一组，像素不变。
 *  为什么留一份在 JS：滑块得有个数好显示；而"用户从没设过"时**不写内联样式**，
 *  让 CSS 那一份做主 —— 少一条能被误当作权威的声明。 */
export const BG_DIM_DEFAULT = 0.25;

function clampAlpha(v: number): number {
  if (!Number.isFinite(v)) return ALPHA_DEFAULT;
  return Math.max(ALPHA_MIN, Math.min(ALPHA_MAX, Math.round(v)));
}

/**
 * 滑块值 → **底色实心度**（喂给 CSS 的 `--bg-solid`，一个数）。
 *
 * ⚠ 滑块是"透多少"，CSS 那一路消费的是"留多少色"，方向相反 —— 2026-11-02 之前就是
 * 栽在把两者当同一件事：旧代码写 `--bg-alpha = ${alpha}%`，等于把透明度当实心度，
 * 拉满 ⇒ 一丝不透，正是用户那句「透明度百分百还是不咋透明」。
 *
 * 2026-11-03 归一化：这里从前返回**三档**（bg / glass / glassSoft，差值 -12 / -4），
 * 由 `applyAlpha` 写成三条内联样式。内联样式压过 `.dark` 选择器，于是 index.css 里
 * 深色那一档（92% / 97%）**从写下来那日起一次都没生效过** —— 真浏览器实测：带内联
 * 读到 88%/96%，把内联清掉才露出 92%/97%。三档的层次关系是 CSS 的事（它管主题、管
 * 材质、管深浅），JS 只管"用户要把底透多少"这一件事。现在只写一个数，层次差改由
 * CSS 里的 `--glass-offset` / `--glass-soft-offset` 承担，深色因此终于能自己调。
 *
 * 方向换算仍留在这里（而不是搬进 CSS）：那正是翻过车的地方，必须能被单测钉住。
 * 纯函数、单点定义：`index.html` 那份首帧脚本按同样算术抄一遍（它跑在打包前的裸
 * HTML 里，import 不到这里），两侧各只写这一条赋值；`scripts/consistency` 的
 * 「外观默认值单一真值」断言把 JS 兜底数字与 index.css 里的字面值对死。
 */
export function bgSolidity(alpha: number): number {
  return ALPHA_MAX - clampAlpha(alpha);
}

/** 明暗（2026-11-03 收进这一份实现）：键存在时以它为准，**只有缺键才**读系统偏好。
 *  从前这条判据住在 components/useTheme.ts，而 index.html 的首帧脚本完全不管明暗 ——
 *  于是深色用户每次启动都是"先按 CSS 默认画一帧浅底，React 起来再翻"。实测那一帧落在
 *  首绘之前（.dark 挂上 t=35ms，first-paint t=704ms），用户看不见；但判据确实只有一份
 *  住得着，另一份（首帧脚本）按同样规则抄。 */
const THEME_KEY = "theme";

export type Theme = "light" | "dark";

export function readTheme(): Theme {
  if (typeof localStorage !== "undefined") {
    const saved = localStorage.getItem(THEME_KEY);
    if (saved === "light" || saved === "dark") return saved;
  }
  if (typeof window !== "undefined" && window.matchMedia) {
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  return "light";
}

export function applyTheme(t: Theme) {
  document.documentElement.classList.toggle("dark", t === "dark");
}

/** 只在**用户明确选过**时落盘。从前是"useEffect 一挂载就写"，于是系统深色偏好第一次
 *  启动就被钉死成 `theme=dark` —— 之后系统改回浅色，应用再也不跟（用户从没做过这个选择）。
 *  缺键 = 跟随系统，这个状态必须能一直保持，所以"应用"与"记住"分成两个动作。 */
export function saveTheme(t: Theme) {
  localStorage.setItem(THEME_KEY, t);
}

/** 材质/配色同 theme 一路：DOM 随时可挂（那是无副作用的显示），落盘只在用户切换时。
 *  2026-11-03 归一化前它们各自的 apply* 都是「挂 DOM + 写 localStorage」绑在一起，
 *  于是 `initAppearance()` 一跑就把 `rolecard.material=glass` 写进盘里 —— 用户从没选过，
 *  盘上却多了个值；`readMaterial()` 的 `=== "panel" ? panel : glass` 于是永远读得到东西，
 *  「缺键」这一路从此不可达。现在拆开：读判缺键，写只在 toggle 里。 */
function applyMaterial(m: Material) {
  document.documentElement.setAttribute("data-material", m);
}

function applyPalette(p: Palette) {
  document.documentElement.setAttribute("data-palette", p);
}

/** 透明度一次写**一个数**：底色实心度。三档层次由 CSS 自己算（见 index.css :root 那段）。 */
function applyAlpha(alpha: number) {
  document.documentElement.style.setProperty("--bg-solid", String(bgSolidity(alpha)));
}

function readMaterial(): Material {
  if (typeof localStorage === "undefined") return "glass";
  return localStorage.getItem(MATERIAL_KEY) === "panel" ? "panel" : "glass";
}

function readPalette(): Palette {
  if (typeof localStorage === "undefined") return "warm";
  return localStorage.getItem(PALETTE_KEY) === "cool" ? "cool" : "warm";
}

function readNumber(key: string, fallback: number): number {
  const v = readNumberOpt(key);
  return v === null ? fallback : v;
}

/** 读一个数字键，**区分「没设过」与「设成 0」**（返回 null 表示前者）。
 *  从前只有一句 `Number(localStorage.getItem(k))`：缺键给出 0（`Number(null)` 是 0 且
 *  有限），于是"没设过"被读成"设了 0"，兜底那一路永远是死码。bgDim 那条的现形方式是
 *  "选了背景图却没有暗化" —— 图上字直接压在亮面上，可读性保障那一格失效。 */
function readNumberOpt(key: string): number | null {
  if (typeof localStorage === "undefined") return null;
  const raw = localStorage.getItem(key);
  if (raw === null || raw === "") return null;
  const v = Number(raw);
  return Number.isFinite(v) ? v : null;
}

function readAlpha(): number {
  const raw = readNumberOpt(ALPHA_KEY);
  return raw === null ? ALPHA_DEFAULT : clampAlpha(raw);
}

/** 落盘只在**用户动作**里发生（save* 一组），挂 DOM 只在 apply* 一组 —— 从前两者绑在
 *  同一个函数里，于是 `initAppearance()` 一跑就把 `rolecard.material=glass`、
 *  `rolecard.alpha=0` 写进盘上：用户从没选过，盘上却有了值，`readXxx()` 的"缺键 ⇒
 *  默认"那一路从此永远走不到（这正是 2026-11-02 那批 `Number(null)===0` 死码的兄弟：
 *  同一件事 —— 把"没表态"当成"表了态"）。 */
function saveMaterial(m: Material) {
  localStorage.setItem(MATERIAL_KEY, m);
}

function savePalette(p: Palette) {
  localStorage.setItem(PALETTE_KEY, p);
}

function saveAlpha(a: number) {
  localStorage.setItem(ALPHA_KEY, String(a));
}

/** 背景图三件套的**唯一**落地原语。传 null 表示「用户没设过」⇒ **什么都不写**，
 *  让 index.css 那份默认做主（从前是"没设过也照兜底值写一条内联"，于是 CSS 里的默认
 *  `--bg-dim: 0.35` 与 JS 里的 0.25 各有主人，用户历来看到的是 0.25，那行 CSS 是死码）。
 *  与"设成空串"是两件事：空串 = 用户点了移除，得真的写回 none 并摘掉属性。
 *
 *  ⚠ 这三个函数是 initAppearance 与 useAppearance **共用**的同一条路：两处落地方式
 *  不同的时候（从前 hook 首拍也写一遍），"没设过"这个状态永远存不住。 */
function applyBgImage(image: string) {
  const root = document.documentElement;
  // 有图时给 <html> 挂一个属性：次要文字在图上要加深（无图时不该动配色）。
  // 用 html 上的**属性**而不是 body 的 class：首帧那段脚本跑在 <head> 里，那时
  // document.body 还不存在，挂不了 class —— 于是历史上「首帧没加深、JS 起来才加深」，
  // 同一份偏好两处落地不一致。属性可以写在 <html> 上，两处都够得着。
  root.toggleAttribute("data-has-bg", !!image);
  root.style.setProperty("--bg-image", image ? `url("${image}")` : "none");
}

function applyBgBlur(blur: number) {
  document.documentElement.style.setProperty("--bg-blur", `${blur}px`);
}

function applyBgDim(dim: number) {
  document.documentElement.style.setProperty("--bg-dim", String(dim));
}

/** 背景图的读数：null = 这台机器从没设过（⇒ 什么都不写，CSS 默认做主）；
 *  "" = 设过又移除了（⇒ 真的要写 `none` 并摘属性）。 */
function readBgImage(): string | null {
  if (typeof localStorage === "undefined") return null;
  return localStorage.getItem(BG_KEY);
}

/** 启动时把偏好写到 <html>（在 React 挂载前跑，避免第一帧闪错材质）。
 *  index.html 里已有一份等价的内联脚本（首帧前更早）；这里是 React 侧的权威 —— 两者
 *  读同一个 localStorage 键，所以值永远一致。**只挂 DOM，不落盘**（落盘是用户动作）。 */
export function initAppearance() {
  applyTheme(readTheme());
  applyMaterial(readMaterial());
  applyPalette(readPalette());
  applyAlpha(readAlpha());
  const img = readBgImage();
  const blur = readNumberOpt(BG_BLUR_KEY);
  const dim = readNumberOpt(BG_DIM_KEY);
  if (img !== null) applyBgImage(img);
  if (blur !== null) applyBgBlur(blur);
  if (dim !== null) applyBgDim(dim);
}

/** 五个开关的当前值 + 切换器。
 *
 *  2026-11-03 归一化：从前这里每个开关挂一个 `useEffect(apply, [state])`，而 apply 里
 *  顺手 `localStorage.setItem` —— 于是**挂载首拍**就把全部五项写进盘上，"用户从没表态"
 *  这个状态在盘上存不住（`readXxx` 的缺键分支因此不可达），且与 `initAppearance` 是
 *  两条各写一遍的落地路径。改成：DOM 与落盘都只在**用户动作**里发生（下面这一组 setter
 *  是唯一的写路径），启动那一路交给 `initAppearance`。少一条路径 = 少一处可能不一致。 */
export function useAppearance() {
  const [material, setMaterialState] = useState<Material>(readMaterial);
  const [palette, setPaletteState] = useState<Palette>(readPalette);
  const [alpha, setAlphaState] = useState<number>(readAlpha);
  const [bgImage, setBgImageState] = useState<string>(() => readBgImage() ?? "");
  const [bgBlur, setBgBlurState] = useState<number>(() => readNumber(BG_BLUR_KEY, BG_BLUR_DEFAULT));
  // 显示用的兜底 = CSS 里那一条（同一个常量，别在这儿再打一个字面量）。
  const [bgDim, setBgDimState] = useState<number>(() => readNumber(BG_DIM_KEY, BG_DIM_DEFAULT));

  const toggleMaterial = useCallback(() => {
    const next: Material = material === "glass" ? "panel" : "glass";
    applyMaterial(next);
    saveMaterial(next);
    setMaterialState(next);
  }, [material]);

  const togglePalette = useCallback(() => {
    const next: Palette = palette === "warm" ? "cool" : "warm";
    applyPalette(next);
    savePalette(next);
    setPaletteState(next);
  }, [palette]);

  const setAlpha = useCallback((v: number) => {
    const next = clampAlpha(v);
    applyAlpha(next);
    saveAlpha(next);
    setAlphaState(next);
  }, []);

  const setBgImage = useCallback((image: string) => {
    applyBgImage(image);
    localStorage.setItem(BG_KEY, image);
    setBgImageState(image);
  }, []);

  const setBgBlur = useCallback((blur: number) => {
    applyBgBlur(blur);
    localStorage.setItem(BG_BLUR_KEY, String(blur));
    setBgBlurState(blur);
  }, []);

  const setBgDim = useCallback((dim: number) => {
    applyBgDim(dim);
    localStorage.setItem(BG_DIM_KEY, String(dim));
    setBgDimState(dim);
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
    setAlpha,
    setBgImage,
    setBgBlur,
    setBgDim,
  };
}
