// 添加模型抽屉（设置 → 模型 → 〔+ 添加模型〕）。
//
// 一条流水线：选供应商 → 选模型名（能拉列表就拉，拉不到退回手填）→ 〔测试连接〕通过才让〔加入〕。
// 为什么值得做这么重：以前"加完就热重建"，配错的行会让下一轮对话静默走回退链 —— 用户看到的
// 是"它突然变笨了"，查不到原因。测连门禁把错误压在添加那一刻（docs/模型页设计稿.md §2）。
//
// 三条纪律：
//   * 选已配好的供应商时**不问 key**（凭据在组上，服务端自己取，从不在网络上往返）；
//   * 〔测试连接〕只做免费的事（问一次模型列表），不烧配额、不外发图片；
//   * 能力探测是**显式**的第二步：确认卡上写清"几次调用 / 图片会不会离开本机"，
//     视觉默认不勾 —— 图片外发是本项目最硬的一条红线。
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  api,
  type ModelProvider,
  type ProbeOutcome,
  type ProviderGroup,
} from "../api";
import { Button, Modal } from "./ui";

/** 供应商来源：已配置的凭据组（沿用 key）或目录里的新供应商（要填 key）。 */
type Source =
  | { kind: "group"; id: string }
  | { kind: "new"; provider: string; base_url: string; api_key: string };

type TestState =
  | { phase: "idle" }
  | { phase: "testing" }
  | { phase: "ok"; listed: boolean | null; detail: string }
  | { phase: "failed"; detail: string };

const GROUP_SOURCE = "__group__";

