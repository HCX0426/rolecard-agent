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
import { Notification, app, ipcMain, type BrowserWindow } from "electron";

import { Backend, consoleUrl, endpoint, serving, type Outcome } from "./backend";
import { createMainWindow, createPetWindow } from "./windows";
import { createTray, type TrayHandle } from "./tray";

const backend = new Backend();

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

function showConsole(): void {
  if (!mainWin) return; // boot 完成之前（第二个实例抢跑）没有窗可显示
  if (mainWin.isMinimized()) mainWin.restore();
  mainWin.show();
  mainWin.focus();
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
  if (mainWin?.isVisible() && mainWin.isFocused()) return;
  const shot = new Notification({ title, body });
  if (threadId) shot.on("click", () => openSession(threadId));
  shot.show();
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

  mainWin = createMainWindow();
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
  });

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

  app.on("before-quit", () => {
    quitting = true;
    backend.stop();
  });
}
