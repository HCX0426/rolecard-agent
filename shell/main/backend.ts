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
import { createWriteStream, existsSync, renameSync, rmSync, statSync } from "node:fs";
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
 * 打包态没有仓库根 —— 后端是 `resources/rolecard-backend/` 里那个自包含可执行文件，
 * 由 index.ts 通过 `sidecarExe` 传进来。
 */
function repoRoot(): string {
  return path.resolve(__dirname, "..", "..", "..");
}

/** `ROLECARD_BACKEND_CMD` 的解析：支持引号，因为打包后的安装路径**一定**带空格
 *  （`C:\Program Files\...`），按空白裸切会把可执行文件切成两半。 */
function splitCommand(line: string): string[] {
  const out: string[] = [];
  const pattern = /"([^"]*)"|(\S+)/g;
  let match: RegExpExecArray | null = pattern.exec(line);
  while (match) {
    out.push(match[1] ?? match[2] ?? "");
    match = pattern.exec(line);
  }
  return out;
}

export type BackendOptions = {
  /** 打包形态：随包后端的绝对路径（`resources/rolecard-backend/rolecard-backend.exe`）。 */
  sidecarExe?: string;
  /** 后端 stdout/stderr 的落盘位置（打包态唯一可查的日志）。 */
  logFile?: string;
  /** 打包态给后端的 cwd：用户数据目录，保证可写（安装目录不保证）。 */
  cwd?: string;
};

function backendCommand(
  options: BackendOptions,
): { program: string; args: string[]; cwd?: string } | null {
  const whole = process.env.ROLECARD_BACKEND_CMD?.trim();
  if (whole) {
    const [program, ...args] = splitCommand(whole);
    if (program && existsSync(program)) return { program, args };
    // 显式给了却指不到文件：不静默回落到开发态，否则"装了包却在跑仓库 venv"这种
    // 状态最难查。
    if (program) {
      console.error(`[shell] ROLECARD_BACKEND_CMD 指向的程序不存在：${program}`);
      return null;
    }
  }
  if (options.sidecarExe) {
    if (!existsSync(options.sidecarExe)) {
      console.error(`[shell] 随包后端不在预期位置：${options.sidecarExe}`);
      return null;
    }
    return { program: options.sidecarExe, args: [] };
  }
  const root = repoRoot();
  const python =
    process.env.ROLECARD_PYTHON ?? path.join(root, ".venv", "Scripts", "python.exe");
  const script = process.env.ROLECARD_API_SCRIPT ?? path.join(root, "scripts", "run_api.py");
  if (!existsSync(python) || !existsSync(script)) return null;
  return { program: python, args: [script], cwd: root };
}

export type Outcome =
  | { kind: "already-serving" }
  | { kind: "spawned"; pid: number }
  | { kind: "failed"; reason: string };

export class Backend {
  private child: ChildProcess | null = null;

  /**
   * @param options.sidecarExe 打包形态下随包后端的绝对路径（开发态不传）
   * @param options.logFile    把后端的 stdout/stderr 落到文件：打包态没有终端，
   *   而"进程归属"这类保证的**判据就是日志里那一行**（`[parent-watch] …`），
   *   没有可查的日志，等于那条保证无法被验证。
   */
  constructor(
    private readonly options: BackendOptions & { logFile?: string } = {},
  ) {}

  /** 端口没人应就拉起后端；已有人应就直接连。不等就绪 —— 着陆页自己重试。 */
  async ensure(): Promise<Outcome> {
    if (await serving()) return { kind: "already-serving" };
    const command = backendCommand(this.options);
    if (!command) {
      return {
        kind: "failed",
        reason:
          "找不到本地后端的启动命令：设 ROLECARD_BACKEND_CMD（整条命令，路径带空格要加引号）或 " +
          "ROLECARD_PYTHON + ROLECARD_API_SCRIPT（开发态用仓库 .venv + scripts/run_api.py）",
      };
    }
    const child = spawn(command.program, command.args, {
      // 开发态按仓库根（后端里的相对路径 data/、frontend/dist 都按它写）；打包态
      // 一律按用户数据目录：安装目录可能不可写，任何残留的相对写都不该落进 Program Files。
      cwd: command.cwd ?? this.options.cwd,
      detached: false,
      windowsHide: true, // 不给桌宠旁边开一个黑框
      stdio: ["ignore", "pipe", "pipe"],
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
    this.echoToConsole(child);
    this.child = child;
    return { kind: "spawned", pid: child.pid };
  }

  /** 后端的输出既要在壳的终端里看得见（开发态），也要落盘（打包态唯一可查的地方）。 */
  private echoToConsole(child: ChildProcess): void {
    const streams = [child.stdout, child.stderr].filter(
      (stream): stream is NonNullable<typeof stream> => Boolean(stream),
    );
    for (const stream of streams) {
      stream.setEncoding("utf8");
      stream.on("data", (chunk: string) => process.stdout.write(chunk));
    }
    const logPath = this.options.logFile;
    if (!logPath) return;
    let sink: ReturnType<typeof createWriteStream>;
    try {
      // 追加 + 8MB 轮转留一代（`R102-64`）：旧的"每次启动覆盖"会在崩溃后壳拉起后端的
      // 那一刻把崩溃现场日志整份抹掉 —— 抹掉的恰恰是最想看的东西。留 `.1` 一代与
      // `core/observability.py` 的 LocalTracer 同一尺寸口径，日志也不会无限长。
      try {
        const stat = statSync(logPath);
        if (stat.size > 8 * 1024 * 1024) {
          rmSync(`${logPath}.1`, { force: true });
          renameSync(logPath, `${logPath}.1`);
        }
      } catch {
        // 文件不存在 = 第一次启动，直接追加即可
      }
      sink = createWriteStream(logPath, { flags: "a" });
    } catch (error) {
      console.warn(`[shell] 后端日志落不了盘：${String(error)}`);
      return;
    }
    // end:false：两条流共用一个文件，任一条结束都不该关掉它（关掉另一条就写不进去了）。
    for (const stream of streams) stream.pipe(sink, { end: false });
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
