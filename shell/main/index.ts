/**
 * rolecard-agent 桌面壳（里程碑 D②，Electron）。
 *
 * 壳只做三件事：**窗口**、**本地后端的进程生命周期**、**原生能力桥**。界面仍是后端托管的
 * 那一份 `frontend/dist` —— 不为壳重写 UI，也不在壳里再存一份，否则"C/S 与 B/S 看到的是
 * 不是同一个东西"会变成下一个要修的架构问题。
 *
 * 本文件负责装配与生命周期：起后端 → 开两扇窗 → 托盘 → 原生桥（点气泡/点通知打开会话）
 * → 退出时回收自己 spawn 的那个进程。
 *
 * 关于"关窗口"的语义（D②-3 定）：**收起而不是退出**。这是一只放在桌面上的宠物，关掉控制台
 * 不该让它消失；真要退出走托盘的「退出」，只有那条路会回收后端进程。
 */
import { BrowserWindow, Notification, app, dialog, globalShortcut, ipcMain } from "electron";
import { appendFileSync } from "node:fs";
import path from "node:path";

import { Backend, consoleUrl, endpoint, serving, type BackendOptions, type Outcome } from "./backend";
import { canManageLoginItem, loginItemEnabled, setLoginItemEnabled, startedByLoginItem } from "./autostart";
import { Ollama } from "./ollama";
import { createMainWindow, createPetWindow, movePetBy, setPetExpanded } from "./windows";
import { createTray, type TrayHandle } from "./tray";

/** 后端的启动配置：打包态用随包的 `resources/rolecard-backend/`，开发态由 backend.ts
 *  自己从仓库布局推（那里还管 cwd=仓库根）。日志两种形态都落盘 —— 打包态没有终端，
 *  而"进程归属"这类保证的判据就是日志里那一行。 */
function backendOptions(): BackendOptions {
  const shared = { logFile: path.join(app.getPath("userData"), "backend.log") };
  if (!app.isPackaged) return shared;
  return {
    ...shared,
    sidecarExe: path.join(process.resourcesPath, "rolecard-backend", "rolecard-backend.exe"),
    cwd: app.getPath("userData"),
  };
}

const backend = new Backend(backendOptions());

/**
 * 壳自己的一行流水，落在与 `backend.log` 同一个目录。
 *
 * 为什么值得有：打包态没有终端，而"这条保证到底兑现了没有"的判据往往就是一句日志 ——
 * 后端那侧早就为此把 stdout 落盘了（`[parent-watch] …`），壳这侧却一直只能靠猜。
 * 最典型的是系统通知：页面决定"这条是真的新到"之后调 `notify`，而**控制台在前台时它是
 * 故意不弹的**（红点就在他眼前，再拍一块 toast 只是噪音）。没有这一行，"没看到 toast"
 * 就永远分不清是"按设计抑制了"、"根本没调到"、还是"Windows 把它吞了"。
 */
const shellLogFile = path.join(app.getPath("userData"), "shell.log");

function logLine(text: string): void {
  try {
    appendFileSync(shellLogFile, `${new Date().toISOString()} ${text}\n`);
  } catch {
    /* 日志写不进去绝不该弄坏功能本身（目录被锁、磁盘满都是这里不该管的事） */
  }
}

/** 本地推理服务的进程（D③-b）。**退出时不跟着壳走**：Ollama 是共用的服务，壳只是替用户
 *  把它拉起来过一次，不该在关掉自己的窗口时把别人正在用的推理也断了。 */
const ollama = new Ollama();

let mainWin: BrowserWindow | null = null;
let petWin: BrowserWindow | null = null;
let tray: TrayHandle | null = null;
let quitting = false;

/** 桌宠/通知要求打开某个会话时，主窗的文档可能还在加载（着陆页→后端地址那次跳转）。
 *  攒一条就够：用户点的是"最新那一条"，第二条会覆盖它，也符合点完之后的预期。 */
let pendingThread: string | null = null;
let consoleReady = false;

// 打包态的 Windows 通知要 AppUserModelID 才不被系统丢弃；那个 ID 必须由安装包注册。
// dev 态故意不设：设一个没注册过的 ID 会让 toast 静默不显示，把"通知有没有写对"变成
// "为什么什么都没弹"。dev 用 Electron 默认的 ID，能正常弹。
if (app.isPackaged) app.setAppUserModelId("com.rolecardagent.desktop");