export function AddModelDrawer({
  open,
  onClose,
  groups,
  catalog,
  onAdded,
}: {
  open: boolean;
  onClose: () => void;
  groups: ProviderGroup[];
  catalog: ModelProvider[];
  onAdded: (name: string) => void;
}) {
  const [source, setSource] = useState<Source>(() => defaultSource(groups, catalog));
  const [model, setModel] = useState("");
  const [models, setModels] = useState<string[]>([]);
  const [listNote, setListNote] = useState("");
  const [test, setTest] = useState<TestState>({ phase: "idle" });
  const [probing, setProbing] = useState(false);
  const [probe, setProbe] = useState<ProbeOutcome | null>(null);
  const [wantTools, setWantTools] = useState(false);
  // 视觉默认 false：勾它意味着"把一张测试图发给这家供应商"。
  const [wantVision, setWantVision] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  // 每次改动来源/端点/key，上一次的测试结论就作废了 —— 门禁不能拿旧结论放行新配置。
  const invalidate = useCallback(() => {
    setTest({ phase: "idle" });
    setModels([]);
    setListNote("");
    setProbe(null);
  }, []);

  useEffect(() => {
    if (!open) {
      setSource(defaultSource(groups, catalog));
      setModel("");
      setTest({ phase: "idle" });
      setModels([]);
      setListNote("");
      setProbe(null);
      setWantTools(false);
      setWantVision(false);
      setError("");
    }
  }, [open, groups, catalog]);

  const target = useMemo(() => sourcePayload(source, model), [source, model]);
  const pickGroup = source.kind === "group" ? groups.find((g) => g.id === source.id) : undefined;
  const hint =
    source.kind === "new"
      ? catalog.find((p) => p.id === source.provider)?.base_url_hint ?? ""
      : pickGroup?.base_url ?? "";

  async function fetchModels() {
    setBusy(true);
    setError("");
    try {
      const res = await api.post<{ models: string[]; reachable: boolean; detail: string }>(
        "/api/settings/models/catalog",
        sourcePayload(source, ""),
      );
      setModels(res.models);
      setListNote(
        res.reachable
          ? res.models.length
            ? ""
            : "这个端点没返回模型列表，请手填模型名。"
          : `拉列表失败：${res.detail || "端点没有响应"} —— 确认名称无误可以直接手填。`,
      );
    } catch (e) {
      setError(`拉取模型列表失败：${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  async function runTest() {
    setTest({ phase: "testing" });
    try {
      const res = await api.post<ProbeOutcome>("/api/settings/models/probe", target);
      setTest(
        res.reachable
          ? { phase: "ok", listed: res.model_listed, detail: res.detail }
          : { phase: "failed", detail: res.detail || "端点没有响应" },
      );
    } catch (e) {
      setTest({ phase: "failed", detail: (e as Error).message });
    }
  }

  async function runCapabilityProbe() {
    setProbing(true);
    setError("");
    try {
      const res = await api.post<ProbeOutcome>("/api/settings/models/probe", {
        ...target,
        test_tools: wantTools,
        test_vision: wantVision,
      });
      setProbe(res);
      setTest(
        res.reachable ? { phase: "ok", listed: res.model_listed, detail: res.detail } : test,
      );
    } catch (e) {
      setError(`探测失败：${(e as Error).message}`);
    } finally {
      setProbing(false);
    }
  }

  async function add() {
    setBusy(true);
    setError("");
    try {
      const res = await api.post<{ added: { name: string } }>(
        "/api/settings/models",
        target,
      );
      const name = res.added.name;
      if (probe && (probe.tools !== null || probe.vision !== null)) {
        await api.patch(`/api/settings/models/${name}/capabilities`, {
          supports_tools: probe.tools,
          supports_vision: probe.vision,
        });
      }
      onAdded(name);
      onClose();
    } catch (e) {
      setError(`加入失败：${(e as Error).message}`);
      setBusy(false);
    }
  }

  const tested = test.phase === "ok";
  return (
    <Modal
      open={open}
      onClose={onClose}
      variant="drawer"
      title="添加模型"
      footer={
        <>
          <Button variant="outline" onClick={runTest} disabled={test.phase === "testing"}>
            {test.phase === "testing" ? "测试中…" : "测试连接"}
          </Button>
          <Button
            onClick={add}
            disabled={!tested || !model.trim() || busy}
            disabledHint={
              !model.trim()
                ? "先选或填一个模型名"
                : tested
                  ? ""
                  : "先测试通过才能加入（点左边的「测试连接」）"
            }
          >
            {busy ? "加入中…" : "加入"}
          </Button>
        </>
      }
    >
      <div className="space-y-5">
        <section>
          <label className="text-xs font-medium text-slate-500 dark:text-slate-400">
            1 · 供应商
          </label>
          <select
            className="mt-1.5 w-full rounded-lg border border-slate-200 bg-white px-2.5 py-2 text-sm dark:border-slate-700 dark:bg-slate-800"
            value={source.kind === "group" ? `${GROUP_SOURCE}${source.id}` : `__new__${source.provider}`}
            onChange={(e) => {
              const v = e.target.value;
              if (v.startsWith(GROUP_SOURCE)) {
                setSource({ kind: "group", id: v.slice(GROUP_SOURCE.length) });
              } else {
                const id = v.slice("__new__".length);
                setSource({ kind: "new", provider: id, base_url: "", api_key: "" });
              }
              invalidate();
            }}
          >
            {groups.length > 0 && (
              <optgroup label="已配置的凭据（沿用已存 key）">
                {groups.map((g) => (
                  <option key={g.id} value={`${GROUP_SOURCE}${g.id}`}>
                    {g.label} · {g.base_url || "默认端点"}
                  </option>
                ))}
              </optgroup>
            )}
            <optgroup label="新增一家供应商">
              {catalog.map((p) => (
                <option key={p.id} value={`__new__${p.id}`}>
                  {p.label}
                </option>
              ))}
            </optgroup>
          </select>
          {source.kind === "new" && (
            <div className="mt-2 space-y-2">
              <input
                value={source.base_url}
                onChange={(e) => {
                  setSource({ ...source, base_url: e.target.value });
                  invalidate();
                }}
                placeholder={hint || "base_url（留空=该厂商默认端点）"}
                className="w-full rounded-lg border border-slate-200 px-2.5 py-2 text-sm dark:border-slate-700 dark:bg-slate-800"
              />
              {needsKey(source.provider, catalog) && (
                <input
                  type="password"
                  value={source.api_key}
                  onChange={(e) => {
                    setSource({ ...source, api_key: e.target.value });
                    invalidate();
                  }}
                  placeholder="api_key（只存本机，保存后不再回显明文）"
                  className="w-full rounded-lg border border-slate-200 px-2.5 py-2 text-sm dark:border-slate-700 dark:bg-slate-800"
                />
              )}
            </div>
          )}
        </section>

        <section>
          <div className="flex items-center gap-2">
            <label className="text-xs font-medium text-slate-500 dark:text-slate-400">
              2 · 模型名
            </label>
            <Button size="sm" variant="ghost" onClick={fetchModels} disabled={busy}>
              {models.length ? "重新拉列表" : "拉取模型列表"}
            </Button>
          </div>
          <input
            value={model}
            onChange={(e) => {
              setModel(e.target.value);
              invalidate();
            }}
            placeholder="如 deepseek-ai/DeepSeek-V4-Flash 或 qwen3-vl:8b"
            className="mt-1.5 w-full rounded-lg border border-slate-200 px-2.5 py-2 font-mono text-sm dark:border-slate-700 dark:bg-slate-800"
          />
          {models.length > 0 && (
            <div className="mt-2 max-h-40 overflow-y-auto rounded-lg border border-slate-200 dark:border-slate-700">
              {models
                .filter((m) => !model || m.toLowerCase().includes(model.toLowerCase()))
                .map((m) => (
                  <button
                    key={m}
                    type="button"
                    onClick={() => {
                      setModel(m);
                      invalidate();
                    }}
                    className={`block w-full px-2.5 py-1.5 text-left font-mono text-xs hover:bg-slate-50 dark:hover:bg-slate-700/60 ${
                      m === model ? "bg-blue-50 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300" : ""
                    }`}
                  >
                    {m}
                  </button>
                ))}
            </div>
          )}
          {listNote && (
            <p className="mt-1.5 text-[11px] text-slate-400 dark:text-slate-500">{listNote}</p>
          )}
        </section>

        <section>
          <label className="text-xs font-medium text-slate-500 dark:text-slate-400">
            3 · 能力探测（可选）
          </label>
          <p className="mt-1 text-[11px] leading-relaxed text-slate-400 dark:text-slate-500">
            不测也能加入 —— 徽章会显示 <b>?</b>（没测过），之后在卡片上随时补测。
          </p>
          <label className="mt-2 flex items-center gap-2 text-xs">
            <input
              type="checkbox"
              checked={wantTools}
              onChange={(e) => setWantTools(e.target.checked)}
            />
            测工具调用（向该端点发 <b>1 次</b>请求，云端算配额）
          </label>
          <label className="mt-1.5 flex items-center gap-2 text-xs">
            <input
              type="checkbox"
              checked={wantVision}
              onChange={(e) => setWantVision(e.target.checked)}
            />
            测视觉（<b>会把一张 16×16 测试图发给该供应商</b>；本地 Ollama 免费且不出机）
          </label>
          {(wantTools || wantVision) && (
            <Button
              size="sm"
              variant="outline"
              className="mt-2"
              onClick={runCapabilityProbe}
              disabled={probing || !tested}
              disabledHint={tested ? "" : "先测试通过才能探测"}
            >
              {probing ? "探测中…" : "开始探测"}
            </Button>
          )}
          {probe && (
            <p className="mt-2 rounded-lg bg-slate-50 px-2.5 py-2 text-[11px] leading-relaxed text-slate-500 dark:bg-slate-800/60 dark:text-slate-400">
              工具：{tri(probe.tools)} · 视觉：{tri(probe.vision)}
              {probe.vision_source === "uploaded-image" && "（已外发测试图）"}
              {probe.vision_source === "free-metadata" && "（来自 /api/show 元数据，未外发）"} ·
              本次调用 {probe.calls_used} 次
              {probe.detail && <><br />{probe.detail}</>}
            </p>
          )}
        </section>

        {test.phase === "testing" && (
          <p className="text-xs text-slate-400">
            <span className="mr-1.5 inline-block h-2 w-2 animate-pulse rounded-full bg-amber-400 align-middle" />
            正在连接…
          </p>
        )}
        {test.phase === "ok" && (
          <p className="rounded-lg bg-green-50 px-3 py-2 text-xs text-green-700 dark:bg-green-900/30 dark:text-green-300">
            连接通过 · {model || "端点可达"}
            {test.listed === false && `（模型名不在它返回的列表里，确认拼写无误即可继续）`}
            {test.detail && <><br />{test.detail}</>}
          </p>
        )}
        {test.phase === "failed" && (
          <p className="rounded-lg bg-red-50 px-3 py-2 text-xs text-red-600 dark:bg-red-900/30 dark:text-red-400">
            连接失败：{test.detail}
          </p>
        )}
        {error && (
          <p className="rounded-lg bg-red-50 px-3 py-2 text-xs text-red-600 dark:bg-red-900/30 dark:text-red-400">
            {error}
          </p>
        )}
      </div>
    </Modal>
  );
}

function tri(value: boolean | null): string {
  return value === null ? "?" : value ? "✓" : "✗";
}

function needsKey(provider: string, catalog: ModelProvider[]): boolean {
  const entry = catalog.find((p) => p.id === provider);
  return entry ? entry.needs_key === "1" : true;
}

function defaultSource(groups: ProviderGroup[], catalog: ModelProvider[]): Source {
  if (groups.length > 0) return { kind: "group", id: groups[0].id };
  const first = catalog.find((p) => p.needs_key === "0") ?? catalog[0];
  return { kind: "new", provider: first?.id ?? "openai", base_url: "", api_key: "" };
}

function sourcePayload(source: Source, model: string): Record<string, string | undefined> {
  const base =
    source.kind === "group"
      ? { provider_id: source.id }
      : {
          provider: source.provider,
          ...(source.base_url.trim() ? { base_url: source.base_url.trim() } : {}),
          ...(source.api_key.trim() ? { api_key: source.api_key.trim() } : {}),
        };
  return { ...base, ...(model ? { model } : {}) };
}
