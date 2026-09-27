/**
 * 同步向导（M7 四屏 + M8 方向档，文案 09-27 深夜定稿）。
 *
 * 三件在设计阶段就定死、代码不许自己发挥的事：
 *
 *   * **「暂不同步」是一个正经选项**。换身份与同步是两件事，登录不等于同意传输；
 *     选了它，界面照常进云端态，两边数据原地不动。
 *   * **裁决屏刻意没有"全部按一侧"的按钮**。那等于把这一屏变成确认框，而它存在的
 *     全部理由就是"这几个条目两端均有修改，机器不该替您决定"。跳过 = 该项保持现状
 *     （未确认的冲突将保留接收侧现有版本），下次同步仍会提示。
 *   * **完成页那两句必须说**：向量索引未随数据迁移（云端基于其嵌入服务自行重建，
 *     否则以后检索质量对不上没人知道为什么）、本机数据未做任何修改（同步是复制不是迁移）。
 *
 * 「整份替换」只在上传方向提供：下载方向的整份替换会以云端数据覆盖本机全部数据，
 * 那一步应由用户逐项执行删除，不作为同步档位提供（界面上以禁用态呈现并说明）。
 */
import { useState } from "react";

import { ApiError } from "../api";
import { read as readDataSource } from "../lib/dataSource";
import {
  DIRECTIONS,
  SYNC_ITEMS,
  UPLOAD_MODES,
  applyUpload,
  canKeepBoth,
  conflictKey,
  fetchPlan,
  planReads,
  pullDownload,
  uploadTarget,
  type Direction,
  type LeftConflict,
  type Plan,
  type PullResult,
  type SyncKind,
  type UploadMode,
} from "../lib/sync";

type Step = "ask" | "dry" | "conflict" | "done";

const ALL_KINDS = SYNC_ITEMS.map((i) => i.kind);

