import { useEffect, useRef, useState } from "react";

import type { RoleCard } from "../../api";
import { useMenus } from "../../hooks/useMenus";
import { useModelBackends, type SamplingField } from "../../hooks/useModelBackends";
import type { Tone } from "../Toast";
import { IconClip, IconModel, IconUser } from "./icons";

/** 角色多到几个，抽屉里才出现搜索框：三五个的时候一个框只是多一个要看的控件。 */
const ROLE_SEARCH_FROM = 6;

/**
 * 采样惩罚那一栏的三行（设计稿 §8.2：我们此前只露了 num_ctx / temperature）。
 *
 * 档位是**保守地照出厂区间**给的，不是"我们认为更好的值"：`null` = 不传 = 听引擎的，排在第一，
 * 而且默认就停在那儿 —— 小模型上调惩罚容易伤连贯（§8.2 原话），没量过就不替用户决定。
 * `nativeOnly` 那一行对云端根本不出现：OpenAI 兼容体没有 `repeat_penalty` 这个标准字段，
 * 给一个"设了也不知道有没有生效"的控件比不给更糟（后端 PATCH 也会 400 挡）。
 */
const SAMPLING_FIELDS: {
  key: SamplingField;
  label: string;
  nativeOnly: boolean;
  options: { value: number | null; label: string }[];
}[] = [
  {
    key: "repeat_penalty",
    label: "重复惩罚",
    nativeOnly: true,
    // 标签自己拼：`String(1.0)` 在 JS 里是 "1"，混在 1.1/1.2 旁边读起来像少了一档。
    options: [
      { value: null, label: "未设置" },
      { value: 1.0, label: "1.0" },
      { value: 1.1, label: "1.1" },
      { value: 1.2, label: "1.2" },
      { value: 1.3, label: "1.3" },
    ],
  },
  {
    key: "frequency_penalty",
    label: "频率惩罚",
    nativeOnly: false,
    options: [
      { value: null, label: "未设置" },
      { value: 0, label: "0.0" },
      { value: 0.1, label: "0.1" },
      { value: 0.2, label: "0.2" },
      { value: 0.3, label: "0.3" },
    ],
  },
  {
    key: "presence_penalty",
    label: "存在惩罚",
    nativeOnly: false,
    options: [
      { value: null, label: "未设置" },
      { value: 0, label: "0.0" },
      { value: 0.1, label: "0.1" },
      { value: 0.2, label: "0.2" },
      { value: 0.3, label: "0.3" },
    ],
  },
];

/**
 * 输入框下方那一排功能（对齐 WorkBuddy）—— 全部对接真实后端能力。
 *
 * 为什么两个菜单要同住一个组件：它们互斥（开角色菜单要关模型菜单），而"鼠标移出后延时
 * 关闭"那把定时器是**共用的一把** —— 拆成两个组件各拿一份定时器，从角色菜单移到模型菜单
 * 就不会取消对方的延时关闭。菜单开合因此是这个工具条的私事，不上报给页面。
 */
