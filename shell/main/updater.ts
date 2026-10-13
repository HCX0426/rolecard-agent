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
import { cpSync, existsSync, mkdirSync, readdirSync, readFileSync, rmSync, statSync } from "node:fs";
import net from "node:net";
import path from "node:path";

import { cleanupConsumedPendingUpdate, type CleanupFs } from "./updateCacheCleanup";

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
  check: () => void;
  download: () => void;
  install: () => Promise<void>;
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
  const listeners: ((state: UpdaterState) => void)[] = [];
  const setState = (state: UpdaterState): void => {
    current = state;
    for (const listener of listeners) listener(state);
  };

  autoUpdater.autoDownload = false; // 红线 1：下载要用户点
  autoUpdater.autoInstallOnAppQuit = false; // 装更要用户点，不许"下次退出顺手装"
  autoUpdater.logger = null; // 日志走 options.log（shell.log 唯一写入口），不给 updater 另开一份

  autoUpdater.on("checking-for-update", () => setState({ kind: "checking" }));
  autoUpdater.on("update-available", (info: UpdateInfo) => {
    options.log(`发现新版本 ${info.version}`);
    setState({ kind: "available", version: info.version });
  });
  autoUpdater.on("update-not-available", () => setState({ kind: "uptodate" }));
  autoUpdater.on("download-progress", (progress: ProgressInfo) =>
    setState({ kind: "downloading", percent: Math.round(progress.percent) }),
  );
  autoUpdater.on("update-downloaded", (info: UpdateInfo) => {
    options.log(`更新包下载完成：${info.version}`);
    setState({ kind: "ready", version: info.version });
  });
  autoUpdater.on("error", (error) => {
    options.log(`更新流程报错：${error?.message ?? String(error)}`);
    setState({ kind: "error", message: String(error?.message ?? error).slice(0, 200) });
  });

  return {
    state: () => current,
    onStateChange: (listener) => listeners.push(listener),
    check: () => {
      void autoUpdater.checkForUpdates().catch((error) => {
        options.log(`检查更新失败：${String(error).slice(0, 160)}`);
        setState({ kind: "error", message: `检查失败：${String(error).slice(0, 160)}` });
      });
    },
    download: () => {
      void autoUpdater.downloadUpdate().catch((error) => {
        options.log(`下载更新失败：${String(error).slice(0, 160)}`);
        setState({ kind: "error", message: `下载失败：${String(error).slice(0, 160)}` });
      });
    },
    install: async () => {
      // 红线 2：先备份，备份失败就**不装**（没有回滚路径的自动迁移不许发生）。
      try {
        snapshotDataRoot(process.env.LOCALAPPDATA, options.log);
      } catch (error) {
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
        const message = `端口 ${port} 上仍有应答：后端没退干净，已中止更新（强装会撞上文件占用）。`;
        options.log(message);
        setState({ kind: "error", message });
        dialog.showErrorBox("更新已中止", message);
        return;
      }
      const version = current.kind === "ready" ? current.version : "?";
      options.log(`进入安装：quitAndInstall（版本 ${version}）`);
      // 第二参 true = 装完自动把新版拉起来：用户点的就是"重启并更新"。
      autoUpdater.quitAndInstall(false, true);
    },
  };
}

/** dev 态判定与装配入口（index.ts 用）。未打包 = 没有 app-update.yml，整条不接。 */
export const updaterAvailable = (): boolean => app.isPackaged && process.platform === "win32";
