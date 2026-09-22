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

/** 桌宠的三项偏好（词汇照 Cherry Studio 的浮动窗设置，设计稿 §7.2 第 4 条）。
 *
 * 为什么单独一个文件而不是塞进 `window-state.json`：那份的形状是"label → 矩形"，被
 * `initialBounds` 直接按 label 取；混进一个非矩形键就要在每个读取点加类型判断，而这两样
 * 东西的演化方向根本不同（位置每拖一次写一次，偏好只在托盘点一下写）。 */
/** 靠边隐藏吸的那几条边。上边故意不做：Windows 顶部有贴边手势与最大化热区。 */
export type DockEdge = "left" | "right" | "bottom";

/** 桌宠偏好的**唯一形状**（托盘是它唯一的写入口，渲染端只能读其中一样）。
 *
 * 为什么单独一个文件而不是塞进 `window-state.json`：那份的形状是"label → 矩形"，被
 * `initialBounds` 直接按 label 取；混进非矩形键就要在每个读取点加类型判断，而这两样
 * 东西的演化方向根本不同（位置每拖一次写一次，偏好只在托盘点一下写）。 */
export type PetPrefs = {
  /** 自动置顶：关掉之后宠物会被别的窗口盖住 —— 有人就这么要求。 */
  alwaysOnTop: boolean;
  /** 0.2–1 的整窗不透明度。**下限不是装饰**：0 等于一扇看不见的置顶窗挡住桌面点击。 */
  opacity: number;
  /** 显示消息内容：关掉后桌宠只给"有 N 条"和输入框，别人站在背后读不到你们聊了什么。 */
  showContent: boolean;
  /** 托盘「靠边隐藏」总开关（默认开：只有"拖到边上"这一个动作会触发它，它不自己动）。 */
  dockEnabled: boolean;
  /** 上次吸在哪条边；null = 没吸。**存的是意图**，藏多深每次按当前工作区重算。 */
  docked: DockEdge | null;
};

export const PET_PREF_DEFAULTS: PetPrefs = {
  alwaysOnTop: true,
  opacity: 1,
  showContent: true,
  dockEnabled: true,
  docked: null,
};

function jsonFile(name: string): string {
  return path.join(app.getPath("userData"), name);
}

function stateFile(): string {
  return jsonFile("window-state.json");
}

function prefsFile(): string {
  return jsonFile("pet-prefs.json");
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

/** 之后自动存：拖拽/缩放结束时写一次（debounce，别把磁盘当心跳）。
 *
 * `persistAs` 是给桌宠留的口子：它会因悬停而临时变大，而**记住的位置必须是收起态那一块**
 * —— 把展开形状存下来，下次开机就是一张 380×520 的透明大窗贴在桌面上（内容还是小宠物，
 * 多出来的部分既看不见又挡住下面的点击）。传进来的永远是窗口当前的真实边界。
 */
export function trackBounds(
  win: BrowserWindow,
  label: string,
  options: { persistAs?: (rect: Rectangle) => Rectangle } = {},
): void {
  let timer: NodeJS.Timeout | null = null;
  const persist = () => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => {
      timer = null;
      const states = load();
      states[label] = options.persistAs ? options.persistAs(win.getBounds()) : win.getBounds();
      save(states);
    }, 400);
  };
  win.on("move", persist);
  win.on("resize", persist);
  win.on("closed", () => {
    if (timer) clearTimeout(timer);
    const states = load();
    const rect = win.getBounds();
    states[label] = options.persistAs ? options.persistAs(rect) : rect;
    save(states);
  });
}

/** 读桌宠偏好：文件读不出来 / 形状不对一律回默认值，坏掉的偏好不该拦住窗口启动。
 *  `opacity` 夹进 [0.2, 1]：这个文件用户可以手改，而"整窗透明到看不见"会留下一扇
 *  点得着却找不着的置顶窗 —— 那是只能重装才能解的坑。 */
export function loadPetPrefs(): PetPrefs {
  let raw: unknown;
  try {
    raw = JSON.parse(readFileSync(prefsFile(), "utf8"));
  } catch {
    return { ...PET_PREF_DEFAULTS };
  }
  const o = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  const flag = (key: "alwaysOnTop" | "showContent" | "dockEnabled") =>
    typeof o[key] === "boolean" ? o[key] : PET_PREF_DEFAULTS[key];
  const opacity = typeof o.opacity === "number" && Number.isFinite(o.opacity) ? o.opacity : 1;
  const docked = o.docked;
  return {
    alwaysOnTop: flag("alwaysOnTop"),
    showContent: flag("showContent"),
    dockEnabled: flag("dockEnabled"),
    opacity: Math.min(1, Math.max(0.2, opacity)),
    // 认不出的边（手改过文件、或上一版还不叫这个名字）一律当"没吸"，不猜。
    docked: docked === "left" || docked === "right" || docked === "bottom" ? docked : null,
  };
}

export function savePetPrefs(prefs: PetPrefs): void {
  try {
    mkdirSync(path.dirname(prefsFile()), { recursive: true });
    writeFileSync(prefsFile(), JSON.stringify(prefs), "utf8");
  } catch (error) {
    console.warn(`[shell] 桌宠偏好没存下：${String(error)}`);
  }
}
