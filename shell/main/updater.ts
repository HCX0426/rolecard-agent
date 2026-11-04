/**
 * 自动更新（设计稿《更新机制与OCR随包》§3.3）：electron-updater 接 GitHub Releases，
 * 但守三条红线，所以它**不是**开箱那套 autoDownload/autoInstall：
 *
 *   1. **用户确认制**：发现新版只提示；下载要用户点「下载」，装要用户点「重启并更新」。
 *      本项目有真用户数据（SQLite + chroma + 记忆），静默后台升级的风险高于收益。
 *   2. **升级前备份数据根**：备份失败就不装 —— 没有回滚路径的自动迁移是本仓反复的教训形状。
 *      备份在 `stopBackend()` **之后**做：库安静下来时整目录拷贝就是一致快照
 *      （`backup_data_root.py` 用 sqlite3.backup() 是因为它对着**活库**；这里后端已停，
 *      连 wal/shm 与 chroma、uploads 一起 cpSync 反而更完整）。
 *   3. **后端干净退出**：NSIS 覆盖安装时 sidecar 若还活着 = 文件占用 = 装到一半失败。
 *      所以先 `stopBackend()`（taskkill 进程树），再**等端口真的空出来**才进安装器。
 *
 * 编号是设计稿的三条红线，**执行顺序是 3 → 2**（2026-11-04 才掰过来）：从前 `doInstall`
 * 真的是"先备份、后停库"，与第 2 条自己写的依据直接相反 —— WAL 开着时对**活库** cpSync，
 * `app.db` 与 `-wal`/`-shm` 不在同一时刻，那份回滚快照本身可能撕裂，而红线 2 保的正是它。
 * 注释当时的因果是对的、代码是错的，所以修的是代码。挪序带来的新分支一起补掉：中止路
 * 必然落在"后端已停"之后 ⇒ 手点那一档要把后端拉回来（`abortInstall` + 判据
 * `updateFeedback.abortFollowUp`），否则修一个撕裂换来一个"应用活着但连不上后端"。
 *
 * 顺带一条：`stopBackend()` 调完不代表库已安静（`taskkill` 是 fire-and-forget），
 * "安静"的真实判据是**端口确认空出来**，所以快照排在 `waitForPortFree` 之后，不是紧跟着停库。
 *
 * dev 态（未打包）整条不装配：electron-updater 在没有 app-update.yml 的环境里只会抛错，
 * 而"看得见但永远无效"的托盘条目比没有更糟（与开机自启同一条理由，`canManageLoginItem`）。
 */
