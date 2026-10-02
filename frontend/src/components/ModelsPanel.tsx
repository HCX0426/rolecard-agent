// 设置 → 「模型」页签：按**凭据组**分层的卡片（docs/archive/模型页设计稿.md 批次②）。
//
// 这一页只回答三个问题：我有哪些凭据、每个凭据下有哪些模型、谁在用它们。
// 三条设计约束落在代码里：
//   * **key 只在组头出现一次**（拆层的界面收益；这一页没有 key 输入框，除了抽屉里新增供应商）；
//   * **用途只读**：`used_by` 派生自「服务」页的引用行，这里给一个跳过去的动作，不在这儿编辑；
//   * **能力位三态**：`视觉 ?` = 没测过（点它=探测），`✗` = 测过且不支持。把"没测过"画成
//     "不支持"是撒谎，而 `✗` 会真的触发调用前拦截 —— 所以 `?` 必须一眼能分出来且有出口。
//
// 不可用控件走 `Button` 的三档口径（禁用必须配"怎么恢复"），错误文案一律原样显示后端 detail。
import { useCallback, useEffect, useState } from "react";
import {
  api,
  type ModelProvider,
  type ModelSettings,
  type ProbeOutcome,
  type ProviderGroup,
  type ProviderModelRow,
  USAGE_LABEL,
} from "../api";
import { AddModelDrawer } from "./AddModelDrawer";
import { LocalServiceCard } from "./LocalServiceCard";
import { useConfirm } from "../hooks/useConfirm";
import { Button, Card, Modal, Notice } from "./ui";

type ProbeTarget = { group: ProviderGroup; row: ProviderModelRow };

