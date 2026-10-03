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
import { isAllowedNavigation } from "./navigation";
import {
  applyPetPrefs,
  createMainWindow,
  createPetWindow,
  movePetBy,
  onPetDockChanged,
  petDragEnded,
  petRetuck,
  petReveal,
  releasePetDock,
  setPetExpanded,
  setPetHitTestArmed,
  setPetHotRects,
} from "./windows";
import { createTray, type TrayHandle } from "./tray";
import { loadPetPrefs, savePetPrefs, type PetPrefs } from "./state";

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
    // 落盘前把换行压成空格（`R102-71`）：`notify 弹出：${title}｜${body}` 那两格来自
    // 页面（角色名可被用户设、消息原文来自模型），都可能含换行 —— 一条消息就能在
    // shell.log 里**伪造出额外的日志行**。压在一行里是这里唯一要的语义：
    // 一行日志 = 一个事件；这个函数是 shell.log 的唯一写入口，收在这里就是全覆盖。
    const oneLine = text.replace(/[\r\n]+/g, " ");
    appendFileSync(shellLogFile, `${new Date().toISOString()} ${oneLine}\n`);
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

/** 桌宠偏好的**唯一真相**（自动置顶 / 透明度 / 显示消息内容 / 靠边隐藏，设计稿 §7.2 第 4 条
 *  与 §7.6）。托盘是它唯一的写入口；渲染端只能读，不能写 —— 页面能改"内容画不画"就等于让
 *  后端托管的那个源自己决定隐私开关，而开关的意义恰恰在于它握在用户手里。 */
let petPrefs: PetPrefs = loadPetPrefs();

const prefsText = (): string =>
  `置顶=${petPrefs.alwaysOnTop}｜不透明度=${petPrefs.opacity}｜显示内容=${petPrefs.showContent}` +
  `｜朗读=${petPrefs.voice}｜靠边隐藏=${petPrefs.dockEnabled}${petPrefs.docked ? `(${petPrefs.docked})` : ""}` +
  `｜面板自动收起=${petPrefs.panelAutoHideMs}ms`;

/** 把"窗表现在到底是什么样"记一行。**为什么值得记**：实测构造参数 `alwaysOnTop: true`
 *  和 `setAlwaysOnTop(true)`（默认 floating 档）都没能让窗真的带上 `WS_EX_TOPMOST`
 *  （量到 `0x280000`，缺 `0x8`），只有 `"screen-saver"` 档兑现了 —— 这种"要的是 A、拿到的
 *  是 B"的偏差光看请求值永远发现不了，打包态又没有终端。 */
function logPetWindow(win: BrowserWindow): void {
  logLine(`桌宠窗实际：置顶=${win.isAlwaysOnTop()}｜不透明度=${win.getOpacity()}`);
}

function updatePetPrefs(patch: Partial<PetPrefs>): void {
  petPrefs = { ...petPrefs, ...patch };
  // 关掉「靠边隐藏」时当场滑回贴边：关了却还藏着，等于这个开关只管下一次、不管这一次。
  if (patch.dockEnabled === false) {
    petPrefs = { ...petPrefs, docked: null };
    if (petWin) releasePetDock(petWin);
  }
  savePetPrefs(petPrefs);
  logLine(`桌宠偏好：${prefsText()}`);
  if (!petWin) return; // 桌宠收起时改的是"下次放出时用哪份偏好"，这里没什么可应用的
  applyPetPrefs(petWin, petPrefs);
  logPetWindow(petWin);
  // 「显示消息内容」是页面该画什么：告诉它一次，之后它自己 pull（见 preload）。
  petWin.webContents.send("shell:pet-content-visible", petPrefs.showContent);
  // 语音旗子同一个形状（页面 pull 首值、push 收变更）。两条**各自独立**发：合成一条
  // 会让"只想改语音"的那次也走一遍内容旗子的处理，语义就不再是一对一了。
  petWin.webContents.send("shell:pet-voice", petPrefs.voice);
  // 面板自动收起的秒数：同一个形状（页面 pull 首值、push 收变更）。**壳不计时** ——
  // "指针在不在面板上 / 在不在输入 / 她说不说话"只有页面知道，判据在 `lib/petPanel.ts`。
  petWin.webContents.send("shell:pet-auto-hide", petPrefs.panelAutoHideMs);
}

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
    petWin = createPetWindow(petPrefs); // 它自己在 ready-to-show 上 showInactive
    wireWindow(petWin);
  } else {
    petWin.showInactive();
  }
}