function report(outcome: Outcome): void {
  switch (outcome.kind) {
    case "already-serving":
      console.log(`[shell] ${endpoint().host}:${endpoint().port} 上已有后端（不是本壳起的）→ 直接连它，退出时不动它`);
      break;
    case "spawned":
      console.log(`[shell] 已拉起本地后端（pid ${outcome.pid}），退出时回收`);
      break;
    case "failed":
      // 不静默：着陆页会显示"没能启动"，但原因只有这里说得清。
      console.error(`[shell] 拉不起本地后端：${outcome.reason}`);
      break;
  }
}

/** 会话 id 是个不透明字符串（后端生成的 `s_…` / uuid，壳不认识它的格式）。
 *  这里只挡住"会让 toast 或窗口管理出洋相"的输入：空、超长、含空白与控制符。 */
function threadIdOf(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const id = value.trim();
  if (!id || id.length > 120 || /[\s\u0000-\u001f]/.test(id)) return null;
  return id;
}

function textOf(value: unknown, max: number): string {
  return typeof value === "string" ? value.trim().slice(0, max) : "";
}

/** 全局热键：唤起/收起控制台。选组合而不是单键，是因为它要能在任何应用头上抢下来而不撞车。 */
const HOTKEY = "CommandOrControl+Alt+R";

function showConsole(): void {
  if (!mainWin) return; // boot 完成之前（第二个实例抢跑）没有窗可显示
  if (mainWin.isMinimized()) mainWin.restore();
  mainWin.show();
  mainWin.focus();
}

/** 热键的动作：看不见就唤出来，正开着（在前台）就收回去。 */
function toggleConsole(): void {
  if (mainWin?.isVisible() && mainWin.isFocused()) mainWin.hide();
  else showConsole();
}

function tellConsole(threadId: string): void {
  if (!mainWin) return;
  if (!consoleReady) {
    pendingThread = threadId;
    return;
  }
  mainWin.webContents.send("shell:open-thread", threadId);
}

/** 把某条主动消息对应的那个会话摊到用户面前（桌宠气泡、系统通知共用这一条路）。 */
function openSession(threadId: string): void {
  showConsole();
  tellConsole(threadId);
}

function notify(title: string, body: string, threadId: string | null): void {
  // 用户正盯着控制台：铃铛红点就在他自己眼前跳，再拍一块系统 toast 只是噪音。
  if (mainWin?.isVisible() && mainWin.isFocused()) {
    logLine(`notify 抑制（控制台在前台）：${title}`);
    return;
  }
  const shot = new Notification({ title, body });
  if (threadId) shot.on("click", () => openSession(threadId));
  shot.show();
  logLine(`notify 弹出：${title}｜${body}｜thread=${threadId ?? "-"}`);
}

/** 托盘那个勾选框能做到的全部：让宠物在桌面上，或者不在。
 *  没有宠物窗时（`ROLECARD_PET=0` 启动）"打开"就是当场创建一扇 —— 勾选框不能是假的。 */
function setPetVisible(visible: boolean): void {
  if (!visible) {
    petWin?.hide();
    return;
  }
  if (!petWin) {
    petWin = createPetWindow(); // 它自己在 ready-to-show 上 showInactive
    wireWindow(petWin);
  } else {
    petWin.showInactive();
  }
}

/** 两扇窗共用的接线：收起而非销毁 + 显隐变化要如实反映到托盘勾选。 */
function wireWindow(win: BrowserWindow): void {
  win.on("close", (event) => {
    if (quitting) return;
    event.preventDefault();
    win.hide();
  });
  win.on("show", () => tray?.refresh());
  win.on("hide", () => tray?.refresh());
}

