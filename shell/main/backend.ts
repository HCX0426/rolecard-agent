/**
 * 本地后端进程的生命周期归属。
 *
 * 一条不可让的规矩：**只动本壳 spawn 出来的那个进程**。端口上已经有别人起的后端时，
 * 壳连它、用它，退出时**不碰它** —— 用户手动开的实例被壳杀掉，是我们赔不起的事故。
 *
 * 硬杀/崩溃那条路不靠这里：后端自己有父进程看门狗（`core/parent_watch.py`，
 * 靠下面注入的 `ROLECARD_PARENT_PID`），壳没机会执行清理时它自己走。
 */
import { spawn, type ChildProcess } from "node:child_process";
import { existsSync } from "node:fs";
import { createConnection } from "node:net";
import path from "node:path";

/** 与 `run_api.py` 的默认绑定一致（开发期固定端口是团队约定，不靠换端口躲冲突）。 */
export const DEFAULT_PORT = 8000;

function envPort(): number {
  const raw = process.env.ROLECARD_API_PORT;
  const port = raw ? Number.parseInt(raw, 10) : NaN;
  return Number.isFinite(port) && port > 0 ? port : DEFAULT_PORT;
}

export function endpoint(): { host: string; port: number } {
  return { host: "127.0.0.1", port: envPort() };
}

/** 控制台地址：着陆页探活成功后跳这里。界面由后端托管，所以壳里不复制一份 dist。 */
export function consoleUrl(): string {
  return process.env.ROLECARD_BACKEND_URL ?? `http://${endpoint().host}:${endpoint().port}`;
}

/** 端口上有人应答吗。只做 TCP 层判定 —— 着陆页还要发真请求，两者语义不同。 */
export function serving(timeoutMs = 300): Promise<boolean> {
  const { host, port } = endpoint();
  return new Promise((resolve) => {
    const socket = createConnection({ host, port });
    const done = (value: boolean) => {
      socket.destroy();
      resolve(value);
    };
    socket.setTimeout(timeoutMs);
    socket.once("connect", () => done(true));
    socket.once("timeout", () => done(false));
    socket.once("error", () => done(false));
  });
}

/**
 * 仓库根：开发态由编译产物位置（`shell/out/main/backend.js`）往上三级推。
 * 打包形态不成立，届时用 `ROLECARD_BACKEND_CMD` 给绝对路径（见 backendCommand）。
 */
function repoRoot(): string {
  return path.resolve(__dirname, "..", "..", "..");
}

function backendCommand(): { program: string; args: string[] } | null {
  const whole = process.env.ROLECARD_BACKEND_CMD?.trim();
  if (whole) {
    const [program, ...args] = whole.split(/\s+/);
    if (program) return { program, args };
  }
  const root = repoRoot();
  const python =
    process.env.ROLECARD_PYTHON ?? path.join(root, ".venv", "Scripts", "python.exe");
  const script = process.env.ROLECARD_API_SCRIPT ?? path.join(root, "scripts", "run_api.py");
  if (!existsSync(python) || !existsSync(script)) return null;
  return { program: python, args: [script] };
}

export type Outcome =
  | { kind: "already-serving" }
  | { kind: "spawned"; pid: number }
  | { kind: "failed"; reason: string };

export class Backend {
  private child: ChildProcess | null = null;

  /** 端口没人应就拉起后端；已有人应就直接连。不等就绪 —— 着陆页自己重试。 */
  async ensure(): Promise<Outcome> {
    if (await serving()) return { kind: "already-serving" };
    const command = backendCommand();
    if (!command) {
      return {
        kind: "failed",
        reason:
          "找不到本地后端的启动命令：设 ROLECARD_BACKEND_CMD（整条命令）或 " +
          "ROLECARD_PYTHON + ROLECARD_API_SCRIPT（开发态用仓库 .venv + scripts/run_api.py）",
      };
    }
    const child = spawn(command.program, command.args, {
      cwd: repoRoot(), // 后端里的相对路径（data/、frontend/dist）按仓库根写，cwd 错了会建到别处
      detached: false,
      windowsHide: true, // 不给桌宠旁边开一个黑框
      stdio: ["ignore", "inherit", "inherit"],
      env: {
        ...process.env,
        // 端口必须**同时**告诉子进程：run_api.py 认的是它自己的 RUN_API_PORT。
        // 不设就是"后端起在 8000、壳在探 8011"这种谁都没错却连不上的事故。
        RUN_API_PORT: String(endpoint().port),
        // 父进程看门狗的输入：壳被硬杀/崩溃时后端自己走（见本文件顶部注释）。
        ROLECARD_PARENT_PID: String(process.pid),
      },
    });
    if (!child.pid) {
      return { kind: "failed", reason: `启动 ${command.program} 失败：拿不到 pid` };
    }
    this.child = child;
    return { kind: "spawned", pid: child.pid };
  }

  /** 退出时回收：连子进程树一起（杀父不杀子会留下孤儿占着端口，下次启动就变谜）。 */
  stop(): void {
    const child = this.child;
    this.child = null;
    if (!child?.pid) return;
    if (process.platform === "win32") {
      spawn("taskkill", ["/PID", String(child.pid), "/T", "/F"], { stdio: "ignore" });
      return;
    }
    child.kill("SIGTERM");
  }
}
