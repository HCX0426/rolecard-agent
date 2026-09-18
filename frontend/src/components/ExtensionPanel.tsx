// 「扩展」子页签（设置页）：本系统对外部世界的连接集中在这里。
//   ① MCP server 接入（架构计划 C·§6.1，operator 主动接入；仅 http）
//   ② 模型后端连通性自检（复用既有 POST /api/services/check）
// 对齐主流 AI-IDE 的 MCP 界面惯例：每 server 一行 + 交通灯状态 + 可展开看工具 + 表单/粘贴 JSON
// 两种接入。安全红线：这是 operator 动作，绝不暴露成 LLM 自助安装；URL 过 SSRF 边界（后端校验）；
// headers 只写不回读（后端掩码，编辑留空=保留）。
import { useCallback, useEffect, useState } from "react";
import {
  api,
  type ConnectivityResult,
  type McpServer,
  type McpServersView,
  type McpTestResult,
} from "../api";

/** 每行的连接态：交通灯 + 展开时显示的工具/错误。 */
type RowTest = { phase: "idle" } | { phase: "testing" } | { phase: "done"; result: McpTestResult };

interface Draft {
  id: string;
  display_name: string;
  url: string;
  headers: [string, string][];
  enabled: boolean;
}

const EMPTY: Draft = { id: "", display_name: "", url: "", headers: [], enabled: true };

function headersToMap(rows: [string, string][]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [k, v] of rows) if (k.trim()) out[k.trim()] = v;
  return out;
}

function StatusDot({ test }: { test: RowTest }) {
  const map = {
    idle: { cls: "bg-slate-300 dark:bg-slate-600", title: "未测试" },
    testing: { cls: "bg-amber-400 animate-pulse", title: "测试中…" },
  } as const;
  let cls: string = map.idle.cls;
  let title: string = map.idle.title;
  if (test.phase === "testing") {
    cls = map.testing.cls;
    title = map.testing.title;
  } else if (test.phase === "done") {
    cls = test.result.ok ? "bg-emerald-500" : "bg-red-500";
    title = test.result.ok
      ? `已连接 · ${test.result.tool_count} 个工具`
      : `失败：${test.result.error ?? "未知错误"}`;
  }
  return <span title={title} className={`inline-block h-2.5 w-2.5 shrink-0 rounded-full ${cls}`} />;
}

