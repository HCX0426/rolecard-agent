import { useCallback, useEffect, useRef, useState } from "react";
import { api, type DomainInfo, type GenericRecord, type ReportRecord } from "../api";
import { useConfirm } from "../hooks/useConfirm";
import { Button, Card, Notice, PageHeader, Tabs, Tag } from "../components/ui";
import { KnowledgeManager } from "../components/KnowledgeManager";
import { describeError } from '../lib/errors';

// 资料 —— 领域内容的管理页（2026-10-24 IA 重构：数据页 + 知识库页合并于此）。
// 两个区块、**两个引擎**（合并的是界面不是存储）：
//   结构化记录 —— SQLite，SQL 精确查，表格可修正；AI 走域查询工具读精确值；
//   知识库     —— Chroma 向量库，语义检索，文档只读可重建；AI 走 search_knowledge。
// 上传一份报告 = 两个区块各出现产物（抽取成记录 + 原文进检索库）——"一次动作的两个
// 产物"回到同一个地方，这正是与旧的两页签形态的差别。
export type MaterialsBlock = "records" | "knowledge";

export default function DataPage({ initialBlock }: { initialBlock?: MaterialsBlock } = {}) {
  const [domains, setDomains] = useState<DomainInfo[]>([]);
  const [block, setBlock] = useState<MaterialsBlock>(initialBlock ?? "records");
  // 选中的域（2026-10-25 玻璃重设计）：域从"垂直分组堆叠"改成**顶部胶囊 tab** ——
  // 一次只看一个域，页面短一半；堆叠时两个域的表格上下相隔一屏，要滚很远才看得到第二个。
  const [activeDomain, setActiveDomain] = useState<string>("");

  useEffect(() => {
    if (initialBlock) setBlock(initialBlock);
  }, [initialBlock]);

  // 领域列表的失败要有出口：从前静默吞掉，"正在加载领域…"成了永久文案——失败与"没数据"
  // 不可区分（2026-10-30 交互审计）。
  const [domainsErr, setDomainsErr] = useState("");
  const loadDomains = useCallback(() => {
    return api
      .get<DomainInfo[]>("/api/domains")
      .then((list) => {
        setDomains(list);
        // 默认停在第一个域（还没选过时）—— 不要让"两个域都铺开"成为初始形态。
        setActiveDomain((cur) => cur || list[0]?.id || "");
        setDomainsErr("");
      })
      .catch((e) => setDomainsErr(describeError(e)));
  }, []);

  useEffect(() => {
    loadDomains();
  }, [loadDomains]);

  return (
    <div className="h-full overflow-y-auto p-4 md:p-6">
      <div className="mx-auto max-w-4xl">
        <PageHeader
          title="资料"
          subtitle={
            <>
              结构化记录（可修正的指标/条目）与知识库（文档语义检索）——
              <b>一次上传，两个产物</b>：抽取成记录 + 原文进检索库。
            </>
          }
          actions={
            <div className="flex rounded-lg border border-slate-300 bg-white p-0.5 shadow-sm dark:border-slate-600 dark:bg-slate-800">
              {(
                [
                  ["records", "结构化记录"],
                  ["knowledge", "知识库"],
                ] as const
              ).map(([key, label]) => (
                <button
                  key={key}
                  type="button"
                  onClick={() => setBlock(key)}
                  className={`rounded-md px-3 py-1.5 text-sm transition-colors ${
                    block === key
                      ? "bg-blue-600 text-white"
                      : "text-slate-600 hover:text-blue-600 dark:text-slate-300 dark:hover:text-blue-400"
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>
          }
        />

        {block === "knowledge" ? (
          <KnowledgeManager />
        ) : (
          <>
            <>
              {/* 域切换：胶囊 tab（一次只看一个域） */}
              {domains.length > 1 && (
                <Tabs
                  className="mt-4"
                  ariaLabel="领域切换"
                  items={domains.map((d) => ({ key: d.id ?? "", label: d.display_name ?? d.id ?? "" }))}
                  active={activeDomain}
                  onChange={(key) => setActiveDomain(key)}
                />
              )}
              {domainsErr ? (
                <div className="mt-4 flex items-center gap-2 text-xs">
                  <span className="text-red-600 dark:text-red-400">领域列表没读到：{domainsErr}</span>
                  <button
                    onClick={() => void loadDomains()}
                    className="rounded border border-slate-200 px-2 py-0.5 text-11 text-slate-600 hover:border-blue-300 dark:border-slate-600 dark:text-slate-300"
                  >
                    重试
                  </button>
                </div>
              ) : domains.length === 0 && (
                <p className="mt-4 text-xs text-slate-400 dark:text-slate-500">正在加载领域…</p>
              )}
              {activeDomain && (
                <section className="mt-4">
                  {activeDomain === "health" ? (
                    <DataManagement />
                  ) : (
                    <GenericDomainData
                      key={activeDomain}
                      domain={activeDomain}
                      displayName={
                        domains.find((d) => d.id === activeDomain)?.display_name ?? activeDomain
                      }
                    />
                  )}
                </section>
              )}
            </>
          </>
        )}
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
  const confirm = useConfirm();
  const [status, setStatus] = useState<{ ok: boolean; msg: string } | null>(null);
  const [adding, setAdding] = useState(false);
  // 分页：数据页一屏只看得下十几条，整表返回会让"补录几十份之后打开数据页"变慢
  // （审查报告 P2）。`total` 用于显示"共 N 条"与翻页边界。
  const PAGE_SIZE = 20;
  const [page, setPage] = useState(0);
  const [total, setTotal] = useState(0);
  // 连点翻页的竞态守卫：慢到的旧页不许顶着新页码留在屏上（表格没有轮询，错一次就一直错）。
  const pageSeqRef = useRef(0);

  const load = useCallback(async () => {
    const req = ++pageSeqRef.current;
    const res = await api.get<{ items: ReportRecord[]; total: number }>(
      `/api/records?limit=${PAGE_SIZE}&offset=${page * PAGE_SIZE}`,
    );
    if (req !== pageSeqRef.current) return; // 已翻到别的页：这份旧页整包作废
    setReports(res.items);
    setTotal(res.total);
  }, [page]);
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
      setStatus({ ok: true, msg: "已保存修正" });
      await load();
    } catch (e) {
      setStatus({ ok: false, msg: `保存失败：${describeError(e)}` });
    }
  }

  async function remove(kind: "report" | "index", id: string) {
    try {
      // 后端没有 /api/records/{kind}/{id} 这条通配路由（契约尺子 `R102-50` 抓的现行）——
      // 两类各有一条真实端点：DELETE /api/records/report/{report_id} 与 /index/{index_id}。
      await api.del(
        kind === "report" ? `/api/records/report/${id}` : `/api/records/index/${id}`,
      );
      setStatus({ ok: true, msg: "已删除" });
      await load();
    } catch (e) {
      setStatus({ ok: false, msg: `删除失败：${describeError(e)}` });
    }
  }

  return (
    <div className="space-y-3">
      <div className="flex items-start justify-between gap-3">
        <p className="text-11 leading-relaxed text-slate-400 dark:text-slate-500">
          主流程是<b>上传报告 / 图片让 AI 解析</b>；这里只是补录入口 —— 手填的数据默认带
          【未经人工校验】标记。
        </p>
        <Button
          variant="outline"
          size="sm"
          type="button"
          className="shrink-0 text-xs"
          onClick={() => setAdding((v) => !v)}
        >
          {adding ? "取消" : "＋ 新增报告"}
        </Button>
      </div>
      {adding && (
        <AddReportForm
          onDone={() => {
            setAdding(false);
            setStatus({ ok: true, msg: "已新增报告" });
            load().catch(() => undefined);
          }}
        />
      )}
      {status && <Notice tone={status.ok ? "ok" : "error"}>{status.msg}</Notice>}
      {total > PAGE_SIZE && (
        <Card className="flex items-center justify-between px-3 py-2 text-xs text-slate-500 dark:text-slate-400">
          <span>
            共 <b>{total}</b> 条，当前第 {page * PAGE_SIZE + 1}–
            {Math.min(total, (page + 1) * PAGE_SIZE)} 条
          </span>
          <span className="flex items-center gap-1">
            <Button
              variant="outline"
              type="button"
              className="px-2 py-0.5 text-xs"
              disabled={page === 0}
              onClick={() => setPage((p) => Math.max(0, p - 1))}
            >
              上一页
            </Button>
            <Button
              variant="outline"
              type="button"
              className="px-2 py-0.5 text-xs"
              disabled={(page + 1) * PAGE_SIZE >= total}
              onClick={() => setPage((p) => p + 1)}
            >
              下一页
            </Button>
          </span>
        </Card>
      )}
      {reports.length === 0 ? (
        <div className={`rounded-lg border border-dashed border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 text-center text-xs text-slate-400 dark:text-slate-500 ${compact ? "p-4" : "p-8 text-sm"}`}>
          该域还没有数据：在对话页上传报告 / 图片（自动解析入索引），或点上方「＋ 新增报告」手动补录。
        </div>
      ) : (
        reports.map((r) => (
        <Card key={r.report_id} className="overflow-hidden">
          <div className="flex items-center justify-between border-b border-slate-100 bg-slate-50/70 px-3 py-2 dark:border-slate-700/60 dark:bg-slate-700/30">
            <div className="text-xs">
              <span className="font-medium text-slate-800 dark:text-slate-100">
                {String(r.check_time).slice(0, 10)} · {r.report_type}
              </span>
              {r.institution && <span className="ml-2 text-slate-400 dark:text-slate-500">{r.institution}</span>}
              {r.note && <span className="ml-2 text-slate-400 dark:text-slate-500">备注：{r.note}</span>}
            </div>
            <Button
              variant="ghost"
              type="button"
              className="px-2 py-1 text-11 text-red-400 dark:text-red-500 hover:bg-red-50 dark:bg-red-900/30 hover:text-red-600 dark:text-red-400 dark:text-red-500"
              onClick={async () => {
                if (await confirm({ title: "删除整份报告？", body: "将删除该报告及其全部指标记录，不可恢复。", confirmText: "确认删除", danger: true })) remove("report", r.report_id ?? "");
              }}
            >
              删除报告
            </Button>
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
              {(r.indices ?? []).map((i) => (
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
                          <Button variant="primary" size="sm" type="button" className="px-2.5 py-1" onClick={() => saveEdit(i.index_id ?? "")}>
                            保存
                          </Button>
                          <Button variant="ghost" size="sm" type="button" className="px-2 py-1" onClick={() => setEditId(null)}>
                            取消
                          </Button>
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
                        <Button variant="ghost" size="sm" type="button" className="px-2 py-1 text-blue-600 dark:text-blue-400" onClick={() => startEdit(i.index_id ?? "", { ...i, is_verified: i.is_verified ?? 0 })}>
                          修正
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          type="button"
                          className="px-2 py-1 text-red-400 dark:text-red-500 hover:bg-red-50 dark:bg-red-900/30 hover:text-red-600 dark:text-red-400 dark:text-red-500"
                          onClick={async () => {
                            if (await confirm({ title: "删除这条指标记录？", body: "将删除该指标项，不可恢复。", confirmText: "确认删除", danger: true })) remove("index", i.index_id ?? "");
                          }}
                        >
                          删除
                        </Button>
                      </td>
                    </>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        </Card>
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
      setErr(`保存失败：${describeError(e)}`);
    } finally {
      setBusy(false);
    }
  }

  const inputCls =
    "rounded border border-slate-200 dark:border-slate-700 px-2 py-1 text-xs outline-none focus:border-blue-400";

  return (
    <div className="rounded-lg border border-blue-200 dark:border-blue-800 bg-blue-50 dark:bg-blue-900/30 p-3">
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
        <Button
          variant="outline"
          size="sm"
          type="button"
          className="px-2 py-1 text-xs"
          onClick={() => setRows((rs) => [...rs, { rowId: nextRowId(), name: "", value: "", unit: "" }])}
        >
          ＋ 加一行
        </Button>
        <Button variant="primary" size="sm" type="button" className="px-3 py-1 text-xs" disabled={busy} onClick={submit}>
          {busy ? "保存中…" : "保存"}
        </Button>
        <Button variant="ghost" size="sm" type="button" className="px-2 py-1 text-xs" onClick={onDone}>
          取消
        </Button>
        <span className="text-11 text-slate-400 dark:text-slate-500">手填默认标记为【未经人工校验】</span>
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
  const confirm = useConfirm();
  const [status, setStatus] = useState<{ ok: boolean; msg: string } | null>(null);
  // 快速切域的竞态守卫：A 域的慢响应不许顶着 B 域的名字留在屏上（表格不自愈）。
  const reqRef = useRef(0);
  // 分页（2026-10-30 交互审计：整表渲染是卡顿隐患，与 health 域口径看齐）。
  // 多取一条探"有没有下一页"，响应形状不动。组件按域 key 重挂 → 翻页态天然归零。
  const PAGE_SIZE = 20;
  const [page, setPage] = useState(0);
  const [hasMore, setHasMore] = useState(false);

  const load = useCallback(async () => {
    const req = ++reqRef.current;
    const rows = await api.listDomainRecords(domain, {
      limit: PAGE_SIZE + 1,
      offset: page * PAGE_SIZE,
    });
    if (req !== reqRef.current) return; // 已切到别的域/页：这份旧表整包作废
    setHasMore(rows.length > PAGE_SIZE);
    setRecords(rows.slice(0, PAGE_SIZE));
  }, [domain, page]);

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
      setStatus({ ok: true, msg: "已保存" });
      await load();
    } catch (e) {
      setStatus({ ok: false, msg: `保存失败：${describeError(e)}` });
    }
  }

  async function remove(id: string) {
    try {
      await api.deleteDomainRecord(domain, id);
      setStatus({ ok: true, msg: "已删除" });
      await load();
    } catch (e) {
      setStatus({ ok: false, msg: `删除失败：${describeError(e)}` });
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
      setStatus({ ok: true, msg: "已新增" });
      await load();
    } catch (e) {
      setStatus({ ok: false, msg: `保存失败：${describeError(e)}` });
    }
  }

  const cls =
    "rounded border border-slate-200 dark:border-slate-700 px-2 py-1 text-xs outline-none focus:border-blue-400";

  return (
    <div className="space-y-3">
      <div className="flex items-start justify-between gap-3">
        <p className="text-11 leading-relaxed text-slate-400 dark:text-slate-500">
          「{displayName}」域的通用数据（标签 + 数值/文本 + 单位 + 备注）。新增领域插件即自动出现此视图。
        </p>
        <Button
          variant="outline"
          size="sm"
          type="button"
          className="shrink-0 text-xs"
          onClick={() => setAdding((v) => !v)}
        >
          {adding ? "取消" : "＋ 新增记录"}
        </Button>
      </div>
      {status && <Notice tone={status.ok ? "ok" : "error"}>{status.msg}</Notice>}
      {adding && (
        <div className="rounded-lg border border-blue-200 dark:border-blue-800 bg-blue-50 dark:bg-blue-900/30 p-3">
          <div className="flex flex-wrap items-center gap-2">
            <input value={draft.label} onChange={(e) => setDraft({ ...draft, label: e.target.value })} placeholder="标签 *（如 体重）" className={`${cls} w-40`} />
            <input value={draft.value_num} onChange={(e) => setDraft({ ...draft, value_num: e.target.value })} placeholder="数值（如 62）" className={`${cls} w-28`} />
            <input value={draft.value_text} onChange={(e) => setDraft({ ...draft, value_text: e.target.value })} placeholder="或文本值" className={`${cls} w-40`} />
            <input value={draft.unit} onChange={(e) => setDraft({ ...draft, unit: e.target.value })} placeholder="单位（可选）" className={`${cls} w-20`} />
            <input value={draft.note} onChange={(e) => setDraft({ ...draft, note: e.target.value })} placeholder="备注（可选）" className={`${cls} w-32`} />
          </div>
          <div className="mt-2 flex items-center gap-2">
            <Button variant="primary" onClick={submit}
              className="rounded px-3 py-1 text-xs hover:bg-blue-700">保存</Button>
            <button onClick={() => setAdding(false)} className="rounded px-2 py-1 text-xs text-slate-500 dark:text-slate-400 hover:bg-slate-100 dark:bg-slate-700/50 dark:hover:bg-slate-700">取消</button>
          </div>
        </div>
      )}
      {records.length === 0 ? (
        <div className="rounded-lg border border-dashed border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 text-center text-xs text-slate-400 dark:text-slate-500 p-8">
          该域还没有数据：点上方「＋ 新增记录」补录。
        </div>
      ) : (
        <Card className="overflow-x-auto">
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
                          <Button variant="primary" onClick={() => saveEdit(r.id)}
                            className="rounded px-2.5 py-1 hover:bg-blue-700">保存</Button>
                          <button onClick={() => setEditId(null)} className="rounded px-2 py-1 text-slate-500 dark:text-slate-400 hover:bg-slate-100 dark:bg-slate-700/50 dark:hover:bg-slate-700">取消</button>
                        </div>
                      </td>
                    </>
                  ) : (
                    <>
                      <td className="px-3 py-2 font-medium text-slate-700 dark:text-slate-200">{r.label}</td>
                      <td className="px-3 py-2 text-slate-600 dark:text-slate-400">{fmt(r)}</td>
                      <td className="px-3 py-2 text-slate-400 dark:text-slate-500">{r.note || "—"}</td>
                      <td className="px-3 py-2 text-right">
                        <Button variant="ghost" size="sm" type="button" className="px-2 py-1 text-blue-600 dark:text-blue-400" onClick={() => startEdit(r)}>修正</Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          type="button"
                          className="px-2 py-1 text-red-400 dark:text-red-500 hover:bg-red-50 dark:bg-red-900/30 hover:text-red-600 dark:text-red-400 dark:text-red-500"
                          onClick={async () => {
                            if (await confirm({ title: "删除这条记录？", body: "将删除该条领域数据，不可恢复。", confirmText: "确认删除", danger: true })) remove(r.id);
                          }}
                        >
                          删除
                        </Button>
                      </td>
                    </>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
          {(hasMore || page > 0) && (
            <div className="mt-3 flex items-center justify-between text-xs text-slate-400 dark:text-slate-500">
              <span>
                第 {page * PAGE_SIZE + 1}–{page * PAGE_SIZE + records.length} 条
                {hasMore ? "（还有更多）" : ""}
              </span>
              <span className="flex gap-1.5">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={page === 0}
                  onClick={() => setPage((p) => p - 1)}
                >
                  上一页
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={!hasMore}
                  onClick={() => setPage((p) => p + 1)}
                >
                  下一页
                </Button>
              </span>
            </div>
          )}
        </Card>
      )}
    </div>
  );
}
