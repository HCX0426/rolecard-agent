/**
 * 窗口位置的持久化（桌宠"记住我放哪了"）。
 *
 * 一个坑必须处理：显示器变了（拔掉副屏、换了分辨率）以后，存下来的坐标可能落在**任何
 * 工作区之外** —— 那扇门还在，只是用户再也找不回来，只能重装。所以还原时一律先夹回
 * 当前主屏工作区。
 *
 * 拆成"读"和"跟"两个函数而不是一个 `attach(win, fallback)`：位置必须在 `new BrowserWindow`
 * **之前**拿到，否则构造后再 setBounds 会先闪一次错误位置、还顺手触发一次 move 事件把
 * 那个错误位置存回去。
 */
import { app, screen, type BrowserWindow, type Rectangle } from "electron";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";

type Saved = Record<string, Rectangle>;

function stateFile(): string {
  return path.join(app.getPath("userData"), "window-state.json");
}

function load(): Saved {
  try {
    const parsed = JSON.parse(readFileSync(stateFile(), "utf8")) as unknown;
    return parsed && typeof parsed === "object" ? (parsed as Saved) : {};
  } catch {
    return {}; // 首次运行 / 文件被手改坏：没有位置可言，用默认落点
  }
}

function save(states: Saved): void {
  try {
    mkdirSync(path.dirname(stateFile()), { recursive: true });
    writeFileSync(stateFile(), JSON.stringify(states), "utf8");
  } catch (error) {
    // 位置记不住不是故障，不值得弹窗打断用户；说一声就够。
    console.warn(`[shell] 窗口位置没存下：${String(error)}`);
  }
}

/** 夹进主屏工作区（保持尺寸不变，只挪位置）。 */
function clampToWorkArea(rect: Rectangle): Rectangle {
  const { workArea } = screen.getPrimaryDisplay();
  const maxX = workArea.x + Math.max(0, workArea.width - rect.width);
  const maxY = workArea.y + Math.max(0, workArea.height - rect.height);
  return {
    ...rect,
    x: Math.min(Math.max(rect.x, workArea.x), maxX),
    y: Math.min(Math.max(rect.y, workArea.y), maxY),
  };
}

/** 这扇窗该开在哪：上次的位置，没有就落到 `fallback`；两种都夹回当前工作区。 */
export function initialBounds(label: string, fallback: Rectangle): Rectangle {
  const stored = load()[label];
  return clampToWorkArea(stored ? { ...fallback, ...stored } : fallback);
}

/** 之后自动存：拖拽/缩放结束时写一次（debounce，别把磁盘当心跳）。 */
export function trackBounds(win: BrowserWindow, label: string): void {
  let timer: NodeJS.Timeout | null = null;
  const persist = () => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => {
      timer = null;
      const states = load();
      states[label] = win.getBounds();
      save(states);
    }, 400);
  };
  win.on("move", persist);
  win.on("resize", persist);
  win.on("closed", () => {
    if (timer) clearTimeout(timer);
    const states = load();
    states[label] = win.getBounds();
    save(states);
  });
}
