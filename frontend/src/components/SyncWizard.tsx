/**
 * 上行同步的四屏（M7，设计稿 `build/upload_mock.png` 09-27 拍板）。
 *
 * ① 询问卡（三档，默认「逐条合并」）→ ② 预检 dry-run（先给数，再要同意，最后才动手）
 * → ③ 冲突逐条裁决 → ④ 完成页。
 *
 * 三件在设计阶段就定死、代码不许自己发挥的事：
 *
 *   * **「这次先不带」是一个正经选项**。换身份与上行是两件事，点了登录不等于同意上传；
 *     选了它，界面照常进云端态，本机那份原地不动。
 *   * **③ 那一屏刻意没有"全按本机的来"的按钮**。那等于把这一屏变成一个确认框，而它存在的
 *     全部理由就是"这几条不一样，机器不该替你决定"。跳过 = 对面不动，本机那条也不删
 *     （下次上行还会再问）。
 *   * **④ 那三句必须说**：带过去了什么（可核对的数）、**向量索引是重建的不是搬的**
 *     （否则以后检索质量对不上没人知道为什么）、**本机那份没动**（上行是复制不是搬家）。
 *
 * 健康档案与上传原件这一版**不给复选框**（用户 09-27：「第二批先不需要传吧」）——
 * 没有实现的复选框比没有复选框更坏。
 */
import { useState } from "react";

import { ApiError } from "../api";
import { read as readDataSource } from "../lib/dataSource";
import {
  SYNC_ITEMS,
  UPLOAD_MODES,
  applyUpload,
  canKeepBoth,
  conflictKey,
  fetchPlan,
  planReads,
  uploadTarget,
  type ApplyResult,
  type Plan,
  type SyncKind,
  type UploadMode,
} from "../lib/sync";

type Step = "ask" | "dry" | "conflict" | "done";

const ALL_KINDS = SYNC_ITEMS.map((i) => i.kind);

export default function SyncWizard({ onClose }: { onClose: () => void }) {
  const [step, setStep] = useState<Step>("ask");
  const [mode, setMode] = useState<UploadMode>("merge");
  const [kinds, setKinds] = useState<SyncKind[]>(ALL_KINDS);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [resolutions, setResolutions] = useState<Record<string, string>>({});
  const [which, setWhich] = useState(0);
  const [result, setResult] = useState<ApplyResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [why, setWhy] = useState("");

  const target = uploadTarget();
  const cloud = readDataSource();
  const where = cloud.mode === "cloud" ? `${cloud.base} · ${cloud.user}` : "";
  const undecided = plan
    ? plan.conflicts.filter((c) => resolutions[conflictKey(c.kind, c.ident)] === undefined).length
    : 0;

  async function lookAtDiff() {
    if (!target) return;
    setBusy(true);
    setWhy("");
    try {
      setPlan(await fetchPlan(target));
      setStep("dry");
    } catch (e) {
      setWhy(errText(e));
    } finally {
      setBusy(false);
    }
  }

  async function start() {
    if (!target) return;
    setBusy(true);
    setWhy("");
    try {
      setResult(await applyUpload(target, kinds, mode, resolutions));
      setStep("done");
    } catch (e) {
      setWhy(errText(e));
    } finally {
      setBusy(false);
    }
  }

  function decide(choice: string) {
    const conflict = plan?.conflicts[which];
    if (!conflict) return;
    setResolutions({ ...resolutions, [conflictKey(conflict.kind, conflict.ident)]: choice });
    // 挑完最后一条就自己回到预检那一屏：停在"冲突都挑完了"的空屏上只是多要一次点击，
    // 而那一屏本来就没有任何信息。
    if (which + 1 >= plan.conflicts.length) setStep("dry");
    else setWhich(which + 1);
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4"
      role="dialog"
      aria-modal="true"
      aria-label="把本机这份带到云端"
    >
      <div className="max-h-[92vh] w-full max-w-lg overflow-y-auto rounded-xl bg-white p-5 shadow-2xl dark:bg-slate-800">
        {step === "ask" && (
          <AskScreen
            where={where}
            mode={mode}
            onMode={setMode}
            onCancel={onClose}
            onNext={() => void lookAtDiff()}
            busy={busy}
            why={why}
          />
        )}
        {step === "dry" && plan && (
          <DryScreen
            plan={plan}
            kinds={kinds}
            onKinds={setKinds}
            mode={mode}
            undecided={undecided}
            onBack={() => setStep("ask")}
            onResolve={() => {
              setWhich(0);
              setStep("conflict");
            }}
            onStart={() => void start()}
            busy={busy}
            why={why}
          />
        )}
        {step === "conflict" && plan && (
          // key：换一条就把那一屏的本地选择状态重挂掉。留着会把上一条的选择带到下一条上，
          // 症状是"我明明挑了对面，推过去的却是本机那份"。
          <ConflictScreen
            key={which}
            plan={plan}
            which={which}
            onBack={() => setStep("dry")}
            onDecide={decide}
          />
        )}
        {step === "done" && result && <DoneScreen result={result} onDone={onClose} />}
      </div>
    </div>
  );
}

