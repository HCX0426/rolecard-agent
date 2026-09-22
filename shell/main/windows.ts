/**
 * 两扇窗：主窗（控制台）与桌宠窗。
 *
 * 两条从 Tauri 实测带过来的规矩，换壳不改变它们：
 * 1. **窗口不直接指向后端地址**。后端没起来时内核浏览器会显示它自己的错误页，我们写的
 *    可读提示根本没机会渲染。所以每扇窗都先加载壳自带的本地着陆页（`shell/web`），
 *    由它探活再跳。
 * 2. **桌宠的透明要 `transparent` + `backgroundColor:#00000000` + 无阴影三者齐**：少任何
 *    一个，那块 200×240 就是一张灰纸贴在桌面上。
 */
import { BrowserWindow, screen } from "electron";
import path from "node:path";

import {
  initialBounds,
  loadPetPrefs,
  trackBounds,
  type DockEdge,
  type PetPrefs,
} from "./state";

export const PET_WIDTH = 200;
export const PET_HEIGHT = 240;

/** 悬停展开后的面板尺寸（设计稿 §7.2）。宽度够读一句话，高度够放最近几条 + 输入框。 */
export const PET_PANEL_WIDTH = 380;
export const PET_PANEL_HEIGHT = 520;

// ---- 靠边隐藏（趴边）的几何，设计稿 §7.6 -------------------------------------------------
// 露出的目标必须是**色片的像素**，不是窗口的像素：色片 88 见方、水平居中在 200 宽的窗里，
// 两侧各 56px 是透明的 —— 推 40px 藏的全是透明边，色片一格没少，看上去根本没吸进去
// （提案那版就是这么算错的）。而透明区不吃 hover，露出的那条窄边要全是色片本体才点得着。
const SPRITE_PX = 88;
const SPRITE_BOTTOM_GAP = 4; // 根节点的 `pb-1`
const SIDE_PAD = (PET_WIDTH - SPRITE_PX) / 2;
/** 藏起来之后仍露在屏外的色片宽度。 */
export const PET_PEEK_PX = 48;
/** 拖完那一刻离边多近才算"往边上放"（再远就是"放在桌面上"，不该吸）。 */
const SNAP_PX = 24;
const SIDE_TUCK = SIDE_PAD + (SPRITE_PX - PET_PEEK_PX); // 56 + 40 = 96
const BOTTOM_TUCK = SPRITE_PX + SPRITE_BOTTOM_GAP - PET_PEEK_PX; // 88 + 4 - 48 = 44

export interface Rect {
  x: number;
  y: number;
  width: number;
  height: number;
}

/**
 * 展开 / 收起时桌宠窗该落在哪：**色片在屏幕上不动**。
 *
 * 页面里色片是"底部居中"（`items-center justify-end`），所以锚点是**下沿与水平中心**：
 * 展开只向上和向两侧长。反过来若锚在左上角，鼠标一悬停宠物就往下右滑走 —— 用户看到的是
 * "我想点它它跑了"。
 *
 * 夹取按工作区（与首次落点同一口径）：贴着屏幕右下角的桌宠展开后会有一截跑到屏幕外，
 * 那里既读不到也点不到，所以宁可让面板往左上长。
 */
export function petBoundsFor(current: Rect, expanded: boolean): Rect {
  const width = expanded ? PET_PANEL_WIDTH : PET_WIDTH;
  const height = expanded ? PET_PANEL_HEIGHT : PET_HEIGHT;
  const centerX = current.x + current.width / 2;
  const bottom = current.y + current.height;
  const next: Rect = {
    x: Math.round(centerX - width / 2),
    y: Math.round(bottom - height),
    width,
    height,
  };
  const { workArea } = screen.getPrimaryDisplay();
  const maxX = workArea.x + Math.max(0, workArea.width - width);
  const maxY = workArea.y + Math.max(0, workArea.height - height);
  return {
    ...next,
    x: Math.min(Math.max(next.x, workArea.x), maxX),
    y: Math.min(Math.max(next.y, workArea.y), maxY),
  };
}