/** 两扇窗共用的接线：收起而非销毁 + 显隐变化要如实反映到托盘勾选 + 导航护栏（`R102-56`）。 */
function wireWindow(win: BrowserWindow): void {
  win.on("close", (event) => {
    if (quitting) return;
    event.preventDefault();
    win.hide();
  });
  win.on("show", () => tray?.refresh());
  win.on("hide", () => tray?.refresh());
  // 导航护栏（`R102-56`）：两扇窗都只该停在后端托管的本地着陆页（`file://`）。页面渲染的是
  // **AI 生成内容**（角色卡是第三方输入），提示注入塞一个 `<a target="_blank">` 或
  // `window.open` 就能把窗导航到远程内容 —— `sandbox: true` 挡得住代码执行，挡不住钓鱼页
  // 与它对后端的"已登录"二次调用。**合法目标不止着陆页**：探活成功后窗要导航去后端那一格
  // （控制台 `http://127.0.0.1:<端口>/`、桌宠同一 origin 的 `#/pet`）—— 批 13 只放 file://
  // 就把这条主路径拦死了（`R102-73`，第二十包装完控制台停在「正在打开控制台…」）。
  // 判据收在 `navigation.ts::isAllowedNavigation`：file:// 与后端那一个 origin 放行，其余拦，
  // 拦与放都留一行痕（打包态没有终端，logLine 是唯一证据）。
  win.webContents.setWindowOpenHandler(({ url }) => {
    logLine(`导航被拒（新开窗）：${url}`);
    return { action: "deny" };
  });
  win.webContents.on("will-navigate", (event, url) => {
    if (isAllowedNavigation(url, consoleUrl())) {
      // 放行也留一行痕：真机冒烟靠这句判"控制台确实到了后端那一格"。批 13 那版把这条
      // 合法路径当成注入拦掉了（`R102-73`），而拦截有痕、放行无痕 ⇒ 屏幕上只剩一个转圈，
      // 日志里却看不出是护栏自己拦错了。
      logLine(`导航放行（页面跳转）：${url}`);
      return;
    }
    event.preventDefault();
    logLine(`导航被拒（页面跳转）：${url}`);
  });
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
  // 松手那一刻交给壳判边（§7.6）。**零参数**：吸哪条边、藏多深由壳按当前工作区量，而面板
  // 还摊着时它只记账 —— 真吸发生在面板收起那一瞬（见 windows.ts 的 `petDragEnded`）。
  ipcMain.on("shell:pet-drag-end", () => {
    if (petWin) petDragEnded(petWin);
  });
  // 悬停 = 只把趴着的半只拉出来；离开 = 让它自己趴回去（面板开着时不动）。**零参数**。
  ipcMain.on("shell:pet-reveal", () => {
    if (petWin) petReveal(petWin);
  });
  ipcMain.on("shell:pet-retuck", () => {
    if (petWin) petRetuck(petWin);
  });
  // 桌宠的"只在画了像素的地方吃点击"（§12.3）。两条通道：一条开关（这页会不会报），
  // 一条是"哪些矩形有东西"。**坐标与尺寸一概不报进来**的是后者反方向 —— 页面说的只是
  // "我在这儿画了东西"，摆窗的权力全在主进程。非数字 / 越界的矩形在 `setPetHotRects` 里丢掉。
  ipcMain.on("shell:pet-hittest", (_event, value: unknown) => {
    if (petWin && typeof value === "boolean") setPetHitTestArmed(petWin, value);
  });
  ipcMain.on("shell:pet-hot-rects", (_event, value: unknown) => {
    if (petWin) setPetHotRects(petWin, value);
  });
  // 桌宠页问"内容该不该画出来"（托盘「显示消息内容」）。**只有读**：写的那一侧只在托盘，
  // 页面能改它就不是隐私开关了，是后端托管的那个源自己把自己藏起来的手势。
  ipcMain.handle("shell:pet-content-visible", () => petPrefs.showContent);
  // 同一个形状：桌宠页问"朗读开着吗"（托盘「朗读消息」）。读音能不能关**只有用户说了算** ——
  // 页面拿到的是只读值，出声与否的最终裁决仍在页面侧（那还要叠上内容旗子那道闸）。
  ipcMain.handle("shell:pet-voice", () => petPrefs.voice);
  // 同上：面板自动收起的毫秒数，**只有读**。写入口只在托盘（与隐私旗子同一条理由：
  // 页面能改它，就等于把"这块面板在桌面上摊多久"交回给被画的那块东西自己决定）。
  ipcMain.handle("shell:pet-auto-hide", () => petPrefs.panelAutoHideMs);
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

  // 吸边状态一变就落库 + 记一行：判边在主进程（windows.ts），落库在这，两边各管一件事。
  onPetDockChanged((edge) => {
    if (edge === petPrefs.docked) return;
    petPrefs = { ...petPrefs, docked: edge };
    savePetPrefs(petPrefs);
    logLine(`桌宠吸边：${edge ?? "取消"}｜靠边隐藏=${petPrefs.dockEnabled}`);
  });

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
    petPrefs: () => petPrefs,
    setPetPrefs: updatePetPrefs,
    canAutostart: canManageLoginItem,
    autostartEnabled: loginItemEnabled,
    setAutostart: setLoginItemEnabled,
  });

  // 全局热键：唤起/收起控制台。刻意不做"让角色开口"那种触发 —— 但**理由不是鉴权**（原先
  // 这里写的"那是要过 operator 鉴权的动作，而那个鉴权还不存在"两处都不成立，09-26 核对）：
  // operator 分族 09-21 就闭环了（`api/access.py` + `ROLE_OPERATOR`），而热键这条链是从
  // 127.0.0.1 发起的 —— `allowed()` 对**本机来源永远放行**，压根拦不住它；收件箱与
  // `/api/chat` 本身也都是使用者档，没有一条"现在就说"的操作员端点存在。
  // 真正的理由是产品侧的：主动开口由五个由头 + 防重复闸门 + 冷却共同决定（`core/anti_repeat.py`），
  // 一个"现在就说"的键等于绕过那一层，落下来的正是它要防的复读连击；而这一次生成本地要
  // 十几到一百几十秒，按下去时壳里还没有能显示它的窗口。想让她说话，桌宠面板的输入框就是那条路。
  // 注册失败（键被别的程序占了）只说一声，不影响启动。
  const registered = globalShortcut.register(HOTKEY, toggleConsole);
  if (!registered) console.warn(`[shell] 全局热键 ${HOTKEY} 被占用，注册失败（其余功能不受影响）`);

  // 桌宠是"能不能不看我"的开关：默认开，ROLECARD_PET=0 关掉（不为此加设置界面）。
  logLine(`桌宠偏好（本次启动读到）：${prefsText()}`);
  if (process.env.ROLECARD_PET !== "0") setPetVisible(true);
  // 上屏之后再量一次实际状态。听 `ready-to-show` 而不是 `show`：`showInactive()` 是在
  // 那个 handler **里面**同步发出 "show" 的，听 "show" 会量在 `applyPetPrefs` 之前
  // （第一版就是这么错的，日志里写下"实际：置顶=false"而偏好明明是 true）。
  // 注册顺序保证这条跑在 `createPetWindow` 里那条之后 —— 同一个事件，后注册的后触发。
  petWin?.once("ready-to-show", () => petWin && logPetWindow(petWin));
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