import { app, dialog } from "electron";
import { autoUpdater, type ProgressInfo, type UpdateInfo } from "electron-updater";
import {
  cpSync,
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import net from "node:net";
import path from "node:path";

import { cleanupConsumedPendingUpdate, snapshotsToEvict, type CleanupFs } from "./updateCacheCleanup";
import {
  abortFollowUp,
  abortFollowUpNotice,
  advanceStageOnDownload,
  failureOf,
  failureTitle,
  isCheckTerminal,
  shouldHandoffQuitToInstall,
  type ManualAction,
} from "./updateFeedback";

/**
 * 自动更新的三档开关（用户 2026-10-14 要的"自动更新开关 + 启动时检测更新"）。
 *
 * 为什么是**三档而不是一个总开关**：这三件事的打扰程度差着量级 —— 检查只是发一个只读
 * 请求（不惊动人）；下载要占带宽与磁盘（一整个 ~281 MB 的包）；装会**杀掉正在用的应用**。
 * 一个开关管三件事，等于"我允许它开机查一下"顺手买下了"它会自己重启我的程序"。
 * 所以逐级放开，每一级都要用户自己点：
 *
 *   - `checkOnStartup`（默认**开**）：开机静默查一次。只读、不打扰，有新版就亮托盘状态行。
 *   - `autoDownload`（默认**关**）：查到新版就自己下载。
 *   - `autoInstallOnQuit`（默认**关**）：已下载好的更新在**应用退出时**自动装上。
 *     刻意不是"下完立刻装" —— 那会在用户正干活时把应用重启掉。
 *
 * 三条红线里只有"用户确认"这一条被开关接管（打开开关本身就是确认）；
 * **备份与后端干净退出不受开关影响** —— 见 `install()` 与 `onQuitAttempt()`：
 * 自动装走的是同一个 `install()`，那两条无条件执行。它们是安全，不是口味。
 *
 * `channel`（P10，默认 **stable**）：更新通道。beta = 桌面版的灰度（预发布 feed，
 * electron-updater 只在 allowPrerelease 时才读得到）；stable 用户绝不该被推到 beta。
 */
export type UpdaterChannel = "stable" | "beta";

export type UpdaterPrefs = {
  checkOnStartup: boolean;
  autoDownload: boolean;
  autoInstallOnQuit: boolean;
  channel: UpdaterChannel;
};

export const UPDATER_PREF_DEFAULTS: UpdaterPrefs = {
  checkOnStartup: true,
  autoDownload: false,
  autoInstallOnQuit: false,
  channel: "stable",
};

function updaterPrefsFile(): string {
  return path.join(app.getPath("userData"), "updater-prefs.json");
}

/** 读不出 / 形状不对一律回默认（与桌宠偏好同一条纪律：坏文件不该拦住启动）。 */
export function loadUpdaterPrefs(): UpdaterPrefs {
  let raw: unknown;
  try {
    raw = JSON.parse(readFileSync(updaterPrefsFile(), "utf8"));
  } catch {
    return { ...UPDATER_PREF_DEFAULTS };
  }
  const o = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  // 布尔键取子集（channel 是枚举，不进 flag 的 boolean 收窄）。
  const flag = (key: "checkOnStartup" | "autoDownload" | "autoInstallOnQuit") =>
    typeof o[key] === "boolean" ? (o[key] as boolean) : UPDATER_PREF_DEFAULTS[key];
  // channel 是白名单枚举：认不出的值一律回 stable —— 坏文件不许把用户拐进灰度通道。
  const channel = o.channel === "beta" ? "beta" : "stable";
  return {
    checkOnStartup: flag("checkOnStartup"),
    autoDownload: flag("autoDownload"),
    autoInstallOnQuit: flag("autoInstallOnQuit"),
    channel,
  };
}

function saveUpdaterPrefs(prefs: UpdaterPrefs): void {
  try {
    mkdirSync(path.dirname(updaterPrefsFile()), { recursive: true });
    writeFileSync(updaterPrefsFile(), JSON.stringify(prefs), "utf8");
  } catch (error) {
    // 存不下只是下次回到默认，不该打断用户（与窗口位置/桌宠偏好同一句理由）。
    console.warn(`[shell] 更新偏好没存下：${String(error)}`);
  }
}

export type UpdaterState =
  | { kind: "idle" }
  | { kind: "checking" }
  | { kind: "available"; version: string }
  | { kind: "downloading"; percent: number }
  | { kind: "ready"; version: string }
  | { kind: "uptodate" }
  | { kind: "error"; message: string };

export type UpdaterOptions = {
  /** 与 shell.log 同一个写入口（打包态唯一可查的日志）。 */
  log: (text: string) => void;
  /** 回收本壳 spawn 的后端（只动自己的 —— backend.ts 顶部那条规矩）。 */
  stopBackend: () => void;
  /** 后端端口（等它空出来才许安装器动文件）。 */
  backendPort: () => number;
  /**
   * 把刚被 `stopBackend()` 停掉的后端拉回来。**只在中止路上用**（备份抛错 / 端口没空出来），
   * 且只在**不是退出触发**那一档用（判据 `updateFeedback.abortFollowUp`）—— 退出触发的中止
   * 直接交回退出、不必再拉。实现走 `backend.ensure()`（它现在会复位 `quitting`，拉回的后端
   * 不裸奔，见 backend.ts）。没有这一口，手动「重启并更新」一旦在停库之后中止，就会留下
   * 「后端死了、应用照旧活着」的新空档 —— 那正是把备份挪到停库之后**新引入**的风险，
   * 必须一起补掉，否则修一个撕裂换来一个掉线。
   */
  restartBackend: () => void;
  /**
   * 系统通知（手动检查的回执走它）。**绕过**"控制台在前台就抑制"那条规则 ——
   * 那条防的是主动开口的噪音；更新回执是用户自己点出来的，他明确要一个答案。
   */
  notify: (title: string, body: string) => void;
};

/** 数据根的备份保留份数：只留最近 N 份，装十次不会攒出十份几百 MB 的快照。 */
const BACKUP_KEEP = 2;

/**
 * 快照目录的**总体积**预算（R1031-29，2026-10-31）：份数闸拦不住"单份 8 GB"的数据根
 * —— 2 份就是 16 GB 静默吃盘。2 GiB 的取值依据：本产品是本地陪聊（文本 + 少量上传图片），
 * 本机实测数据根 8 MB、重度文档工作区按 10 GB 天花板算，2 GiB 覆盖"日常重度用户的一半
 * 快照"，同时给系统盘留出比"一整份快照"更硬的底线。**最新一份永远不删**（回滚路径），
 * 所以这份预算的失败形状是"超预算多留一晚"，不是"无路可退"。
 */
const BACKUP_MAX_BYTES = 2 * 1024 * 1024 * 1024;

const sleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

/** 端口还能连上 = 后端还没死透。连不上（ECONNREFUSED/超时）才算空。 */
function portBusy(port: number): Promise<boolean> {
  return new Promise((resolve) => {
    const socket = net.createConnection({ host: "127.0.0.1", port });
    const done = (value: boolean): void => {
      socket.destroy();
      resolve(value);
    };
    socket.setTimeout(300);
    socket.once("connect", () => done(true));
    socket.once("timeout", () => done(false));
    socket.once("error", () => done(false));
  });
}

async function waitForPortFree(port: number, timeoutMs: number): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    if (!(await portBusy(port))) return true;
    if (Date.now() >= deadline) return false;
    await sleep(250);
  }
}

