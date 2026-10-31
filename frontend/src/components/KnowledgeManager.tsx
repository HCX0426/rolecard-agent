/** 知识库管理块（2026-10-24 IA 重构：原「知识库」页的管理面搬到这里，住进「资料」页）。
 *
 *  新增的两件管理能力（此前只能被对话页上传"顺便"创建）：
 *  * 新建知识库：空的 RAG 作用域，建完即出现在角色卡的作用域下拉；
 *  * 导入文档：多选文件 → POST /api/knowledge/scopes/{scope}/documents（后台任务）→
 *    轮询 /api/uploads/tasks/{id}（与会话上传同源）→ 完成后刷新概览。
 *
 *  保留的原有能力：来源浏览、重建（清空）、检索延迟细分、上传目录回收。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { Button, Card, Notice } from "./ui";
import {
  api,
  type CleanupResult,
  type KnowledgeScope,
  type OrphanReport,
  type RagMetrics,
  type RagStageMs,
} from "../api";
import { useConfirm } from "../hooks/useConfirm";
import { formatBytes } from "../lib/uploadOutcome";
import { describeError } from '../lib/errors';

// 检索延迟细分：阶段顺序 + 空值显示（无样本时后端返回 null，显示 "—" 而非 0）。
const LATENCY_STAGES: [keyof RagStageMs, string][] = [
  ["embed_ms", "嵌入"],
  ["vector_ms", "向量检索"],
  ["rerank_ms", "重排"],
  ["total_ms", "合计"],
];

function fmtMs(v: number | null | undefined): string {
  return v === null || v === undefined ? "—" : v.toFixed(2);
}

export function KnowledgeManager() {
  const [scopes, setScopes] = useState<KnowledgeScope[]>([]);
  const [metrics, setMetrics] = useState<RagMetrics | null>(null);
  // 状态条带 tone：从前是裸串 + Notice 默认 error —— "已创建/已导入"这些成功文案
  // 全被渲染成红条，每次成功看起来都像失败（2026-10-30 交互审计）。
  const [status, setStatus] = useState<{ tone: "ok" | "warn" | "error"; msg: string } | null>(null);
  const say = (tone: "ok" | "warn" | "error", msg: string) => setStatus({ tone, msg });
  const confirm = useConfirm();
  const [orphans, setOrphans] = useState<OrphanReport | null>(null);
  const [busy, setBusy] = useState(false);
  // 新建作用域
  const [newScope, setNewScope] = useState("");
  const [creating, setCreating] = useState(false);
  // 导入文档：每个作用域一个隐藏 file input（多选），导入中按 scope 记账
  const fileRef = useRef<HTMLInputElement>(null);
  const [importScope, setImportScope] = useState<string | null>(null);
  const [importingScopes, setImportingScopes] = useState<string[]>([]);

  const load = useCallback(async () => {
    setScopes(await api.get<KnowledgeScope[]>("/api/knowledge"));
    // 延迟指标是增强信息：失败静默（不打扰知识库主视图）。
    api
      .get<RagMetrics>("/api/rag/metrics")
      .then(setMetrics)
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    load().catch((e) => say("error", `加载失败：${e.message}`));
  }, [load]);

  /** 新建一个空作用域（POST /api/knowledge/scopes，幂等）。 */
  async function createScope() {
    const name = newScope.trim();
    if (!name) return;
    setCreating(true);
    try {
      await api.post<{ scope: string }>("/api/knowledge/scopes", { name });
      setNewScope("");
      say("ok", `已创建知识库 ${name} —— 在角色卡勾选作用域即可授权检索`);
      await load();
    } catch (e) {
      say("error", `创建失败：${describeError(e)}`);
    } finally {
      setCreating(false);
    }
  }

  /** 向作用域导入文档（多选）：逐个 POST（后台任务），全部排队后轮询到齐再刷新。 */
  async function importDocuments(scope: string, files: FileList) {
    const importing = Array.from(files);
    setImportingScopes((s) => [...s, scope]);
    try {
      const tasks: string[] = [];
      for (const f of importing) {
        const form = new FormData();
        form.append("file", f);
        const body = await api.post<{ task_id: string; queued: boolean }>(
          `/api/knowledge/scopes/${encodeURIComponent(scope)}/documents`,
          form,
        );
        if (body.queued) tasks.push(body.task_id);
      }
      // 轮询到全部任务收尾（台账 failed 也算收尾——进度端点会带回错误）
      const deadline = Date.now() + 180_000;
      while (Date.now() < deadline) {
        const states = await Promise.all(
          tasks.map((id) => api.get<{ running: boolean; status: string; error: string | null }>(
            `/api/uploads/tasks/${id}`,
          )),
        );
        if (states.every((s) => !s.running)) {
          const failed = states.filter((s) => s.status === "failed").length;
          say(
            failed ? "warn" : "ok",
            failed
              ? `导入完成，但有 ${failed} 个文件失败（详情见进度条目）`
              : `已导入 ${tasks.length} 个文档到 ${scope}`,
          );
          break;
        }
        await new Promise((r) => setTimeout(r, 300));
      }
      await load();
    } catch (e) {
      say("error", `导入失败：${describeError(e)}`);
    } finally {
      setImportingScopes((s) => s.filter((x) => x !== scope));
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  /** 清空一个作用域（破坏性）：换嵌入模型后维度不兼容时的重建入口，走二次确认 + 审计。 */
  async function resetScope(scope: string) {
    try {
      const r = await api.del<{ removed_chunks: number }>(
        `/api/knowledge/${encodeURIComponent(scope)}`,
      );
      say("ok", `已清空作用域 ${scope}（移除 ${r.removed_chunks} 段，写入审计）`);
      await load();
    } catch (e) {
      say("error", `清空失败：${describeError(e)}`);
    }
  }

  /** 盘点上传目录里的孤儿文件（**只读**，不动任何文件）。 */
  async function scanOrphans() {
    setBusy(true);
    try {
      setOrphans(await api.get<OrphanReport>("/api/uploads/orphans"));
    } catch (e) {
      say("error", `盘点失败：${describeError(e)}`);
    } finally {
      setBusy(false);
    }
  }

  /** 回收孤儿文件（破坏性）：先盘点给人看清单，确认后才执行，后端写审计。 */
  async function cleanupOrphans() {
    setBusy(true);
    try {
      const r = await api.post<CleanupResult>("/api/uploads/cleanup", {});
      setOrphans(null);
      say(
        "ok",
        r.deleted === 0
          ? `没有可回收的文件（扫描 ${r.scanned} 个，全部被台账引用）`
          : `已回收 ${r.deleted} 个文件，释放 ${formatBytes(r.freed_bytes)}（写入审计）`,
      );
    } catch (e) {
      say("error", `回收失败：${describeError(e)}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mt-4">
      {status && <Notice tone={status.tone}>{status.msg}</Notice>}

      {/* 新建知识库 + 导入文档的操作行 */}
      <Card className="p-4">
        <div className="flex flex-wrap items-center gap-2">
          <input
            value={newScope}
            onChange={(e) => setNewScope(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && newScope.trim()) void createScope();
            }}
            placeholder="新知识库名（如 trip_notes）"
            className="flex-1 min-w-48 rounded-lg border border-slate-200 dark:border-slate-700 px-2.5 py-1.5 text-xs"
          />
          <Button
            variant="primary"
            size="sm"
            type="button"
            disabled={!newScope.trim() || creating}
            onClick={() => void createScope()}
          >
            {creating ? "创建中…" : "＋ 新建知识库"}
          </Button>
          <Button
            variant="outline"
            size="sm"
            type="button"
            disabled={importingScopes.length > 0}
            onClick={() => fileRef.current?.click()}
          >
            导入文档到选中库 →
          </Button>
          <input
            ref={fileRef}
            type="file"
            multiple
            accept=".txt,.md,.pdf,.docx,.pptx,.xlsx,image/*"
            className="hidden"
            onChange={(e) => {
              const scope = importScope;
              const files = e.target.files;
              if (scope && files && files.length) void importDocuments(scope, files);
            }}
          />
        </div>
        <p className="mt-1.5 text-11 leading-relaxed text-slate-400 dark:text-slate-500">
          「导入文档 →」作用于下面<b>选中的那个库</b>（点卡片选中）；支持 .txt/.md/.pdf/
          .docx/.pptx/.xlsx 与图片（OCR）。导入走后台任务，完成后概览自动刷新。
        </p>
      </Card>

      {metrics && (
        <Card className="mt-3 p-4">
          <div className="flex items-center justify-between">
            <span className="text-sm font-medium text-slate-700 dark:text-slate-200">检索延迟细分（ms）</span>
            <span className="text-xs text-slate-400 dark:text-slate-500">
              样本 {metrics.samples} · 嵌入 {metrics.embedder} · 重排
              {metrics.rerank_enabled ? "开" : "关"}
            </span>
          </div>
          {metrics.samples === 0 ? (
            <p className="mt-2 text-xs text-slate-400 dark:text-slate-500">
              尚无检索样本：在对话里提问一次（触发 search_knowledge）即可看到分位。
            </p>
          ) : (
            <table className="mt-2 w-full text-left text-xs">
              <thead className="text-slate-400 dark:text-slate-500">
                <tr>
                  <th className="py-1 font-normal">阶段</th>
                  <th className="py-1 font-normal">P50</th>
                  <th className="py-1 font-normal">P95</th>
                  <th className="py-1 font-normal">P99</th>
                </tr>
              </thead>
              <tbody className="text-slate-600 dark:text-slate-300">
                {LATENCY_STAGES.map(([key, label]) => (
                  <tr key={key} className="border-t border-slate-100 dark:border-slate-800">
                    <td className="py-1">{label}</td>
                    <td className="py-1">{fmtMs(metrics.p50[key])}</td>
                    <td className="py-1">{fmtMs(metrics.p95[key])}</td>
                    <td className="py-1">{fmtMs(metrics.p99[key])}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
      )}

      <div className="mt-3 space-y-3">
        {scopes.length === 0 && (
          <Card className="border-dashed p-8 text-center text-sm text-slate-400 dark:text-slate-500">
            还没有知识库：上面新建一个，或在对话页上传 .txt/.md/.pdf 等文档自动建库。
          </Card>
        )}
        {scopes.map((s) => {
          const selected = importScope === s.scope;
          return (
            <div
              key={s.scope}
              role="button"
              tabIndex={0}
              onClick={() => setImportScope(s.scope)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  setImportScope(s.scope);
                }
              }}
              title="点击选中：「导入文档 →」作用于选中的库"
              className={`rounded-xl border bg-white dark:bg-slate-800 p-4 cursor-pointer transition-colors outline-none focus-visible:border-blue-500 ${
                selected
                  ? "border-blue-400 dark:border-blue-600"
                  : "border-slate-200 hover:border-slate-300 dark:border-slate-700 dark:hover:border-slate-600"
              }`}
            >
              <div className="flex items-center justify-between">
                <div>
                  <code className="rounded bg-slate-100 dark:bg-slate-700/50 px-1.5 py-0.5 text-xs text-slate-700 dark:text-slate-200">
                    {s.scope}
                  </code>
                  <span className="ml-2 text-xs text-slate-500 dark:text-slate-400">{s.chunks} 段</span>
                  {/* R1031-33：每作用域吃盘字节（量的是 HNSW 落盘目录，不是文本大小）。
                      null = 没量到（账读不出）—— 如实显示"体积未知"，不拿 0 冒充。 */}
                  <span className="ml-2 text-xs text-slate-500 dark:text-slate-400">
                    {s.bytes === null || s.bytes === undefined ? "体积未知" : formatBytes(s.bytes)}
                  </span>
                  <span className="ml-2 rounded-full bg-slate-100 dark:bg-slate-700/50 px-2 py-0.5 text-11 text-slate-500 dark:text-slate-400">
                    嵌入：{s.embedder}
                  </span>
                  {importingScopes.includes(s.scope ?? "") && (
                    <span className="ml-2 text-11 text-blue-600 dark:text-blue-400">导入中…</span>
                  )}
                </div>
                <Button
                  variant="outline"
                  size="sm"
                  type="button"
                  className="text-xs hover:border-red-300 hover:text-red-600 dark:hover:border-red-700 dark:hover:text-red-400"
                  onClick={async (e) => {
                    e.stopPropagation();
                    if (await confirm({ title: "清空知识库作用域？", body: `将清空作用域 ${s.scope} 的 ${s.chunks} 段内容，此操作不可恢复，且写入审计。`, confirmText: "确认清空", danger: true })) void resetScope(s.scope ?? "");
                  }}
                  title="删除该作用域的集合 —— 换嵌入模型后维度不兼容时用它重建（写审计）"
                >
                  重建（清空）
                </Button>
              </div>
              {/* 来源文件 chips：比裸文本列表更易扫读（行数多时两列排布）。 */}
              <ul className="mt-2 grid grid-cols-1 gap-1 sm:grid-cols-2">
                {(s.sources ?? []).map((src) => (
                  <li
                    key={src}
                    className="truncate rounded bg-slate-50 px-2 py-0.5 font-mono text-11 text-slate-500 dark:bg-slate-700/40 dark:text-slate-400"
                    title={src}
                  >
                    {src}
                  </li>
                ))}
              </ul>
            </div>
          );
        })}

        {/* 上传目录回收：只增不减的目录需要一个出口，但它是**删用户文件**的动作，
            所以做成"先盘点 → 人看清单 → 二次确认 → 执行"，而不是一键清理。 */}
        <Card className="p-4">
          <div className="flex items-center justify-between gap-3">
            <div>
              <span className="text-sm font-medium text-slate-700 dark:text-slate-200">
                上传目录清理
              </span>
              <p className="mt-0.5 text-xs text-slate-400 dark:text-slate-500">
                回收 data/uploads 里没有被任何登记任务引用的文件（旧版本重复上传留下的副本、
                解析中断的半成品）。被引用的文件与在用的解析文本副本不会被动。
              </p>
            </div>
            <Button
              variant="outline"
              size="sm"
              type="button"
              className="shrink-0 text-xs"
              disabled={busy}
              onClick={scanOrphans}
            >
              {busy ? "处理中…" : "检查可回收文件"}
            </Button>
          </div>

          {orphans && (
            <div className="mt-3 rounded-lg bg-slate-50 dark:bg-slate-800/50 p-3">
              <p className="text-xs text-slate-600 dark:text-slate-300">
                扫描 {orphans.scanned} 个文件，其中 {orphans.referenced} 个被台账引用；
                可回收 <b>{(orphans.orphans ?? []).length}</b> 个（
                {formatBytes(orphans.total_bytes ?? 0)}）。
              </p>
              {(orphans.dangling ?? []).length > 0 && (
                <p className="mt-1 text-11 leading-relaxed text-amber-700 dark:text-amber-400">
                  另有 <b>{(orphans.dangling ?? []).length}</b> 行台账指向这台机器上不存在的原件
                  （{(orphans.dangling ?? []).slice(0, 3).join("、")}
                  {(orphans.dangling ?? []).length > 3 ? " 等" : ""}）—— 换过数据根时是这个样子，
                  原件在另一份数据目录里。
                </p>
              )}
              {(orphans.orphans ?? []).length > 0 && (
                <>
                  <ul className="mt-1.5 max-h-40 space-y-0.5 overflow-y-auto">
                    {(orphans.orphans ?? []).map((o) => (
                      <li
                        key={o.name}
                        className="flex items-center justify-between gap-2 text-11 text-slate-500 dark:text-slate-400"
                      >
                        <code className="truncate">{o.name}</code>
                        <span className="shrink-0">
                          {o.companion ? "解析副本 · " : ""}
                          {formatBytes(o.size ?? 0)}
                        </span>
                      </li>
                    ))}
                  </ul>
                  <div className="mt-2 flex items-center gap-2">
                    <span className="text-11 text-red-600 dark:text-red-400 dark:text-red-500">
                      删除后不可恢复，确认回收？
                    </span>
                    <Button
                      variant="danger"
                      type="button"
                      className="px-2 py-1 text-11"
                      disabled={busy}
                      onClick={cleanupOrphans}
                    >
                      确认回收
                    </Button>
                    <Button variant="ghost" type="button" className="px-2 py-1 text-11" onClick={() => setOrphans(null)}>
                      取消
                    </Button>
                  </div>
                </>
              )}
            </div>
          )}
        </Card>
      </div>
    </div>
  );
}
