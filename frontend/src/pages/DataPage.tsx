import { useCallback, useEffect, useState } from "react";
import { api, type GenericRecord, type PluginRow, type ReportRecord } from "../api";
import Tag from "../components/ui/Tag";

// 数据 —— 领域数据的唯一归属地（自"插件 → 详情"里升为独立顶层页）。
// 数据随领域归属：未来新增领域插件时，这里自动多出一个分组。
// 主流程是"上传报告 / 图片让 AI 解析"，手动补录只是兜底（所以表单刻意做得最小）。
export default function DataPage() {
  const [plugins, setPlugins] = useState<PluginRow[]>([]);

  useEffect(() => {
    api.get<PluginRow[]>("/api/plugins").then(setPlugins).catch(() => undefined);
  }, []);

  return (
    <div className="h-full overflow-y-auto p-4 md:p-6">
      <div className="mx-auto max-w-4xl">
        <h2 className="text-base font-semibold text-slate-900 dark:text-slate-100">数据</h2>
        <p className="mt-0.5 text-xs text-slate-400 dark:text-slate-500">
          <b>数据随领域归属</b>：每个领域插件管自己的数据。主流程是<b>上传报告 / 图片让 AI 解析</b>
          （解析结果进检索索引），下方的手动补录只是兜底入口。
        </p>
        <div className="mt-5 space-y-7">
          {plugins.length === 0 && (
            <p className="text-xs text-slate-400 dark:text-slate-500">正在加载领域…</p>
          )}
          {plugins.map((p) => (
            <section key={p.plugin_id}>
              <div className="flex flex-wrap items-center gap-2 border-b border-slate-100 dark:border-slate-800 pb-2">
                <h3 className="text-sm font-medium text-slate-700 dark:text-slate-200">{p.display_name}</h3>
                {p.display_name !== p.plugin_id && (
                  <code className="rounded bg-slate-100 dark:bg-slate-700/50 px-1.5 py-0.5 text-[11px] text-slate-500 dark:text-slate-400 dark:text-slate-500">
                    {p.plugin_id}
                  </code>
                )}
                {!p.enabled && (
                  <span className="rounded-full bg-amber-50 dark:bg-amber-900/30 px-2 py-0.5 text-[11px] text-amber-600 dark:text-amber-400">
                    该领域插件已停用（数据保留）
                  </span>
                )}
              </div>
              <div className="mt-3">
                {p.plugin_id === "health" ? (
                  <DataManagement />
                ) : (
                  <GenericDomainData domain={p.plugin_id} displayName={p.display_name} />
                )}
              </div>
            </section>
          ))}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- 数据管理（随域归属）

function fmtValue(i: { index_value: number | null; value_text: string | null; unit: string | null }): string {
  if (i.index_value !== null) return `${i.index_value}${i.unit ? " " + i.unit : ""}`;
  return i.value_text || "（无）";
}

function DataManagement({ compact = false }: { compact?: boolean }) {
  const [reports, setReports] = useState<ReportRecord[]>([]);
  const [editId, setEditId] = useState<string | null>(null);
  const [draft, setDraft] = useState({ index_value: "", value_text: "", unit: "", ref_range: "", verified: false });
  const [confirmDel, setConfirmDel] = useState<string | null>(null);
  const [status, setStatus] = useState<{ ok: boolean; msg: string } | null>(null);
  const [adding, setAdding] = useState(false);

  const load = useCallback(async () => {
    setReports(await api.get<ReportRecord[]>("/api/records"));
  }, []);
  useEffect(() => {
    load().catch((e) => setStatus({ ok: false, msg: `加载失败：${e.message}` }));
  }, [load]);

  function startEdit(indexId: string, current: { index_value: number | null; value_text: string | null; unit: string | null; ref_range: string | null; is_verified: number | boolean }) {
    setEditId(indexId);
    setDraft({
      index_value: current.index_value === null ? "" : String(current.index_value),
      value_text: current.value_text || "",
      unit: current.unit || "",
      ref_range: current.ref_range || "",
      verified: !!current.is_verified,
    });
  }

  async function saveEdit(indexId: string) {
    const changes: Record<string, unknown> = { is_verified: draft.verified };
    if (draft.index_value.trim() !== "") changes.index_value = Number(draft.index_value);
    else changes.index_value = null;
    if (draft.value_text.trim() !== "") changes.value_text = draft.value_text.trim();
    else changes.value_text = null;
    changes.unit = draft.unit.trim() || null;
    changes.ref_range = draft.ref_range.trim() || null;
    try {
      await api.patch(`/api/records/index/${indexId}`, changes);
      setEditId(null);
      setStatus({ ok: true, msg: "已保存修正（写入审计）" });
      await load();
    } catch (e) {
      setStatus({ ok: false, msg: `保存失败：${(e as Error).message}` });
    }
  }

  async function remove(kind: "report" | "index", id: string) {
    try {
      await api.del(`/api/records/${kind}/${id}`);
      setConfirmDel(null);
      setStatus({ ok: true, msg: "已删除（写入审计）" });
      await load();
    } catch (e) {
      setStatus({ ok: false, msg: `删除失败：${(e as Error).message}` });
    }
  }

  return (
    <div className="space-y-3">
      <div className="flex items-start justify-between gap-3">
        <p className="text-[11px] leading-relaxed text-slate-400 dark:text-slate-500">
          主流程是<b>上传报告 / 图片让 AI 解析</b>；这里只是补录入口 —— 手填的数据默认带
          【未经人工校验】标记。
        </p>
        <button
          onClick={() => setAdding((v) => !v)}
          className="shrink-0 rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 px-2.5 py-1 text-xs text-slate-600 dark:text-slate-300 dark:text-slate-600 hover:border-blue-300 dark:hover:border-blue-700 hover:text-blue-600 dark:text-blue-400"
        >
          {adding ? "取消" : "＋ 新增报告"}
        </button>
      </div>
      {adding && (
        <AddReportForm
          onDone={() => {
            setAdding(false);
            setStatus({ ok: true, msg: "已新增报告（写入审计）" });
            load().catch(() => undefined);
          }}
        />
      )}
      {status && (
        <p className={`rounded-lg px-3 py-2 text-xs ${status.ok ? "bg-green-50 dark:bg-green-900/30 text-green-700 dark:text-green-300" : "bg-red-50 dark:bg-red-900/30 text-red-600 dark:text-red-400 dark:text-red-500"}`}>
          {status.msg}
        </p>
      )}
      {reports.length === 0 ? (
        <div className={`rounded-lg border border-dashed border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 text-center text-xs text-slate-400 dark:text-slate-500 ${compact ? "p-4" : "p-8 text-sm"}`}>
          该域还没有数据：在对话页上传报告 / 图片（自动解析入索引），或点上方「＋ 新增报告」手动补录。
        </div>
      ) : (
        reports.map((r) => (
        <div key={r.report_id} className="overflow-hidden rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800">
          <div className="flex items-center justify-between border-b border-slate-100 bg-slate-50/70 px-3 py-2 dark:border-slate-700/60 dark:bg-slate-700/30">
            <div className="text-xs">
              <span className="font-medium text-slate-800 dark:text-slate-100">
                {String(r.check_time).slice(0, 10)} · {r.report_type}
              </span>
              {r.institution && <span className="ml-2 text-slate-400 dark:text-slate-500">{r.institution}</span>}
              {r.note && <span className="ml-2 text-slate-400 dark:text-slate-500">备注：{r.note}</span>}
            </div>
            {confirmDel === r.report_id ? (
              <button
                onClick={() => remove("report", r.report_id)}
                className="rounded bg-red-500 px-2 py-1 text-[11px] text-white hover:bg-red-600"
              >
                确认删除整份报告
              </button>
            ) : (
              <button
                onClick={() => setConfirmDel(r.report_id)}
                className="rounded px-2 py-1 text-[11px] text-red-400 dark:text-red-500 hover:bg-red-50 dark:bg-red-900/30 hover:text-red-600 dark:text-red-400 dark:text-red-500"
              >
                删除报告
              </button>
            )}
          </div>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[28rem] text-left text-xs">
            <thead>
              <tr className="text-slate-400 dark:text-slate-500">
                <th className="px-3 py-1.5 font-medium">指标</th>
                <th className="px-3 py-1.5 font-medium">数值</th>
                <th className="px-3 py-1.5 font-medium">校验</th>
                <th className="px-3 py-1.5"></th>
              </tr>
            </thead>
            <tbody>
              {r.indices.map((i) => (
                <tr key={i.index_id} className="border-t border-slate-50">
                  {editId === i.index_id ? (
                    <>
                      <td className="px-3 py-2 font-medium text-slate-700 dark:text-slate-200">{i.index_name}</td>
                      <td className="px-3 py-2" colSpan={3}>
                        <div className="flex flex-wrap items-center gap-2">
                          <input
                            value={draft.index_value}
                            onChange={(e) => setDraft({ ...draft, index_value: e.target.value })}
                            placeholder="数值（可空）"
                            className="w-24 rounded border border-slate-200 dark:border-slate-700 px-2 py-1"
                          />
                          <input
                            value={draft.value_text}
                            onChange={(e) => setDraft({ ...draft, value_text: e.target.value })}
                            placeholder="文本值（可空）"
                            className="w-32 rounded border border-slate-200 dark:border-slate-700 px-2 py-1"
                          />
                          <input
                            value={draft.unit}
                            onChange={(e) => setDraft({ ...draft, unit: e.target.value })}
                            placeholder="单位"
                            className="w-20 rounded border border-slate-200 dark:border-slate-700 px-2 py-1"
                          />
                          <input
                            value={draft.ref_range}
                            onChange={(e) => setDraft({ ...draft, ref_range: e.target.value })}
                            placeholder="参考区间"
                            className="w-24 rounded border border-slate-200 dark:border-slate-700 px-2 py-1"
                          />
                          <label className="flex items-center gap-1">
                            <input
                              type="checkbox"
                              checked={draft.verified}
                              onChange={(e) => setDraft({ ...draft, verified: e.target.checked })}
                            />
                            已人工校验
                          </label>
                          <button
                            onClick={() => saveEdit(i.index_id)}
                            className="rounded bg-blue-600 px-2.5 py-1 text-white hover:bg-blue-700"
                          >
                            保存
                          </button>
                          <button
                            onClick={() => setEditId(null)}
                            className="rounded px-2 py-1 text-slate-500 dark:text-slate-400 dark:text-slate-500 hover:bg-slate-100 dark:bg-slate-700/50 dark:hover:bg-slate-700"
                          >
                            取消
                          </button>
                        </div>
                      </td>
                    </>
                  ) : (
                    <>
                      <td className="px-3 py-2 font-medium text-slate-700 dark:text-slate-200">{i.index_name}</td>
                      <td className="px-3 py-2">
                        {fmtValue(i)}
                        {i.ref_range ? <span className="ml-1 text-slate-400 dark:text-slate-500">（参考 {i.ref_range}）</span> : null}
                        {!i.is_verified && <span className="ml-1 text-amber-600 dark:text-amber-400">【未经人工校验】</span>}
                      </td>
                      <td className="px-3 py-2">
                        <Tag tone={i.is_verified ? "green" : "amber"}>
                          {i.is_verified ? "已校验" : "未校验"}
                        </Tag>
                      </td>
                      <td className="px-3 py-2 text-right">
                        <button
                          onClick={() => startEdit(i.index_id, i)}
                          className="rounded px-2 py-1 text-blue-600 dark:text-blue-400 hover:bg-blue-50 dark:bg-blue-900/30"
                        >
                          修正
                        </button>
                        {confirmDel === i.index_id ? (
                          <button
                            onClick={() => remove("index", i.index_id)}
                            className="rounded bg-red-500 px-2 py-1 text-white hover:bg-red-600"
                          >
                            确认
                          </button>
                        ) : (
                          <button
                            onClick={() => setConfirmDel(i.index_id)}
                            className="rounded px-2 py-1 text-red-400 dark:text-red-500 hover:bg-red-50 dark:bg-red-900/30 hover:text-red-600 dark:text-red-400 dark:text-red-500"
                          >
                            删除
                          </button>
                        )}
                      </td>
                    </>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        </div>
        ))
      )}
    </div>
  );
}

// ---------------------------------------------------------------- 手动补录（最小可用）

interface DraftRow {
  /** 稳定 React key（M7）：删行/插行时输入框状态不串行；提交时只挑业务字段，不会外泄。 */
  rowId: number;
  name: string;
  value: string;
  unit: string;
}

// 行 id 发号器：模块级自增即可（key 只需在当前列表实例内唯一）。
let draftRowSeq = 0;
function nextRowId() {
  return (draftRowSeq += 1);
}

const EMPTY_ROWS: DraftRow[] = [{ rowId: nextRowId(), name: "", value: "", unit: "" }];

/** 手动补录一份报告 —— **兜底入口**，主流程是上传报告/图片让 AI 解析。
 *
 * 最小可用契约：类型 + 检查时间必填，至少一行指标；每行需「指标名 + 数值或文本」。
 * 一个输入框同时接受数值与文本：能 parse 成数字就当数值，否则原样存 value_text。
 */
function AddReportForm({ onDone }: { onDone: () => void }) {
  const [reportType, setReportType] = useState("");
  const [checkTime, setCheckTime] = useState("");
  const [institution, setInstitution] = useState("");
  const [note, setNote] = useState("");
  const [rows, setRows] = useState<DraftRow[]>(EMPTY_ROWS);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  function setRow(i: number, patch: Partial<DraftRow>) {
    setRows((rs) => rs.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  }

  async function submit() {
    setErr("");
    if (!reportType.trim() || !checkTime.trim()) {
      setErr("报告类型与检查时间必填");
      return;
    }
    const filled = rows.filter((r) => r.name.trim());
    if (filled.length === 0) {
      setErr("至少填一行指标（指标名 + 数值或文本）");
      return;
    }
    const indices = filled.map((r) => {
      const v = r.value.trim();
      const numeric = v !== "" && !Number.isNaN(Number(v));
      return {
        index_name: r.name.trim(),
        index_value: numeric ? Number(v) : null,
        value_text: numeric ? null : v || null,
        unit: r.unit.trim() || null,
      };
    });
    const missing = indices.find((i) => i.index_value === null && !i.value_text);
    if (missing) {
      setErr(`指标「${missing.index_name}」需要数值或文本`);
      return;
    }
    setBusy(true);
    try {
      await api.post("/api/records/report", {
        report_type: reportType.trim(),
        check_time: checkTime.trim(),
        institution: institution.trim() || null,
        note: note.trim() || null,
        indices,
      });
      onDone();
    } catch (e) {
      setErr(`保存失败：${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  const inputCls =
    "rounded border border-slate-200 dark:border-slate-700 px-2 py-1 text-xs outline-none focus:border-blue-400";

  return (
    <div className="rounded-lg border border-blue-200 dark:border-blue-800 bg-blue-50 dark:bg-blue-900/30/40 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <input
          value={reportType}
          onChange={(e) => setReportType(e.target.value)}
          placeholder="报告类型 *（如 腹部超声）"
          className={`${inputCls} w-44`}
        />
        <input
          type="date"
          value={checkTime}
          onChange={(e) => setCheckTime(e.target.value)}
          title="检查时间 *"
          className={`${inputCls} w-36`}
        />
        <input
          value={institution}
          onChange={(e) => setInstitution(e.target.value)}
          placeholder="机构（可选）"
          className={`${inputCls} w-32`}
        />
        <input
          value={note}
          onChange={(e) => setNote(e.target.value)}
          placeholder="备注（可选）"
          className={`${inputCls} w-32`}
        />
      </div>

      <div className="mt-2 space-y-1.5">
        {rows.map((r, i) => (
          <div key={r.rowId} className="flex items-center gap-2">
            <input
              value={r.name}
              onChange={(e) => setRow(i, { name: e.target.value })}
              placeholder="指标名 *（如 结石直径）"
              className={`${inputCls} w-44`}
            />
            <input
              value={r.value}
              onChange={(e) => setRow(i, { value: e.target.value })}
              placeholder="数值或文本（如 6.1 / 未见异常）"
              className={`${inputCls} w-48`}
            />
            <input
              value={r.unit}
              onChange={(e) => setRow(i, { unit: e.target.value })}
              placeholder="单位"
              className={`${inputCls} w-20`}
            />
            {rows.length > 1 && (
              <button
                onClick={() => setRows((rs) => rs.filter((_, j) => j !== i))}
                className="rounded px-1.5 py-0.5 text-xs text-slate-400 dark:text-slate-500 hover:bg-slate-100 dark:bg-slate-700/50 dark:hover:bg-slate-700 hover:text-red-500"
                title="删除该行"
              >
                ✕
              </button>
            )}
          </div>
        ))}
      </div>

      <div className="mt-2 flex items-center gap-2">
        <button
          onClick={() => setRows((rs) => [...rs, { rowId: nextRowId(), name: "", value: "", unit: "" }])}
          className="rounded border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 px-2 py-1 text-xs text-slate-600 dark:text-slate-300 dark:text-slate-600 hover:border-blue-300 dark:hover:border-blue-700"
        >
          ＋ 加一行
        </button>
        <button
          onClick={submit}
          disabled={busy}
          className="rounded bg-blue-600 px-3 py-1 text-xs text-white hover:bg-blue-700 disabled:bg-slate-300 dark:bg-slate-600"
        >
          {busy ? "保存中…" : "保存"}
        </button>
        <button
          onClick={onDone}
          className="rounded px-2 py-1 text-xs text-slate-500 dark:text-slate-400 dark:text-slate-500 hover:bg-slate-100 dark:bg-slate-700/50 dark:hover:bg-slate-700"
        >
          取消
        </button>
        <span className="text-[11px] text-slate-400 dark:text-slate-500">手填默认标记为【未经人工校验】</span>
      </div>

      {err && <p className="mt-2 text-xs text-red-600 dark:text-red-400 dark:text-red-500">{err}</p>}
    </div>
  );
}

// ---------------------------------------------------------------- 通用领域数据（非 health 域）

/** 非 health 域的通用数据视图：标签 + 数值/文本 + 单位 + 备注，走 /api/domains/{domain}/records。
 *
 * health 域有更丰富的报告/指标模型（见 DataManagement），不在此重复；其它域无需写领域服务
 * 即可在「数据」页拥有增删改查能力 —— 这是「数据随领域归属」的真正多领域化。 */
function GenericDomainData({
  domain,
  displayName,
}: {
  domain: string;
  displayName: string;
}) {
  const [records, setRecords] = useState<GenericRecord[]>([]);
  const [adding, setAdding] = useState(false);
  const [editId, setEditId] = useState<string | null>(null);
  const [draft, setDraft] = useState({
    label: "",
    value_text: "",
    value_num: "",
    unit: "",
    note: "",
  });
  const [confirmDel, setConfirmDel] = useState<string | null>(null);
  const [status, setStatus] = useState<{ ok: boolean; msg: string } | null>(null);

  const load = useCallback(async () => {
    setRecords(await api.listDomainRecords(domain));
  }, [domain]);

  useEffect(() => {
    load().catch((e) => setStatus({ ok: false, msg: `加载失败：${e.message}` }));
  }, [load]);

  function fmt(r: GenericRecord): string {
    if (r.value_num !== null && r.value_num !== undefined) {
      return `${r.value_num}${r.unit ? " " + r.unit : ""}`;
    }
    return r.value_text || "（无）";
  }

  function startEdit(r: GenericRecord) {
    setEditId(r.id);
    setDraft({
      label: r.label,
      value_text: r.value_text || "",
      value_num: r.value_num === null || r.value_num === undefined ? "" : String(r.value_num),
      unit: r.unit || "",
      note: r.note || "",
    });
  }

  async function saveEdit(id: string) {
    const changes: Record<string, unknown> = { label: draft.label.trim() };
    changes.value_text = draft.value_text.trim() || null;
    changes.value_num = draft.value_num.trim() === "" ? null : Number(draft.value_num);
    changes.unit = draft.unit.trim() || null;
    changes.note = draft.note.trim() || null;
    try {
      await api.patchDomainRecord(domain, id, changes);
      setEditId(null);
      setStatus({ ok: true, msg: "已保存（写入审计）" });
      await load();
    } catch (e) {
      setStatus({ ok: false, msg: `保存失败：${(e as Error).message}` });
    }
  }

  async function remove(id: string) {
    try {
      await api.deleteDomainRecord(domain, id);
      setConfirmDel(null);
      setStatus({ ok: true, msg: "已删除（写入审计）" });
      await load();
    } catch (e) {
      setStatus({ ok: false, msg: `删除失败：${(e as Error).message}` });
    }
  }

  async function submit() {
    if (!draft.label.trim()) {
      setStatus({ ok: false, msg: "标签必填" });
      return;
    }
    if (!draft.value_text.trim() && draft.value_num.trim() === "") {
      setStatus({ ok: false, msg: "数值与文本至少填一个" });
      return;
    }
    try {
      await api.addDomainRecord(domain, {
        label: draft.label.trim(),
        value_text: draft.value_text.trim() || null,
        value_num: draft.value_num.trim() === "" ? null : Number(draft.value_num),
        unit: draft.unit.trim() || null,
        note: draft.note.trim() || null,
      });
      setAdding(false);
      setDraft({ label: "", value_text: "", value_num: "", unit: "", note: "" });
      setStatus({ ok: true, msg: "已新增（写入审计）" });
      await load();
    } catch (e) {
      setStatus({ ok: false, msg: `保存失败：${(e as Error).message}` });
    }
  }

  const cls =
    "rounded border border-slate-200 dark:border-slate-700 px-2 py-1 text-xs outline-none focus:border-blue-400";

  return (
    <div className="space-y-3">
      <div className="flex items-start justify-between gap-3">
        <p className="text-[11px] leading-relaxed text-slate-400 dark:text-slate-500">
          「{displayName}」域的通用数据（标签 + 数值/文本 + 单位 + 备注）。新增领域插件即自动出现此视图。
        </p>
        <button
          onClick={() => setAdding((v) => !v)}
          className="shrink-0 rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 px-2.5 py-1 text-xs text-slate-600 dark:text-slate-300 dark:text-slate-600 hover:border-blue-300 dark:hover:border-blue-700 hover:text-blue-600 dark:text-blue-400"
        >
          {adding ? "取消" : "＋ 新增记录"}
        </button>
      </div>
      {status && (
        <p className={`rounded-lg px-3 py-2 text-xs ${status.ok ? "bg-green-50 dark:bg-green-900/30 text-green-700 dark:text-green-300" : "bg-red-50 dark:bg-red-900/30 text-red-600 dark:text-red-400 dark:text-red-500"}`}>
          {status.msg}
        </p>
      )}
      {adding && (
        <div className="rounded-lg border border-blue-200 dark:border-blue-800 bg-blue-50 dark:bg-blue-900/30/40 p-3">
          <div className="flex flex-wrap items-center gap-2">
            <input value={draft.label} onChange={(e) => setDraft({ ...draft, label: e.target.value })} placeholder="标签 *（如 体重）" className={`${cls} w-40`} />
            <input value={draft.value_num} onChange={(e) => setDraft({ ...draft, value_num: e.target.value })} placeholder="数值（如 62）" className={`${cls} w-28`} />
            <input value={draft.value_text} onChange={(e) => setDraft({ ...draft, value_text: e.target.value })} placeholder="或文本值" className={`${cls} w-40`} />
            <input value={draft.unit} onChange={(e) => setDraft({ ...draft, unit: e.target.value })} placeholder="单位（可选）" className={`${cls} w-20`} />
            <input value={draft.note} onChange={(e) => setDraft({ ...draft, note: e.target.value })} placeholder="备注（可选）" className={`${cls} w-32`} />
          </div>
          <div className="mt-2 flex items-center gap-2">
            <button onClick={submit} className="rounded bg-blue-600 px-3 py-1 text-xs text-white hover:bg-blue-700">保存</button>
            <button onClick={() => setAdding(false)} className="rounded px-2 py-1 text-xs text-slate-500 dark:text-slate-400 dark:text-slate-500 hover:bg-slate-100 dark:bg-slate-700/50 dark:hover:bg-slate-700">取消</button>
          </div>
        </div>
      )}
      {records.length === 0 ? (
        <div className="rounded-lg border border-dashed border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 text-center text-xs text-slate-400 dark:text-slate-500 p-8">
          该域还没有数据：点上方「＋ 新增记录」补录。
        </div>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800">
          <table className="w-full min-w-[26rem] text-left text-xs">
            <thead>
              <tr className="text-slate-400 dark:text-slate-500">
                <th className="px-3 py-1.5 font-medium">标签</th>
                <th className="px-3 py-1.5 font-medium">值</th>
                <th className="px-3 py-1.5 font-medium">备注</th>
                <th className="px-3 py-1.5"></th>
              </tr>
            </thead>
            <tbody>
              {records.map((r) => (
                <tr key={r.id} className="border-t border-slate-50">
                  {editId === r.id ? (
                    <>
                      <td className="px-3 py-2">
                        <input value={draft.label} onChange={(e) => setDraft({ ...draft, label: e.target.value })} className={`${cls} w-32`} />
                      </td>
                      <td className="px-3 py-2" colSpan={2}>
                        <div className="flex flex-wrap items-center gap-2">
                          <input value={draft.value_num} onChange={(e) => setDraft({ ...draft, value_num: e.target.value })} placeholder="数值" className={`${cls} w-24`} />
                          <input value={draft.value_text} onChange={(e) => setDraft({ ...draft, value_text: e.target.value })} placeholder="文本值" className={`${cls} w-32`} />
                          <input value={draft.unit} onChange={(e) => setDraft({ ...draft, unit: e.target.value })} placeholder="单位" className={`${cls} w-20`} />
                          <input value={draft.note} onChange={(e) => setDraft({ ...draft, note: e.target.value })} placeholder="备注" className={`${cls} w-28`} />
                          <button onClick={() => saveEdit(r.id)} className="rounded bg-blue-600 px-2.5 py-1 text-white hover:bg-blue-700">保存</button>
                          <button onClick={() => setEditId(null)} className="rounded px-2 py-1 text-slate-500 dark:text-slate-400 dark:text-slate-500 hover:bg-slate-100 dark:bg-slate-700/50 dark:hover:bg-slate-700">取消</button>
                        </div>
                      </td>
                    </>
                  ) : (
                    <>
                      <td className="px-3 py-2 font-medium text-slate-700 dark:text-slate-200">{r.label}</td>
                      <td className="px-3 py-2 text-slate-600 dark:text-slate-400 dark:text-slate-500">{fmt(r)}</td>
                      <td className="px-3 py-2 text-slate-400 dark:text-slate-500">{r.note || "—"}</td>
                      <td className="px-3 py-2 text-right">
                        <button onClick={() => startEdit(r)} className="rounded px-2 py-1 text-blue-600 dark:text-blue-400 hover:bg-blue-50 dark:bg-blue-900/30">修正</button>
                        {confirmDel === r.id ? (
                          <button onClick={() => remove(r.id)} className="rounded bg-red-500 px-2 py-1 text-white hover:bg-red-600">确认</button>
                        ) : (
                          <button onClick={() => setConfirmDel(r.id)} className="rounded px-2 py-1 text-red-400 dark:text-red-500 hover:bg-red-50 dark:bg-red-900/30 hover:text-red-600 dark:text-red-400 dark:text-red-500">删除</button>
                        )}
                      </td>
                    </>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