const PRELOAD = path.join(__dirname, "..", "preload", "bridge.js");
const LANDING = path.join(__dirname, "..", "..", "web", "index.html");

/** 桌宠现在是不是摊开的。只有 `setPetExpanded` 会改它，所以它就是"展开态"的那一份真相。 */
let petIsExpanded = false;

/** 展开**之前**那块收起态矩形。收起时回到这里，而不是从展开态反推 —— 边上面板放不下时
 *  夹取会把整扇窗往里挪（实测贴着左沿悬停一下，色片就被推到离边 90px 处），于是"我拖它，
 *  它自己跑开"，而靠边隐藏判的正是离边 ≤ 24px，永远够不到。宠物不该因为被看了一眼就搬家。 */
let petRectBeforeExpand: Rect | null = null;

/** 吸在哪条边、此刻藏着还是露着。null = 没吸边。 */
let petDock: { edge: DockEdge; tucked: boolean } | null = null;

/** 拖完时面板还摊着 ⇒ 先记账，等收起那一刻再吸（见 `petDragEnded`）。 */
let petDockPending = false;

/** 「靠边隐藏」总开关的当前值。`applyPetPrefs` 每次落偏好时顺手带进来。 */
let dockEnabled = true;

/** 吸边状态变了就通知一次（落库与日志都在 index.ts 那一侧，这里只管窗）。 */
let onDockChanged: ((edge: DockEdge | null) => void) | null = null;

export function onPetDockChanged(cb: (edge: DockEdge | null) => void): void {
  onDockChanged = cb;
}

function workRect(): Rect {
  const { workArea } = screen.getPrimaryDisplay();
  return workArea;
}

function clamp(n: number, low: number, high: number): number {
  return Math.min(Math.max(n, low), high);
}

/** 贴边但**一格都没藏**的那块矩形（另两条轴仍夹在工作区里）。 */
function flushRect(edge: DockEdge, rect: Rect, work: Rect): Rect {
  const size = petSize();
  const x = clamp(rect.x, work.x, work.x + Math.max(0, work.width - size.width));
  const y = clamp(rect.y, work.y, work.y + Math.max(0, work.height - size.height));
  if (edge === "left") return { ...size, x: work.x, y };
  if (edge === "right") return { ...size, x: work.x + work.width - size.width, y };
  return { ...size, x, y: work.y + work.height - size.height };
}

/** 藏起来之后的矩形：沿吸的那条轴推出去，露出 `PET_PEEK_PX` 的色片。 */
function tuckedRect(edge: DockEdge, rect: Rect, work: Rect): Rect {
  const flush = flushRect(edge, rect, work);
  if (edge === "left") return { ...flush, x: flush.x - SIDE_TUCK };
  if (edge === "right") return { ...flush, x: flush.x + SIDE_TUCK };
  return { ...flush, y: flush.y + BOTTOM_TUCK };
}

// ---- 滑进滑出，不瞬移 -------------------------------------------------------------------
// 任务栏、GNOME 的 Dash、macOS 的 Dock 这些都是**滑**进滑出的，一帧到位在桌面上读起来是
// "它不见了"而不是"它收起来了"。时长与曲线照那类东西的量级来：百来毫秒到三百毫秒，
// **起步快、收尾慢**（ease-out cubic）—— 用户报"140ms 太快、没渐进"，缺的就是这条缓动。
const DOCK_MS = 280;
const GROW_MS = 200;
const SHRINK_MS = 160;
const SLIDE_STEP_MS = 16; // ≈60fps
let slideTimer: NodeJS.Timeout | null = null;

function easeOut(t: number): number {
  return 1 - Math.pow(1 - t, 3);
}

function stopSlide(): void {
  if (slideTimer) clearInterval(slideTimer);
  slideTimer = null;
}

