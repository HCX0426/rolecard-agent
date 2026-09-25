#!/usr/bin/env node
/**
 * 桌面壳的「退出」冒烟：证明 `app.quit()` 真能把进程收掉，而不是停在半路。
 *
 * 用法：
 *   node scripts/shell_quit_smoke.js
 *
 * 为什么需要它（2026-09-25 用户报"托盘那个退出程序有问题"）：根因是 `trackBounds` 在
 * `closed` 回调里读 `win.getBounds()` —— 那时原生窗已销毁，抛
 * `TypeError: Object has been destroyed`，而主进程未捕获异常会弹一扇**模态**错误框，
 * 那扇框把 quit 的收尾整条堵住。同一套探针实测：修之前 `app.quit()` 到进程真退要
 * **80.2 秒**（6114ms → 86275ms，中间全靠人把框点掉），修之后 **28 毫秒**。这类毛病
 * 单测抓不到：它要真窗、真销毁、真事件循环，所以只能起一次真壳。
 *
 * 设计取舍：
 *   - **不碰用户那份实例**：自带一个临时 `--user-data-dir`（Electron 的单实例锁是按
 *     userData 路径派生的，不复用就等于不抢他的锁），也不 spawn 后端
 *     （`ROLECARD_BACKEND_CMD` 指到一个不存在的程序 → 归属判定走 "failed" 分支，
 *     退出时 `backend.stop()` 是空操作）。被测的是退出路径，不是后端。
 *   - 探针是**从外面贴到编译产物上**的（`shell/out` 是 gitignore 的构建输出，下一次
 *     `tsc` 就冲掉），生产代码里不留调试钩子、也不为它加分支。
 *   - 缺依赖时**跳过而不是报红**（没装 shell 依赖 / 没有图形会话 → exit 0 并说明原因），
 *     与 `ui_smoke.js` 同一套口径：环境差异不该变成仓库的红。
 *
 * 环境覆盖：
 *   SHELL_SMOKE_SKIP_BUILD=1  不重新 `tsc`（已经 build 过时省十几秒）
 *   SHELL_SMOKE_QUIT_MS       起壳多久之后打 quit（默认 4000）
 *   SHELL_SMOKE_BUDGET_MS     允许的 quit→退出耗时上限（默认 5000）
 *   SHELL_SMOKE_HARD_MS       整轮的上限，超了就判死（默认 30000）
 */