function boot(): void {
  ipcMain.handle("shell:backend-url", () => consoleUrl());
  ipcMain.handle("shell:backend-reachable", () => serving());
  ipcMain.handle("shell:open-session", (_event, value: unknown) => {
    const id = threadIdOf(value);
    if (id) openSession(id);
  });
  ipcMain.handle("shell:notify", (_event, title: unknown, body: unknown, value: unknown) => {
    notify(textOf(title, 80), textOf(body, 200), threadIdOf(value));
  });
  // 桌宠悬停展开 / 收起（设计稿 §7.2）。参数只有一个布尔：**几何不让页面报**——
  // 工作区在哪、色片锚在哪个角只有主进程知道，交给后端托管的那个源就等于把"窗口能摆到哪"
  // 交了出去。非布尔一律不动窗（跟 `threadIdOf` / `textOf` 同一套"输入不可信"的写法）。
  ipcMain.handle("shell:pet-expanded", (_event, value: unknown) => {
    if (typeof value !== "boolean" || !petWin) return false;
    setPetExpanded(petWin, value);
    return true;
  });
  // 拖动桌宠：页面只报**增量**（`[dx, dy]`），摆哪、能不能出屏由壳定（`movePetBy`）。
  // 为什么不用 CSS 的 `-webkit-app-region: drag`：见 PetPage 里那段注释 —— 实测拖拽区
  // 会把鼠标事件整个吞掉，悬停展开就没法工作（那篇悬浮球实现说的"drag 与点击冲突"是同一件事）。
  ipcMain.on("shell:pet-drag", (_event, value: unknown) => {
    if (!petWin || !Array.isArray(value) || value.length !== 2) return;
    const [dx, dy] = value;
    if (typeof dx !== "number" || typeof dy !== "number" || !Number.isFinite(dx) || !Number.isFinite(dy)) return;
    movePetBy(petWin, dx, dy);
  });
  // 本地推理服务的进程（D③-b）。**三个方法都不收参数**：起停一个本机进程能碰到的东西比
  // 打开一个会话多得多，参数一旦是路径/命令，桥就成了任意执行入口。
  ipcMain.handle("shell:ollama-owner", () => ollama.owner());
  ipcMain.handle("shell:ollama-start", () => ollama.start());
  ipcMain.handle("shell:ollama-stop", () => ollama.stop());
  // 原生目录选择器（D②-6）。**零参数 + 路径由用户在对话框里选**：页面拿不到"指定任意路径"
  // 的能力，所以这条不构成新的口子。取消返回 null，由界面决定什么都不做。
  ipcMain.handle("shell:pick-directory", async (event) => {
    const owner = BrowserWindow.fromWebContents(event.sender) ?? mainWin ?? undefined;
    const result = owner
      ? await dialog.showOpenDialog(owner, { properties: ["openDirectory", "createDirectory"] })
      : await dialog.showOpenDialog({ properties: ["openDirectory", "createDirectory"] });
    return result.canceled ? null : (result.filePaths[0] ?? null);
  });
  // 渲染端挂了监听才敢投递"要打开的会话"（早于监听 send 就是丢消息）。
  ipcMain.handle("shell:renderer-ready", (event) => {
    if (!mainWin || event.sender.id !== mainWin.webContents.id) return;
    consoleReady = true;
    if (pendingThread) {
      mainWin.webContents.send("shell:open-thread", pendingThread);
      pendingThread = null;
    }
  });

  void backend.ensure().then(report);

  // 开机自启带起来的那次启动：只放桌宠，不弹一扇控制台盖住用户刚打开的工作。
  mainWin = createMainWindow({ visible: !startedByLoginItem() });
  wireWindow(mainWin);
  // 真·跨文档跳转（着陆页 → 后端地址，以及后端地址换新地址）期间监听者不存在；
  // 页内 hash 变化（isInPlace）不能清，清了会把正常深链挤掉一次。
  mainWin.webContents.on("did-start-navigation", (_event, _url, isInPlace, isMainFrame) => {
    if (isMainFrame && !isInPlace) consoleReady = false;
  });

  tray = createTray({
    showConsole,
    petVisible: () => Boolean(petWin && petWin.isVisible()),
    setPetVisible,
    canAutostart: canManageLoginItem,
    autostartEnabled: loginItemEnabled,
    setAutostart: setLoginItemEnabled,
  });

  // 全局热键：唤起/收起控制台。刻意不做"让角色开口"那种触发 —— 那是要过 operator 鉴权的
  // 动作，而那个鉴权还不存在（P0-3）。注册失败（键被别的程序占了）只说一声，不影响启动。
  const registered = globalShortcut.register(HOTKEY, toggleConsole);
  if (!registered) console.warn(`[shell] 全局热键 ${HOTKEY} 被占用，注册失败（其余功能不受影响）`);

  // 桌宠是"能不能不看我"的开关：默认开，ROLECARD_PET=0 关掉（不为此加设置界面）。
  if (process.env.ROLECARD_PET !== "0") setPetVisible(true);
}

// 单实例：第二个实例直接退出（它没有自己的后端可管，留着只会两个壳抢同一个端口）。
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => showConsole());

  void app.whenReady().then(boot);

  app.on("window-all-closed", () => {
    // 关窗口是"收起"，所以这条只在真正退出之后到达；留着当兜底，别让壳变成没有窗也没有
    // 退出入口的僵尸。托盘的「退出」才是用户那侧唯一的退出入口。
    app.quit();
  });

  app.on("will-quit", () => globalShortcut.unregisterAll());

  app.on("before-quit", () => {
    quitting = true;
    backend.stop();
  });
}