export function ExtensionPanel() {
  const [servers, setServers] = useState<McpServer[]>([]);
  const [effective, setEffective] = useState(0);
  const [tests, setTests] = useState<Record<string, RowTest>>({});
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [status, setStatus] = useState<{ ok: boolean; msg: string } | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [form, setForm] = useState<Draft | null>(null); // null=收起；编辑时 form.id 锁定
  const [editingId, setEditingId] = useState<string | null>(null);
  const [headersTouched, setHeadersTouched] = useState(false);
  const [pasteOpen, setPasteOpen] = useState(false);
  const [pasteText, setPasteText] = useState("");
  const [confirmDel, setConfirmDel] = useState<string | null>(null);
  const [conn, setConn] = useState<ConnectivityResult | null>(null);
  const [connBusy, setConnBusy] = useState(false);

  const load = useCallback(async () => {
    const v = await api.get<McpServersView>("/api/mcp/servers");
    setServers(v.servers);
    setEffective(v.effective_count);
    setLoaded(true);
  }, []);

  useEffect(() => {
    load().catch((e) => setStatus({ ok: false, msg: `加载失败：${(e as Error).message}` }));
  }, [load]);

  function openAdd() {
    setEditingId(null);
    setHeadersTouched(true);
    setForm({ ...EMPTY, headers: [] });
  }
  function openEdit(s: McpServer) {
    setEditingId(s.id);
    setHeadersTouched(false); // 默认保留原 headers
    setForm({ id: s.id, display_name: s.display_name, url: s.url, headers: [], enabled: s.enabled });
  }
  function closeForm() {
    setForm(null);
    setEditingId(null);
  }

  async function submit() {
    if (!form) return;
    setBusy("form");
    setStatus(null);
    try {
      if (editingId) {
        const body: Record<string, unknown> = {
          display_name: form.display_name.trim() || editingId,
          url: form.url.trim(),
          enabled: form.enabled,
        };
        if (headersTouched) body.headers = headersToMap(form.headers); // 省略=保留
        await api.patch(`/api/mcp/servers/${encodeURIComponent(editingId)}`, body);
      } else {
        await api.post("/api/mcp/servers", {
          id: form.id.trim(),
          display_name: form.display_name.trim() || form.id.trim(),
          url: form.url.trim(),
          headers: headersToMap(form.headers),
          enabled: form.enabled,
        });
      }
      await load();
      setTests((t) => ({ ...t, [editingId ?? form.id.trim()]: { phase: "idle" } }));
      closeForm();
      setStatus({ ok: true, msg: "已保存并热生效（启用/停用即时刷新工具集，无需重启）。" });
    } catch (e) {
      setStatus({ ok: false, msg: `保存失败：${(e as Error).message}` });
    } finally {
      setBusy(null);
    }
  }

  async function toggle(s: McpServer) {
    setBusy(s.id);
    try {
      await api.patch(`/api/mcp/servers/${encodeURIComponent(s.id)}`, { enabled: !s.enabled });
      await load();
    } catch (e) {
      setStatus({ ok: false, msg: `操作失败：${(e as Error).message}` });
    } finally {
      setBusy(null);
    }
  }

  async function test(s: McpServer) {
    setTests((t) => ({ ...t, [s.id]: { phase: "testing" } }));
    setExpanded((e) => ({ ...e, [s.id]: true }));
    try {
      const r = await api.post<McpTestResult>(`/api/mcp/servers/${encodeURIComponent(s.id)}/test`);
      setTests((t) => ({ ...t, [s.id]: { phase: "done", result: r } }));
    } catch (e) {
      setTests((t) => ({
        ...t,
        [s.id]: { phase: "done", result: { id: s.id, ok: false, tool_count: 0, tools: [], error: (e as Error).message } },
      }));
    }
  }

  async function remove(s: McpServer) {
    setBusy(s.id);
    try {
      await api.del(`/api/mcp/servers/${encodeURIComponent(s.id)}`);
      await load();
      setConfirmDel(null);
      setStatus({ ok: true, msg: `已移除 ${s.display_name}。` });
    } catch (e) {
      setStatus({ ok: false, msg: `删除失败：${(e as Error).message}` });
    } finally {
      setBusy(null);
    }
  }

  /** 粘贴 mcp.json 片段批量接入：兼容 {mcpServers:{...}} 与扁平 {id:{url,headers}} 两种形状。 */
  async function submitPaste() {
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(pasteText) as Record<string, unknown>;
    } catch (e) {
      setStatus({ ok: false, msg: `JSON 解析失败：${(e as Error).message}` });
      return;
    }
    const map = (parsed.mcpServers ?? parsed) as Record<string, { url?: string; headers?: Record<string, string>; name?: string }>;
    const entries = Object.entries(map).filter(([, cfg]) => cfg && typeof cfg === "object" && cfg.url);
    if (!entries.length) {
      setStatus({ ok: false, msg: "没解析到带 url 的 MCP server（支持 {mcpServers:{id:{url,headers}}} 或扁平结构）。" });
      return;
    }
    setBusy("paste");
    const errs: string[] = [];
    let n = 0;
    for (const [id, cfg] of entries) {
      try {
        await api.post("/api/mcp/servers", {
          id,
          display_name: cfg.name || id,
          url: cfg.url as string,
          headers: cfg.headers || {},
          enabled: true,
        });
        n += 1;
      } catch (e) {
        errs.push(`${id}: ${(e as Error).message}`);
      }
    }
    await load();
    setBusy(null);
    setPasteOpen(false);
    setPasteText("");
    setStatus(
      errs.length
        ? { ok: n > 0, msg: `接入 ${n} 个，${errs.length} 个失败 —— ${errs.join("; ")}` }
        : { ok: true, msg: `已接入 ${n} 个 MCP server 并热生效。` },
    );
  }

  async function runCheck() {
    setConnBusy(true);
    setStatus(null);
    try {
      setConn(await api.post<ConnectivityResult>("/api/services/check"));
    } catch (e) {
      setStatus({ ok: false, msg: `检测失败：${(e as Error).message}` });
    } finally {
      setConnBusy(false);
    }
  }

  if (!loaded) return <p className="text-sm text-slate-500 dark:text-slate-400">加载中…</p>;

  return (
    <div className="mt-6 flex flex-col gap-6">
      {status && (
        <p
          className={`rounded-lg px-3 py-2 text-xs ${
            status.ok
              ? "bg-emerald-50 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300"
              : "bg-red-50 text-red-600 dark:bg-red-900/30 dark:text-red-400"
          }`}
        >
          {status.msg}
        </p>
      )}

      {/* ---- ① MCP server 接入 ---- */}
      <section className="flex flex-col gap-2">
        <div className="flex items-center justify-between">
          <div className="flex items-baseline gap-2">
            <h3 className="text-sm font-medium text-slate-900 dark:text-slate-100">MCP 工具接入</h3>
            <span className="text-[11px] text-slate-400 dark:text-slate-500">
              仅 http；接入后需在角色卡白名单授权该工具，角色才可见
            </span>
          </div>
          <div className="flex gap-1.5">
            <button
              onClick={() => setPasteOpen((v) => !v)}
              className="rounded-lg border border-slate-200 px-2.5 py-1 text-xs text-slate-600 hover:border-blue-300 hover:text-blue-600 dark:border-slate-700 dark:text-slate-300"
            >
              {pasteOpen ? "收起" : "粘贴 JSON"}
            </button>
            <button
              onClick={openAdd}
              className="rounded-lg bg-blue-600 px-2.5 py-1 text-xs text-white hover:bg-blue-700"
            >
              ＋ 接入 MCP server
            </button>
          </div>
        </div>

        {pasteOpen && (
          <div className="rounded-lg border border-blue-200 bg-blue-50 p-3 dark:border-blue-800 dark:bg-blue-900/20">
            <textarea
              value={pasteText}
              onChange={(e) => setPasteText(e.target.value)}
              placeholder={'{\n  "mcpServers": {\n    "my-tools": { "url": "https://example.com/mcp", "headers": { "Authorization": "***" } }\n  }\n}'}
              className="h-28 w-full rounded border border-slate-200 bg-white p-2 font-mono text-xs dark:border-slate-700 dark:bg-slate-800"
            />
            <button
              disabled={busy === "paste" || !pasteText.trim()}
              onClick={submitPaste}
              className="mt-2 rounded bg-blue-600 px-3 py-1 text-xs text-white hover:bg-blue-700 disabled:opacity-50"
            >
              {busy === "paste" ? "接入中…" : "从 JSON 接入"}
            </button>
          </div>
        )}

        {form && (
          <div className="rounded-lg border border-blue-200 bg-blue-50 p-3 dark:border-blue-800 dark:bg-blue-900/20">
            <div className="flex flex-col gap-2">
              <div className="flex flex-wrap gap-2">
                <input
                  value={form.id}
                  disabled={!!editingId}
                  onChange={(e) => setForm({ ...form, id: e.target.value })}
                  placeholder="id（字母数字 . _ -）"
                  className="w-40 rounded border border-slate-200 bg-white px-2 py-1 text-xs disabled:opacity-60 dark:border-slate-700 dark:bg-slate-800"
                />
                <input
                  value={form.display_name}
                  onChange={(e) => setForm({ ...form, display_name: e.target.value })}
                  placeholder="显示名"
                  className="w-40 rounded border border-slate-200 bg-white px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-800"
                />
                <input
                  value={form.url}
                  onChange={(e) => setForm({ ...form, url: e.target.value })}
                  placeholder="https://…（公网 http(s) 端点）"
                  className="flex-1 rounded border border-slate-200 bg-white px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-800"
                />
              </div>
              {editingId && Object.keys(servers.find((s) => s.id === editingId)?.headers ?? {}).length > 0 && (
                <p className="text-[11px] text-slate-400 dark:text-slate-500">
                  现有鉴权头：{Object.keys(servers.find((s) => s.id === editingId)?.headers ?? {}).join(", ")}（值不可见）
                </p>
              )}
              <div className="flex items-center gap-2">
                <span className="text-[11px] text-slate-500 dark:text-slate-400">
                  鉴权头{editingId && !headersTouched ? "（留空=不改，填任一条=整体替换）" : ""}
                </span>
                <button
                  onClick={() => {
                    setHeadersTouched(true);
                    setForm({ ...form, headers: [...form.headers, ["", ""]] });
                  }}
                  className="rounded border border-slate-200 px-2 py-0.5 text-[11px] text-slate-500 dark:border-slate-700"
                >
                  ＋ 键值对
                </button>
              </div>
              {(headersTouched || !editingId) && form.headers.map((row, i) => (
                <div key={i} className="flex gap-2">
                  <input
                    value={row[0]}
                    onChange={(e) => setForm({ ...form, headers: form.headers.map((r, j) => (j === i ? [e.target.value, r[1]] : r)) })}
                    placeholder="Header 名"
                    className="w-40 rounded border border-slate-200 bg-white px-2 py-1 font-mono text-xs dark:border-slate-700 dark:bg-slate-800"
                  />
                  <input
                    value={row[1]}
                    onChange={(e) => setForm({ ...form, headers: form.headers.map((r, j) => (j === i ? [r[0], e.target.value] : r)) })}
                    placeholder="值（密钥）"
                    type="password"
                    className="flex-1 rounded border border-slate-200 bg-white px-2 py-1 font-mono text-xs dark:border-slate-700 dark:bg-slate-800"
                  />
                  <button
                    onClick={() => setForm({ ...form, headers: form.headers.filter((_, j) => j !== i) })}
                    className="text-xs text-red-400 hover:text-red-600"
                  >
                    ✕
                  </button>
                </div>
              ))}
              <label className="flex items-center gap-2 text-xs text-slate-600 dark:text-slate-300">
                <input type="checkbox" checked={form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} />
                接入后启用
              </label>
              <div className="flex gap-2">
                <button
                  disabled={busy === "form" || !form.url.trim() || (!editingId && !form.id.trim())}
                  onClick={submit}
                  className="rounded bg-blue-600 px-3 py-1 text-xs text-white hover:bg-blue-700 disabled:opacity-50"
                >
                  保存
                </button>
                <button onClick={closeForm} className="rounded border border-slate-200 px-3 py-1 text-xs text-slate-500 dark:border-slate-700">
                  取消
                </button>
              </div>
            </div>
          </div>
        )}

        {servers.length === 0 && (
          <p className="rounded-lg border border-dashed border-slate-200 px-3 py-6 text-center text-xs text-slate-400 dark:border-slate-700">
            还没有接入任何 MCP server。点「＋ 接入」填一条 http 端点，或「粘贴 JSON」批量导入。
          </p>
        )}

        {servers.map((s) => {
          const t: RowTest = tests[s.id] ?? { phase: "idle" };
          const isOpen = !!expanded[s.id];
          const hdrKeys = Object.keys(s.headers);
          return (
            <div
              key={s.id}
              className={`rounded-lg border px-3 py-2 ${
                s.enabled ? "border-slate-200 dark:border-slate-700" : "border-dashed border-slate-200 opacity-60 dark:border-slate-700"
              }`}
            >
              <div className="flex items-center gap-2.5">
                <StatusDot test={t} />
                <span className="rounded-full bg-blue-100 px-2 py-0.5 text-[11px] text-blue-700 dark:bg-blue-900/40 dark:text-blue-300">
                  {s.transport}
                </span>
                <span className="text-sm text-slate-800 dark:text-slate-200">{s.display_name}</span>
                <span className="font-mono text-[11px] text-slate-400 dark:text-slate-500" title={s.url}>
                  {s.url.length > 40 ? `${s.url.slice(0, 40)}…` : s.url}
                </span>
                {hdrKeys.length > 0 && (
                  <span className="font-mono text-[11px] text-slate-400 dark:text-slate-500" title="鉴权头已配置（值不可见）">
                    {hdrKeys.join(",")} ****
                  </span>
                )}
                <span className="ml-auto flex items-center gap-1.5">
                  <button
                    onClick={() => setExpanded((e) => ({ ...e, [s.id]: !isOpen }))}
                    className="rounded px-2 py-0.5 text-[11px] text-slate-500 hover:bg-slate-100 dark:hover:bg-slate-800"
                    title="展开看工具/错误"
                  >
                    {isOpen ? "收起 ▲" : "详情 ▼"}
                  </button>
                  <button
                    disabled={busy === s.id}
                    onClick={() => toggle(s)}
                    className={`rounded-full px-2.5 py-0.5 text-[11px] ${
                      s.enabled
                        ? "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300"
                        : "bg-slate-100 text-slate-500 dark:bg-slate-700 dark:text-slate-300"
                    }`}
                  >
                    {s.enabled ? "启用中" : "已停用"}
                  </button>
                  <button
                    onClick={() => test(s)}
                    disabled={busy === s.id || t.phase === "testing"}
                    className="rounded border border-slate-200 px-2 py-0.5 text-[11px] text-slate-500 hover:bg-slate-50 disabled:opacity-40 dark:border-slate-600 dark:hover:bg-slate-800"
                  >
                    测试连接
                  </button>
                  <button onClick={() => openEdit(s)} className="rounded px-2 py-0.5 text-[11px] text-blue-500 hover:bg-blue-50 dark:hover:bg-blue-900/30">
                    编辑
                  </button>
                  {confirmDel === s.id ? (
                    <button disabled={busy === s.id} onClick={() => remove(s)} className="rounded bg-red-500 px-2 py-0.5 text-[11px] text-white hover:bg-red-600">
                      确认删除
                    </button>
                  ) : (
                    <button onClick={() => setConfirmDel(s.id)} className="rounded px-2 py-0.5 text-[11px] text-red-400 hover:bg-red-50 dark:hover:bg-red-900/30">
                      删除
                    </button>
                  )}
                </span>
              </div>
              {isOpen && t.phase === "done" && (
                <div className="mt-2 pl-5 text-xs">
                  {t.result.ok ? (
                    <ul className="list-inside list-disc text-slate-500 dark:text-slate-400">
                      {t.result.tools.map((name) => (
                        <li key={name} className="font-mono text-[11px]">{name}</li>
                      ))}
                      {t.result.tools.length === 0 && <li>（无工具）</li>}
                    </ul>
                  ) : (
                    <p className="text-red-500 dark:text-red-400">{t.result.error}</p>
                  )}
                </div>
              )}
              {isOpen && t.phase !== "done" && (
                <p className="mt-2 pl-5 text-[11px] text-slate-400 dark:text-slate-500">
                  {t.phase === "testing" ? "正在连接…" : "点「测试连接」看该 server 暴露的工具。"}
                </p>
              )}
            </div>
          );
        })}
      </section>

      {/* ---- ② 模型后端连通性自检 ---- */}
      <section className="flex flex-col gap-2 border-t border-slate-100 pt-4 dark:border-slate-800">
        <div className="flex items-baseline gap-2">
          <h3 className="text-sm font-medium text-slate-900 dark:text-slate-100">模型后端连通性自检</h3>
          <span className="text-[11px] text-slate-400 dark:text-slate-500">对默认后端真发一次轻探活（不耗推理配额）</span>
        </div>
        <div>
          <button
            onClick={runCheck}
            disabled={connBusy}
            className="rounded-lg border border-slate-300 px-3 py-1.5 text-xs text-slate-600 hover:bg-slate-50 disabled:opacity-50 dark:border-slate-600 dark:text-slate-300 dark:hover:bg-slate-800"
          >
            {connBusy ? "检测中…" : "检测默认模型后端"}
          </button>
        </div>
        {conn &&
          Object.entries(conn).map(([provider, probe]) => (
            <p
              key={provider}
              className={`rounded-lg px-3 py-2 text-xs ${
                probe.reachable
                  ? "bg-emerald-50 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300"
                  : "bg-amber-50 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300"
              }`}
            >
              {probe.reachable ? "✓ 可达" : "✗ 不可达"}（{provider} · {probe.detail}）
              {probe.models?.length ? `：${probe.models.slice(0, 6).join(", ")}` : ""}
            </p>
          ))}
        <p className="text-[11px] text-slate-400 dark:text-slate-500">
          生效 MCP server 合计（含 env 与已停用外）：{effective}
        </p>
      </section>
    </div>
  );
}