export default function SyncWizard({
  onClose,
  initialConflicts,
}: {
  onClose: () => void;
  /** 登录对账留下的未决冲突：向导直接落在裁决屏，走"两端各按选择搬一次"的合成档。 */
  initialConflicts?: LeftConflict[];
}) {
  const [step, setStep] = useState<Step>(initialConflicts ? "conflict" : "ask");
  const [direction, setDirection] = useState<Direction>("up");
  const [mode, setMode] = useState<UploadMode>("merge");
  const [kinds, setKinds] = useState<SyncKind[]>(ALL_KINDS);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [resolutions, setResolutions] = useState<Record<string, string>>({});
  const [which, setWhich] = useState(0);
  const [busy, setBusy] = useState(false);
  const [why, setWhy] = useState("");
  const [done, setDone] = useState<{ up: number; down: number; skipped: number } | null>(null);

  const target = uploadTarget();
  const cloud = readDataSource();
  const where = cloud.mode === "cloud" ? `${cloud.base} · ${cloud.user}` : "";
  // 冲突两个方向看见的是同一批：裁决一次，按选择两端各搬各的。
  const conflicts: LeftConflict[] = plan
    ? plan.conflicts.map((c) => ({
        kind: c.kind,
        ident: c.ident,
        mine: { at: c.mine.at, preview: c.mine.preview },
        theirs: { at: c.theirs.at, preview: c.theirs.preview },
      }))
    : (initialConflicts ?? []);
  const undecided = conflicts.filter(
    (c) => resolutions[conflictKey(c.kind, c.ident)] === undefined,
  ).length;

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

  /** 裁决定稿后的执行：上传方向的归 apply，下载方向的归 pull，各跑各的。
   *  `override`：最后一条的裁决在 ConflictScreen 的本地状态里，父层还不知道 ——
   *  由它把定稿后的整张裁决表递上来，否则用户对最后一条的选择会被静默丢弃
   *  （实测：应用选择显示"完成"，实际两端什么都没搬）。 */
  async function commit(override?: Record<string, string>) {
    if (!target) return;
    setBusy(true);
    setWhy("");
    try {
      let up = 0;
      let down = 0;
      let skipped = 0;
      if (initialConflicts) {
        // 对账的未决项：按用户对每个版本的选择，两端各搬各的。
        const pushRes: Record<string, string> = {};
        const pullRes: Record<string, string> = {};
        for (const c of conflicts) {
          const key = conflictKey(c.kind, c.ident);
          const choice = (override ?? resolutions)[key];
          if (choice === "keepLocal") pushRes[key] = "mine";
          if (choice === "keepRemote") pullRes[key] = "theirs";
          if (choice === "keepBoth") {
            pushRes[key] = "both";
            pullRes[key] = "both";
          }
          if (!choice) skipped += 1;
        }
        const [upResult, downResult] = await Promise.all([
          Object.keys(pushRes).length ? applyUpload(target, ALL_KINDS, "merge", pushRes) : null,
          Object.keys(pullRes).length ? pullDownload(target, ALL_KINDS, pullRes) : null,
        ]);
        up = upResult?.sent ?? 0;
        down = downResult?.pulled ?? 0;
      } else if (direction === "up") {
        const r = await applyUpload(target, kinds, mode, toBackend(resolutions, "up"));
        up = r.sent;
        skipped = Object.values(r.remote?.skipped ?? {}).reduce((a, b) => a + b, 0);
      } else {
        const r: PullResult = await pullDownload(
          target,
          kinds,
          toBackend(resolutions, "down"),
        );
        down = r.pulled;
        skipped = Object.values(r.local?.skipped ?? {}).reduce((a, b) => a + b, 0);
      }
      setDone({ up, down, skipped });
      setStep("done");
    } catch (e) {
      setWhy(errText(e));
    } finally {
      setBusy(false);
    }
  }

  function decide(choice: string) {
    const conflict = conflicts[which];
    if (!conflict) return;
    const next = { ...resolutions, [conflictKey(conflict.kind, conflict.ident)]: choice };
    setResolutions(next);
    // 挑完最后一条就自己回上一屏：停在"都挑完了"的空屏上只是多要一次点击。
    if (which + 1 >= conflicts.length) {
      if (initialConflicts) void commit(next);
      else setStep("dry");
    } else setWhich(which + 1);
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4"
      role="dialog"
      aria-modal="true"
      aria-label="数据同步"
    >
      <div className="max-h-[92vh] w-full max-w-lg overflow-y-auto rounded-xl bg-white p-5 shadow-2xl dark:bg-slate-800">
        {step === "ask" && (
          <AskScreen
            where={where}
            direction={direction}
            onDirection={setDirection}
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
            direction={direction}
            kinds={kinds}
            onKinds={setKinds}
            mode={mode}
            undecided={undecided}
            onBack={() => setStep("ask")}
            onResolve={() => {
              setWhich(0);
              setStep("conflict");
            }}
            onStart={() => void commit()}
            busy={busy}
            why={why}
          />
        )}
        {step === "conflict" && conflicts.length > 0 && (
          // key：换一条就把那一屏的本地选择状态重挂掉。留着会把上一条的选择带到下一条上，
          // 症状是"我明明选了保留云端版本，推过去的却是本机那份"。
          <ConflictScreen
            key={which}
            conflicts={conflicts}
            which={which}
            direction={initialConflicts ? "reconcile" : direction}
            onBack={() => setStep(initialConflicts ? "done" : "dry")}
            onDecide={decide}
            busy={busy}
          />
        )}
        {step === "done" && done && <DoneScreen done={done} onDone={onClose} />}
      </div>
    </div>
  );
}

/** 界面语义（保留哪一版）→ 后端语义（mine/theirs/both）。没选中的键直接不发：
 *  后端对缺失键的默认是"保留接收侧"，与界面里"跳过此项"的承诺一致。 */
