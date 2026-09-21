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

import { initialBounds, trackBounds } from "./state";

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

/**
 * 悬停展开 / 收起桌宠窗。页面只说"要不要展开"，**几何全在壳里** —— 只有主进程知道工作区
 * 在哪、色片锚在哪个角，让页面报坐标等于把"窗口能摆到哪"交给一个后端托管的源。
 */
export function setPetExpanded(win: BrowserWindow, expanded: boolean): void {
  win.setBounds(petBoundsFor(win.getBounds(), expanded));
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

/** 桌宠窗：无边框透明置顶、不进任务栏、不可缩放。 */
export function createPetWindow(): BrowserWindow {
  const bounds = initialBounds("pet", { ...petOrigin(), width: PET_WIDTH, height: PET_HEIGHT });
  const win = new BrowserWindow({
    ...bounds,
    frame: false,
    transparent: true,
    backgroundColor: "#00000000",
    hasShadow: false,
    alwaysOnTop: true,
    skipTaskbar: true,
    resizable: false,
    maximizable: false,
    minimizable: false,
    fullscreenable: false,
    show: false,
    title: "rolecard-agent · 桌宠",
    webPreferences: webPreferences(),
  });
  // 记的一直是**收起态**那块矩形：展开是悬停期间的临时形状（见 `petBoundsFor` 与
  // `trackBounds` 的 `persistAs`）。
  trackBounds(win, "pet", { persistAs: (rect) => petBoundsFor(rect, false) });
  // 着陆页的 `<title>` 是"控制台"，不拦一下的话桌宠在窗口列表里也叫那个名字 —— 它会让人
  // 以为桌宠是一扇控制台窗（ Alt+Tab / 截屏工具 / 辅助技术都只看标题）。
  win.on("page-title-updated", (event) => event.preventDefault());
  // 桌宠不该抢你正在打字的焦点：亮出来就行。
  win.once("ready-to-show", () => win.showInactive());
  void win.loadFile(LANDING, { query: { pet: "1" } });
  return win;
}
