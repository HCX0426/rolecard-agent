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
/**
 * 桌宠窗**唯一**的尺寸：一张常驻画布（2026-09-24，用户第三次报"展开有重影"之后定的）。
 *
 * 之前每次展开都要 `setBounds(200×240 → 380×520)`，而 DWM 在尺寸变化后的头几帧会把
 * **上一帧的内容拉伸到新尺寸** —— 透明窗上那就是"从色片上扩大出来的一团残影"。少发几次
 * `setBounds` 只是少几帧残影（`395f578`、`df71b35` 干的就是这件事），一次也还是有。
 * 所以这次改成：**窗口永远不变尺寸**。画布 = 面板宽 + 左右各 90 的余量，色片照旧能贴屏幕边，
 * 面板在画布里水平挪动来待在最宽的那 380px 屏内区域（`panelShiftFor`）—— 挪的是页面里的
 * 一个 transform，窗口一格都不动。位置变化不会让 DWM 拉伸任何东西，这一类重影于是从根上没了。
 */
export const PET_CANVAS_WIDTH = PET_PANEL_WIDTH + (PET_PANEL_WIDTH - PET_WIDTH); // 560
export const PET_CANVAS_HEIGHT = PET_PANEL_HEIGHT; // 520
/** 色片那块 200×240 永远在画布的**下沿居中**，两侧各挂出去 180、上方留 280 给面板。 */
const CANVAS_SIDE_SLACK = (PET_CANVAS_WIDTH - PET_WIDTH) / 2; // 180
const CANVAS_TOP_SLACK = PET_CANVAS_HEIGHT - PET_HEIGHT; // 280

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
 * 把任意一块矩形折回**锚点框**（色片那块 200×240）：色片在屏幕上不动。
 *
 * 页面里色片是"底部居中"（`items-center justify-end`），所以锚点是**下沿与水平中心**。
 * 反过来若锚在左上角，鼠标一悬停宠物就往下右滑走 —— 用户看到的是"我想点它它跑了"。
 *
 * 夹取夹的是**这块锚点**，不是画布：画布两侧那 180px 余量本来就是留给色片贴边用的，
 * 拿画布去夹会让色片离屏幕边永远有 146px，而靠边隐藏判的是 ≤24px —— 那就永远吸不上边了。
 *
 * 展开与它无关：窗口永远是那张画布，展开只是页面上多画一块面板（`setPetExpanded`）。
 */