function toBackend(
  resolutions: Record<string, string>,
  direction: Direction,
): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [key, choice] of Object.entries(resolutions)) {
    if (direction === "up") {
      if (choice === "keepLocal") out[key] = "mine";
      if (choice === "keepBoth") out[key] = "both";
    } else {
      if (choice === "keepRemote") out[key] = "theirs";
      if (choice === "keepBoth") out[key] = "both";
    }
  }
  return out;
}

function errText(e: unknown): string {
  return e instanceof ApiError ? e.message : (e as Error).message || "同步失败";
}

function Title({ children }: { children: React.ReactNode }) {
  return (
    <h2 className="text-[15px] font-semibold text-slate-900 dark:text-slate-100">{children}</h2>
  );
}

function Hint({ children }: { children: React.ReactNode }) {
  return <p className="mt-1 text-xs leading-relaxed text-slate-500 dark:text-slate-400">{children}</p>;
}

function ErrorBox({ children }: { children: React.ReactNode }) {
  return (
    <p className="mt-3 rounded-lg border border-rose-200 bg-rose-50 p-2.5 text-[11px] leading-relaxed text-rose-800 dark:border-rose-700 dark:bg-rose-900/30 dark:text-rose-200">
      {children}
    </p>
  );
}

function AskScreen({
  where,
  direction,
  onDirection,
  mode,
  onMode,
  onCancel,
  onNext,
  busy,
  why,
}: {
  where: string;
  direction: Direction;
  onDirection: (d: Direction) => void;
  mode: UploadMode;
  onMode: (m: UploadMode) => void;
  onCancel: () => void;
  onNext: () => void;
  busy: boolean;
  why: string;
}) {
  return (
    <>
      <Title>数据同步</Title>
      <Hint>
        云端是另一份完整数据集。当前连接 <b>{where}</b>，选择同步方向与合并方式。
      </Hint>
      <div
        className="mt-3 flex rounded-lg border border-slate-200 p-1 dark:border-slate-600"
        role="radiogroup"
        aria-label="同步方向"
      >
        {DIRECTIONS.map((d) => (
          <button
            key={d.direction}
            onClick={() => onDirection(d.direction)}
            aria-pressed={direction === d.direction}
            className={`flex-1 rounded-md px-3 py-1.5 text-xs ${
              direction === d.direction
                ? "bg-blue-600 text-white"
                : "text-slate-600 dark:text-slate-300"
            }`}
          >
            {d.label}
            <span className="ml-1 text-[10px] opacity-70">{d.sub}</span>
          </button>
        ))}
      </div>
      <div className="mt-3 space-y-2">
        {UPLOAD_MODES.map((m) => {
          const unavailable = Boolean(m.upOnly) && direction === "down";
          return (
            <label
              key={m.mode}
              className={`block rounded-lg border p-3 text-[11px] ${
                unavailable
                  ? "border-slate-200 opacity-50 dark:border-slate-600"
                  : mode === m.mode
                    ? "border-blue-500 bg-blue-50/60 dark:bg-blue-900/20"
                    : "border-slate-200 dark:border-slate-600"
              }`}
            >
              <span className="flex items-center gap-2">
                <input
                  type="radio"
                  name="upload-mode"
                  checked={mode === m.mode}
                  disabled={unavailable}
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
                  {unavailable ? "下载方向不可用" : m.note}
                </span>
              </span>
              <span className="mt-1 block leading-relaxed text-slate-500 dark:text-slate-400">
                {unavailable
                  ? "将以云端数据覆盖本机全部数据。如需清除本机数据，请使用删除功能。"
                  : m.cost}
              </span>
            </label>
          );
        })}
      </div>
      {why && <ErrorBox>{why}</ErrorBox>}
      <div className="mt-3 flex items-center justify-end gap-2">
        <button
          onClick={onCancel}
          className="px-2 py-2 text-xs text-slate-500 hover:text-slate-700 dark:text-slate-400"
        >
          暂不同步
        </button>
        <button
          onClick={onNext}
          disabled={busy}
          className="rounded-lg bg-blue-600 px-3.5 py-2 text-xs text-white disabled:opacity-50"
        >
          {busy ? "正在比对…" : "下一步：查看差异"}
        </button>
      </div>
      <Hint>
        「暂不同步」不影响登录状态：界面照常使用云端数据，两端数据保持原样。
        未勾选的类目不会同步，保留对端现有数据。
      </Hint>
    </>
  );
}

