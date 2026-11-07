import { useCallback, useEffect, useRef, useState } from "react";
import { api, type DomainInfo, type GenericRecord } from "../api";
import { useConfirm } from "../hooks/useConfirm";
import { Button, Card, Notice, PageHeader, Tabs } from "../components/ui";
import { KnowledgeManager } from "../components/KnowledgeManager";
import { DataBackupCard } from "../components/DataBackupCard";
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
  // 域清单是否真的读回来过一次：空清单与"还没读回来"分开说（删域后归零是可达状态）。
  const [domainsLoaded, setDomainsLoaded] = useState(false);
  // 域增删（用户自己决定有哪些域）：输入框 + 一条在途旗防连点。
  const [newDomain, setNewDomain] = useState("");
  const [domainBusy, setDomainBusy] = useState(false);
  const confirmDomain = useConfirm();
  const loadDomains = useCallback(() => {
    return api
      .get<DomainInfo[]>("/api/domains")
      .then((list) => {
        setDomains(list);
        // 默认停在第一个域（还没选过时）—— 不要让"两个域都铺开"成为初始形态。
        setActiveDomain((cur) => cur || list[0]?.id || "");
        setDomainsErr("");
        setDomainsLoaded(true);
      })
      .catch((e) => setDomainsErr(describeError(e)));
  }, []);

  useEffect(() => {
    loadDomains();
  }, [loadDomains]);

  async function createDomain() {
    const id = newDomain.trim();
    if (!id) return;
    setDomainBusy(true);
    try {
      await api.post("/api/settings/domains", { id });
      setNewDomain("");
      await loadDomains();
    } catch (e) {
      setDomainsErr(describeError(e));
    } finally {
      setDomainBusy(false);
    }
  }

  async function removeDomain() {
    const id = activeDomain;
    if (!id) return;
    if (
      !(await confirmDomain({
        title: "删除这个域？",
        body: `将从资料页删除域「${id}」，它名下的全部结构化记录一并删除，不可恢复。`,
        confirmText: "确认删除",
        danger: true,
      }))
    )
      return;
    setDomainBusy(true);
    try {
      await api.del(`/api/settings/domains/${encodeURIComponent(id)}`);
      // 删的是当前选中域：清空选择，让 loadDomains 落到剩下的第一个（或归零）。
      setActiveDomain("");
      await loadDomains();
    } catch (e) {
      setDomainsErr(describeError(e));
    } finally {
      setDomainBusy(false);
    }
  }

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
            <div className="flex rounded-lg border border-slate-300 bg-surface p-0.5 shadow-sm">
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
                      : "text-slate-600 hover:text-blue-600 dark:hover:text-blue-400"
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
              {/* 域清单的增删（用户拍板："域让用户自己决定，不要卡死功能"）。两条都打
                  `/api/settings/domains` —— 后端把它们放在操作员档（与知识库写侧同一条
                  纪律），单机档本机来源永远放行，这里零感知。 */}
              <div className="mt-4 flex flex-wrap items-center gap-2">
                <input
                  value={newDomain}
                  onChange={(e) => setNewDomain(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") void createDomain();
                  }}
                  placeholder="新域名（小写字母开头，如 trip_notes）"
                  className="min-w-48 flex-1 rounded-lg border border-slate-200 px-2.5 py-1.5 text-xs outline-none focus:border-blue-400"
                />
                <Button
                  variant="outline"
                  size="sm"
                  type="button"
                  className="text-xs"
                  disabled={!newDomain.trim() || domainBusy}
                  onClick={() => void createDomain()}
                >
                  ＋ 新建域
                </Button>
                {activeDomain && (
                  <Button
                    variant="ghost"
                    size="sm"
                    type="button"
                    className="ml-auto text-xs text-red-600 hover:bg-red-50 dark:text-red-400 dark:hover:bg-red-900/30"
                    disabled={domainBusy}
                    onClick={() => void removeDomain()}
                  >
                    删除当前域
                  </Button>
                )}
              </div>
              {domainsErr ? (
                <div className="mt-4 flex items-center gap-2 text-xs">
                  <span className="text-red-600 dark:text-red-400">领域列表没读到：{domainsErr}</span>
                  <button
                    onClick={() => void loadDomains()}
                    className="rounded border border-slate-200 px-2 py-0.5 text-11 text-slate-600 hover:border-blue-300"
                  >
                    重试
                  </button>
                </div>
              ) : domains.length === 0 && (
                <p className="mt-4 text-xs text-slate-400">
                  {/* 2026-10-30 那条审计的另一半：删域成了可达状态后，"真的一个都没有"与
                      "还在读"必须分开，否则空清单会被那句永久 loading 话术盖住。 */}
                  {domainsLoaded
                    ? "还没有任何域：用上面「＋ 新建域」建一个，往里面补录结构化记录。"
                    : "正在加载领域…"}
                </p>
              )}
              {activeDomain && (
                <section className="mt-4">
                  <GenericDomainData
                    key={activeDomain}
                    domain={activeDomain}
                    displayName={
                      domains.find((d) => d.id === activeDomain)?.display_name ?? activeDomain
                    }
                  />
                </section>
              )}
            </>
          </>
        )}

        {/* 全量备份（R1031-15）：**两个 block 都看得到** —— 它备份的是"这台机器的她"
            （整根数据根），跟"当前看结构化记录还是知识库"无关。挂在页尾，
            不抢首屏（记录/知识库才是这一页的正事）。 */}
        <section className="mt-6">
          <DataBackupCard />
        </section>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- 通用领域数据