function slidePetTo(win: BrowserWindow, target: Rect, ms: number): void {
  stopSlide();
  const from = win.getBounds();
  if (
    from.x === target.x &&
    from.y === target.y &&
    from.width === target.width &&
    from.height === target.height
  )
    return;
  const start = Date.now();
  slideTimer = setInterval(() => {
    if (win.isDestroyed()) return stopSlide();
    const t = Math.min(1, (Date.now() - start) / ms);
    if (t >= 1) {
      stopSlide();
      win.setBounds(target);
      return;
    }
    const k = easeOut(t);
    const lerp = (a: number, b: number) => Math.round(a + (b - a) * k);
    win.setBounds({
      x: lerp(from.x, target.x),
      y: lerp(from.y, target.y),
      width: lerp(from.width, target.width),
      height: lerp(from.height, target.height),
    });
  }, SLIDE_STEP_MS);
}

/**
 * 拖完那一刻判：该吸哪条边。
 *
 * 贴角上时**左右优先于下**：侧着趴是这类桌宠最常见的那个姿势，而且下边推出去会压到任务栏
 * 那条带（`workArea` 不含任务栏），风险留给它自己那条（§7.6 要求本机实测为准）。
 */
export function petDockEdgeFor(rect: Rect, work: Rect): DockEdge | null {
  const size = petSize();
  const right = rect.x + size.width;
  const bottom = rect.y + size.height;
  if (rect.x - work.x <= SNAP_PX) return "left";
  if (work.x + work.width - right <= SNAP_PX) return "right";
  if (work.y + work.height - bottom <= SNAP_PX) return "bottom";
  return null;
}

/**
 * 藏着的话滑回贴边。展开面板前、拖之前、关掉「靠边隐藏」时都要先走这一步。
 * `instant` 是给拖拽留的：拖的过程中滑，等于跟用户的手抢那 140ms。
 */
export function petUntuck(win: BrowserWindow, options: { instant?: boolean } = {}): void {
  if (!petDock || !petDock.tucked) return;
  petDock = { ...petDock, tucked: false };
  const target = flushRect(petDock.edge, win.getBounds(), workRect());
  if (options.instant) {
    stopSlide();
    win.setBounds(target);
    return;
  }
  slidePetTo(win, target, DOCK_MS);
}

/**
 * 判边 → 藏起来 → 把边报出去。
 *
 * 没到任何一条边（或总开关关着）就**什么都不挪**（它就在用户放手的地方），只把吸边状态清掉
 * —— 清掉这一步不能省：`collapsedPetRect` 会照吸边状态把落库矩形换算成贴边位，留着旧状态
 * 就等于用户把它拖到桌面中间，下次存盘却被拽回边上。
 */
function settlePetDock(win: BrowserWindow): DockEdge | null {
  const edge = dockEnabled ? petDockEdgeFor(petBoundsFor(win.getBounds(), false), workRect()) : null;
  if (!edge) {
    if (petDock) onDockChanged?.(null);
    petDock = null;
    return null;
  }
  petDock = { edge, tucked: true };
  slidePetTo(win, tuckedRect(edge, win.getBounds(), workRect()), DOCK_MS);
  onDockChanged?.(edge);
  return edge;
}

/**
 * 松手那一刻（§7.6）。
 *
 * **面板还摊着时不吸，只记账**：拖拽必然带着悬停（鼠标在色片上才拖得动），那时候窗口是
 * 380×520 的展开形状 —— 拿它判边、再按它算贴边位，等于把"藏一半"作用在面板上：面板会被
 * 推出屏幕，而你正在读它。所以吸边发生在**面板收起的那一瞬间**（`setPetExpanded(false)`）。
 */
export function petDragEnded(win: BrowserWindow): void {
  if (!dockEnabled) {
    petDockPending = false;
    settlePetDock(win);
    return;
  }
  if (petIsExpanded) {
    petDockPending = true;
    return;
  }
  settlePetDock(win);
}

/** 开机恢复：偏好里记着吸哪条边且开关没关，就按**当前**工作区重算贴边位再藏进去。 */
export function restorePetDock(win: BrowserWindow, prefs: PetPrefs): void {
  if (!prefs.dockEnabled || !prefs.docked) return;
  stopSlide(); // 开机那一下不滑：从屏外滑进来会让人以为宠物是自己跑出去的
  petDock = { edge: prefs.docked, tucked: false };
  win.setBounds(flushRect(prefs.docked, win.getBounds(), workRect())); // 先落到屏内的贴边位
  petDock = { edge: prefs.docked, tucked: true };
  win.setBounds(tuckedRect(prefs.docked, win.getBounds(), workRect()));
}

