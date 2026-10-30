// 「主动开口」总闸条（2026-10-30 从记忆页签搬来，由角色卡页托管）。
//
// 为什么搬家：用户 2026-10-30「记忆页签里的主动开口，就一个设置，直接也放进角色卡就不好了」
// —— per-role 的开口开关（reachout_enabled / 关系驱动三档 / 定时问候）本来就长在每张
// 角色卡的编辑区里；记忆页签那张卡管的是**全局总闸**与**收件箱折叠窗口**，它们跟记忆
// 没有关系，放在记忆页签里等于让"记忆"这一页管着不属于它的事。
//
// 总闸的语义没变（runtime 覆盖 `reachout_enabled`，保存即热生效）：
//   关 = 所有角色都不会主动找你（静默，无提示音）；
//   开 = 也只有角色卡上勾了「角色会主动找你」的角色才会开口。
// 折叠窗口（`reachout_merge_days`）是收件箱的显示口径：同一角色在一个窗口里的开口折成一行。
import { useCallback, useEffect, useState } from "react";
import { api, type RuntimePayload } from "../api";
import { describeError } from "../lib/errors";

export function ReachoutMasterCard() {
  const [reachoutOn, setReachoutOn] = useState<boolean | null>(null);
  const [mergeDays, setMergeDays] = useState<number | null>(null);
  const [mergeChoices, setMergeChoices] = useState<string[]>(["1", "3", "7"]);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");

  const load = useCallback(async () => {
    const rt = await api.get<RuntimePayload>("/api/settings/runtime");
    const items = (rt.groups ?? []).flatMap((g) => g.items ?? []);
    const sw = items.find((i) => i.field === "reachout_enabled");
    setReachoutOn(sw ? sw.value !== "0" && sw.value !== "未设置" : null);
    const merge = items.find((i) => i.field === "reachout_merge_days");
    setMergeDays(merge ? Number.parseInt(merge.value ?? "", 10) || 1 : null);
    if (merge?.choices && merge.choices.length > 0) setMergeChoices(merge.choices);
  }, []);

  useEffect(() => {
    load().catch(() => setReachoutOn(null));
  }, [load]);

  async function toggle() {
    setErr("");
    setMsg("");
    if (reachoutOn === null) return;
    try {
      await api.put<RuntimePayload>("/api/settings/runtime", {
        values: { reachout_enabled: reachoutOn ? "0" : "1" },
      });
      setReachoutOn(!reachoutOn);
      setMsg(
        reachoutOn ? "已关闭：所有角色都不会主动找你" : "已开启：还要在角色卡里勾「角色会主动找你」",
      );
    } catch (e) {
      setErr(`保存失败：${describeError(e)}`);
    }
  }

  async function changeMergeDays(next: string) {
    setErr("");
    setMsg("");
    try {
      await api.put<RuntimePayload>("/api/settings/runtime", {
        values: { reachout_merge_days: next },
      });
      setMergeDays(Number.parseInt(next, 10));
      setMsg(`收件箱已改为每 ${next} 天一摞`);
    } catch (e) {
      setErr(`保存失败：${describeError(e)}`);
    }
  }

  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-lg border border-slate-200 bg-white/60 px-3 py-2 text-xs dark:border-slate-700 dark:bg-slate-800/40">
      <span className="text-slate-600 dark:text-slate-300">主动开口</span>
      <button
        onClick={() => void toggle()}
        disabled={reachoutOn === null}
        aria-pressed={reachoutOn === null ? undefined : reachoutOn}
        className={`rounded-full px-3 py-1 text-xs font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${
          reachoutOn
            ? "bg-blue-600 text-white"
            : "bg-slate-200 text-slate-500 dark:bg-slate-700 dark:text-slate-300"
        }`}
      >
        {reachoutOn === null ? "…" : reachoutOn ? "总闸已开启" : "总闸已关闭"}
      </button>
      <span className="min-w-0 flex-1 text-11 text-slate-400 dark:text-slate-500">
        关了之后谁都不会来找你；开了之后也只有角色卡里勾了「会主动找你」的角色会开口。
      </span>
      <label className="flex shrink-0 items-center gap-1.5 text-11 text-slate-500 dark:text-slate-400">
        收件箱
        <select
          value={String(mergeDays ?? 1)}
          disabled={mergeDays === null}
          onChange={(e) => void changeMergeDays(e.target.value)}
          className="rounded border border-slate-200 bg-white px-1.5 py-0.5 text-11 dark:border-slate-700 dark:bg-slate-800"
        >
          {mergeChoices.map((c) => (
            <option key={c} value={c}>
              每 {c} 天一摞
            </option>
          ))}
        </select>
      </label>
      {msg && <span className="text-emerald-600 dark:text-emerald-400">{msg}</span>}
      {err && <span className="text-red-600 dark:text-red-400">{err}</span>}
    </div>
  );
}
