import { useCallback, useEffect, useState } from "react";
import { api, type DomainInfo, type ToolCatalog } from "../api";
import { Button, Card, Notice, PageHeader } from "../components/ui";
import { ExtensionPanel } from "../components/ExtensionPanel";
import { describeError } from "../lib/errors";

/** 渠道行（/api/channels 的一个条目）。`blocked_reason` 非空 = 没在连的原因。 */
interface ChannelRow {
  id: string;
  label: string;
  enabled: boolean;
  running: boolean;
  blocked_reason: string | null;
  role_id: string;
  inflight_turns: number;
  messages_sent: number;
}

/** 「渠道」区块（2026-10-25 渠道可配置化）：QQ 官方机器人的状态灯、表单与启停。
 *
 *  配置走「设置 → 运行环境」的渠道组（qq_* 四项，保存即热重启渠道）；本区块负责
 *  三件运行期的事：状态一眼可见（连没连、为什么没连）、凭证表单快捷入口（写同一个
 *  运行环境覆盖，Secret 掩码）、以及"重连"按钮（stop→start 读最新配置）。
 */
function ChannelsPanel() {
  const [channels, setChannels] = useState<ChannelRow[]>([]);
  const [status, setStatus] = useState("");
  const [appId, setAppId] = useState("");
  const [secret, setSecret] = useState("");
  const [roleId, setRoleId] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    const body = await api.get<{ channels: ChannelRow[] }>("/api/channels");
    setChannels(body.channels);
    // 表单初值从运行环境读（secret 是掩码，留空提交 = 保留原值）
    const runtime = await api.get<{ groups: { items: { field: string; value: string }[] }[] }>(
      "/api/settings/runtime",
    );
    const byField = Object.fromEntries(
      runtime.groups.flatMap((g) => g.items.map((it) => [it.field, it.value] as const)),
    );
    setAppId(byField["qq_app_id"] ?? "");
    setRoleId(byField["qq_channel_role_id"] ?? "");
  }, []);

  useEffect(() => {
    load().catch((e) => setStatus(`加载失败：${describeError(e)}`));
  }, [load]);

  async function save() {
    setBusy(true);
    try {
      const values: Record<string, string> = {};
      if (appId.trim()) values["QQ_APP_ID"] = appId.trim();
      if (secret.trim()) values["QQ_APP_SECRET"] = secret.trim();
      values["QQ_CHANNEL_ROLE_ID"] = roleId.trim();
      values["QQ_CHANNEL_ENABLED"] = "1"; // 在这里点保存 = 开闸并连
      await api.put("/api/settings/runtime", { values });
      setSecret("");
      setStatus("配置已保存，渠道正在按新配置重连…");
      await load();
    } catch (e) {
      setStatus(`保存失败：${describeError(e)}`);
    } finally {
      setBusy(false);
    }
  }

  async function reconnect() {
    setBusy(true);
    try {
      // 重连 = 重存当前值（后端据此 restart）
      await api.put("/api/settings/runtime", { values: { QQ_CHANNEL_ENABLED: "1" } });
      setStatus("已按当前配置重连渠道。");
      await load();
    } catch (e) {
      setStatus(`重连失败：${describeError(e)}`);
    } finally {
      setBusy(false);
    }
  }

  async function disable() {
    setBusy(true);
    try {
      await api.put("/api/settings/runtime", { values: { QQ_CHANNEL_ENABLED: "0" } });
      setStatus("渠道已停用。");
      await load();
    } catch (e) {
      setStatus(`停用失败：${describeError(e)}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-3">
      {status && <Notice>{status}</Notice>}
      {channels.map((ch) => (
        <Card key={ch.id} className="p-5">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              {/* 状态灯：绿 = 在连；黄 = 配置齐但没在跑；灰 = 被闸/缺凭证挡住 */}
              <span
                className={`inline-block h-2.5 w-2.5 rounded-full ${
                  ch.running
                    ? "bg-green-500"
                    : ch.enabled
                      ? "bg-amber-400"
                      : "bg-slate-300 dark:bg-slate-600"
                }`}
                title={ch.running ? "已连接" : ch.blocked_reason || "未运行"}
              />
              <span className="text-sm font-medium text-slate-900 dark:text-slate-100">{ch.label}</span>
              <span className="text-xs text-slate-400 dark:text-slate-500">
                {ch.running
                  ? `已连接 · 角色卡：${ch.role_id} · 已发 ${ch.messages_sent} 条`
                  : ch.blocked_reason}
              </span>
            </div>
            <div className="flex items-center gap-2">
              {ch.enabled && (
                <Button variant="outline" size="sm" type="button" disabled={busy} onClick={reconnect}>
                  重连
                </Button>
              )}
              <Button
                variant="ghost"
                size="sm"
                type="button"
                disabled={busy || !ch.enabled}
                onClick={disable}
              >
                停用
              </Button>
            </div>
          </div>
          {/* 凭证表单：Secret 掩码（留空提交 = 保留），角色卡即渠道应答者 */}
          <div className="mt-3 grid gap-2 sm:grid-cols-3">
            <input
              value={appId}
              onChange={(e) => setAppId(e.target.value)}
              placeholder="AppID（q.qq.com → 开发设置）"
              className="rounded-lg border border-slate-200 dark:border-slate-700 px-2.5 py-1.5 text-xs"
            />
            <input
              value={secret}
              onChange={(e) => setSecret(e.target.value)}
              type="password"
              placeholder="AppSecret（留空 = 保留已存的）"
              className="rounded-lg border border-slate-200 dark:border-slate-700 px-2.5 py-1.5 text-xs"
            />
            <input
              value={roleId}
              onChange={(e) => setRoleId(e.target.value)}
              placeholder="应答角色卡 role_id（空 = 通用助手）"
              className="rounded-lg border border-slate-200 dark:border-slate-700 px-2.5 py-1.5 text-xs"
            />
          </div>
          <div className="mt-2 flex items-center justify-between">
            <p className="text-11 leading-relaxed text-slate-400 dark:text-slate-500">
              机器人账号在 <b>q.qq.com</b> 注册（个人可建，bot 账号零封号风险）；保存即开闸并重连，
              无需重启程序。主动消息受平台配额约束。
            </p>
            <Button variant="primary" size="sm" type="button" disabled={busy || !appId.trim()} onClick={save}>
              {busy ? "保存中…" : "保存并连接"}
            </Button>
          </div>
        </Card>
      ))}
    </div>
  );
}

/**
 * 工具页（2026-10-24 IA 重构：原「插件」页改造）。
 *
 * 两块内容，回答"这个助手能用哪些能力、从哪来"：
 *   ① MCP 服务器管理 —— 外接能力（自设置页的扩展面板整体搬入，CRUD + 连通性测试）；
 *   ② 内置工具目录 —— 按域分组的全部工具（角色卡白名单的"选项清单"）。
 *
 * 权限模型（2026-10-24 收口）：**域常开，白名单即唯一边界** —— 本页不再有域开关，
 * 模型能不能用某个工具只看角色卡勾没勾。MCP 的服务器级启停保留（管连接，不管权限）。
 * 安全红线不变：MCP 接入是操作员动作，绝不暴露成 LLM 自助安装。
 */
export default function ToolsPage() {
  const [catalog, setCatalog] = useState<ToolCatalog | null>(null);
  const [domains, setDomains] = useState<DomainInfo[]>([]);

  useEffect(() => {
    api.get<ToolCatalog>("/api/tools/catalog").then(setCatalog).catch(() => undefined);
    api.get<DomainInfo[]>("/api/domains").then(setDomains).catch(() => undefined);
  }, []);

  const domainName = (id: string) =>
    domains.find((d) => d.id === id)?.display_name ?? id;

  return (
    <div className="h-full overflow-y-auto p-6">
      <div className="mx-auto max-w-4xl">
        <PageHeader
          title="工具"
          subtitle={
            <>
              MCP 服务器与内置工具的<b>管理页</b>。模型能不能用某个工具，
              由<b>角色卡的工具白名单</b>决定（域常开，无需先开开关）。
            </>
          }
        />

        {/* 锚点条（2026-10-25 玻璃重设计）：这一页有三个长区块，滚动找人靠的是眼睛；
            锚点条把"去哪儿"变成一次点击。 */}
        <nav className="mt-4 flex flex-wrap gap-1.5 text-xs" aria-label="本页区块">
          {[
            ["channels", "聊天渠道"],
            ["mcp", "MCP 服务器"],
            ["catalog", "内置工具目录"],
          ].map(([id, label]) => (
            <a
              key={id}
              href={`#${id}`}
              className="rounded-full px-3 py-1 text-slate-500 transition-colors hover:bg-white/60 hover:text-slate-700 dark:text-slate-400 dark:hover:bg-white/5 dark:hover:text-slate-200"
            >
              {label}
            </a>
          ))}
        </nav>

        {/* ① 聊天渠道（QQ 官方机器人等）—— 外面的门 */}
        <h2 id="channels" className="mt-6 mb-3 scroll-mt-4 text-sm font-semibold text-slate-700 dark:text-slate-200">
          聊天渠道
        </h2>
        <ChannelsPanel />

        {/* ② MCP 服务器管理（自设置 → 扩展 整体搬入） */}
        <h2 id="mcp" className="mt-8 mb-3 scroll-mt-4 text-sm font-semibold text-slate-700 dark:text-slate-200">
          MCP 服务器
        </h2>
        <ExtensionPanel />

        {/* ③ 内置工具目录（角色卡白名单的选项清单） */}
        <h2 id="catalog" className="mt-8 mb-3 scroll-mt-4 text-sm font-semibold text-slate-700 dark:text-slate-200">
          内置工具目录
        </h2>
        {catalog && (
          <div className="space-y-4">
            <Card className="p-4">
              <p className="text-xs font-medium text-slate-500 dark:text-slate-400">内核工具</p>
              {catalog.kernel.map((t) => (
                <div key={t.name} className="mt-1 text-xs">
                  <code className="text-slate-700 dark:text-slate-200">{t.name}</code>
                  <span className="ml-2 text-slate-400 dark:text-slate-500">{t.description}</span>
                </div>
              ))}
            </Card>
            {Object.entries(catalog.domains).map(([id, tools]) => (
              <Card key={id} className="p-4">
                <p className="text-xs font-medium text-slate-500 dark:text-slate-400">
                  {domainName(id)}{" "}
                  <code className="rounded bg-slate-100 dark:bg-slate-700/50 px-1 py-0.5 text-11 text-slate-400">
                    {id}
                  </code>
                </p>
                {/* 两列网格（2026-10-25 玻璃重设计）：单列码表要滚很久才看完一个域，
                    两列卡片一眼扫完。 */}
                <div className="mt-2 grid gap-1.5 sm:grid-cols-2">
                  {tools.map((t) => (
                    <div key={t.name} className="rounded-lg bg-white/50 px-2 py-1.5 text-xs dark:bg-white/5">
                      <code className="text-slate-700 dark:text-slate-200">{t.name}</code>
                      <p className="mt-0.5 line-clamp-2 text-11 leading-relaxed text-slate-400 dark:text-slate-500">
                        {t.description}
                      </p>
                    </div>
                  ))}
                </div>
              </Card>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
