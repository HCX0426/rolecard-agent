/**
 * 本地推理服务（Ollama）进程的起停 —— 里程碑 D③-b。
 *
 * 为什么做在壳里而不是后端：这是**进程生命周期**，而生命周期归壳管（后端只管"怎么跟一个
 * 已经在跑的推理服务说话"）。也正因为如此，"ollama 这个可执行文件在哪"只有这里回答 ——
 * 后端不引入 `OLLAMA_BIN`，否则同一个事实会有两处真相（§11.1 那次合并就是因为这个）。
 *
 * 一条不可让的规矩：**只停自己 spawn 的那个**。端口上跑着一个开机自启 / 用户自己开的
 * Ollama 时，这里不碰它（那是别人的服务），界面上也就不会出现"停止"按钮 —— 见 `owner()`
 * 只回答归属，不回答"在不在跑"（后者由 `/api/local-service` 说）。
 */
import { spawn, spawnSync, type ChildProcess } from "node:child_process";
import { existsSync } from "node:fs";
import { createConnection } from "node:net";
import path from "node:path";

/** 壳侧的 env 契约（与 ROLECARD_PET 同一族，不是 Python 的 Settings）。 */
const BIN_ENV = "ROLECARD_OLLAMA_BIN";
const URL_ENV = "ROLECARD_OLLAMA_URL";
const DEFAULT_URL = "http://127.0.0.1:11434";

/** 推理服务的地址。默认端口与后端 `core/probes.py` 里那个一致；换端口时两边都经 env，
 *  不在这里再猜一次后端配了哪个地址（那是模型页的事，壳只管进程在不在）。 */
export function serviceUrl(): string {
  return process.env[URL_ENV]?.trim() || DEFAULT_URL;
}

function whichAll(name: string): string[] {
  const finder = process.platform === "win32" ? "where" : "which";
  try {
    const probe = spawnSync(finder, [name], { encoding: "utf8", windowsHide: true });
    if (probe.status !== 0) return [];
    return probe.stdout
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean);
  } catch {
    return []; // 找不到 finder 不是故障：还有别的候选位置
  }
}

function candidates(): string[] {
  const explicit = process.env[BIN_ENV]?.trim();
  if (explicit) return [explicit];
  const local = process.env.LOCALAPPDATA ?? "";
  const pf = process.env["ProgramFiles"] ?? "C:\\Program Files";
  const pf86 = process.env["ProgramFiles(x86)"] ?? "C:\\Program Files (x86)";
  return [
    // 官方 Windows 安装器的两个默认落点（按用户 / 整机）
    path.join(local, "Programs", "Ollama", "ollama.exe"),
    path.join(pf, "Ollama", "ollama.exe"),
    path.join(pf86, "Ollama", "ollama.exe"),
    // 装在 PATH 里（含 scoop/choco 那类，也覆盖"自己挪了位置并加了 PATH"）
    ...whichAll("ollama.exe"),
    ...whichAll("ollama"),
  ];
}

/** 我们要用的那个 ollama 可执行文件；没找到返回 null（不猜、不编路径）。 */
export function resolveBinary(): string | null {
  return candidates().find((candidate) => existsSync(candidate)) ?? null;
}

export type Owner = { managed: boolean; pid: number | null; binary: string | null };

export type Attempt = { ok: boolean; reason?: string };

export class Ollama {
  private child: ChildProcess | null = null;

  /** 归属：只说"是不是我们起的 + pid"，不说"在不在跑"。 */
  owner(): Owner {
    const pid = this.child?.pid ?? null;
    return { managed: pid !== null, pid, binary: resolveBinary() };
  }

  /** 拉起 `ollama serve`。端口上已经在跑（不管是哪个起的）就不重复起 —— 端口只有一个。 */
  async start(): Promise<Attempt> {
    if (this.child?.pid) return { ok: true }; // 我们起的那个还在
    if (await reachable(serviceUrl())) {
      return {
        ok: false,
        reason: `${serviceUrl()} 上已经有一个 Ollama 在跑（不是本应用起的），不再起一个。`,
      };
    }
    const bin = resolveBinary();
    if (!bin) {
      return {
        ok: false,
        reason: `没找到 ollama 可执行文件。装一份，或给壳设 ${BIN_ENV} 指到它。`,
      };
    }
    const child = spawn(bin, ["serve"], {
      detached: false,
      windowsHide: true,
      stdio: "ignore", // 它的日志归它自己的日志文件，不并进壳（那是后端日志才需要跨进程可查）
    });
    if (!child.pid) return { ok: false, reason: `启动 ${bin} 失败：拿不到 pid` };
    child.on("exit", () => {
      this.child = null;
    });
    this.child = child;
    return { ok: true };
  }

  /** 停我们 spawn 的那个（连子进程树：ollama 会派生 runner）。别人的不碰。 */
  stop(): Attempt {
    const pid = this.child?.pid;
    if (!pid) {
      return { ok: false, reason: "本应用没有起过 Ollama，所以不停任何进程。" };
    }
    if (process.platform === "win32") {
      spawn("taskkill", ["/PID", String(pid), "/T", "/F"], { stdio: "ignore" });
    } else {
      this.child?.kill("SIGTERM");
    }
    this.child = null;
    return { ok: true };
  }
}

/** 推理服务在不在这个端口上应答（壳自己的判定，只用来决定"要不要再起一个"）。 */
export function reachable(url: string, timeoutMs = 800): Promise<boolean> {
  return new Promise((resolve) => {
    let target: URL;
    try {
      target = new URL(url);
    } catch {
      resolve(false);
      return;
    }
    const socket = createConnection({
      host: target.hostname,
      port: Number(target.port) || 11434, // URL.port 是字符串，且空串时要回落到默认端口
    });
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