function errText(e: unknown): string {
  return e instanceof ApiError ? e.message : (e as Error).message || "出错了";
}

function Title({ children }: { children: React.ReactNode }) {
  return (
    <h2 className="text-[15px] font-semibold text-slate-900 dark:text-slate-100">{children}</h2>
  );
}

function Hint({ children }: { children: React.ReactNode }) {
  return <p className="mt-1 text-xs leading-relaxed text-slate-500 dark:text-slate-400">{children}</p>;
}

function AskScreen({
  where,
  mode,
  onMode,
  onCancel,
  onNext,
  busy,
  why,
}: {
  where: string;
  mode: UploadMode;
  onMode: (m: UploadMode) => void;
  onCancel: () => void;
  onNext: () => void;
  busy: boolean;
  why: string;
}) {
  return (
    <>
      <Title>要把本机这份带过去吗？</Title>
      <Hint>
        云端是另一份完整数据集。你现在连的是 <b>{where}</b>，它已经有一些东西了 ——
        所以下面这一档会影响两边怎么并。
      </Hint>
      <div className="mt-3 space-y-2">
        {UPLOAD_MODES.map((m) => (
          <label
            key={m.mode}
            className={`block rounded-lg border p-3 text-[11px] ${
              mode === m.mode
                ? "border-blue-500 bg-blue-50/60 dark:bg-blue-900/20"
                : "border-slate-200 dark:border-slate-600"
            }`}
          >
            <span className="flex items-center gap-2">
              <input
                type="radio"
                name="upload-mode"
                checked={mode === m.mode}
                onChange={() => onMode(m.mode)}
                className="h-3.5 w-3.5"
              />
              <b className="text-[12px] text-slate-800 dark:text-slate-100">{m.label}</b>
              <span
                className={`rounded px-1.5 py-0.5 text-[10px] ${
                  m.mode === "replace"
                    ? "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-200"
                    : "bg-slate-100 text-slate-500 dark:bg-slate-700 dark:text-slate-300"
                }`}
              >
                {m.note}
              </span>
            </span>
            <span className="mt-1 block leading-relaxed text-slate-500 dark:text-slate-400">
              {m.cost}
            </span>
          </label>
        ))}
      </div>
      {why && <ErrorBox>{why}</ErrorBox>}
      <div className="mt-3 flex items-center justify-end gap-2">
        <button
          onClick={onCancel}
          className="px-2 py-2 text-xs text-slate-500 hover:text-slate-700 dark:text-slate-400"
        >
          这次先不带
        </button>
        <button
          onClick={onNext}
          disabled={busy}
          className="rounded-lg bg-blue-600 px-3.5 py-2 text-xs text-white disabled:opacity-50"
        >
          {busy ? "比对中…" : "下一步：看差异"}
        </button>
      </div>
      <Hint>
        「这次先不带」是正经选项：换身份与上行是两件事，点了登录不等于同意上传。
        选了它，界面就正常进云端态，本机那份原地不动。
      </Hint>
    </>
  );
}

