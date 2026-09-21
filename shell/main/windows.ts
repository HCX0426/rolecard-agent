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

import { initialBounds, loadPetPrefs, trackBounds, type PetPrefs } from "./state";

export const PET_WIDTH = 200;
export const PET_HEIGHT = 240;

/** 悬停展开后的面板尺寸（设计稿 §7.2）。宽度够读一句话，高度够放最近几条 + 输入框。 */
export const PET_PANEL_WIDTH = 380;
export const PET_PANEL_HEIGHT = 520;

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

/**
 * 悬停展开 / 收起桌宠窗。页面只说"要不要展开"，**几何全在壳里** —— 只有主进程知道工作区
 * 在哪、色片锚在哪个角，让页面报坐标等于把"窗口能摆到哪"交给一个后端托管的源。
 */
export function setPetExpanded(win: BrowserWindow, expanded: boolean): void {
  petIsExpanded = expanded;
  win.setBounds(petBoundsFor(win.getBounds(), expanded));
}

/**
 * 存盘时该存哪块矩形：**收起态那一块**（否则下次开机是一张 380×520 的透明大窗贴在桌面上）。
 *
 * 两条各自会自己走路的漂移，都在这一个函数里按掉：
 *  - **尺寸**：`setBounds(200×240)` 之后 `getBounds()` 报回来的是 202×244（DWM 给无边框
 *    透明窗留的那圈不可见边），照它存就每次开机把窗口撑大 4px（实测 243→244→248）。
 *    所以尺寸**永远存常量**：请求什么存什么，往返幂等。
 *  - **横向**：展开态时按"下沿与中心不动"反推收起矩形，而中心是按常量 200 算的、真实宽度
 *    是 202 ⇒ 每存一次盘 x 被推出去 1.5px（实测九次重启从 1345 漂到 1363）。所以只有
 *    **真的摊开着**才反推，收起着就用自己的左上角。
 */
export function collapsedPetRect(rect: Rect): Rect {
  const origin = petIsExpanded ? petBoundsFor(rect, false) : rect;
  return { ...origin, width: PET_WIDTH, height: PET_HEIGHT };
}

/**
 * 按**增量**挪桌宠（手动拖，见 PetPage 里"为什么不用 CSS drag"那段注释）。
 *
 * 页面只说"往这边走 12px"，摆到哪、能不能出屏全由壳定 —— 与 `setPetExpanded` 同一条分工：
 * 能碰桌面的参数越少越好，绝对坐标就是一种"你把窗放哪"的权力。
 */
export function movePetBy(win: BrowserWindow, dx: number, dy: number): void {
  const current = win.getBounds();
  const { workArea } = screen.getPrimaryDisplay();
  const maxX = workArea.x + Math.max(0, workArea.width - current.width);
  const maxY = workArea.y + Math.max(0, workArea.height - current.height);
  win.setBounds({
    ...current,
    x: Math.min(Math.max(current.x + Math.round(dx), workArea.x), maxX),
    y: Math.min(Math.max(current.y + Math.round(dy), workArea.y), maxY),
  });
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
  });
  void win.loadFile(LANDING, { query: { pet: "1" } });
  return win;
}