function isDirectory(candidate: string): boolean {
  try {
    return statSync(candidate).isDirectory();
  } catch {
    return false;
  }
}

/** 装机形态的数据根（`base/paths.py::_platform_data_root` 的打包分支同一个位置）。 */
export function installedDataRoot(localAppData: string | undefined): string | null {
  if (!localAppData) return null;
  const root = path.join(localAppData, "rolecard-agent");
  return isDirectory(root) ? root : null;
}

function backupRootDir(localAppData: string): string {
  return path.join(localAppData, "rolecard-agent-update-backups");
}

/**
 * 整目录快照。返回备份路径；数据根不存在（全新机器没数据可备份）返回 null 但**不算失败**。
 * 名字里的时间戳是 ISO 段（字典序 = 时间序），保留份数按它裁。
 * 裁两刀（R1031-29）：份数超 BACKUP_KEEP 先走，剩下的一份份量字节、超 BACKUP_MAX_BYTES
 * 预算继续从最旧删 —— 但**刚写的这份永远不删**（它就是这次升级的回滚路径本身）。
 * 量字节复用下面清理读数那台 `dirBytes`（同一个事实只住一个地址）。
 */
export function snapshotDataRoot(
  localAppData: string | undefined,
  log: (text: string) => void,
): string | null {
  const root = installedDataRoot(localAppData);
  if (!root || !localAppData) return null;
  const stamp = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
  const dest = path.join(backupRootDir(localAppData), `backup-${stamp}`);
  mkdirSync(dest, { recursive: true });
  cpSync(root, dest, { recursive: true });
  const dir = backupRootDir(localAppData);
  const all = readdirSync(dir).filter((name) => name.startsWith("backup-")).sort();
  const sizeOf = (name: string): number => dirBytes(path.join(dir, name));
  const doomed = snapshotsToEvict(all, sizeOf, BACKUP_MAX_BYTES, BACKUP_KEEP);
  for (const name of doomed) {
    rmSync(path.join(dir, name), { recursive: true, force: true });
    log(`升级快照已裁：${name}（份数/体积双上限，最新一份永远保留）`);
  }
  log(`数据根已快照：${dest}`);
  return dest;
}