/** 关掉「靠边隐藏」：露出来并忘掉吸边（开关关掉之后还藏着，就是"关了却还有影响"）。 */
export function releasePetDock(win: BrowserWindow): void {
  petDockPending = false;
  petUntuck(win);
  if (petDock) onDockChanged?.(null);
  petDock = null;
}

/**
 * 悬停展开 / 收起桌宠窗。页面只说"要不要展开"，**几何全在壳里** —— 只有主进程知道工作区
 * 在哪、色片锚在哪个角，让页面报坐标等于把"窗口能摆到哪"交给一个后端托管的源。
 */
export function setPetExpanded(win: BrowserWindow, expanded: boolean): void {
  // 状态没变就一个 setBounds 都别发：收起动画若在拖拽刚开始时跑起来，它每帧都会把窗口按回
  // 自己的目标矩形，用户那几下位移全被吃掉（实测拖了 94px，窗口纹丝不动）。
  if (expanded === petIsExpanded && !petDockPending) return;
  // 面板要 380 宽，藏着一半没法看 ⇒ 展开前先滑回贴边。**收起时不再自动藏回去**
  // （用户 2026-09-22 拍的：只在拖到边上那一刻吸，之后不再自动收）。
  stopSlide(); // 展开/收起是用户的动作，赢过任何在跑的滑动
  if (expanded) {
    if (!petIsExpanded) petRectBeforeExpand = petBoundsFor(win.getBounds(), false);
    petUntuck(win, { instant: true }); // 展开时不滑：滑 + 同时长大会糊成一次跳动
    petDockPending = false; // 又在看它了 ⇒ 那次"等收起再吸"作废，以最后一次放手为准
  }
  petIsExpanded = expanded;
  const target = expanded
    ? petBoundsFor(win.getBounds(), true)
    : (petRectBeforeExpand ?? petBoundsFor(win.getBounds(), false));
  if (!expanded) petRectBeforeExpand = null;
  if (!expanded && petDockPending) {
    // 要演"吸进去"那一段，收起这步就别再叠一段动画（两段串起来读起来是卡了一下）。
    petDockPending = false;
    win.setBounds(target);
    settlePetDock(win); // 拖到边上之后是在"面板收起那一瞬"吸进去的（见 `petDragEnded`）
    return;
  }
  slidePetTo(win, target, expanded ? GROW_MS : SHRINK_MS);
}

/**
 * 存盘时该存哪块矩形：**收起态、且一格不藏**那一块。
 *
 * 三条各自会自己走路的问题，都在这一个函数里按掉：
 *  - **尺寸**：`setBounds(200×240)` 之后 `getBounds()` 报回来的是 202×244（DWM 给无边框
 *    透明窗留的那圈不可见边），照它存就每次开机把窗口撑大 4px（实测 243→244→248）。
 *    所以尺寸**永远存常量**：请求什么存什么，往返幂等。
 *  - **横向**：展开态时按"下沿与中心不动"反推收起矩形，而中心是按常量 200 算的、真实宽度
 *    是 202 ⇒ 每存一次盘 x 被推出去 1.5px（实测九次重启从 1345 漂到 1363）。所以只有
 *    **真的摊开着**才反推，收起着就用自己的左上角。
 *  - **藏起来的时候**：屏外坐标一旦落库，拔掉副屏 / 改分辨率之后就是一扇找不回来的窗。
 *    所以存的是贴边那块，"藏"这个意图另存在偏好里（`docked`），每次开机按当前工作区重算。
 */
export function collapsedPetRect(rect: Rect): Rect {
  const origin = petIsExpanded
    ? (petRectBeforeExpand ?? petBoundsFor(rect, false))
    : rect;
  const pinned = { ...origin, width: PET_WIDTH, height: PET_HEIGHT };
  return petDock ? flushRect(petDock.edge, pinned, workRect()) : pinned;
}

