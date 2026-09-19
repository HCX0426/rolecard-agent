// 本地推理服务（Ollama）状态与显存控制卡（设置→模型）。
//
// 为什么值得占模型页一栏：默认模型按 `keep_alive=-1` 常驻，**它自己永远不会让出显存**
// （8GB 卡上就是 5.8 GB）——要玩游戏、要换模型，只能敲 curl。这一张卡把"看一眼 + 按一下"
// 交回用户手里。Ollama 闲置 ~5min 会自己卸载，卸载后首条消息要冷加载十几~几十秒，所以
// "常驻"按钮与"释放显存"按钮是同一枚硬币的两面，必须同屏（分居两处就会各说一套状态）。
//
// 停止 / 启动服务两档**故意不在这里**：那是起停本机进程，属于桌面壳（里程碑 D）的职责。
import { useCallback, useEffect, useState } from "react";

import { api, type LocalServiceStatus } from "../api";

function gb(bytes: number): string {
  return `${(bytes / 1024 / 1024 / 1024).toFixed(1)} GB`;
}

export function LocalServiceCard() {
  const [status, setStatus] = useState<LocalServiceStatus | null>(null);
  const [busy, setBusy] = useState<"pin" | "unload" | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const load = useCallback(async () => {
    try {
      setStatus(await api.getLocalService());
    } catch {
      /* 拉不到就静默不显示：模型页主功能是后端 CRUD，不被这张附属卡片干扰 */
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function act(kind: "pin" | "unload") {
    setBusy(kind);
    setMsg(null);
    try {
      if (kind === "pin") {
        const r = await api.pinLocalModel();
        setMsg({ ok: true, text: `已把 ${r.model} 常驻显存（不再 5 分钟自动卸载）。` });
      } else {
        const r = await api.unloadLocalModel();
        const left = r.skipped.length ? `（未释放：${r.skipped.join("、")}）` : "";
        setMsg(
          r.unloaded.length
            ? { ok: true, text: `已释放 ${r.unloaded.join("、")}${left}。` }
            : { ok: true, text: "本地没有驻留的模型，无需释放。" },
        );
      }
    } catch (e) {
      // 失败句子由后端给（502 的 detail 说清了"连不上"还是"加载不了/多为显存不足"）。
      setMsg({ ok: false, text: (e as Error).message });
    } finally {
      setBusy(null);
      await load();
    }
  }

  if (!status) return null;
  const names = status.resident.map((m) => (m.name ?? "").split(":")[0]);
  const defaultMissing =
    status.is_local && !!status.model && !names.includes(status.model.split(":")[0]);
  const line = !status.running
    ? "未运行：本机 Ollama 没起来（或地址配错），对话会失败"
    : status.resident.length === 0
      ? "在跑，但显存里没有模型 —— 下一条消息会冷加载（较慢）"
      : `${status.resident
          .map((m) => `${m.name} ${gb(m.size_bytes)}${m.pinned ? "（常驻）" : ""}`)
          .join("、")} · 合计 ${gb(status.resident_bytes)}${
          defaultMissing ? ` · 默认 ${status.model} 不在显存` : ""
        }`;

  return (
    <section className="mb-4 rounded-lg border border-slate-200 p-3 dark:border-slate-700">
      <div className="flex items-center justify-between gap-2">
        <div className="min-w-0">
          <h3 className="flex items-center gap-2 text-sm font-medium text-slate-900 dark:text-slate-100">
            <span
              className={`inline-block h-2 w-2 shrink-0 rounded-full ${
                status.running ? "bg-emerald-500" : "bg-slate-400 dark:bg-slate-600"
              }`}
            />
            本地推理服务
            <span className="truncate font-mono text-[11px] font-normal text-slate-400">
              {status.base_url}
            </span>
          </h3>
          <p className="truncate text-[11px] text-slate-500 dark:text-slate-400">{line}</p>
        </div>
        <div className="flex shrink-0 gap-2">
          {status.is_local && (
            <button
              onClick={() => void act("pin")}
              disabled={busy !== null}
              className="rounded-lg border border-blue-300 px-3 py-1.5 text-xs text-blue-700 hover:bg-blue-50 disabled:opacity-50 dark:border-blue-700 dark:text-blue-300 dark:hover:bg-blue-900/30"
            >
              {busy === "pin" ? "载入中…（可能十几秒）" : "预热 / 常驻默认模型"}
            </button>
          )}
          {status.resident.length > 0 && (
            <button
              onClick={() => void act("unload")}
              disabled={busy !== null}
              className="rounded-lg border border-amber-300 px-3 py-1.5 text-xs text-amber-700 hover:bg-amber-50 disabled:opacity-50 dark:border-amber-700 dark:text-amber-300 dark:hover:bg-amber-900/30"
            >
              {busy === "unload" ? "释放中…" : "释放显存（卸载模型）"}
            </button>
          )}
        </div>
      </div>
      {msg && (
        <p
          className={`mt-2 rounded px-2 py-1 text-[11px] ${
            msg.ok
              ? "bg-emerald-50 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300"
              : "bg-red-50 text-red-600 dark:bg-red-900/30 dark:text-red-400"
          }`}
        >
          {msg.text}
        </p>
      )}
    </section>
  );
}