/** 通用数据视图：标签 + 数值/文本 + 单位 + 备注，走 /api/domains/{domain}/records。
 *
 * 出厂不带任何代码侧的域，所有域（用户自建的和将来代码侧再出厂的）都走这一份 ——
 * 「数据随领域归属」不再需要按域名分支，也就不再有第二套渲染逻辑。 */
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
    load().catch((e) => setStatus({ ok: false, msg: `加载失败：${describeError(e)}` }));
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
    "rounded border border-slate-200 px-2 py-1 text-xs outline-none focus:border-blue-400";

  return (
    <div className="space-y-3">
      <div className="flex items-start justify-between gap-3">
        <p className="text-11 leading-relaxed text-slate-400">
          「{displayName}」域的通用数据。
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
        <div className="rounded-lg border border-slate-600 bg-slate-100/70 p-3">
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
            <button onClick={() => setAdding(false)} className="rounded px-2 py-1 text-xs text-slate-500 hover:bg-slate-100">取消</button>
          </div>
        </div>
      )}
      {records.length === 0 ? (
        <div className="rounded-lg border border-dashed border-slate-200 bg-surface text-center text-xs text-slate-400 p-8">
          该域还没有数据：点上方「＋ 新增记录」补录。
        </div>
      ) : (
        <Card className="overflow-x-auto">
          <table className="w-full min-w-[26rem] text-left text-xs">
            <thead>
              <tr className="text-slate-400">
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
                          <input value={draft.unit} onChange={(e) => setDraft({ ...draft, unit: e.target.value })} placeholder="单位（可选）" className={`${cls} w-20`} />
                          <input value={draft.note} onChange={(e) => setDraft({ ...draft, note: e.target.value })} placeholder="备注" className={`${cls} w-28`} />
                          <Button variant="primary" onClick={() => saveEdit(r.id)}
                            className="rounded px-2.5 py-1 hover:bg-blue-700">保存</Button>
                          <button onClick={() => setEditId(null)} className="rounded px-2 py-1 text-slate-500 hover:bg-slate-100">取消</button>
                        </div>
                      </td>
                    </>
                  ) : (
                    <>
                      <td className="px-3 py-2 font-medium text-slate-700">{r.label}</td>
                      <td className="px-3 py-2 text-slate-600">{fmt(r)}</td>
                      <td className="px-3 py-2 text-slate-400">{r.note || "—"}</td>
                      <td className="px-3 py-2 text-right">
                        <Button variant="ghost" size="sm" type="button" className="px-2 py-1 text-blue-600 dark:text-blue-400" onClick={() => startEdit(r)}>修正</Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          type="button"
                          className="px-2 py-1 text-red-600 hover:bg-red-50 dark:text-red-400 dark:bg-red-900/30 dark:hover:bg-red-900/30"
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
            <div className="mt-3 flex items-center justify-between text-xs text-slate-400">
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