/**
 * 当前该是哪块尺寸：**永远按常量算，不拿 `getBounds()` 里的宽高用**。
 *
 * DWM 给无边框透明窗留了一圈不可见边：实测 `setBounds(200×240)` 之后 `getBounds()` 报回来
 * 202×244。把这个值再喂进 `setBounds` 就是"每来一个 pointermove 长两像素" —— 一路拖到屏幕
 * 边能把气泡撑成一条宽带（用户报的现象），而夹取用的 `maxX = 工作区宽 − 窗宽` 也跟着变小，
 * 色片就永远贴不到边（同一个根因的两个症状）。请求什么就用什么，往返才幂等。
 */
function petSize(): { width: number; height: number } {
  return petIsExpanded
    ? { width: PET_PANEL_WIDTH, height: PET_PANEL_HEIGHT }
    : { width: PET_WIDTH, height: PET_HEIGHT };
}

/**
 * 按**增量**挪桌宠（手动拖，见 PetPage 里"为什么不用 CSS drag"那段注释）。
 *
 * 页面只说"往这边走 12px"，摆到哪、能不能出屏全由壳定 —— 与 `setPetExpanded` 同一条分工：
 * 能碰桌面的参数越少越好，绝对坐标就是一种"你把窗放哪"的权力。
 */
export function movePetBy(win: BrowserWindow, dx: number, dy: number): void {
  petUntuck(win, { instant: true }); // 拖着藏着的那一小条走 = 一动手就先把它拉回屏内；拖的时候不滑
  stopSlide(); // 手在拖 ⇒ 任何在跑的动画让开，否则它每帧把窗口按回自己的目标，位移被吃掉
  const current = win.getBounds();
  const size = petSize();
  const { workArea } = screen.getPrimaryDisplay();
  const maxX = workArea.x + Math.max(0, workArea.width - size.width);
  const maxY = workArea.y + Math.max(0, workArea.height - size.height);
  const x = Math.min(Math.max(current.x + Math.round(dx), workArea.x), maxX);
  const y = Math.min(Math.max(current.y + Math.round(dy), workArea.y), maxY);
  win.setBounds({ ...size, x, y });
  // **拖到哪，"展开前那块"就跟到哪。**不记这一步的话：拖的时候面板是开着的（悬停必然带开），
  // 松手后那次收起会把窗口按 `petRectBeforeExpand` 放回去 —— 整段拖拽被抹掉，宠物弹回原地
  // （实测拖 94px 松手后停在原位，于是靠边隐藏的 ≤24px 判据永远够不到，就是用户报的现象）。
  if (petRectBeforeExpand) {
    petRectBeforeExpand = { ...petRectBeforeExpand, x: petRectBeforeExpand.x + (x - current.x), y: petRectBeforeExpand.y + (y - current.y) };
  }
}

/** 把托盘那三项偏好里的**窗口两样**落到窗上：置顶与整窗不透明度。
 *
 * 「显示消息内容」不在这里 —— 它是页面该画什么，不碰窗口，所以走 IPC 通知渲染端
 * （见 index.ts 的 `shell:pet-content-visible`）。
 *
 * **置顶必须用 `"screen-saver"` 档**，这是量出来的（Win11 26200 / Electron 44）：
 *  - 构造参数 `alwaysOnTop: true` → exstyle `0x200000`，**没有** `WS_EX_TOPMOST`；
 *  - 上屏后 `setAlwaysOnTop(true)`（默认 floating 档）→ `0x280000`，同样没有；
 *  - `setAlwaysOnTop(true, "screen-saver")` → `0x280008`，桌宠才真的压在别的窗口上面。
 * 换句话说 D②-2 那句"置顶"在加这个开关之前从来没兑现过（窗口列表里它一直排在最后面），
 * 而"要的是 A、拿到的是 B"这种事只有量窗口样式才能发现 —— 截图看不出来，因为壳自己的
 * 窗口捕获是 occlusion-safe 的，被挡住也照样拍得到。
 *
 * `setOpacity` 与 `transparent` 是乘算关系：我们那 200×240 里透明的那部分本来就是 0，
 * 再乘一个系数仍然是 0 ⇒ 淡出只淡内容，不会把透明区变成一块灰纸。 */
