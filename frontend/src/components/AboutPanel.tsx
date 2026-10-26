// 「关于与系统状态」子页签（P3-1 第二刀：从 pages/SettingsPage.tsx **逐字搬来**，
// 正文一字未动 —— 等价证明同 MemoryPanel：`SettingsPage.test.tsx` 从整页经 DOM 驱动，
// 382 支用例数一字不变；import 面由 tsc 的 noUnusedLocals 看守）。
import { useCallback, useEffect, useRef, useState } from "react";
import { api, type DomainInfo, type ModelSettings, type RoleCard, type ThreadRow } from "../api";
import { Button, Card } from "./ui";
import { DiagnosticsCard } from "./DiagnosticsCard";
import { ShellReleaseCard } from "./ShellReleaseCard";
import { UpdateCheckCard } from "./UpdateCheckCard";
import { describeError } from "../lib/errors";
import { useAppearance } from "../lib/appearance";

// ---------------------------------------------------------------- 关于与系统状态

export function AboutPanel({
  onOpenChat,
  theme,
  onToggleTheme,
}: {
  onOpenChat?: () => void;
  theme?: string;
  onToggleTheme?: () => void;
}) {
  const [info, setInfo] = useState<{
    defaultBackend: string;
    plugins: string;
    roles: number;
    sessions: number;
  }>({ defaultBackend: "…", plugins: "…", roles: 0, sessions: 0 });
  const [refreshed, setRefreshed] = useState(false);
  const [loadError, setLoadError] = useState("");

  const loadInfo = useCallback(async () => {
    const [ms, domains, roles, sessions] = await Promise.all([
      api.get<ModelSettings>("/api/settings/models"),
      api.get<DomainInfo[]>("/api/domains"),
      api.get<RoleCard[]>("/api/roles"),
      api.get<ThreadRow[]>("/api/sessions"),
    ]);
    setInfo({
      defaultBackend: ms.default || "env 默认（local）",
      plugins: `${domains.length} 个域`,
      roles: roles.length,
      sessions: sessions.length,
    });
  }, []);

  useEffect(() => {
    loadInfo().catch((e) => setLoadError(`加载失败：${describeError(e)}`));
  }, [loadInfo]);

  const bgInputRef = useRef<HTMLInputElement>(null);
  const {
    material,
    palette,
    alpha,
    bgImage,
    bgBlur,
    bgDim,
    toggleMaterial,
    togglePalette,
    setAlpha,
    setBgImage,
    setBgBlur,
    setBgDim,
  } = useAppearance();

  /** 选一张本地图当背景：上传到本机数据根，拿到 URL 后交给外观层。 */
  async function uploadBg(file: File) {
    try {
      const form = new FormData();
      form.append("file", file);
      const body = await api.post<{ url: string }>("/api/appearance/background", form);
      setBgImage(body.url);
      setLoadError("背景图已设置");
    } catch (e) {
      setLoadError(`背景图上传失败：${describeError(e)}`);
    }
  }

  /** 移除背景图（同时删掉服务端那张，免得目录越积越多）。 */
  async function clearBg() {
    const name = bgImage.split("/").pop() || "";
    setBgImage("");
    if (!name) return;
    try {
      await api.del(`/api/appearance/background/${encodeURIComponent(name)}`);
    } catch {
      /* 删不掉不报错：界面上的"移除"已经生效（URL 不用了） */
    }
  }

  return (
    <div className="mt-6 space-y-4">
      {/* 外观三轴（2026-10-25）：材质（玻璃 ↔ 实板）、配色（暖 ↔ 冷）、明暗。
          三个开关正交 —— 毛玻璃的"透明"和暖色调是**两件事**，之前混成一锅就调不了。 */}
      <Card className="p-5">
        <h3 className="text-sm font-medium text-slate-900 dark:text-slate-100">外观</h3>
        <p className="mt-1 text-xs text-slate-400 dark:text-slate-500">
          材质、配色与明暗各管各的，任意组合都成立；切换即时生效、随本机记住。
        </p>
        <div className="mt-2.5 flex flex-wrap items-center gap-4">
          <span className="flex items-center gap-2 text-xs text-slate-500 dark:text-slate-400">
            材质
            <span className="rounded-lg bg-slate-100 px-2 py-1 dark:bg-slate-700/50">
              {material === "glass" ? "毛玻璃" : "实板"}
            </span>
            <button
              onClick={toggleMaterial}
              className="rounded-lg border border-slate-200 px-2.5 py-1 text-xs text-slate-600 hover:border-blue-300 dark:border-slate-600 dark:text-slate-300 dark:hover:border-blue-700"
            >
              切换为{material === "glass" ? "实板" : "毛玻璃"}
            </button>
          </span>
          <span className="flex items-center gap-2 text-xs text-slate-500 dark:text-slate-400">
            配色
            <span className="rounded-lg bg-slate-100 px-2 py-1 dark:bg-slate-700/50">
              {palette === "warm" ? "暖可可" : "冷中性"}
            </span>
            <button
              onClick={togglePalette}
              className="rounded-lg border border-slate-200 px-2.5 py-1 text-xs text-slate-600 hover:border-blue-300 dark:border-slate-600 dark:text-slate-300 dark:hover:border-blue-700"
            >
              切换为{palette === "warm" ? "冷中性" : "暖可可"}
            </button>
          </span>
          {/* 透明度：**整个界面**的透光量（0 = 底色全实，100 = 底色全透）。
              可输入任意值（数字框）+ 拖动（滑杆）—— 用户要的是"能输"，不是几个档位。 */}
          <label className="flex items-center gap-2 text-xs text-slate-500 dark:text-slate-400">
            透明度
            <input
              type="number"
              min={0}
              max={100}
              value={alpha}
              onChange={(e) => setAlpha(Number(e.target.value))}
              className="w-16 rounded-lg border border-slate-200 bg-white px-2 py-1 text-xs dark:border-slate-600 dark:bg-slate-800"
              aria-label="界面透明度（百分比）"
            />
            <span>%</span>
            <input
              type="range"
              min={0}
              max={100}
              value={alpha}
              onChange={(e) => setAlpha(Number(e.target.value))}
              className="w-32 accent-[var(--accent)]"
              aria-label="界面透明度滑杆"
            />
          </label>
          {onToggleTheme && (
            <span className="flex items-center gap-2 text-xs text-slate-500 dark:text-slate-400">
              明暗
              <span className="rounded-lg bg-slate-100 px-2 py-1 dark:bg-slate-700/50">
                {theme === "dark" ? "深色" : "浅色"}
              </span>
              <button
                onClick={onToggleTheme}
                className="rounded-lg border border-slate-200 px-2.5 py-1 text-xs text-slate-600 hover:border-blue-300 dark:border-slate-600 dark:text-slate-300 dark:hover:border-blue-700"
              >
                切换为{theme === "dark" ? "浅色" : "深色"}
              </button>
            </span>
          )}
        </div>
      </Card>

      {/* 背景图（2026-10-25，第五轴）：选一张本地图当界面的底 —— 有了它，
          "透明度"才看得出层次（不然只是把底色调淡）。模糊与暗化是**可读性保障**：
          没有暗化，图上的字会看不清。 */}
      <Card className="p-5">
        <h3 className="text-sm font-medium text-slate-900 dark:text-slate-100">背景图</h3>
        <p className="mt-1 text-xs text-slate-400 dark:text-slate-500">
          选一张本地图片当界面的底（png/jpg/webp/gif）。图片存在本机数据根里，不出网。
        </p>
        <div className="mt-2.5 flex flex-wrap items-center gap-3">
          <input
            ref={bgInputRef}
            type="file"
            accept=".png,.jpg,.jpeg,.webp,.gif"
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) void uploadBg(f);
              e.target.value = "";
            }}
          />
          <Button variant="outline" size="sm" type="button" onClick={() => bgInputRef.current?.click()}>
            选一张图
          </Button>
          {bgImage && (
            <>
              <span className="rounded-lg bg-slate-100 px-2 py-1 text-11 text-slate-500 dark:bg-slate-700/50 dark:text-slate-400">
                已设置
              </span>
              <Button variant="ghost" size="sm" type="button" onClick={() => void clearBg()}>
                移除
              </Button>
            </>
          )}
          <label className="flex items-center gap-1.5 text-xs text-slate-500 dark:text-slate-400">
            模糊
            <input
              type="range"
              min={0}
              max={24}
              value={bgBlur}
              onChange={(e) => setBgBlur(Number(e.target.value))}
              className="w-24 accent-[var(--accent)]"
              aria-label="背景图模糊"
            />
            <span className="w-8 text-11">{bgBlur}px</span>
          </label>
          <label className="flex items-center gap-1.5 text-xs text-slate-500 dark:text-slate-400">
            暗化
            <input
              type="range"
              min={0}
              max={80}
              value={Math.round(bgDim * 100)}
              onChange={(e) => setBgDim(Number(e.target.value) / 100)}
              className="w-24 accent-[var(--accent)]"
              aria-label="背景图暗化"
            />
            <span className="w-8 text-11">{Math.round(bgDim * 100)}%</span>
          </label>
        </div>
      </Card>

      <Card className="p-5">
        <h3 className="text-sm font-medium text-slate-900 dark:text-slate-100">关于</h3>
        <p className="mt-1.5 text-xs leading-relaxed text-slate-500 dark:text-slate-400">
          rolecard-agent 控制台。多角色对话 Agent：角色卡控制人设与工具权限，
          插件以数据驱动启停。
        </p>
        <p className="mt-1 text-xs text-slate-400 dark:text-slate-500">语言：简体中文（内置）</p>
      </Card>

      <Card className="p-5">
        <h3 className="text-sm font-medium text-slate-900 dark:text-slate-100">系统状态</h3>
        {loadError && <p className="mt-1 text-xs text-red-600 dark:text-red-400 dark:text-red-500">{loadError}</p>}
        <dl className="mt-2 grid grid-cols-2 gap-x-6 gap-y-2 text-xs">
          <div className="flex justify-between border-b border-slate-50 py-1">
            <dt className="text-slate-400 dark:text-slate-500">对话默认（服务页第 1 位）</dt>
            <dd className="font-mono">{info.defaultBackend}</dd>
          </div>
          <div className="flex justify-between border-b border-slate-50 py-1">
            <dt className="text-slate-400 dark:text-slate-500">领域插件（启用/注册）</dt>
            <dd className="font-mono">{info.plugins}</dd>
          </div>
          <div className="flex justify-between border-b border-slate-50 py-1">
            <dt className="text-slate-400 dark:text-slate-500">角色卡数量</dt>
            <dd className="font-mono">{info.roles}</dd>
          </div>
          <div className="flex justify-between border-b border-slate-50 py-1">
            <dt className="text-slate-400 dark:text-slate-500">对话数量</dt>
            <dd className="font-mono">{info.sessions}</dd>
          </div>
        </dl>
        <p className="mt-2 text-11 text-slate-400 dark:text-slate-500">
          对话默认谁算、失败时依次回退给谁 → 在「服务」里改；模型的增删与它的密钥 → 在「模型」里改。
          插件的启停在左侧「插件」页（停用立即生效）。
        </p>
      </Card>

      <Card className="p-5">
        <h3 className="text-sm font-medium text-slate-900 dark:text-slate-100">演示数据</h3>
        <p className="mt-1.5 text-xs text-slate-500 dark:text-slate-400">
          评测 / 演示用的虚构档案可由脚本重建；对话与数据的管理操作在对话页与插件页。
        </p>
        <div className="mt-2 flex gap-2">
          <button
            onClick={() => onOpenChat?.()}
            className="rounded-lg border border-slate-200 dark:border-slate-700 px-3 py-1.5 text-xs text-slate-600 dark:text-slate-300 hover:border-blue-300 dark:hover:border-blue-700"
          >
            前往对话
          </button>
          <button
            onClick={async () => {
              await loadInfo();
              setRefreshed(true);
              setTimeout(() => setRefreshed(false), 2000);
            }}
            className="rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 px-3 py-1.5 text-xs text-slate-600 dark:text-slate-300 hover:border-blue-300 dark:hover:border-blue-700"
          >
            {refreshed ? "已刷新 ✓" : "刷新状态"}
          </button>
        </div>
      </Card>

      {/* 检查更新（设计稿《更新机制与OCR随包》）：只读比对，判据在后端，这里照实画。 */}
      <UpdateCheckCard />

      {/* 诊断（P2-②）：一键导出诊断包 —— 错误签名 + 双日志 + 版本指纹，报 bug 附带现场。 */}
      <DiagnosticsCard />

      {/* 桌面壳安装包（D②-4）：没有产物时整卡不渲染，所以它放在最后也不会在页面上留空位。 */}
      <ShellReleaseCard />
    </div>
  );
}