export function ModelsPanel({ onOpenServices }: { onOpenServices?: () => void }) {
  const [data, setData] = useState<ModelSettings | null>(null);
  const [catalog, setCatalog] = useState<ModelProvider[]>([]);
  const [status, setStatus] = useState<{ ok: boolean; msg: string } | null>(null);
  const [adding, setAdding] = useState(false);
  const [probeFor, setProbeFor] = useState<ProbeTarget | null>(null);
  const [probing, setProbing] = useState(false);
  const [justAdded, setJustAdded] = useState("");
  const confirm = useConfirm();

  const load = useCallback(async () => {
    const settings = await api.get<ModelSettings>("/api/settings/models");
    setData(settings);
    return settings;
  }, []);

  useEffect(() => {
    load().catch((e) => setStatus({ ok: false, msg: `加载失败：${(e as Error).message}` }));
    api
      .get<{ providers: ModelProvider[] }>("/api/settings/model-providers")
      .then((s) => setCatalog(s.providers ?? []))
      .catch(() => setCatalog([])); // 目录拉不到不拦路：抽屉里的供应商下拉退到内置默认
  }, [load]);

  async function removeModel(row: ProviderModelRow) {
    const others = row.used_by.filter((u) => u !== "chat");
    const ok = await confirm({
      title: `删除模型「${row.name}」？`,
      body: others.length
        ? `这行还被「${others.map((u) => USAGE_LABEL[u] ?? u).join("、")}」用着 —— 删除后服务页那一行会显示引用已失效，需要你另选一个模型。`
        : "只删这一行模型配置，同供应商的其他模型与凭据不受影响。",
      confirmText: "确认删除",
      danger: true,
    });
    if (!ok) return;
    try {
      await api.del(`/api/settings/models/${row.name}`);
      await load();
      setStatus({ ok: true, msg: `已删除 ${row.name}（下一轮对话起生效）。` });
    } catch (e) {
      setStatus({ ok: false, msg: `删除失败：${(e as Error).message}` });
    }
  }

  async function runProbe(target: ProbeTarget, tools: boolean, vision: boolean) {
    setProbing(true);
    try {
      const outcome = await api.post<ProbeOutcome>("/api/settings/models/probe", {
        provider_id: target.group.id,
        model: target.row.model,
        test_tools: tools,
        test_vision: vision,
      });
      const write: Record<string, boolean> = {};
      // 只写**确定**的结论：探测失败得到 null 时不写，否则会把已知的 ✓/✗ 抹成"没测过"。
      if (tools && outcome.tools !== null) write.supports_tools = outcome.tools;
      if (vision && outcome.vision !== null) write.supports_vision = outcome.vision;
      if (Object.keys(write).length > 0) {
        await api.patch(`/api/settings/models/${target.row.name}/capabilities`, write);
      }
      await load();
      setProbeFor(null);
      setStatus({
        ok: outcome.reachable,
        msg: outcome.reachable
          ? `探测完成（本次 ${outcome.calls_used} 次调用）：工具 ${tri(outcome.tools)} · 视觉 ${tri(outcome.vision)}` +
            (outcome.detail ? ` · ${outcome.detail}` : "")
          : `端点不通：${outcome.detail || "没有响应"}`,
      });
    } catch (e) {
      setStatus({ ok: false, msg: `探测失败：${(e as Error).message}` });
    } finally {
      setProbing(false);
    }
  }

  if (!data) {
    return (
      <div className="mt-6 space-y-4">
        <Card className="p-5">
          <div className="space-y-2.5">
            {[0, 1, 2].map((i) => (
              <div key={i} className="h-9 animate-pulse rounded-lg bg-slate-100 dark:bg-slate-700/50" />
            ))}
          </div>
        </Card>
        {status && <Notice tone="error">{status.msg}</Notice>}
      </div>
    );
  }

  const groups = data.providers ?? [];
  return (
    <div className="mt-6 space-y-4">
      <LocalServiceCard />

      <div className="flex items-center gap-2">
        <h3 className="text-sm font-medium text-slate-900 dark:text-slate-100">供应商与模型</h3>
        <span className="ml-auto flex gap-2">
          <Button size="sm" variant="outline" onClick={() => load().catch(() => undefined)}>
            重新载入
          </Button>
          <Button size="sm" onClick={() => setAdding(true)}>
            + 添加模型
          </Button>
        </span>
      </div>

      {status && (
        <Notice tone={status.ok ? "ok" : "error"}>{status.msg}</Notice>
      )}

      {groups.length === 0 && (
        <Card className="p-5">
          <p className="text-sm text-slate-600 dark:text-slate-300">
            还没有配置任何模型。本地 Ollama 免配置：装好并 <code>ollama serve</code> 之后，
            直接添加一家"本地 Ollama"就行。
          </p>
          <Button className="mt-3" onClick={() => setAdding(true)}>
            + 添加模型
          </Button>
        </Card>
      )}

      {groups.map((group) => (
        <ProviderCard
          key={group.id}
          group={group}
          highlight={justAdded}
          onProbe={(row) => setProbeFor({ group, row })}
          onDelete={(row) => removeModel(row)}
          onOpenServices={onOpenServices}
        />
      ))}

      <AddModelDrawer
        open={adding}
        onClose={() => setAdding(false)}
        groups={groups}
        catalog={catalog}
        onAdded={(name) => {
          setJustAdded(name);
          load().catch((e) => setStatus({ ok: false, msg: `刷新失败：${(e as Error).message}` }));
        }}
      />

      <ProbeConfirm
        target={probeFor}
        busy={probing}
        onClose={() => setProbeFor(null)}
        onRun={runProbe}
      />
    </div>
  );
}

