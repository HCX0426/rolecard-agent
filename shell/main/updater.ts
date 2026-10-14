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

import { cleanupConsumedPendingUpdate, type CleanupFs } from "./updateCacheCleanup";

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
 */
export type UpdaterPrefs = {
  checkOnStartup: boolean;
  autoDownload: boolean;
  autoInstallOnQuit: boolean;
};

export const UPDATER_PREF_DEFAULTS: UpdaterPrefs = {
  checkOnStartup: true,
  autoDownload: false,
  autoInstallOnQuit: false,
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
  const flag = (key: keyof UpdaterPrefs) =>
    typeof o[key] === "boolean" ? (o[key] as boolean) : UPDATER_PREF_DEFAULTS[key];
  return {
    checkOnStartup: flag("checkOnStartup"),
    autoDownload: flag("autoDownload"),
    autoInstallOnQuit: flag("autoInstallOnQuit"),
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
   * 系统通知（手动检查的回执走它）。**绕过**"控制台在前台就抑制"那条规则 ——
   * 那条防的是主动开口的噪音；更新回执是用户自己点出来的，他明确要一个答案。
   */
  notify: (title: string, body: string) => void;
};

/** 数据根的备份保留份数：只留最近 N 份，装十次不会攒出十份几百 MB 的快照。 */
const BACKUP_KEEP = 2;

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
  for (const name of all.slice(0, Math.max(0, all.length - BACKUP_KEEP))) {
    rmSync(path.join(dir, name), { recursive: true, force: true });
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
   * 本次检查是**用户手点的**（托盘「检查更新」）⇒ 每个终态都要弹 toast 送回执。
   * 开机自检（startup）不置位 —— 静默是它的默认，发现新版亮托盘状态行就够。
   * 终态送达后清掉，防止一个陈旧的 true 让之后的静默事件也弹窗。
   */
  let manualCheck = false;
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
  };
  applyPrefs();
  autoUpdater.logger = null; // 日志走 options.log（shell.log 唯一写入口），不给 updater 另开一份

  autoUpdater.on("checking-for-update", () => setState({ kind: "checking" }));
  autoUpdater.on("update-available", (info: UpdateInfo) => {
    options.log(`发现新版本 ${info.version}`);
    setState({ kind: "available", version: info.version });
    // 手动检查的回执（用户 10-14 的反馈：点了「检查更新」一点提示都没有）：用户点了
    // 就明确要一个答案，直接弹 toast 送他眼前 —— 托盘菜单是打开那一刻的快照，点完收起，
    // 后台 refresh 他根本看不见（这正是那次"一点提示都没有"的根因）。**不受**
    // "控制台在前台就抑制"那条规则管：那条防的是主动开口的噪音，这里是他自己要的回执。
    if (manualCheck) options.notify(`发现新版本 ${info.version}`, `去托盘点「下载更新」，或到设置 → 关于看详情。`);
  });
  autoUpdater.on("update-not-available", () => {
    setState({ kind: "uptodate" });
    if (manualCheck) {
      options.notify("已是最新版本", `当前 ${app.getVersion()}，没有可用的更新。`);
      manualCheck = false; // 终态已送达：别让下一次开机检查的落点再弹（开机那次不设 manualCheck）
    }
  });
  autoUpdater.on("download-progress", (progress: ProgressInfo) =>
    setState({ kind: "downloading", percent: Math.round(progress.percent) }),
  );
  autoUpdater.on("update-downloaded", (info: UpdateInfo) => {
    options.log(`更新包下载完成：${info.version}`);
    setState({ kind: "ready", version: info.version });
    // 下载完成的回执：不管是谁发起的（手点 or autoDownload），ready 都意味着
    // "有个重启并更新在等人"，值得出一声 —— 不出声它可能一直躺在托盘第二行里。
    options.notify(`新版本 ${info.version} 已下载`, "托盘点「重启并更新」即可完成升级（升级前会自动备份你的数据）。");
  });
  autoUpdater.on("error", (error) => {
    options.log(`更新流程报错：${error?.message ?? String(error)}`);
    setState({ kind: "error", message: String(error?.message ?? error).slice(0, 200) });
    if (manualCheck) {
      options.notify("检查更新失败", String(error?.message ?? error).slice(0, 120));
      manualCheck = false;
    }
  });

  // 装的动作单独成函数：`onQuitAttempt` 要调它，而它住在下面那个返回对象里 ——
  // 在对象字面量里自指（`updater.install()`）拿到的是 undefined。
  const doInstall = async (): Promise<void> => {
    if (installing) return;
    installing = true;
    // 红线 2：先备份，备份失败就**不装**（没有回滚路径的自动迁移不许发生）。
    try {
      snapshotDataRoot(process.env.LOCALAPPDATA, options.log);
    } catch (error) {
      installing = false;
      const message = `升级前备份数据失败，已中止更新：${String(error).slice(0, 200)}`;
      options.log(message);
      setState({ kind: "error", message });
      dialog.showErrorBox("更新已中止", message);
      return;
    }
    // 红线 3：后端干净退出 —— 先收进程树，再等端口空出来，才交给安装器。
    options.stopBackend();
    const port = options.backendPort();
    if (!(await waitForPortFree(port, 10_000))) {
      installing = false;
      const message = `端口 ${port} 上仍有应答：后端没退干净，已中止更新（强装会撞上文件占用）。`;
      options.log(message);
      setState({ kind: "error", message });
      dialog.showErrorBox("更新已中止", message);
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
        `更新偏好：开机检查=${prefs.checkOnStartup} 自动下载=${prefs.autoDownload} 退出时自动装=${prefs.autoInstallOnQuit}`,
      );
    },
    check: (source = "manual") => {
      if (source === "manual") {
        // 立刻给一声"点到了"：检查要 1~3 秒（问 GitHub），这三秒里托盘菜单已收起，
        // 没有这一声用户面对的就是彻底的无声 —— 10-14 反馈的原话是"都不知道有没有检查过"。
        manualCheck = true;
        options.notify("正在检查更新…", "几秒内有结果，会再提示你。");
      }
      void autoUpdater.checkForUpdates().catch((error) => {
        // 开机那次检查失败不该弹任何东西：它只是"顺手查一下"，托盘保持"检查更新"入口，
        // 用户真要点会自己点（error 读数进状态行，看得见但不打扰）。
        options.log(`检查更新失败（${source}）：${String(error).slice(0, 160)}`);
        if (source === "manual") {
          options.notify("检查更新失败", String(error).slice(0, 120));
          manualCheck = false;
        } else {
          setState({ kind: "error", message: `检查失败：${String(error).slice(0, 160)}` });
        }
      });
    },
    download: () => {
      // 点击回执：下载 ~281 MB 要几十秒到几分钟，第一声确认"点到了、在下了"。
      options.notify("开始下载更新…", "进度看托盘菜单的「下载中 N%」，完成后会再提示。");
      void autoUpdater.downloadUpdate().catch((error) => {
        options.log(`下载更新失败：${String(error).slice(0, 160)}`);
        setState({ kind: "error", message: `下载失败：${String(error).slice(0, 160)}` });
        options.notify("下载更新失败", String(error).slice(0, 120));
      });
    },
    install: doInstall,
    onQuitAttempt: () => {
      if (!prefs.autoInstallOnQuit || current.kind !== "ready" || installing) return false;
      options.log(`退出时自动装（版本 ${current.version}）：走同一条 install()，备份与后端退出照常`);
      // 不 await：调用方是同步的 quit 链。doInstall 里的 quitAndInstall 会接管这轮退出，
      // 所以返回 true 告诉调用方**别再自己 app.quit()**（那会打断安装）。
      void doInstall();
      return true;
    },
  };
}

/** dev 态判定与装配入口（index.ts 用）。未打包 = 没有 app-update.yml，整条不接。 */
export const updaterAvailable = (): boolean => app.isPackaged && process.platform === "win32";
