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

export const PET_WIDTH = 200;
export const PET_HEIGHT = 240;

const PRELOAD = path.join(__dirname, "..", "preload", "bridge.js");
const LANDING = path.join(__dirname, "..", "..", "web", "index.html");

/** 落点：主屏**工作区**右下角（离任务栏与屏幕边各 24px）。
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

export function createMainWindow(): BrowserWindow {
  const win = new BrowserWindow({
    width: 1240,
    height: 860,
    minWidth: 720,
    minHeight: 480,
    title: "rolecard-agent",
    show: false,
    webPreferences: webPreferences(),
  });
  win.once("ready-to-show", () => win.show());
  void win.loadFile(LANDING);
  return win;
}

/** 桌宠窗：无边框透明置顶、不进任务栏、不可缩放。 */
export function createPetWindow(): BrowserWindow {
  const win = new BrowserWindow({
    ...petOrigin(),
    width: PET_WIDTH,
    height: PET_HEIGHT,
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
  // 桌宠不该抢你正在打字的焦点：亮出来就行。
  win.once("ready-to-show", () => win.showInactive());
  void win.loadFile(LANDING, { query: { pet: "1" } });
  return win;
}