export default function ChatToolbar({
  roles,
  currentRoleId,
  displayRole,
  unreadByRole,
  sessionId,
  selectionNonce,
  busy,
  uploading,
  selectMode,
  sessionModel,
  sessionMode,
  onPickRole,
  onSwitchModel,
  onSwitchMode,
  onUpload,
  onToggleSelectMode,
  onStatus,
}: {
  roles: RoleCard[];
  /** 当前会话所属角色（算生效模型要用它的 `model_name`）。 */
  currentRoleId: string;
  /** 角色下拉显示的那一个：会话的角色，没会话时是默认角色。 */
  displayRole: string;
  unreadByRole: Record<string, number>;
  sessionId: string | null;
  /** 页面每选中一条会话自增一次 —— 菜单收合的扳机，见上面那个 effect。 */
  selectionNonce: number;
  busy: boolean;
  uploading: boolean;
  selectMode: boolean;
  /** 会话级模型覆盖（null = 跟随设置里的默认后端）。 */
  sessionModel: string | null;
  sessionMode: string;
  onPickRole: (roleId: string) => void;
  /** 返回是否切换成功 —— 失败时菜单保持打开，让用户看得见没换成。 */
  onSwitchModel: (name: string | null) => Promise<boolean>;
  onSwitchMode: (mode: "chat" | "agent") => void;
  onUpload: (file: File) => void;
  onToggleSelectMode: () => void;
  onStatus: (text: string, tone?: Tone) => void;
}) {
  const {
    modelMenuOpen,
    setModelMenuOpen,
    roleMenuOpen,
    setRoleMenuOpen,
    ctxOpen,
    setCtxOpen,
    sampOpen,
    setSampOpen,
    armMenuClose,
    cancelMenuClose,
    closeAllMenus,
  } = useMenus();
  const { backends, providerLabels, defaultBackend, grouped, setModelCtx, setModelSampling } =
    useModelBackends(onStatus);
  // 抽屉里的搜索词：只活在这一个抽屉里，所以是组件的私事（打开时复位，见角色按钮）。
  const [roleFilter, setRoleFilter] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);

  /** 换了一条会话就把菜单收掉：勾选与「当前」那两格说的是刚换掉的这一条。
   *  `selectionNonce` 只在页面选中会话时自增 —— 发送时顺手新建一条**不算**，
   *  那个时刻用户可能正把手停在菜单上（原先这一步写在 `selectSession` 里，跟着它一起搬过来）。 */
  useEffect(() => {
    closeAllMenus();
  }, [selectionNonce]); // eslint-disable-line react-hooks/exhaustive-deps

  const roleBackend = roles.find((r) => r.role_id === currentRoleId)?.model_name || null;
  const effectiveBackend = sessionModel || roleBackend || defaultBackend;
  /** 抽屉里当前列得出来的角色：按名字子串过滤（大小写不敏感）。没有搜索词时就是全量。 */
  const roleQuery = roleFilter.trim().toLowerCase();
  const shownRoles = roleQuery
    ? roles.filter((r) => r.role_name.toLowerCase().includes(roleQuery))
    : roles;

  return (
    <div className="mx-auto mt-2 flex max-w-3xl items-center gap-2">
      <span
        className="relative"
        onMouseEnter={cancelMenuClose}
        onMouseLeave={() => armMenuClose(() => setRoleMenuOpen(false))}
        onKeyDown={(e) => {
          // 键盘用户的第二条退路：菜单靠鼠标移出关闭，Esc 必须也能关。
          if (e.key === "Escape") closeAllMenus();
        }}
      >
        {/* 角色切换（WorkBuddy 式自定义菜单）：原生 select 的弹层系统绘制、样式突兀，
            换成与模型菜单同款的面板——角色名 + 内置徽标 + 当前项勾选。 */}
        <button
          onClick={() => {
            setModelMenuOpen(false); // 两个菜单互斥
            if (!roleMenuOpen) setRoleFilter(""); // 每次打开都是全量：上次的过滤词留着会让人以为角色变少了
            setRoleMenuOpen((o) => !o);
          }}
          aria-haspopup="true"
          aria-expanded={roleMenuOpen}
          title="换个说话的角色 —— 会打开那个角色自己的那条对话（各角色的话留在各自那条线里）"
          className="flex items-center gap-1.5 rounded-full border border-slate-200 bg-white px-3 py-1 text-xs text-slate-600 hover:border-blue-300 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300 dark:hover:border-blue-700"
        >
          <IconUser />
          {roles.find((r) => r.role_id === displayRole)?.role_name ?? "角色"} ▾
        </button>
        {roleMenuOpen && (
          <>
            {/* 抽屉式而非"全量浮层"（用户 09-26："角色多了咋办"）：列表封顶 60vh 内部滚动，
                角色一多再给一个搜索框 —— 少了这个封顶，十几个角色就能把浮层顶出屏幕，
                而它往上长是会被头部截掉的。 */}
            <div className="absolute bottom-full left-0 z-20 mb-2 flex max-h-[min(60vh,420px)] w-64 flex-col overflow-hidden rounded-xl border border-slate-200 bg-white shadow-lg dark:border-slate-600 dark:bg-slate-800">
              <p className="shrink-0 bg-slate-50 px-3 py-1.5 text-[11px] font-medium text-slate-400 dark:bg-slate-800/60 dark:text-slate-500">
                选角色 = 进那个角色自己的那条对话（原来那条留在左侧）
              </p>
              {roles.length > ROLE_SEARCH_FROM && (
                <input
                  value={roleFilter}
                  onChange={(e) => setRoleFilter(e.target.value)}
                  placeholder={`搜角色（${roles.length} 个）`}
                  aria-label="搜角色"
                  className="mx-2 mt-2 shrink-0 rounded-lg border border-slate-200 px-2 py-1 text-xs outline-none focus:border-blue-400 dark:border-slate-600 dark:bg-slate-900"
                />
              )}
              <div className="min-h-0 flex-1 overflow-y-auto py-1">
                {shownRoles.map((r) => {
                  const unread = unreadByRole[r.role_id] ?? 0;
                  return (
                    <button
                      key={r.role_id}
                      onClick={() => {
                        setRoleMenuOpen(false);
                        onPickRole(r.role_id);
                      }}
                      className="flex w-full items-center justify-between gap-2 px-3 py-2 text-xs hover:bg-blue-50 dark:hover:bg-blue-900/30"
                    >
                      <span className="truncate text-slate-700 dark:text-slate-200">{r.role_name}</span>
                      <span className="ml-auto flex shrink-0 items-center gap-1.5">
                        {unread > 0 && (
                          <span
                            className="rounded-full bg-blue-600 px-1.5 text-[10px] text-white"
                            title={`${unread} 条主动找你，还没读`}
                          >
                            {unread}
                          </span>
                        )}
                        {r.is_builtin && (
                          <span className="rounded bg-slate-100 px-1 text-[10px] text-slate-400 dark:bg-slate-700/60 dark:text-slate-400">
                            内置
                          </span>
                        )}
                        {displayRole === r.role_id && (
                          <span className="text-blue-600 dark:text-blue-400">✓</span>
                        )}
                      </span>
                    </button>
                  );
                })}
                {shownRoles.length === 0 && (
                  <p className="px-3 py-2 text-xs text-slate-400 dark:text-slate-500">
                    没有匹配「{roleFilter}」的角色
                  </p>
                )}
              </div>
            </div>
          </>
        )}
      </span>
      {/* 对话/智能体 模式切换（会话级，PATCH /api/session）：agent = 多步自主任务
          —— 注入规划指令、步数上限自动翻倍。与角色/模型同款"下一轮生效"。 */}
      <div
        className="flex items-center rounded-full border border-slate-200 bg-white p-0.5 text-xs dark:border-slate-700 dark:bg-slate-800"
        title={sessionMode === "agent" ? "智能体模式：多步自主任务" : "对话模式：一问一答"}
      >
        <button
          onClick={() => onSwitchMode("chat")}
          className={`rounded-full px-2.5 py-1 transition-colors ${
            sessionMode !== "agent" ? "bg-blue-600 text-white" : "text-slate-500 dark:text-slate-400"
          }`}
        >
          对话
        </button>
        <button
          onClick={() => onSwitchMode("agent")}
          className={`rounded-full px-2.5 py-1 transition-colors ${
            sessionMode === "agent" ? "bg-blue-600 text-white" : "text-slate-500 dark:text-slate-400"
          }`}
        >
          智能体
        </button>
      </div>
      <div
        className="relative"
        onMouseEnter={cancelMenuClose}
        onMouseLeave={() => armMenuClose(() => setModelMenuOpen(false))}
      >
        <button
          onClick={() => {
            setRoleMenuOpen(false); // 两个菜单互斥
            setModelMenuOpen((o) => !o);
          }}
          aria-haspopup="true"
          aria-expanded={modelMenuOpen}
          title="切换本对话使用的模型（按供应商分组；选中即开对话）"
          className="flex items-center gap-1.5 rounded-full border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 px-3 py-1 text-xs text-slate-600 dark:text-slate-300 hover:border-blue-300 dark:hover:border-blue-700"
        >
          <IconModel />
          {backends.find((b) => b.name === effectiveBackend)?.model || effectiveBackend || "模型"} ▾
        </button>
        {modelMenuOpen && (
          <>
            <div className="absolute bottom-full left-0 z-20 mb-2 max-h-72 w-72 overflow-y-auto rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 shadow-lg">
            <button
              onClick={async () => {
                if (await onSwitchModel(null)) setModelMenuOpen(false);
              }}
              className="flex w-full items-center justify-between px-3 py-2 text-xs hover:bg-blue-50 dark:bg-blue-900/30"
            >
              <span>默认后端（跟随设置）</span>
              {sessionModel === null && <span className="text-blue-600 dark:text-blue-400">✓</span>}
            </button>
            {grouped.map(([provider, list]) => (
              <div key={provider}>
                <p className="bg-slate-50 dark:bg-slate-800/50 px-3 py-1 text-[11px] font-medium text-slate-400 dark:text-slate-500">
                  {providerLabels[provider] ?? provider}
                </p>
                {list.map((b) => (
                  <div key={b.name} className="relative">
                    {/* 选择按钮与上下文按钮是**兄弟**：嵌在 button 内部的徽章点击会被
                        父按钮的激活吞掉（实测），拆开才互不影响。 */}
                    <div className="flex items-center">
                      <button
                        onClick={async () => {
                          if (await onSwitchModel(b.name)) setModelMenuOpen(false);
                        }}
                        className="flex min-w-0 flex-1 items-center justify-between px-3 py-1.5 text-xs hover:bg-blue-50 dark:hover:bg-blue-900/30"
                      >
                        <span className="flex min-w-0 items-center gap-1.5">
                          <span className="font-mono truncate">{b.model}</span>
                          {b.supports_vision && (
                            <span className="shrink-0 rounded bg-emerald-100 px-1 py-0.5 text-[9px] font-medium text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300">
                              视觉
                            </span>
                          )}
                          {b.supports_tools && (
                            <span className="shrink-0 rounded bg-sky-100 px-1 py-0.5 text-[9px] font-medium text-sky-700 dark:bg-sky-900/40 dark:text-sky-300">
                              工具
                            </span>
                          )}
                        </span>
                        <span className="ml-2 flex min-w-0 items-center gap-1.5">
                          <span className="truncate text-slate-400 dark:text-slate-500">{b.name}</span>
                        </span>
                        {effectiveBackend === b.name && (
                          <span className="ml-1 text-blue-600 dark:text-blue-400">✓</span>
                        )}
                      </button>
                      {(b.style === "native") && (
                        <button
                          onClick={() => setCtxOpen((c) => (c === b.name ? null : b.name))}
                          title="设置该模型的上下文窗口（num_ctx）"
                          className="mr-2 shrink-0 rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-500 hover:bg-slate-200 dark:bg-slate-700/60 dark:text-slate-400 dark:hover:bg-slate-600"
                        >
                          {b.num_ctx ? `${Math.round(b.num_ctx / 1024)}k ▾` : "上下文 ▾"}
                        </button>
                      )}
                      {/* 采样惩罚：两类客户端都有这一栏（重复惩罚只对本地，见
                          `SAMPLING_FIELDS`）。徽章上的"·已设"只说"至少一栏不是默认"，
                          具体数值在面板里逐栏回显 —— 徽章上摆三个数是给人在菜单里读表格。 */}
                      <button
                        onClick={() => setSampOpen((s) => (s === b.name ? null : b.name))}
                        title="采样惩罚（重复 / 频率 / 存在）：保存即热重建，下一轮生效"
                        className="mr-2 shrink-0 rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-500 hover:bg-slate-200 dark:bg-slate-700/60 dark:text-slate-400 dark:hover:bg-slate-600"
                      >
                        {b.repeat_penalty != null ||
                        b.frequency_penalty != null ||
                        b.presence_penalty != null
                          ? "采样 ·已设 ▾"
                          : "采样 ▾"}
                      </button>
                    </div>
                    {/* 上下文选项：点行内「上下文」徽章展开（inline，触屏可用） */}
                    {(b.style === "native") &&
                      ctxOpen === b.name && (
                      <div className="border-t border-slate-100 px-3 py-2 dark:border-slate-700/60">
                        <p className="pb-1.5 text-[10px] font-medium text-slate-400 dark:text-slate-500">
                          上下文窗口 · {b.model}
                        </p>
                        <div className="grid grid-cols-3 gap-1">
                          {[
                            { label: "引擎默认", value: null },
                            { label: "2048", value: 2048 },
                            { label: "4096", value: 4096 },
                            { label: "8192", value: 8192 },
                            { label: "16384", value: 16384 },
                            { label: "32768", value: 32768 },
                          ].map((opt) => (
                            <button
                              key={opt.label}
                              onClick={() => setModelCtx(b.name, opt.value)}
                              className={`rounded px-2 py-1 text-[11px] ${
                                (b.num_ctx ?? null) === opt.value
                                  ? "bg-blue-600 text-white"
                                  : "text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-700/60"
                              }`}
                            >
                              {opt.label}
                            </button>
                          ))}
                        </div>
                        <p className="mt-1.5 text-[10px] leading-relaxed text-slate-400 dark:text-slate-500">
                          Ollama 默认仅 2048 tokens，调大才能真正用上模型窗口
                        </p>
                      </div>
                    )}
                    {/* 采样惩罚面板：一栏一行、行内点档位，第一档永远是「未设置」。 */}
                    {sampOpen === b.name && (
                      <div className="border-t border-slate-100 px-3 py-2 dark:border-slate-700/60">
                        {SAMPLING_FIELDS.filter(
                          (f) =>
                            !f.nativeOnly || b.style === "native",
                        ).map((f) => (
                          <div key={f.key} className="flex items-start gap-1.5 py-0.5">
                            <span className="w-16 shrink-0 pt-1 text-[10px] text-slate-400 dark:text-slate-500">
                              {f.label}
                            </span>
                            <div className="flex flex-wrap gap-1">
                              {f.options.map((opt) => (
                                <button
                                  key={opt.label}
                                  onClick={() => void setModelSampling(b.name, f.key, opt.value)}
                                  className={`rounded px-2 py-0.5 text-[11px] ${
                                    (b[f.key] ?? null) === opt.value
                                      ? "bg-blue-600 text-white"
                                      : "text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-700/60"
                                  }`}
                                >
                                  {opt.label}
                                </button>
                              ))}
                            </div>
                          </div>
                        ))}
                        <p className="mt-1 text-[10px] leading-relaxed text-slate-400 dark:text-slate-500">
                          「未设置」= 不传这项、听引擎的（Ollama 出厂重复惩罚就是 1.1）。
                          上调能压复读，但小模型上更容易伤连贯 —— 拿不准就留未设置。
                        </p>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            ))}
            </div>
          </>
        )}
      </div>
      <button
        onClick={() => fileRef.current?.click()}
        disabled={uploading}
        title="上传报告 / 图片，自动解析并入检索索引（.txt/.md/.pdf/.docx/.pptx/.xlsx + 图片 OCR）"
        className="flex items-center gap-1.5 rounded-full border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 px-3 py-1 text-xs text-slate-600 dark:text-slate-300 hover:border-blue-300 dark:hover:border-blue-700 disabled:opacity-50"
      >
        <IconClip />
        {uploading ? "上传中…" : "上传报告"}
      </button>
      <button
        onClick={onToggleSelectMode}
        disabled={busy || !sessionId}
        title="删除历史里的某几段问答：勾选任意一问或一答，会自动带上配对的另一侧"
        className={`flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs disabled:opacity-50 ${
          selectMode
            ? "border-amber-300 bg-amber-50 text-amber-700 dark:border-amber-700 dark:bg-amber-900/30 dark:text-amber-300"
            : "border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 text-slate-600 dark:text-slate-300 hover:border-amber-300 hover:text-amber-600"
        }`}
      >
        {selectMode ? "退出删除模式" : "删除对话"}
      </button>
      <input
        ref={fileRef}
        type="file"
        className="hidden"
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) onUpload(f);
          e.target.value = "";
        }}
      />
      <span className="ml-auto text-[11px] text-slate-300 dark:text-slate-600">
        Enter 发送 · 生成中可停止 · 停用插件即刻生效
      </span>
    </div>
  );
}