function ProviderCard({
  group,
  highlight,
  onProbe,
  onDelete,
  onOpenServices,
}: {
  group: ProviderGroup;
  highlight: string;
  onProbe: (row: ProviderModelRow) => void;
  onDelete: (row: ProviderModelRow) => void;
  onOpenServices?: () => void;
}) {
  // 组头状态色：缺 key 才 amber（能恢复 —— 去抽屉里补一把）；本地类供应商本就没 key，不算异常。
  const broken = group.needs_key && !group.has_key;
  const otherUses = new Set(group.models.flatMap((m) => m.used_by.filter((u) => u !== "chat")));
  return (
    <Card className="overflow-hidden p-0">
      <div
        className={`flex items-center gap-2 border-b border-slate-100 px-4 py-3 dark:border-slate-700 ${
          broken ? "bg-amber-50/60 dark:bg-amber-900/20" : ""
        }`}
      >
        <span
          className={`h-4 w-[3px] rounded-full ${broken ? "bg-amber-400" : "bg-emerald-500"}`}
        />
        <span className="text-sm font-medium text-slate-900 dark:text-slate-100">
          {group.label}
          {group.style === "native" && (
            <span className="ml-1.5 text-[11px] text-slate-400">（本机）</span>
          )}
        </span>
        <span className="text-[11px] text-slate-400 dark:text-slate-500">
          {group.base_url || "默认端点"}
        </span>
        <span className="ml-auto text-[11px] text-slate-400 dark:text-slate-500">
          {group.needs_key
            ? group.has_key
              ? `key 已存 ${group.key_masked ?? "••••"}`
              : "未配置 key —— 加模型时填一把"
            : "本机程序，无需 key"}
        </span>
      </div>

      <div className="divide-y divide-slate-100 dark:divide-slate-800">
        {group.models.map((row) => (
          <div
            key={row.name}
            className={`flex min-h-[40px] flex-wrap items-center gap-x-3 gap-y-1.5 px-4 py-2.5 hover:bg-slate-50 dark:hover:bg-slate-800/60 ${
              row.name === highlight ? "animate-[fadein_120ms_ease-out]" : ""
            }`}
          >
            <StatusGlyph row={row} />
            <div className="min-w-0">
              <div className="truncate font-mono text-xs text-slate-700 dark:text-slate-200">
                {row.name}
                {row.is_default && <Chip tone="blue">第 1 位</Chip>}
              </div>
              <div className="truncate text-[11px] text-slate-400 dark:text-slate-500">
                {row.model}
                {row.num_ctx ? ` · 窗口 ${row.num_ctx}` : ""}
              </div>
            </div>
            <div className="ml-auto flex flex-wrap items-center gap-1.5">
              {row.used_by.length === 0 && <Chip tone="slate">未使用</Chip>}
              {row.used_by.map((u) => (
                <Chip key={u} tone={u === "chat" ? "green" : "slate"}>
                  {USAGE_LABEL[u] ?? u}
                </Chip>
              ))}
              <CapBadge label="视觉" value={row.supports_vision} onProbe={() => onProbe(row)} />
              <CapBadge label="工具" value={row.supports_tools} onProbe={() => onProbe(row)} />
              <Button size="sm" variant="ghost" onClick={() => onProbe(row)}>
                探测
              </Button>
              <Button size="sm" variant="ghost" onClick={() => onDelete(row)}>
                删除
              </Button>
            </div>
          </div>
        ))}
        {group.models.length === 0 && (
          <p className="px-4 py-3 text-xs text-slate-400 dark:text-slate-500">
            这个凭据组下还没有模型 —— 用〔+ 添加模型〕加一个。
          </p>
        )}
      </div>

      {(otherUses.size > 0 || group.models.some((m) => m.used_by.length === 0)) && (
        <div className="flex items-center gap-2 border-t border-slate-100 bg-slate-50/60 px-4 py-2 text-[11px] text-slate-500 dark:border-slate-800 dark:bg-slate-800/40 dark:text-slate-400">
          {otherUses.size > 0 && (
            <span>
              这家还用于：{[...otherUses].map((u) => USAGE_LABEL[u] ?? u).join("、")}
            </span>
          )}
          {onOpenServices && (
            <button
              type="button"
              onClick={onOpenServices}
              className="ml-auto text-blue-600 hover:underline dark:text-blue-400"
            >
              在服务页调整用途 →
            </button>
          )}
        </div>
      )}
    </Card>
  );
}