export function applyPetPrefs(win: BrowserWindow, prefs: PetPrefs): void {
  dockEnabled = prefs.dockEnabled; // 吸边判据要在"没有偏好可查"的时刻也知道开关状态
  win.setAlwaysOnTop(prefs.alwaysOnTop, "screen-saver");
  win.setOpacity(prefs.opacity);
}

/** 首次落点：主屏**工作区**右下角（离任务栏与屏幕边各 24px）。
 *  按 workArea 而不是屏幕尺寸：桌宠被任务栏压住半张脸是这类应用最常见的差评。 */
function petOrigin(): { x: number; y: number } {
  const { workArea } = screen.getPrimaryDisplay();
  return {
    x: Math.round(workArea.x + workArea.width - PET_WIDTH - 24),
    y: Math.round(workArea.y + workArea.height - PET_HEIGHT - 24),
  };
}

function webPreferences(): Electron.WebPreferences {
  return {
    preload: PRELOAD,
    // 桌宠页与着陆页都只该拿到 `rolecardShell` 那一个方法，不给它们 nodeIntegration。
    contextIsolation: true,
    nodeIntegration: false,
    sandbox: true,
  };
}

/** @param options.visible 就绪后要不要显示出来。开机自启带起来的那次是 false：
 *  桌宠该在桌面上，但不该弹一扇控制台盖住用户正在做的事。 */
export function createMainWindow(options: { visible?: boolean } = {}): BrowserWindow {
  const visible = options.visible !== false;
  const bounds = initialBounds("main", {
    x: 120,
    y: 80,
    width: 1240,
    height: 860,
  });
  const win = new BrowserWindow({
    ...bounds,
    minWidth: 720,
    minHeight: 480,
    title: "rolecard-agent",
    show: false,
    webPreferences: webPreferences(),
  });
  trackBounds(win, "main");
  win.once("ready-to-show", () => {
    if (visible) win.show();
  });
  void win.loadFile(LANDING);
  return win;
}

/** 桌宠窗：无边框透明、不进任务栏、不可缩放。置顶与不透明度按托盘里那三项偏好（§7.2 第 4 条）。 */
export function createPetWindow(prefs: PetPrefs = loadPetPrefs()): BrowserWindow {
  const bounds = initialBounds("pet", { ...petOrigin(), width: PET_WIDTH, height: PET_HEIGHT });
  const win = new BrowserWindow({
    ...bounds,
    frame: false,
    transparent: true,
    backgroundColor: "#00000000",
    hasShadow: false,
    alwaysOnTop: prefs.alwaysOnTop,
    skipTaskbar: true,
    resizable: false,
    maximizable: false,
    minimizable: false,
    fullscreenable: false,
    show: false,
    title: "rolecard-agent · 桌宠",
    webPreferences: webPreferences(),
  });
  // 记的一直是**收起态**那块矩形：展开是悬停期间的临时形状（见 `collapsedPetRect` 与
  // `trackBounds` 的 `persistAs`）。
  petIsExpanded = false;
  trackBounds(win, "pet", { persistAs: collapsedPetRect });
  // 着陆页的 `<title>` 是"控制台"，不拦一下的话桌宠在窗口列表里也叫那个名字 —— 它会让人
  // 以为桌宠是一扇控制台窗（ Alt+Tab / 截屏工具 / 辅助技术都只看标题）。
  win.on("page-title-updated", (event) => event.preventDefault());
  // 桌宠不该抢你正在打字的焦点：亮出来就行。
  //
  // 偏好**在显示之后**落：构造参数只管初始形状，而 `alwaysOnTop` 那条实测不生效
  // （见 `applyPetPrefs` 里量到的三个 exstyle）。上屏之后一次 `setAlwaysOnTop(true,
  // "screen-saver")` 才是把 `WS_EX_TOPMOST` 真按上去的那一下。
  win.once("ready-to-show", () => {
    win.showInactive();
    applyPetPrefs(win, prefs);
    restorePetDock(win, prefs);
  });
  void win.loadFile(LANDING, { query: { pet: "1" } });
  return win;
}