export type Updater = {
  state: () => UpdaterState;
  onStateChange: (listener: (state: UpdaterState) => void) => void;
  prefs: () => UpdaterPrefs;
  setPrefs: (patch: Partial<UpdaterPrefs>) => void;
  check: (source?: "startup" | "manual") => void;
  download: () => void;
  install: () => Promise<void>;
  /**
   * 应用正在退出时给自动装留的一手（`autoInstallOnQuit` 开着且已下载好）。
   * **刻意不用 electron-updater 自带的 `autoInstallOnAppQuit`** —— 那个标志会让 updater
   * 自己在 quit 时直接 `quitAndInstall`，**绕过红线 2 与 3**（不备份、不等后端退干净），
   * 症状是"某次退出后应用没装好/装坏了没有回滚"。所以这里走同一个 `install()`：
   * 开关只接管"要不要问用户"，那两条无条件执行。
   * 返回 true = 这一轮退出被更新接管（调用方不该继续 quit）。
   */
  onQuitAttempt: () => boolean;
};

/** 递归量目录字节（清理读数的"释放了多少"；单文件直接量）。 */
function dirBytes(target: string): number {
  try {
    const stat = statSync(target);
    if (stat.isFile()) return stat.size;
    let total = 0;
    for (const child of readdirSync(target)) total += dirBytes(path.join(target, child));
    return total;
  } catch {
    return 0;
  }
}

const realCleanupFs: CleanupFs = {
  exists: (p) => existsSync(p),
  readFile: (p) => readFileSync(p, "utf8"),
  sizeOf: (p) => dirBytes(p),
  removeDir: (p) => rmSync(p, { recursive: true, force: true }),
};

/**
 * 更新缓存目录 = `%LOCALAPPDATA%\<updaterCacheDirName>`。
 * 名字**不自己拼**（`sanitizeFileName(name).toLowerCase()+"-updater"` 是 app-builder-lib
 * 的内部推导，抄一份就是第二个事实面，它改了这边静默指错目录）—— 读构建期写进包内的
 * `resources/app-update.yml`（electron-updater 自己也读这份，AppUpdater.js:545）。
 * 读不到回 null：调用方按"不动"处理。
 */
function resolveUpdaterCacheDir(): string | null {
  const local = process.env.LOCALAPPDATA;
  if (!local) return null;
  try {
    const yml = readFileSync(path.join(process.resourcesPath, "app-update.yml"), "utf8");
    const name = /^updaterCacheDirName:\s*(\S+)\s*$/m.exec(yml)?.[1] ?? null;
    return name ? path.join(local, name) : null;
  } catch {
    return null;
  }
}

/**
 * 升级完成后的临时文件清理（用户 2026-10-13 的规矩）。在**新版的每一次开机**都跑一次：
 * 判据自己会沉默（pending 不存在/版本不是当前 → keep），所以不需要"这是不是升级后第一次"
 * 这种记不住的状态。electron-updater 的 Windows 路径没有任何装后清理（读源码核实，
 * 见 updateCacheCleanup.ts 的文件头），不补就是每次升级泄漏一整个安装包。
 * 任何异常都不许影响启动 —— 清理失败最多是文件多留着，不该让壳起不来。
 */
export function runPostInstallCleanup(log: (text: string) => void): void {
  try {
    const cacheDir = resolveUpdaterCacheDir();
    if (!cacheDir) {
      log("升级清理：读不到 app-update.yml 的 updaterCacheDirName，跳过（不猜路径）");
      return;
    }
    const report = cleanupConsumedPendingUpdate(cacheDir, app.getVersion(), realCleanupFs);
    if (report.kind === "removed") {
      log(
        `升级清理：删掉已装完的 pending/${report.file}（${report.version}），` +
          `释放 ${(report.freedBytes / 1024 / 1024).toFixed(1)} MB`,
      );
    } else {
      log(`升级清理：${report.reason}`);
    }
  } catch (error) {
    log(`升级清理异常（不影响功能）：${String(error).slice(0, 160)}`);
  }
}