export function petAnchorFor(current: Rect): Rect {
  const width = PET_WIDTH;
  const height = PET_HEIGHT;
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

/**
 * 把"面板该往屏内挪多少"告诉页面。收起态一律 0（那时候根本没有面板，色片本来就在画布居中）。
 *
 * 走的是**页面里的一个 transform**：窗口一格都不动。旧写法是反过来 —— 把窗口往屏内推、
 * 再让色片在窗内自挪回原位，于是每次展开都必带一次 `setBounds`，而 DWM 会在那一帧把旧内容
 * 拉伸成新尺寸，那就是用户报了三次的重影。
 */
function sendPanelShift(win: BrowserWindow, shiftX: number): void {
  const next = Math.round(shiftX);
  if (next === lastPanelShift) return;
  lastPanelShift = next;
  if (!win.isDestroyed()) win.webContents.send("shell:pet-panel-shift", next);
}

let lastPanelShift = 0;

/** 桌宠现在是不是摊开的。只有 `setPetExpanded` 会改它，所以它就是"展开态"的那一份真相。
 *  （尺寸与它无关了 —— 它只剩"要不要面板""摊着的时候别自动趴回去"两个作用。） */
let petIsExpanded = false;

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
  const size = anchorSize();
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
  // 滑的这整段路都吃点击：窗在走而手不动 ⇒ 色片会从光标底下滑走，那一刻光标落到透明带上
  // （见 §12.3 那段的"点它它跑了"）。多给的 400ms 是留给手反应的时间。
  holdPetClickable(win, ms + 400);
  const from = petAnchor(win);
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
      placePet(win, target);
      return;
    }
    const k = easeOut(t);
    const lerp = (a: number, b: number) => Math.round(a + (b - a) * k);
    placePet(win, {
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
  const size = anchorSize();
  const right = rect.x + size.width;
  const bottom = rect.y + size.height;
  if (rect.x - work.x <= SNAP_PX) return "left";
  if (work.x + work.width - right <= SNAP_PX) return "right";
  if (work.y + work.height - bottom <= SNAP_PX) return "bottom";
  return null;
}

/**
 * 悬停只做一件事：把趴在边上的那半只拉出来（§7.6 改）。**不再负责弹面板** —— 悬停若改窗口
 * 尺寸，就会把宠物从光标底下挪走，于是"离开→收起→又进入→展开"自激（用户报的"鼠标移过去
 * 桌宠乱晃"）。面板改成点击才弹，这条环就断了。
 */
export function petReveal(win: BrowserWindow): void {
  if (!petDock || !petDock.tucked) return;
  petUntuck(win); // 滑出来，带缓动
}

/** 指针离开桌面件之后：只有"本来就吸着边、且面板没开着"的才自己趴回去。 */
export function petRetuck(win: BrowserWindow): void {
  if (!dockEnabled || !petDock || petDock.tucked || petIsExpanded) return;
  petDock = { ...petDock, tucked: true };
  slidePetTo(win, tuckedRect(petDock.edge, petAnchor(win), workRect()), DOCK_MS);
}

/**
 * 藏着的话滑回贴边。展开面板前、拖之前、关掉「靠边隐藏」时都要先走这一步。
 * `instant` 是给拖拽留的：拖的过程中滑，等于跟用户的手抢那一两百毫秒。
 *
 * **`force` 补的是另一件事**：`tucked` 已经是 false 但滑出的动画还在路上时，光看旗子会以为
 * "已经在屏内了"而什么都不做 —— 于是调用方拿到的是**半路上的坐标**（实测：悬停滑出 280ms，
 * 第 62ms 就点开了面板，基准成了 x=1555 而不是贴边的 1507，色片一开场就被算歪）。
 * force = "别管旗子，把窗按到贴边那块去"。
 */
export function petUntuck(win: BrowserWindow, options: { instant?: boolean; force?: boolean } = {}): void {
  if (!petDock) return;
  if (!petDock.tucked && !options.force) return;
  const wasTucked = petDock.tucked;
  petDock = { ...petDock, tucked: false };
  const target = flushRect(petDock.edge, petAnchor(win), workRect());
  // `force` 蕴含 `instant`：要 force 就是要"现在、一步、到位"。留着动画起去，等于在调用方
  // 已经算完落点之后，还有一个定时器每 16ms 把窗按回收起尺寸（实测：展开成功 34ms 后窗被
  // 按回 202×242，面板照画 ⇒ 用户看到"点一下它变形"）。
  if (options.instant || options.force || wasTucked === false) {
    stopSlide();
    placePet(win, target);
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
  const edge = dockEnabled ? petDockEdgeFor(petAnchorFor(petAnchor(win)), workRect()) : null;
  if (!edge) {
    if (petDock) onDockChanged?.(null);
    petDock = null;
    return null;
  }
  petDock = { edge, tucked: true };
  slidePetTo(win, tuckedRect(edge, petAnchor(win), workRect()), DOCK_MS);
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
  placePet(win, flushRect(prefs.docked, petAnchor(win), workRect())); // 先落到屏内的贴边位
  petDock = { edge: prefs.docked, tucked: true };
  placePet(win, tuckedRect(prefs.docked, petAnchor(win), workRect()));
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
  // 展开/收起时画面在变而手没动（贴边时还要先把色片拉回屏内），照滑动的规矩给宽限期。
  holdPetClickable(win, 500);
  if (expanded === petIsExpanded && !petDockPending) return;
  stopSlide(); // 展开/收起是用户的动作，赢过任何在跑的滑动
  if (expanded) {
    // 藏着一半的时候看不了面板 ⇒ 先滑回屏内。**收起时不再自动藏回去**
    // （用户 2026-09-22 拍的：只在拖到边上那一刻吸，之后不再自动收）。
    //
    // 这一步现在只剩一次**位移**。旧写法是"先按贴边位、再按展开位"两次 setBounds，
    // 实测一次点击的矩形序列 `200×240 → 201×240 → 380×520`，中间那帧 DWM 把旧内容
    // 拉伸了一次 —— 那就是用户三次报告的"从桌宠上扩大出来的重影"。窗口尺寸从此恒定，
    // 拉伸这件事没有载体了。
    petUntuck(win, { force: true });
    petDockPending = false; // 又在看它了 ⇒ 那次"等收起再吸"作废，以最后一次放手为准
  }
  petIsExpanded = expanded;
  // **窗口一格都不动**：展开 = 页面上多画一块面板。色片贴着屏幕边时画布有 180px 在屏外，
  // 面板就在那 380px 屏内区域里对齐（页内 transform，见 `panelShiftFor`）。
  sendPanelShift(win, expanded ? panelShiftFor(petAnchor(win), workRect()) : 0);
  if (!expanded && petDockPending) {
    // 要演"吸进去"那一段，收起这步就别再叠一段动画（两段串起来读起来是卡了一下）。
    petDockPending = false;
    settlePetDock(win); // 拖到边上之后是在"面板收起那一瞬"吸进去的（见 `petDragEnded`）
  }
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
  // 读进来的是**画布**（`trackBounds` 给的就是 `win.getBounds()`），存的必须是收起态那块锚点：
  // 把画布坐标落库，下次开机就是一张 560×524 的透明大窗贴在桌面上（内容还是小宠物，
  // 多出来的部分既看不见又挡住下面的点击）。
  const pinned = { ...anchorOf(rect), width: PET_WIDTH, height: PET_HEIGHT };
  return petDock ? flushRect(petDock.edge, pinned, workRect()) : pinned;
}

/**
 * 壳里所有几何用的都是这块**锚点**尺寸（色片那 200×240），不是画布：贴边位、藏出去多少、
 * 判"离边 ≤24px 算不算吸边"、存进偏好的一块 —— 全部按锚点算，窗口尺寸由 `canvasOf` 单独给。
 *
 * 尺寸**永远按常量算，绝不拿 `getBounds()` 的宽高反喂**：DWM 给无边框透明窗留了一圈不可见边
 * （实测 `setBounds(200×240)` 之后 getBounds 报 202×244），把那个值再喂回 setBounds 就是
 * "每来一次 pointermove 长两像素"，一路拖到屏幕边能把气泡撑成一条宽带（用户报过）。
 */
function anchorSize(): { width: number; height: number } {
  return { width: PET_WIDTH, height: PET_HEIGHT };
}

/**
 * 锚点框（色片那块 200×240）→ 窗口矩形。
 *
 * **壳里所有几何都在锚点上算**：夹取、吸边判定、藏出去多少、存进偏好的那块矩形 —— 全部
 * 一字不改地沿用"窗就是 200×240"那套语义（`trackBounds`、`petDockEdgeFor`、`SIDE_TUCK` 都靠它）。
 * 只有落到窗口的那一步经过这里，唯一的变化就是尺寸永远是画布。
 */
function canvasOf(anchor: Rect): Rect {
  return {
    x: Math.round(anchor.x - CANVAS_SIDE_SLACK),
    y: Math.round(anchor.y + PET_HEIGHT - PET_CANVAS_HEIGHT),
    width: PET_CANVAS_WIDTH,
    height: PET_CANVAS_HEIGHT,
  };
}

/** `canvasOf` 的反向。宽高**取常量**，绝不拿 `getBounds()` 报回来的那对数用（见上面那段 DWM 边）。 */
function anchorOf(rect: Rect): Rect {
  return {
    x: Math.round(rect.x + CANVAS_SIDE_SLACK),
    y: Math.round(rect.y + PET_CANVAS_HEIGHT - PET_HEIGHT),
    width: PET_WIDTH,
    height: PET_HEIGHT,
  };
}

/** 摆窗子的唯一出口：传进来的永远是锚点。 */
function placePet(win: BrowserWindow, anchor: Rect): void {
  win.setBounds(canvasOf(anchor));
}

/** 读窗子的唯一出口：拿回来的永远是锚点（色片那块）。 */
function petAnchor(win: BrowserWindow): Rect {
  return anchorOf(win.getBounds());
}

/**
 * 面板该在画布里水平挪多少像素（0 = 正中，正数往屏幕里挪）。
 *
 * 色片贴到屏幕右边时，画布右侧有 180px 本来就在屏外 —— 面板要是还按画布居中画，那一截就被
 * 切掉了。以前这件事靠"把窗口往屏内推 + 色片在窗内自挪回去"（`shiftX`）解决，代价是一次
 * `setBounds`，而重影就长在那一次上。现在窗口不动，只把面板往屏内最宽的那 380px 上对齐。
 */
function panelShiftFor(anchor: Rect, work: Rect): number {
  const canvas = canvasOf(anchor);
  const left = Math.max(canvas.x, work.x);
  const right = Math.min(canvas.x + canvas.width, work.x + work.width);
  const visibleCenter = (left + right) / 2;
  const raw = Math.round(visibleCenter - (canvas.x + canvas.width / 2));
  const limit = (PET_CANVAS_WIDTH - PET_PANEL_WIDTH) / 2; // 90：再偏就出屏了
  return clamp(raw, -limit, limit);
}

/**
 * 按**增量**挪桌宠（手动拖，见 PetPage 里"为什么不用 CSS drag"那段注释）。
 *
 * 页面只说"往这边走 12px"，摆到哪、能不能出屏全由壳定 —— 与 `setPetExpanded` 同一条分工：
 * 能碰桌面的参数越少越好，绝对坐标就是一种"你把窗放哪"的权力。
 *
 * **锚点是色片，不是窗口左上角**：色片在页面里是"底部居中"的，所以拖动要按 `petAnchorFor`
 * 那条规则走（下沿与水平中心跟着手走，尺寸变化围绕它做）。以前这里直接沿用 `getBounds()`
 * 的左上角 + 增量，于是"面板开着拖第一下"就是一次跳位：那一刻 `petIsExpanded` 已经被页面
 * 发来的 `setPetExpanded(false)` 改成 false、尺寸要缩成 200×240，而左上角还是 380×520 那块的
 * 左上角 —— 色片瞬间往上弹约 280px、往左约 90px（用户报的"长按桌宠它一下子跳到上面"）。
 */
export function movePetBy(win: BrowserWindow, dx: number, dy: number): void {
  // 拖着的时候整窗吃点击：拖拽必然让光标滑出色片那块 88 见方（手快、窗慢），按新规矩一滑出
  // 就把点击漏给桌面 ⇒ 拖到一半突然在拖桌面。松手后 400ms 自动收回（见 §12.3）。
  holdPetClickable(win, 400);
  petUntuck(win, { instant: true }); // 拖着藏着的那一小条走 = 一动手就先把它拉回屏内；拖的时候不滑
  stopSlide(); // 手在拖 ⇒ 任何在跑的动画让开，否则它每帧把窗口按回自己的目标，位移被吃掉
  const current = petAnchor(win);
  const moved: Rect = {
    ...current,
    x: current.x + Math.round(dx),
    y: current.y + Math.round(dy),
  };
  // 尺寸按锚点常量算（`anchorSize()`），不拿 getBounds 的宽高反喂（那是 DWM 报的 562×524）。
  const target = petAnchorFor(moved);
  placePet(win, target);
  // 拖到哪，面板下次展开就在哪 —— 不再有"展开前那块"要记：窗口尺寸不变，
  // 收起要做的只是把面板从页面上拿掉（`setPetExpanded`）。
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

/**
 * 透明带不再挡桌面点击（审计 §12.3）。
 *
 * 200×240 里色片只占中间 88 见方，两侧各 56px、上下还有余量 —— 那些像素是全透明的，
 * 但整扇窗都在吃鼠标，于是用户点下面的桌面图标会点不到（2026-09-22 他就问过这件事）。
 *
 * **为什么是"主进程轮光标 + 页面只报有哪几块像素"**，而不是 Electron 文档那条
 * `setIgnoreMouseEvents(true, { forward: true })` 让页面自己判 hover：真机量出来翻不回来 ——
 * 窗一带上 `WS_EX_TRANSPARENT`，`forward` 声称会送的 mousemove **一条都没到页面**
 * （Electron 44 / Win11 26200：跳回色片 1.6s，样式位不动、壳也没收到任何判定）。也就是
 * "一旦放行就再也没有回来的路"，桌宠会变成一只点不动的东西。主进程问
 * `screen.getCursorScreenPoint()` 不看这扇窗自己的样式位，环因此闭得上：40ms 一次，
 * 只在页面 arm 之后跑。
 *
 * 分工仍守得住：**页面只报"画了东西的块"在哪**（色片 / 气泡 / 面板三个矩形，窗口坐标），
 * 摆窗的权力、工作区、夹取、吸边全在主进程 —— 它报的是"哪里有我看得见的像素"，
 * 不是"把窗放到哪"。
 *
 * 三道保险，缺一道就是一个新 bug：
 *  - **没接线之前一切照旧**：后端没起时那是着陆页、旧界面也不会报矩形；没听到"我来判"之前
 *    绝不改成透明。而且**一个矩形都没报到时也按整窗吃点击** —— 宁可不放行。
 *  - **宽限期**（`petHoldUntil`）：壳自己动窗的那段路整窗吃点击 —— 滑动 280+400ms、展开/收起
 *    500ms、拖拽每来一个增量续 400ms。堵的是 2026-09-22 那个"我点它它跑了"（悬停把窗往里拉
 *    96px，色片从光标底下滑走而光标落到透明带上，见 PetPage 根节点那段注释）。
 *  - **导航即复位**：跳回着陆页 / 重载 ⇒ 没人报矩形了，回到吃点击并停表。
 */
let petHitTestArmed = false;
let petWantsClicks = true;
let petHoldUntil = 0;
let petHoldTimer: NodeJS.Timeout | null = null;
let petMouseAccepting = true;
let petHotRects: Rect[] = [];
let petPoll: NodeJS.Timeout | null = null;

/** 40ms 实测太粗：光标落到色片后 40ms 内按下的那一下仍然被放行（我自己就是这么点空了两次）。
 *  一次 tick 只是 `getCursorScreenPoint` + 至多八个矩形比较，60Hz 的量级不值一提。 */
const PET_POLL_MS = 16;
/** 矩形外扩：DWM 给无边框透明窗留了 1~3px 不可见边（`setBounds(200×240)` 之后 `getBounds`
 *  报 202×244），光标压在边缘上那点差值不该判成"已经离开"。 */
const PET_HOT_PAD = 6;

function applyPetMouse(win: BrowserWindow): void {
  if (win.isDestroyed()) return;
  const accept = !petHitTestArmed || petWantsClicks || Date.now() < petHoldUntil;
  if (accept === petMouseAccepting) return;
  petMouseAccepting = accept;
  win.setIgnoreMouseEvents(!accept, accept ? undefined : { forward: true });
}

/** 光标落在不在"画了东西的块"上。一个矩形都没报到 ⇒ 算落在里面（那是"还没报"，不是"没有东西"）。 */
function petCursorOnPainted(win: BrowserWindow): boolean {
  if (!petHotRects.length) return true;
  const b = win.getBounds();
  const p = screen.getCursorScreenPoint();
  const x = p.x - b.x;
  const y = p.y - b.y;
  return petHotRects.some(
    (r) =>
      x >= r.x - PET_HOT_PAD &&
      y >= r.y - PET_HOT_PAD &&
      x <= r.x + r.width + PET_HOT_PAD &&
      y <= r.y + r.height + PET_HOT_PAD,
  );
}

function petPollTick(win: BrowserWindow): void {
  if (win.isDestroyed()) {
    stopPetPolling();
    return;
  }
  setPetClickable(win, petCursorOnPainted(win));
}

function startPetPolling(win: BrowserWindow): void {
  if (petPoll) return;
  petPoll = setInterval(() => petPollTick(win), PET_POLL_MS);
}

function stopPetPolling(): void {
  if (petPoll) clearInterval(petPoll);
  petPoll = null;
}

/** 页面报来的"画了东西的块"（窗口坐标、CSS 像素）。非数字、越界、超 8 块的一律丢掉。 */
export function setPetHotRects(win: BrowserWindow, rects: unknown): void {
  const next: Rect[] = [];
  for (const item of Array.isArray(rects) ? rects.slice(0, 8) : []) {
    if (!Array.isArray(item) || item.length !== 4) continue;
    const nums = item.map((n) =>
      typeof n === "number" && Number.isFinite(n) ? Math.round(n) : Number.NaN,
    );
    if (nums.some((n) => Number.isNaN(n))) continue;
    const [x, y, width, height] = nums as [number, number, number, number];
    if (width <= 0 || height <= 0 || width > 4000 || height > 4000) continue;
    next.push({ x, y, width, height });
  }
  petHotRects = next;
  if (petHitTestArmed) petPollTick(win); // 布局刚变完就判一次，不等下一个 tick
}

/** 页面挂载/卸载"这页会报像素块"。关掉 = 立刻恢复吃点击并停表。 */
export function setPetHitTestArmed(win: BrowserWindow, armed: boolean): void {
  petHitTestArmed = armed;
  petWantsClicks = true;
  if (armed) startPetPolling(win);
  else {
    petHotRects = [];
    stopPetPolling();
  }
  applyPetMouse(win);
}

/** 落点判定的唯一写入口：轮询与复位路径调它。 */
export function setPetClickable(win: BrowserWindow, clickable: boolean): void {
  petWantsClicks = clickable;
  applyPetMouse(win);
}

/**
 * 短时间内整窗照旧吃点击（拖拽中、滑动中）。每次调用都往后推，所以拖多久保多久；
 * 手停了 `ms` 之后自动松开，不需要页面做任何事。
 */
export function holdPetClickable(win: BrowserWindow, ms: number): void {
  petHoldUntil = Math.max(petHoldUntil, Date.now() + ms);
  applyPetMouse(win);
  if (petHoldTimer) clearTimeout(petHoldTimer);
  petHoldTimer = setTimeout(() => {
    petHoldTimer = null;
    applyPetMouse(win);
  }, ms);
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
  // 偏好里存的一直是**锚点**（改之前也是、现在也是），所以这里读的、夹的都是那块 200×240；
  // 交给 BrowserWindow 的才是画布。老偏好文件因此不需要任何迁移 —— 语义没变，变的只是落窗那一步。
  const anchor = initialBounds("pet", { ...petOrigin(), ...anchorSize() });
  const win = new BrowserWindow({
    ...canvasOf(anchor),
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
  // 记的一直是**锚点**那块 200×240：窗口本身永远是画布，展开只是页面上多画一块面板
  // （见 `collapsedPetRect` 与 `trackBounds` 的 `persistAs`）。
  petIsExpanded = false;
  trackBounds(win, "pet", { persistAs: collapsedPetRect });
  // 着陆页的 `<title>` 是"控制台"，不拦一下的话桌宠在窗口列表里也叫那个名字 —— 它会让人
  // 以为桌宠是一扇控制台窗（ Alt+Tab / 截屏工具 / 辅助技术都只看标题）。
  win.on("page-title-updated", (event) => event.preventDefault());
  // 桌宠页换了（跳回着陆页、重载、后端挂了）⇒ 没人报"光标在不在东西上"了，立刻回到吃点击。
  // 不复位就是"桌宠突然点不动"，而那正是这条优化想修的那类毛病。
  win.webContents.on("did-start-navigation", () => setPetHitTestArmed(win, false));
  win.on("closed", () => {
    if (petHoldTimer) clearTimeout(petHoldTimer);
    petHoldTimer = null;
    stopPetPolling();
    petHitTestArmed = false;
    petHotRects = [];
    petMouseAccepting = true;
    petHoldUntil = 0;
  });
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