const { spawn, spawnSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const SHELL = path.join(ROOT, "shell");
const MAIN_JS = path.join(SHELL, "out", "main", "index.js");
const QUIT_MS = Number(process.env.SHELL_SMOKE_QUIT_MS || 4000);
const BUDGET_MS = Number(process.env.SHELL_SMOKE_BUDGET_MS || 5000);
const HARD_MS = Number(process.env.SHELL_SMOKE_HARD_MS || 30000);

/** 跳过 = exit 0：这只壳不是每台机器都装得起依赖，也不是每次都有桌面会话。 */
function skip(why) {
  console.log(`SKIP shell quit smoke: ${why}`);
  process.exit(0);
}

function electronBinary() {
  try {
    const p = require(path.join(SHELL, "node_modules", "electron"));
    return typeof p === "string" && fs.existsSync(p) ? p : null;
  } catch {
    return null;
  }
}

/**
 * 贴到编译产物尾部的探针：把 quit 生命周期按毫秒记进一个文件。
 *
 * 为什么不用 `console.log`：上一轮实测就是栽在这儿 —— 主进程 stdout 经 npx 转发，
 * 进程一死尾部就被截掉，最关键的几行恰好没了。落盘写就没这个问题。
 */
const PROBE = `
// shell_quit_smoke.js 贴进来的临时探针（构建产物，不进版本库）
{
  const fsx = require("node:fs");
  const outFile = process.env.SHELL_SMOKE_LOG;
  const t0 = Date.now();
  const say = (m) => { try { fsx.appendFileSync(outFile, (Date.now() - t0) + "ms " + m + "\\n"); } catch (e) {} };
  const winDump = () => electron_1.BrowserWindow.getAllWindows()
    .map((w) => w.id + (w.isDestroyed() ? ":destroyed" : ":" + (w.isVisible() ? "vis" : "hid"))).join(",") || "-";
  for (const name of ["before-quit", "will-quit", "quit", "window-all-closed"])
    electron_1.app.on(name, () => say("app:" + name + " windows=" + winDump()));
  process.on("exit", (code) => say("PROCESS EXIT code=" + code));
  void electron_1.app.whenReady().then(() => {
    for (const w of electron_1.BrowserWindow.getAllWindows()) {
      w.on("close", (e) => say("win" + w.id + " close prevented=" + e.defaultPrevented));
      w.on("closed", () => say("win" + w.id + " closed"));
    }
    say("ready windows=" + winDump());
    setTimeout(() => { say("calling app.quit()"); electron_1.app.quit(); }, Number(process.env.SHELL_SMOKE_QUIT_MS));
  });
}
`;

function stamp() {
  return new Date().toISOString().slice(11, 23);
}

async function main() {
  if (process.platform !== "win32") skip(`本冒烟按 Windows 的模态错误框行为定的（当前 ${process.platform}）`);
  if (!fs.existsSync(MAIN_JS)) skip("shell/out 还没编译（先跑 npm --prefix shell run build）");
  const exe = electronBinary();
  if (!exe) skip("没装 shell/node_modules/electron，起不了壳");

  if (!process.env.SHELL_SMOKE_SKIP_BUILD) {
    // 直接拿本进程的解释器去跑 `tsc`，不经过 npm：Windows 上的 `npm.cmd`/`tsc.cmd` 用
    // `spawnSync` 起要么得开 `shell: true`（参数变成字符串拼接，Node 自己会警告），
    // 要么直接 EINVAL。少一层壳也少一类"在这台机器上起不来"的环境差异。
    const tsc = path.join(SHELL, "node_modules", "typescript", "bin", "tsc");
    if (!fs.existsSync(tsc)) skip("没装 shell/node_modules/typescript，编译不了壳");
    const built = spawnSync(process.execPath, [tsc, "-p", "tsconfig.json"], {
      cwd: SHELL,
      encoding: "utf8",
    });
    if (built.status !== 0) {
      console.error(`FAIL shell quit smoke: 壳没编译过\n${built.stdout || ""}${built.stderr || ""}`);
      process.exit(1);
    }
  }

  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "rolecard-shell-smoke-"));
  const logFile = path.join(profile, "quit.log");
  const original = fs.readFileSync(MAIN_JS, "utf8");
  fs.writeFileSync(MAIN_JS, original + PROBE);

  let child;
  let killed = false;
  try {
    child = spawn(exe, [".", `--user-data-dir=${profile}`], {
      cwd: SHELL,
      windowsHide: true,
      stdio: "ignore",
      env: {
        ...process.env,
        // 指到一个不可能存在的程序：壳判定"拉不起后端"就照常开窗，退出时也没有子进程要收。
        ROLECARD_BACKEND_CMD: path.join(profile, "no-such-backend.exe"),
        SHELL_SMOKE_LOG: logFile,
        SHELL_SMOKE_QUIT_MS: String(QUIT_MS),
      },
    });
    const died = await new Promise((resolve) => {
      const timer = setTimeout(() => {
        killed = true;
        try {
          spawnSync("taskkill", ["/PID", String(child.pid), "/T", "/F"], { stdio: "ignore" });
        } catch (e) {
          child.kill("SIGKILL");
        }
        resolve(null);
      }, HARD_MS);
      child.once("exit", (code) => {
        clearTimeout(timer);
        resolve(code);
      });
    });
    const raw = fs.existsSync(logFile) ? fs.readFileSync(logFile, "utf8") : "";
    const at = (needle) => {
      const line = raw.split("\n").find((l) => l.includes(needle));
      return line ? Number(line.slice(0, line.indexOf("ms")).trim()) : null;
    };
    const quitAt = at("calling app.quit()");
    const exitAt = at("PROCESS EXIT");
    const prevented = raw.split("\n").filter((l) => l.includes("prevented=true"));
    const failures = [];
    if (killed) failures.push(`超过 ${HARD_MS}ms 没自己退出（被强杀）`);
    if (quitAt === null) failures.push("探针没记到 quit 那一行，壳可能没起来");
    if (exitAt === null) failures.push("探针没记到 PROCESS EXIT，进程没走到退出");
    if (prevented.length) failures.push(`有窗把 close 拦了：${prevented.join(" / ")}`);
    if (quitAt !== null && exitAt !== null && exitAt - quitAt > BUDGET_MS)
      failures.push(`quit→退出用了 ${exitAt - quitAt}ms，超过预算 ${BUDGET_MS}ms`);

    const secs = quitAt !== null && exitAt !== null ? ((exitAt - quitAt) / 1000).toFixed(3) : "-";
    console.log(`[${stamp()}] shell quit smoke: quit→进程退出 ${secs}s（预算 ${BUDGET_MS}ms）`);
    if (failures.length) {
      for (const f of failures) console.error(`FAIL ${f}`);
      console.error(`--- 探针日志 ---\n${raw}`);
      process.exit(1);
    }
    console.log("PASS 退出路径干净：没有窗拦 close，没有未捕获异常把收尾堵住");
  } finally {
    fs.writeFileSync(MAIN_JS, original); // 探针只活在这一轮里
    try {
      fs.rmSync(profile, { recursive: true, force: true });
    } catch (e) {
      /* 临时目录清不掉不影响结论 */
    }
  }
}

void main();