function DryScreen({
  plan,
  direction,
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
  direction: Direction;
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
  const receiver = direction === "up" ? "云端" : "本机";
  return (
    <>
      <Title>差异确认</Title>
      <Hint>此步骤不写入任何数据。以下数字来自两端的实际比对，非估算。</Hint>
      <div className="mt-3 grid grid-cols-4 gap-2">
        {planReads(plan, direction).map((r) => (
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
                  本机 {totalOf(bucket)} 项
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
          {plan.skipped.length} 项无法同步，已整项跳过：
          {plan.skipped.slice(0, 3).map((s) => (
            <span key={`${s.kind}${s.ident}`} className="block">
              · {s.preview || s.ident} —— {s.reason}
            </span>
          ))}
          {plan.skipped.length > 3 && (
            <span className="block">…另有 {plan.skipped.length - 3} 项，原因相同</span>
          )}
        </p>
      )}
      {why && <ErrorBox>{why}</ErrorBox>}
      <Hint>
        未勾选的类目不会同步，保留{receiver}现有数据。
        {mode === "merge" && plan.conflicts.length > 0 && (
          <>
            {" "}冲突 {plan.conflicts.length} 项中尚有 <b>{undecided} 项未确认</b>；
            未确认的冲突项将保留{receiver}现有版本，不做修改。
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
            处理 {plan.conflicts.length} 项冲突
          </button>
        )}
        <button
          onClick={onStart}
          disabled={busy || kinds.length === 0}
          className="rounded-lg bg-blue-600 px-3.5 py-2 text-xs text-white disabled:opacity-50"
        >
          {busy ? "正在同步…" : `开始同步（${going} 项）`}
        </button>
      </div>
    </>
  );
}

function totalOf(bucket: Record<string, number>): number {
  return (bucket.only_local ?? 0) + (bucket.same ?? 0) + (bucket.conflicts ?? 0);
}

type Screen3Direction = Direction | "reconcile";

function ConflictScreen({
  conflicts,
  which,
  direction,
  onBack,
  onDecide,
  busy,
}: {
  conflicts: LeftConflict[];
  which: number;
  direction: Screen3Direction;
  onBack: () => void;
  onDecide: (choice: string) => void;
  busy: boolean;
}) {
  const [choice, setChoice] = useState(direction === "up" ? "keepRemote" : "keepLocal");
  const conflict = conflicts[which];
  if (!conflict) return null; // 调用方挑完最后一条就切走了，这一屏不会拿着越界的下标渲染
  const label = SYNC_ITEMS.find((i) => i.kind === conflict.kind)?.label ?? conflict.kind;
  const last = which + 1 >= conflicts.length;
  return (
    <>
      <Title>
        冲突 {which + 1} / {conflicts.length} · {label}
      </Title>
      <Hint>
        {canKeepBoth(conflict.kind)
          ? "同一条目（标识相同）两端均有修改。请选择保留的版本，或两个版本均保留。"
          : "同一身份两端内容不同。该类目的身份即此 id，「保留两个版本」不适用 —— 请选择其一。"}
      </Hint>
      <div className="mt-3 grid grid-cols-2 gap-2 text-[11px]">
        <Preview title="本机版本" at={conflict.mine.at} text={conflict.mine.preview} />
        <Preview title="云端版本" at={conflict.theirs.at} text={conflict.theirs.preview} />
      </div>
      <div className="mt-3 space-y-2 text-[11px]">
        <Radio
          value={choice}
          onChange={setChoice}
          name="keepRemote"
          title="保留云端版本"
          note={
            direction === "up"
              ? "默认。云端版本保持不变，本机版本不会上传。"
              : "下载该条目，覆盖本机版本。"
          }
        />
        <Radio
          value={choice}
          onChange={setChoice}
          name="keepLocal"
          title="保留本机版本"
          note={
            direction === "up"
              ? "以本机版本改写云端。"
              : "默认。本机版本保持不变，云端版本不会下载。"
          }
        />
        {canKeepBoth(conflict.kind) && (
          <Radio
            value={choice}
            onChange={setChoice}
            name="keepBoth"
            title="保留两个版本"
            note="两个版本均保留：本机版本保持不变，云端版本以新标识另存一份。稍后可在记忆整理中合并。"
          />
        )}
      </div>
      <div className="mt-3 flex items-center justify-end gap-2">
        <button
          onClick={onBack}
          className="px-2 py-2 text-xs text-slate-500 hover:text-slate-700 dark:text-slate-400"
        >
          返回
        </button>
        <button
          onClick={() => onDecide("skip")}
          className="rounded-lg border border-slate-200 px-3.5 py-2 text-xs text-slate-600 dark:border-slate-600 dark:text-slate-300"
        >
          跳过此项
        </button>
        <button
          onClick={() => onDecide(choice)}
          disabled={busy && last && direction === "reconcile"}
          className="rounded-lg bg-blue-600 px-3.5 py-2 text-xs text-white disabled:opacity-50"
        >
          {last && direction === "reconcile" ? "应用选择" : last ? "完成" : "下一项"}
        </button>
      </div>
      <Hint>
        本屏不提供「全部按一侧处理」：两侧均有修改的条目应由您逐项确认。
        跳过 = 该项保持现状（未确认的冲突将保留现有版本），下次同步仍会提示。
      </Hint>
    </>
  );
}

function Preview({ title, at, text }: { title: string; at: string; text: string }) {
  return (
    <div className="rounded-lg border border-slate-200 p-2.5 dark:border-slate-600">
      <div className="text-[10px] text-slate-400 dark:text-slate-500">
        {title}
        {at ? ` · ${at.slice(0, 10)}` : ""}
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

function DoneScreen({
  done,
  onDone,
}: {
  done: { up: number; down: number; skipped: number };
  onDone: () => void;
}) {
  return (
    <>
      <Title>同步完成</Title>
      <div className="mt-2 h-1 w-full rounded bg-emerald-500" />
      <p className="mt-3 text-xs leading-relaxed text-slate-600 dark:text-slate-300">
        已上传 <b className="text-emerald-700 dark:text-emerald-300">{done.up}</b> 项，下载{" "}
        <b className="text-emerald-700 dark:text-emerald-300">{done.down}</b> 项
        {done.skipped > 0 && <>；{done.skipped} 项因已存在而跳过</>}。
      </p>
      <p className="mt-2 text-xs leading-relaxed text-slate-600 dark:text-slate-300">
        <b className="text-amber-700 dark:text-amber-300">向量索引未随数据迁移</b>
        ——云端将基于其嵌入服务自行重建。两端嵌入服务不同时，向量之间不可直接比较。
      </p>
      <p className="mt-2 text-xs leading-relaxed text-slate-600 dark:text-slate-300">
        本机数据<b className="text-emerald-700 dark:text-emerald-300">未做任何修改</b>
        ——同步为复制操作，非迁移。
      </p>
      <div className="mt-3 flex justify-end">
        <button onClick={onDone} className="rounded-lg bg-blue-600 px-3.5 py-2 text-xs text-white">
          知道了
        </button>
      </div>
    </>
  );
}