export function createUpdater(options: UpdaterOptions): Updater {
  let current: UpdaterState = { kind: "idle" };
  let prefs = loadUpdaterPrefs();
  /** 装正在跑：挡住"退出触发的第二次装"（quitAndInstall 自己会走 quit 链）。 */
  let installing = false;
  /**
   * 这一次装是不是**退出触发**的（托盘「重启并更新」直接调 install() 时是 false）。
   *
   * 为什么需要这个旗子（2026-11-03 审查 A-6）：`before-quit` 为了把退出交给安装器，
   * 调了 `event.preventDefault()`。可 `doInstall` 的两条中止路（备份抛错、端口没空出来）
   * 只 `return` —— 没有任何人再触发一次退出。于是用户点「退出」看到的是：一个错误框，
   * 然后应用照旧活着，**既没退也没装**。这比"退出没反应"更糟，因为它给了一次明确的
   * 失败读数、却把那轮退出永久吞掉了。
   *
   * 补一次 `app.quit()` 不会死循环：两条中止路都先 `setState({kind:"error"})`，
   * 而 `onQuitAttempt` 的第一条判据就是 `current.kind !== "ready"` ⇒ 返回 false ⇒
   * `before-quit` 走普通收尾（quitting = true、backend.stop()）。
   */
  let quitDriven = false;
  /**
   * **用户手点的那一轮处在哪个阶段**（null = 不是手点的，比如开机自检 ⇒ 静默档）。
   *
   * 从前这里是个布尔 `manualCheck`，两个症状都从它来：
   *  - 检查成功后不清（那是事实：接下来用户要点下载），于是**下载**失败时 error
   *    监听器拿着旧上下文弹"检查更新失败" —— 检查明明成功了，文案在说谎；
   *  - 失败读数有两个主人（监听器 + 调用方 `.catch`）：electron-updater 在 reject
   *    之前**先 emit("error")**（AppUpdater.js:271 与 :439/:453→:477，已核源码），
   *    于是同一次失败弹两条同名 toast。
   * 现在带阶段，且失败读数只有监听器一个主人（判据在 `updateFeedback.ts`）。
   */
  let manualAction: ManualAction = null;
  const listeners: ((state: UpdaterState) => void)[] = [];
  const setState = (state: UpdaterState): void => {
    current = state;
    for (const listener of listeners) listener(state);
  };

  // 两个自动档都从偏好映射到 electron-updater 的开关上。autoInstallOnAppQuit **恒关**
  // —— 自动装由 onQuitAttempt 走 install()（理由见 Updater.onQuitAttempt 的注释）。
  const applyPrefs = (): void => {
    autoUpdater.autoDownload = prefs.autoDownload;
    autoUpdater.autoInstallOnAppQuit = false;
    // P10 通道映射：beta = 显式允许预发布 + 读 beta channel feed（GitHub provider 下
    // electron-updater 只在 allowPrerelease 时才把 prerelease 的 Release 当候选）。
    // setPrefs 每次都会重跑这里 —— 切通道下一次检查就生效，不用重启。
    autoUpdater.channel = prefs.channel;
    autoUpdater.allowPrerelease = prefs.channel === "beta";
  };
  applyPrefs();
  autoUpdater.logger = null; // 日志走 options.log（shell.log 唯一写入口），不给 updater 另开一份

  autoUpdater.on("checking-for-update", () => setState({ kind: "checking" }));
  autoUpdater.on("update-available", (info: UpdateInfo) => {
    options.log(`发现新版本 ${info.version}`);
    setState({ kind: "available", version: info.version });
    // 手动那一轮的回执（用户 10-14 的反馈：点了「检查更新」一点提示都没有）：用户点了
    // 就明确要一个答案，直接弹 toast 送他眼前 —— 托盘菜单是打开那一刻的快照，点完收起，
    // 后台 refresh 他根本看不见（这正是那次"一点提示都没有"的根因）。**不受**
    // "控制台在前台就抑制"那条规则管：那条防的是主动开口的噪音，这里是他自己要的回执。
    // 上下文**不清**：接下来用户要点「下载更新」，那一轮的成败还是他要的答案，
    // 而失败报哪个阶段就由这个上下文决定（从前它是布尔，于是下载失败被冒名成检查失败）。
    if (manualAction !== null) options.notify(`发现新版本 ${info.version}`, `去托盘点「下载更新」，或到设置 → 关于看详情。`);
  });
  autoUpdater.on("update-not-available", () => {
    setState({ kind: "uptodate" });
    if (isCheckTerminal(manualAction)) {
      options.notify("已是最新版本", `当前 ${app.getVersion()}，没有可用的更新。`);
      manualAction = null; // 检查的终态已送达（判据见 updateFeedback.isCheckTerminal）
    }
  });
  autoUpdater.on("download-progress", (progress: ProgressInfo) => {
    // 开始下载就是"这一轮进入下载阶段"的客观证据：autoDownload 下没人点过第二下，
    // 而上下文还留在 "check" —— 不推进的话下载失败会报"检查更新失败"（检查是成功的）。
    // 静默档（null）由判据保持静默：自动开始下载不等于用户要答案。
    manualAction = advanceStageOnDownload(manualAction);
    setState({ kind: "downloading", percent: Math.round(progress.percent) });
  });
  autoUpdater.on("update-downloaded", (info: UpdateInfo) => {
    options.log(`更新包下载完成：${info.version}`);
    setState({ kind: "ready", version: info.version });
    // 下载完成的回执：不管是谁发起的（手点 or autoDownload），ready 都意味着
    // "有个重启并更新在等人"，值得出一声 —— 不出声它可能一直躺在托盘第二行里。
    options.notify(`新版本 ${info.version} 已下载`, "托盘点「重启并更新」即可完成升级（升级前会自动备份你的数据）。");
  });
  // **失败读数的唯一主人**（A-8）。electron-updater 在 reject 之前先 emit("error")
  // （AppUpdater.js:271 检查路、:439/:453→:477 下载路，已核源码），所以 toast 只能在这里出 ——
  // 调用方的 .catch 再弹一次就是同一次失败两条同名 toast（从前正是这样）。
  // 标题按 manualAction 的阶段给：手点检查之后转下载，失败要报"下载更新失败"，
  // 不能拿着旧的检查上下文冒名（布尔时代就是这个症状）。
  autoUpdater.on("error", (error) => {
    options.log(`更新流程报错：${error?.message ?? String(error)}`);
    setState({ kind: "error", message: String(error?.message ?? error).slice(0, 200) });
    const report = failureOf(manualAction);
    if (report.shouldNotify) {
      options.notify(failureTitle(report.stage), String(error?.message ?? error).slice(0, 120));
    }
    if (report.consumesAction) manualAction = null; // 静默档没有上下文可消费
  });

  // 装的动作单独成函数：`onQuitAttempt` 要调它，而它住在下面那个返回对象里 ——
  // 在对象字面量里自指（`updater.install()`）拿到的是 undefined。
  /**
   * 中止安装：复位旗子、落状态读数、按档位收尾、再给用户那一声（审查 A-6 的续）。
   *
   * **两条中止路共用这一个函数**（端口没空出来 / 备份抛错）—— 从前它们各自抄了一遍
   * 同样的四步，于是"读数尾巴"和"实际做了什么"能各自漂（本轮改序时我若照旧抄两遍，
   * 就是再造一个两处声明）。尾巴文案由 `abortFollowUpNotice(followUp)` 给，与下面那个
   * 分支**同一个判据**：写"退出已交回应用"却走了拉后端那条，就是文案说谎。
   *
   * 收尾分两档（判据在 `updateFeedback.abortFollowUp`）：
   *   - 退出触发 ⇒ `app.quit()` 把用户那一下「退出」交回应用（`onQuitAttempt` 那时
   *     `kind === "error"` 判 false，走普通收尾，不死循环）；
   *   - 手点「重启并更新」⇒ 应用要继续活着，可后端此刻**已经被停了**（备份挪到停库之后
   *     ⇒ 中止路必然落在停库之后）⇒ 必须 `restartBackend()` 拉回来，否则修一个快照撕裂
   *     换来一个"应用活着但连不上后端"的新洞。
   * `restartBackend()` 走 `backend.ensure()`：它幂等（端口有别人在应答就 already-serving，
   * 不会双起），且现在会复位 `quitting`，拉回的后端不失去崩溃自愈（见 backend.ts）。
   *
   * 顺序：读数在前、收尾在后。理由只有一条、且不依赖 `showErrorBox` 是否阻塞 JS 线程
   * （那是 Electron 文档没给到粒度、本机也核不到的事，不敢当依据写）：收尾那两步
   * （`app.quit()` / `restartBackend()`）万一抛错，读数已经先落进 shell.log 与状态机、
   * 对话框也已经弹出 —— 用户至少知道"为什么中止"，不会只剩一个卡住的界面。
   */
  const abortInstall = (reason: string): void => {
    installing = false;
    const followUp = abortFollowUp(quitDriven);
    const message = `${reason}${abortFollowUpNotice(followUp)}`;
    options.log(message);
    setState({ kind: "error", message });
    dialog.showErrorBox("更新已中止", message);
    if (followUp === "release-quit") {
      quitDriven = false;
      app.quit();
    } else {
      options.restartBackend();
    }
  };

  const doInstall = async (): Promise<void> => {
    if (installing) return;
    installing = true;
    // 红线 3 在前、红线 2 在后，顺序承重（2026-11-04 本轮）：备份必须是**安静库**的快照。
    // 从前这里是"先备份、再停后端"，与文件头那条设计依据直接相反（"库安静下来时整目录
    // 拷贝就是一致快照"）：WAL 开着时对活库 cpSync，`app.db` 与 `-wal`/`-shm` 不在同一
    // 时刻，回滚快照可能撕裂 —— 而这份快照正是红线 2 要保的"回滚路径"本身。
    // "库安静"的真实判据不是调过 `stopBackend()`（`taskkill` 是 fire-and-forget，调完
    // 后端可能还没死），而是**端口确认空出来**，所以快照排在 `waitForPortFree` 之后。
    // 代价：中止路现在必然落在停库之后 ⇒ 中止必须把后端拉回来（见 abortInstall）。
    options.stopBackend();
    const port = options.backendPort();
    if (!(await waitForPortFree(port, 10_000))) {
      abortInstall(
        `端口 ${port} 上仍有应答：后端没退干净，已中止更新（强装会撞上文件占用）。`,
      );
      return;
    }
    // 红线 2：库已安静 ⇒ 快照；备份失败就**不装**（没有回滚路径的自动迁移不许发生）。
    try {
      snapshotDataRoot(process.env.LOCALAPPDATA, options.log);
    } catch (error) {
      abortInstall(`升级前备份数据失败，已中止更新：${String(error).slice(0, 200)}。`);
      return;
    }
    const version = current.kind === "ready" ? current.version : "?";
    options.log(`进入安装：quitAndInstall（版本 ${version}）`);
    // 两个参都是 true，且这不是口味：读 BaseUpdater.js:13-16 与 NsisUpdater.js:108-112，
    // `isSilent=false` 时 electron-updater **不给安装器传 /S** —— 我们这包是
    // `oneClick: false`，于是 NSIS 向导会弹出来等人点「下一步」。那会让两条路都坏：
    // 手点「重启并更新」的用户被一个安装向导拦住（人走开就卡在弹窗上，更新永远不完），
    // 而「退出时自动安装」更是直接名不副实。且 `isSilent=false` 时第二参会被忽略
    // （改读 autoRunAppAfterInstall）—— 传 true 反而不生效。
    // isSilent=true → `/S`（今天手动装 0.4.2 用的就是它，实测无向导、静默成功）；
    // isForceRunAfter=true → `--force-run`（装完把新版拉起来）。
    autoUpdater.quitAndInstall(true, true);
  };

  return {
    state: () => current,
    onStateChange: (listener) => listeners.push(listener),
    prefs: () => ({ ...prefs }),
    setPrefs: (patch) => {
      prefs = { ...prefs, ...patch };
      saveUpdaterPrefs(prefs);
      applyPrefs();
      options.log(
        `更新偏好：开机检查=${prefs.checkOnStartup} 自动下载=${prefs.autoDownload} 退出时自动装=${prefs.autoInstallOnQuit} 通道=${prefs.channel}`,
      );
    },
    check: (source = "manual") => {
      if (source === "manual") {
        // 立刻给一声"点到了"：检查要 1~3 秒（问 GitHub），这三秒里托盘菜单已收起，
        // 没有这一声用户面对的就是彻底的无声 —— 10-14 反馈的原话是"都不知道有没有检查过"。
        manualAction = "check";
        options.notify("正在检查更新…", "几秒内有结果，会再提示你。");
      }
      void autoUpdater.checkForUpdates().catch((error) => {
        // **只记日志**：上游在 reject 之前已经 emit("error")（AppUpdater.js:271），
        // 状态读数与失败 toast 都由上面那个监听器认领（判据在 updateFeedback.failureOf）。
        // 从前这里对 manual 又 notify 一次 —— 同一次失败两条同名 toast 就是这么来的。
        // 留着这一声 log 是因为它带 source（manual/startup），比监听器那条更能定位是谁发起的。
        options.log(`检查更新失败（${source}）：${String(error).slice(0, 160)}`);
      });
    },
    download: () => {
      // 点击回执：下载 ~281 MB 要几十秒到几分钟，第一声确认"点到了、在下了"。
      // 上下文进 download 阶段：此后这一轮的失败要报"下载更新失败"。
      manualAction = "download";
      options.notify("开始下载更新…", "进度看托盘菜单的「下载中 N%」，完成后会再提示。");
      void autoUpdater.downloadUpdate().catch((error) => {
        // **只记日志**：与 check() 同一条理由 —— downloadUpdate 在 reject 之前先
        // emit("error")（AppUpdater.js:439/:453→:477），状态读数与失败 toast 都归
        // 那个监听器。从前这里既 setState 又 notify：读数有两个主人，toast 弹两条。
        options.log(`下载更新失败：${String(error).slice(0, 160)}`);
      });
    },
    install: doInstall,
    onQuitAttempt: () => {
      // 判据住在 updateFeedback.shouldHandoffQuitToInstall（三个条件：开关开着、
      // 有下载好的等着装、这一轮没在装）。搬出去是为了能被 node 直接测 ——
      // 尤其"没在装"那一条：它同时是挡住死循环的那一条（quitAndInstall 自己会
      // 再触发一次 before-quit）。
      if (
        !shouldHandoffQuitToInstall({
          autoInstallOnQuit: prefs.autoInstallOnQuit,
          stateKind: current.kind,
          installing,
        })
      )
        return false;
      // 交接成立才写这一行日志（从前判据写在 if 里、日志无条件跟在后面，
      // 于是 kind 不是 ready 时这里读 current.version —— 那个分支上它根本没有 version）。
      const version = current.kind === "ready" ? current.version : "?";
      options.log(`退出时自动装（版本 ${version}）：走同一条 install()，备份与后端退出照常`);
      // 不 await：调用方是同步的 quit 链。doInstall 里的 quitAndInstall 会接管这轮退出，
      // 所以返回 true 告诉调用方**别再自己 app.quit()**（那会打断安装）。
      // 置旗子：这轮退出是被安装**借走**的，两条中止路必须把它交回去（A-6）——
      // 必须在 doInstall() 之前置：它的开头是同步的，中止可能发生在第一个 await 之后、
      // 而旗子要在任何一次 abortInstall 读到之前就已经是 true（它据此选 release-quit 档）。
      quitDriven = true;
      void doInstall();
      return true;
    },
  };
}

/** dev 态判定与装配入口（index.ts 用）。未打包 = 没有 app-update.yml，整条不接。 */
export const updaterAvailable = (): boolean => app.isPackaged && process.platform === "win32";