/** 行首状态 glyph：探测过且不通 = ⚠，其余 = ✓（"没测过"不是问题，不吓人的橙）。 */
function StatusGlyph({ row }: { row: ProviderModelRow }) {
  const unconfigured = row.used_by.length === 0;
  const text = unconfigured ? "○" : "✓";
  const tone = unconfigured ? "text-slate-300 dark:text-slate-600" : "text-emerald-500";
  return <span className={`w-4 shrink-0 text-center text-sm ${tone}`}>{text}</span>;
}

function Chip({ tone, children }: { tone: "green" | "blue" | "slate"; children: React.ReactNode }) {
  const TONES = {
    green: "bg-emerald-50 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300",
    blue: "bg-blue-50 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300",
    slate: "bg-slate-100 text-slate-500 dark:bg-slate-700/50 dark:text-slate-400",
  } as const;
  return (
    <span className={`ml-1.5 rounded px-1.5 py-0.5 text-[10px] ${TONES[tone]}`}>{children}</span>
  );
}

/** 三态徽章。`?` 可点（点开就是探测确认卡）—— "没测过"必须有出口。 */
function CapBadge({
  label,
  value,
  onProbe,
}: {
  label: string;
  value: boolean | null;
  onProbe: () => void;
}) {
  const tone =
    value === true
      ? "bg-emerald-50 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300"
      : value === false
        ? // "不能看"是事实不是错误，所以是 slate 而不是红
          "bg-slate-100 text-slate-500 dark:bg-slate-700/50 dark:text-slate-400"
        : "bg-sky-50 text-sky-700 dark:bg-sky-900/30 dark:text-sky-300";
  const mark = value === true ? "✓" : value === false ? "✗" : "?";
  const title =
    value === null ? `${label}：没测过 —— 点这里探测` : `${label}：${value ? "支持" : "不支持"}`;
  const cls = `rounded px-1.5 py-0.5 text-[10px] ${tone}`;
  if (value !== null) {
    return (
      <span title={title} className={cls}>
        {label} {mark}
      </span>
    );
  }
  return (
    <button type="button" title={title} onClick={onProbe} className={`${cls} hover:underline`}>
      {label} {mark}
    </button>
  );
}

/** 能力探测确认卡：把"花几次调用、发不发图片、多久"写在点之前。
 *  默认高亮在「只测工具」，「全部测试」是明确的第二选择 —— 图片外发不该被一次点击顺带做掉。 */
function ProbeConfirm({
  target,
  busy,
  onClose,
  onRun,
}: {
  target: ProbeTarget | null;
  busy: boolean;
  onClose: () => void;
  onRun: (t: ProbeTarget, tools: boolean, vision: boolean) => void;
}) {
  if (!target) return null;
  const local = target.group.style === "native";
  return (
    <Modal
      open
      onClose={onClose}
      title={`探测 ${target.row.model} 的能力？`}
      footer={
        <>
          <Button variant="outline" onClick={onClose} disabled={busy}>
            取消
          </Button>
          <Button variant="outline" disabled={busy} onClick={() => onRun(target, true, false)}>
            {busy ? "探测中…" : "只测工具"}
          </Button>
          <Button disabled={busy} onClick={() => onRun(target, true, true)}>
            全部测试
          </Button>
        </>
      }
    >
      <ul className="list-disc space-y-1.5 pl-4 text-xs leading-relaxed">
        <li>只测工具：向 {target.group.label} 发 1 次请求{local ? "（本机，不花钱）" : "，消耗配额"}。</li>
        <li>
          全部测试：再发 1 次，并
          {local
            ? "读这台引擎自己报的能力信息（图片不出本机，本地视觉本来就是免费的）"
            : "上传一张 16×16 的测试图片 —— 图片会离开本机，发给这家供应商"}
          。
        </li>
        <li>结论写回这一行的能力徽章；探测失败徽章仍是 ?（不会误标成不支持）。</li>
      </ul>
    </Modal>
  );
}

function tri(value: boolean | null): string {
  return value === null ? "?" : value ? "✓" : "✗";
}