function ErrorBox({ children }: { children: React.ReactNode }) {
  return (
    <p className="mt-3 rounded-lg border border-rose-200 bg-rose-50 p-2.5 text-[11px] leading-relaxed text-rose-800 dark:border-rose-700 dark:bg-rose-900/30 dark:text-rose-200">
      {children}
    </p>
  );
}

function DryScreen({
  plan,
  kinds,
  onKinds,
  mode,
  undecided,
  onBack,
  onResolve,
  onStart,
  busy,
  why,
}: {
  plan: Plan;
  kinds: SyncKind[];
  onKinds: (k: SyncKind[]) => void;
  mode: UploadMode;
  undecided: number;
  onBack: () => void;
  onResolve: () => void;
  onStart: () => void;
  busy: boolean;
  why: string;
}) {
  const going = kinds.reduce((n, k) => {
    const bucket = plan.by_kind[k] ?? {};
    return n + (mode === "replace" ? totalOf(bucket) : (bucket.only_local ?? 0));
  }, 0);
  return (
    <>
      <Title>差异看完了</Title>
      <Hint>这一步什么都没写。数字来自两边的实际比对，不是估算。</Hint>
      <div className="mt-3 grid grid-cols-4 gap-2">
        {planReads(plan).map((r) => (
          <div
            key={r.label}
            className={`rounded-lg border p-2.5 ${
              r.tone === "warn" && r.value > 0
                ? "border-rose-200 bg-rose-50 dark:border-rose-700 dark:bg-rose-900/30"
                : "border-slate-200 dark:border-slate-600"
            }`}
          >
            <div className="text-lg font-semibold text-slate-900 dark:text-slate-100">{r.value}</div>
            <div className="text-[10px] leading-tight text-slate-500 dark:text-slate-400">
              {r.label}
            </div>
          </div>
        ))}
      </div>
      <table className="mt-3 w-full text-[11px]">
        <tbody>
          {SYNC_ITEMS.map((item) => {
            const bucket = plan.by_kind[item.kind] ?? {};
            return (
              <tr key={item.kind} className="border-t border-slate-100 dark:border-slate-700">
                <td className="w-6 py-2 align-top">
                  <input
                    type="checkbox"
                    checked={kinds.includes(item.kind)}
                    aria-label={`同步${item.label}`}
                    onChange={(e) =>
                      onKinds(
                        e.target.checked
                          ? [...kinds, item.kind]
                          : kinds.filter((k) => k !== item.kind),
                      )
                    }
                    className="mt-0.5 h-3.5 w-3.5"
                  />
                </td>
                <td className="py-2 align-top text-slate-700 dark:text-slate-200">{item.label}</td>
                <td className="py-2 align-top text-slate-500 dark:text-slate-400">
                  本机 {totalOf(bucket)} 条
                  {bucket.only_local ? ` · 独有 ${bucket.only_local}` : ""}
                  {bucket.conflicts ? ` · 冲突 ${bucket.conflicts}` : ""}
                  {bucket.skipped ? ` · 跳过 ${bucket.skipped}` : ""}
                </td>
                <td className="py-2 text-right align-top text-[10px] text-slate-400 dark:text-slate-500">
                  {item.hint}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {plan.skipped.length > 0 && (
        <p className="mt-2 rounded-lg border border-amber-200 bg-amber-50 p-2.5 text-[11px] leading-relaxed text-amber-800 dark:border-amber-700 dark:bg-amber-900/30 dark:text-amber-200">
          有 {plan.skipped.length} 条整条跳过，不搬也不猜：
          {plan.skipped.slice(0, 3).map((s) => (
            <span key={`${s.kind}${s.ident}`} className="block">
              · {s.preview || s.ident} —— {s.reason}
            </span>
          ))}
          {plan.skipped.length > 3 && (
            <span className="block">…另外 {plan.skipped.length - 3} 条同因</span>
          )}
        </p>
      )}
      {why && <ErrorBox>{why}</ErrorBox>}
      <Hint>
        不勾的类不会碰对面 —— 那些就用云端上已有的那一份。
        {mode === "merge" && plan.conflicts.length > 0 && (
          <>
            {" "}
            冲突 {plan.conflicts.length} 条里还有 <b>{undecided} 条没挑</b>；
            没挑的按对面那份留着（上行不该顺手覆盖别人已经写好的）。
          </>
        )}
      </Hint>
      <div className="mt-3 flex items-center justify-end gap-2">
        <button
          onClick={onBack}
          className="rounded-lg border border-slate-200 px-3.5 py-2 text-xs text-slate-600 dark:border-slate-600 dark:text-slate-300"
        >
          返回
        </button>
        {mode === "merge" && plan.conflicts.length > 0 && (
          <button
            onClick={onResolve}
            className="rounded-lg border border-blue-300 px-3.5 py-2 text-xs text-blue-700 dark:border-blue-600 dark:text-blue-300"
          >
            先处理 {plan.conflicts.length} 条冲突
          </button>
        )}
        <button
          onClick={onStart}
          disabled={busy || kinds.length === 0}
          className="rounded-lg bg-blue-600 px-3.5 py-2 text-xs text-white disabled:opacity-50"
        >
          {busy ? "上行中…" : `开始上行（${going} 项）`}
        </button>
      </div>
    </>
  );
}

function totalOf(bucket: Record<string, number>): number {
  return (bucket.only_local ?? 0) + (bucket.same ?? 0) + (bucket.conflicts ?? 0);
}

function ConflictScreen({
  plan,
  which,
  onBack,
  onDecide,
}: {
  plan: Plan;
  which: number;
  onBack: () => void;
  onDecide: (choice: string) => void;
}) {
  const [choice, setChoice] = useState("theirs");
  const conflict = plan.conflicts[which];
  if (!conflict) return null; // 调用方挑完最后一条就切走了，这一屏不会拿着越界的下标渲染
  const label = SYNC_ITEMS.find((i) => i.kind === conflict.kind)?.label ?? conflict.kind;
  const last = which + 1 >= plan.conflicts.length;
  return (
    <>
      <Title>
        冲突 {which + 1} / {plan.conflicts.length} · 一条{label}
      </Title>
      <Hint>
        {canKeepBoth(conflict.kind)
          ? "同一条事实（uid 相同）两边内容不一样。选一个，或两份都留。"
          : "同一个身份两边内容不一样。这一类的身份就是那一个 id，所以“都留”讲不通 —— 挑一份。"}
      </Hint>
      <div className="mt-3 grid grid-cols-2 gap-2 text-[11px]">
        <Preview title="本机这份" at={conflict.mine.at} text={conflict.mine.preview} />
        <Preview title="对面那份" at={conflict.theirs.at} text={conflict.theirs.preview} />
      </div>
      <div className="mt-3 space-y-2 text-[11px]">
        <Radio value={choice} onChange={setChoice} name="theirs" title="保留对面那份"
          note="默认。对面那条不动，本机的这条也不会被推过去。" />
        <Radio value={choice} onChange={setChoice} name="mine" title="保留本机这份"
          note="对面那条按本机这份改写。" />
        {canKeepBoth(conflict.kind) && (
          <Radio value={choice} onChange={setChoice} name="both" title="两份都留"
            note="不判断谁对：对面那条原样留着，本机这条换一枚新身份多出来。以后可以在「整理记忆」里再分开。" />
        )}
      </div>
      <div className="mt-3 flex items-center justify-end gap-2">
        <button
          onClick={onBack}
          className="px-2 py-2 text-xs text-slate-500 hover:text-slate-700 dark:text-slate-400"
        >
          先不挑了
        </button>
        <button
          onClick={() => onDecide("theirs")}
          className="rounded-lg border border-slate-200 px-3.5 py-2 text-xs text-slate-600 dark:border-slate-600 dark:text-slate-300"
        >
          跳过这条（保持对面原样）
        </button>
        <button
          onClick={() => onDecide(choice)}
          className="rounded-lg bg-blue-600 px-3.5 py-2 text-xs text-white"
        >
          {last ? "就这么定" : "下一条"}
        </button>
      </div>
      <Hint>
        这里刻意没有"全按本机的来"的按钮：那等于把这一屏变成一个确认框，而这一屏存在的全部理由
        就是这几条不一样、机器不该替你决定。跳过 = 对面不动，本机那条也不删（下次上行还会再问）。
      </Hint>
    </>
  );
}

function Preview({ title, at, text }: { title: string; at: string; text: string }) {
  return (
    <div className="rounded-lg border border-slate-200 p-2.5 dark:border-slate-600">
      <div className="text-[10px] text-slate-400 dark:text-slate-500">
        {title}
        {at ? ` · ${at.slice(0, 10)} 写下` : ""}
      </div>
      <div className="mt-1 leading-relaxed text-slate-700 dark:text-slate-200">
        {text || "（空）"}
      </div>
    </div>
  );
}

function Radio({
  value,
  onChange,
  name,
  title,
  note,
}: {
  value: string;
  onChange: (v: string) => void;
  name: string;
  title: string;
  note: string;
}) {
  return (
    <label
      className={`block rounded-lg border p-2.5 ${
        value === name
          ? "border-blue-500 bg-blue-50/60 dark:bg-blue-900/20"
          : "border-slate-200 dark:border-slate-600"
      }`}
    >
      <span className="flex items-center gap-2">
        <input
          type="radio"
          name="conflict-choice"
          checked={value === name}
          onChange={() => onChange(name)}
          className="h-3.5 w-3.5"
        />
        <b className="text-[12px] text-slate-800 dark:text-slate-100">{title}</b>
      </span>
      <span className="mt-0.5 block leading-relaxed text-slate-500 dark:text-slate-400">{note}</span>
    </label>
  );
}

function DoneScreen({ result, onDone }: { result: ApplyResult; onDone: () => void }) {
  const written = result.remote.written ?? {};
  const detail = SYNC_ITEMS.filter((i) => written[i.kind]).map(
    (i) => `${i.label} ${written[i.kind]}`,
  );
  const errors = result.remote.errors ?? [];
  const skipped = Object.values(result.remote.skipped ?? {}).reduce((a, b) => a + b, 0);
  return (
    <>
      <Title>上行完成</Title>
      <div className="mt-2 h-1 w-full rounded bg-emerald-500" />
      <p className="mt-3 text-xs leading-relaxed text-slate-600 dark:text-slate-300">
        <b className="text-emerald-700 dark:text-emerald-300">已带过去 {result.sent} 项</b>
        {detail.length > 0 && <>（{detail.join(" · ")}）</>}
      </p>
      <Hint>
        对面收了 {Object.values(written).reduce((a, b) => a + b, 0)} 项；跳过 {skipped} 项
        （已经有的一律不重复写）。
      </Hint>
      <p className="mt-2 text-xs leading-relaxed text-slate-600 dark:text-slate-300">
        <b className="text-amber-700 dark:text-amber-300">向量索引没有搬</b> ——
        对面那份正在按它自己的嵌入后端重建。两台的嵌入模型一旦不同，向量之间本来就不可比。
      </p>
      <p className="mt-2 text-xs leading-relaxed text-slate-600 dark:text-slate-300">
        本机这一份<b className="text-emerald-700 dark:text-emerald-300">一个字都没改</b>。
        切回「本机」时它还在原地 —— 上行是复制，不是搬家。
      </p>
      {errors.length > 0 && (
        <ErrorBox>
          有 {errors.length} 条对面没收下：
          {errors.slice(0, 4).map((e) => (
            <span key={`${e.kind}${e.ident}`} className="block">
              · {e.kind} {e.ident} —— {e.error}
            </span>
          ))}
        </ErrorBox>
      )}
      <div className="mt-3 flex justify-end">
        <button
          onClick={onDone}
          className="rounded-lg bg-blue-600 px-3.5 py-2 text-xs text-white"
        >
          知道了
        </button>
      </div>
    </>
  );
}
