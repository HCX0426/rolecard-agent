import { useCallback, useEffect, useState } from "react";
import {
  api,
  type CleanupResult,
  type KnowledgeScope,
  type OrphanReport,
  type RagMetrics,
  type RagStageMs,
} from "../api";
import { formatBytes } from "../lib/uploadOutcome";

// ---------------------------------------------------------------- 知识库（v2.1 RAG）

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

function KnowledgePanel() {
  const [scopes, setScopes] = useState<KnowledgeScope[]>([]);
  const [metrics, setMetrics] = useState<RagMetrics | null>(null);
  const [status, setStatus] = useState("");
  const [confirmReset, setConfirmReset] = useState<string | null>(null);
  const [orphans, setOrphans] = useState<OrphanReport | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setScopes(await api.get<KnowledgeScope[]>("/api/knowledge"));
    // 延迟指标是增强信息：失败静默（不打扰知识库主视图）。
    api
      .get<RagMetrics>("/api/rag/metrics")
      .then(setMetrics)
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    load().catch((e) => setStatus(`加载失败：${e.message}`));
  }, [load]);

  /** 清空一个作用域（破坏性）：换嵌入后端后维度不兼容时的重建入口，走二次确认 + 审计。 */
  async function resetScope(scope: string) {
    try {
      const r = await api.del<{ removed_chunks: number }>(
        `/api/knowledge/${encodeURIComponent(scope)}`,
      );
      setConfirmReset(null);
      setStatus(`已清空作用域 ${scope}（移除 ${r.removed_chunks} 段，写入审计）`);
      await load();
    } catch (e) {
      setStatus(`清空失败：${(e as Error).message}`);
    }
  }

  /** 盘点上传目录里的孤儿文件（**只读**，不动任何文件）。 */
  async function scanOrphans() {
    setBusy(true);
    try {
      setOrphans(await api.get<OrphanReport>("/api/uploads/orphans"));
    } catch (e) {
      setStatus(`盘点失败：${(e as Error).message}`);
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
      setStatus(
        r.deleted === 0
          ? `没有可回收的文件（扫描 ${r.scanned} 个，全部被台账引用）`
          : `已回收 ${r.deleted} 个文件，释放 ${formatBytes(r.freed_bytes)}（写入审计）`,
      );
    } catch (e) {
      setStatus(`回收失败：${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mt-6">
      {status && <p className="rounded-lg bg-red-50 dark:bg-red-900/30 px-3 py-2 text-xs text-red-600 dark:text-red-400 dark:text-red-500">{status}</p>}
      <p className="rounded-lg bg-slate-50 dark:bg-slate-800/50 px-3 py-2.5 text-xs leading-relaxed text-slate-400 dark:text-slate-500">
        知识库是<b>内核能力</b>（search_knowledge），不属于任何插件：库归内核，角色经
        knowledge_scopes 声明可检索的作用域（角色卡页配置）。上传 .txt/.md/.pdf/.docx/.pptx/.xlsx
        或图片会自动入库到 health_reports 作用域；切换嵌入后端后删除 data/chroma 目录重启即重建。
      </p>

      {metrics && (
        <div className="mt-3 rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 p-4">
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
              <tbody className="text-slate-600 dark:text-slate-300 dark:text-slate-600">
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
        </div>
      )}

      <div className="mt-3 space-y-3">
        {scopes.length === 0 && (
          <div className="rounded-xl border border-dashed border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 p-8 text-center text-sm text-slate-400 dark:text-slate-500">
            知识库还是空的：在对话页上传 .txt/.md 文档，或运行 scripts/seed_demo_data.py
            注入演示知识。
          </div>
        )}
        {scopes.map((s) => (
          <div key={s.scope} className="rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 p-4">
            <div className="flex items-center justify-between">
              <div>
                <code className="rounded bg-slate-100 dark:bg-slate-700/50 px-1.5 py-0.5 text-xs text-slate-700 dark:text-slate-200">
                  {s.scope}
                </code>
                <span className="ml-2 text-xs text-slate-500 dark:text-slate-400 dark:text-slate-500">{s.chunks} 段</span>
                <span className="ml-2 rounded-full bg-slate-100 dark:bg-slate-700/50 px-2 py-0.5 text-[11px] text-slate-500 dark:text-slate-400 dark:text-slate-500">
                  嵌入：{s.embedder}
                </span>
              </div>
              {confirmReset === s.scope ? (
                <div className="flex items-center gap-2">
                  <span className="text-[11px] text-red-600 dark:text-red-400 dark:text-red-500">
                    清空 {s.chunks} 段且不可恢复？
                  </span>
                  <button
                    onClick={() => resetScope(s.scope)}
                    className="rounded bg-red-500 px-2 py-1 text-[11px] text-white hover:bg-red-600"
                  >
                    确认清空
                  </button>
                  <button
                    onClick={() => setConfirmReset(null)}
                    className="rounded px-2 py-1 text-[11px] text-slate-500 dark:text-slate-400 dark:text-slate-500 hover:bg-slate-100 dark:bg-slate-700/50 dark:hover:bg-slate-700"
                  >
                    取消
                  </button>
                </div>
              ) : (
                <button
                  onClick={() => setConfirmReset(s.scope)}
                  title="删除该作用域的集合 —— 换嵌入后端后维度不兼容时用它重建（写审计）"
                  className="rounded-lg border border-slate-200 dark:border-slate-700 px-2.5 py-1 text-xs text-slate-500 dark:text-slate-400 dark:text-slate-500 hover:border-red-300 dark:hover:border-red-700 hover:text-red-600 dark:text-red-400 dark:text-red-500"
                >
                  重建（清空）
                </button>
              )}
            </div>
            <ul className="mt-2 space-y-0.5">
              {s.sources.map((src) => (
                <li key={src} className="text-xs text-slate-500 dark:text-slate-400 dark:text-slate-500">
                  · {src}
                </li>
              ))}
            </ul>
          </div>
        ))}

        {/* 上传目录回收：只增不减的目录需要一个出口，但它是**删用户文件**的动作，
            所以做成"先盘点 → 人看清单 → 二次确认 → 执行"，而不是一键清理。 */}
        <div className="rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 p-4">
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
            <button
              onClick={scanOrphans}
              disabled={busy}
              className="shrink-0 rounded-lg border border-slate-200 dark:border-slate-700 px-2.5 py-1 text-xs text-slate-600 dark:text-slate-300 dark:text-slate-600 hover:border-blue-300 dark:hover:border-blue-700 hover:text-blue-600 dark:text-blue-400 disabled:opacity-50"
            >
              {busy ? "处理中…" : "检查可回收文件"}
            </button>
          </div>

          {orphans && (
            <div className="mt-3 rounded-lg bg-slate-50 dark:bg-slate-800/50 p-3">
              <p className="text-xs text-slate-600 dark:text-slate-300 dark:text-slate-600">
                扫描 {orphans.scanned} 个文件，其中 {orphans.referenced} 个被台账引用；
                可回收 <b>{orphans.orphans.length}</b> 个（{formatBytes(orphans.total_bytes)}）。
              </p>
              {orphans.orphans.length > 0 && (
                <>
                  <ul className="mt-1.5 max-h-40 space-y-0.5 overflow-y-auto">
                    {orphans.orphans.map((o) => (
                      <li
                        key={o.name}
                        className="flex items-center justify-between gap-2 text-[11px] text-slate-500 dark:text-slate-400 dark:text-slate-500"
                      >
                        <code className="truncate">{o.name}</code>
                        <span className="shrink-0">
                          {o.companion ? "解析副本 · " : ""}
                          {formatBytes(o.size)}
                        </span>
                      </li>
                    ))}
                  </ul>
                  <div className="mt-2 flex items-center gap-2">
                    <span className="text-[11px] text-red-600 dark:text-red-400 dark:text-red-500">
                      删除后不可恢复，确认回收？
                    </span>
                    <button
                      onClick={cleanupOrphans}
                      disabled={busy}
                      className="rounded bg-red-500 px-2 py-1 text-[11px] text-white hover:bg-red-600 disabled:opacity-50"
                    >
                      确认回收
                    </button>
                    <button
                      onClick={() => setOrphans(null)}
                      className="rounded px-2 py-1 text-[11px] text-slate-500 dark:text-slate-400 dark:text-slate-500 hover:bg-slate-100 dark:bg-slate-700/50 dark:hover:bg-slate-700"
                    >
                      取消
                    </button>
                  </div>
                </>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}


// 知识库（RAG）—— 自"设置"升为独立顶层页。
// 理由：检索是内核能力（search_knowledge），与"插件启停"是两件事；混在一起正是
// 「插件页到底是 MCP 还是 RAG」这一困惑的来源（见 docs/UI设计与信息架构（修订）.md）。
export default function KnowledgePage({ onOpenChat }: { onOpenChat?: () => void }) {
  return (
    <div className="h-full overflow-y-auto p-6">
      <div className="mx-auto max-w-3xl">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h2 className="text-base font-semibold text-slate-900 dark:text-slate-100">知识库</h2>
            <p className="mt-0.5 text-xs text-slate-400 dark:text-slate-500">
              检索是<b>内核能力</b>（search_knowledge），不属于任何插件：库归内核，角色经
              knowledge_scopes 声明可检索的作用域。
            </p>
          </div>
          <button
            onClick={() => onOpenChat?.()}
            className="shrink-0 rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 px-3 py-1.5 text-xs text-slate-600 dark:text-slate-300 dark:text-slate-600 hover:border-blue-300 dark:hover:border-blue-700 hover:text-blue-600 dark:text-blue-400"
            title="上传必须在某个会话里进行（解析结果会注入该会话）"
          >
            去对话页上传文档 →
          </button>
        </div>

        <KnowledgePanel />
      </div>
    </div>
  );
}
