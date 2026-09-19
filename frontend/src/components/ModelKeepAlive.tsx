// 本地模型"预热 / 常驻"卡片（设置→模型）。Ollama 闲置 ~5min 会卸载模型，下一条消息要冷加载
// （8GB 卡上十几~几十秒）→ 体感"很慢/连不上"。这里把默认模型按 keep_alive:-1 载入常驻。
// 云端后端无 keep_alive 概念 → 组件自动隐藏。加载真可能失败（显存不够），如实反馈不粉饰。
import { useCallback, useEffect, useState } from "react";

import { api, type ResidentInfo } from "../api";

function fmtSize(bytes: number | null): string {
  if (!bytes) return "";
  return ` ${(bytes / 1024 / 1024 / 1024).toFixed(1)} GB`;
}

export function ModelKeepAlive() {
  const [info, setInfo] = useState<ResidentInfo | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const load = useCallback(async () => {
    try {
      setInfo(await api.get<ResidentInfo>("/api/settings/models/resident"));
    } catch {
      /* 拉不到就静默不显示（模型页主功能是后端 CRUD，不被这个附属卡片干扰） */
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function warm() {
    if (!info) return;
    setBusy(true);
    setMsg(null);
    try {
      const r = await api.post<{ ok: boolean; model: string }>("/api/settings/models/keepalive", {
        keep_alive: -1,
      });
      setMsg({
        ok: r.ok,
        text: r.ok
          ? `已把 ${r.model} 常驻显存（不再 5 分钟自动卸载）。`
          : "常驻失败：多半是显存不足或模型未安装，请查看 Ollama 状态。",
      });
      await load();
    } catch (e) {
      setMsg({ ok: false, text: `请求失败：${(e as Error).message}` });
    } finally {
      setBusy(false);
    }
  }

  if (!info || !info.is_local) return null;
  const resident = info.loaded.some((m) => (m.name ?? "").split(":")[0] === info.model.split(":")[0]);

  return (
    <section className="mb-4 rounded-lg border border-slate-200 p-3 dark:border-slate-700">
      <div className="flex items-center justify-between gap-2">
        <div className="min-w-0">
          <h3 className="text-sm font-medium text-slate-900 dark:text-slate-100">本地模型常驻</h3>
          <p className="truncate text-[11px] text-slate-500 dark:text-slate-400" title={info.model}>
            默认 <code className="font-mono">{info.model}</code>
            {resident
              ? ` · 已常驻${fmtSize(info.loaded.find((m) => (m.name ?? "").split(":")[0] === info.model.split(":")[0])?.size ?? null)}`
              : " · 当前未常驻（首条消息会冷加载，较慢）"}
          </p>
        </div>
        <button
          onClick={warm}
          disabled={busy}
          className="shrink-0 rounded-lg border border-blue-300 px-3 py-1.5 text-xs text-blue-700 hover:bg-blue-50 disabled:opacity-50 dark:border-blue-700 dark:text-blue-300 dark:hover:bg-blue-900/30"
        >
          {busy ? "载入中…（可能十几秒）" : "预热 / 常驻默认模型"}
        </button>
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
      {info.loaded.length > 0 && (
        <p className="mt-1 text-[11px] text-slate-400 dark:text-slate-500">
          显存中：{info.loaded.map((m) => `${m.name}${fmtSize(m.size ?? null)}`).join("、")}
        </p>
      )}
    </section>
  );
}
