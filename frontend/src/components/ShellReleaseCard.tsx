// 桌面壳安装包（D②-4）：设置→通用 底部一张卡。
//
// 为什么 B/S 里要有这个入口：分发形态是 A（后端打进包，用户自己的机器上跑），所以"下载桌面
// 版"不能指向某个公网服务器 —— 最合理的托管者就是**这个应用自己的后端**（它手里就有那份产物）。
//
// 整卡在 `available=false` 时**不渲染**（而不是灰着一个按钮）：判据是产物文件在不在，
// 配置了目录但没拷产物时，界面不该出现一个点了没反应的东西。
import { useCallback, useEffect, useState } from "react";

import { api, type ShellRelease } from "../api";

function humanSize(bytes: number): string {
  if (bytes >= 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024 / 1024).toFixed(1)} GB`;
  return `${Math.round(bytes / 1024 / 1024)} MB`;
}

function buildDay(iso?: string): string {
  if (!iso) return "";
  const parsed = new Date(iso);
  return Number.isNaN(parsed.getTime()) ? "" : parsed.toLocaleDateString("zh-CN");
}

export function ShellReleaseCard() {
  const [release, setRelease] = useState<ShellRelease | null>(null);

  const load = useCallback(async () => {
    try {
      setRelease(await api.getShellRelease());
    } catch {
      /* 拉不到就不显示：这张卡是可选入口，不该在通用页上留一句报错占位 */
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (!release?.available) return null;
  const name = release.file_name ?? "桌面壳安装包";
  const day = buildDay(release.built_at);
  const meta = [
    release.size_bytes ? humanSize(release.size_bytes) : "",
    day ? `构建于 ${day}` : "",
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <div className="rounded-xl border border-slate-200 bg-white p-5 dark:border-slate-700 dark:bg-slate-800">
      <h3 className="text-sm font-medium text-slate-900 dark:text-slate-100">
        桌面壳（Windows 桌宠）
      </h3>
      <p className="mt-1 flex items-center gap-2 text-xs text-slate-500 dark:text-slate-400">
        <span className="h-2 w-2 rounded-full bg-emerald-500" />
        有可下载的安装包
      </p>
      <p className="mt-2 font-mono text-xs text-slate-700 dark:text-slate-300">
        {name}
        {meta && <span className="ml-2 text-slate-400 dark:text-slate-500">{meta}</span>}
      </p>
      <p className="mt-1.5 text-xs text-slate-500 dark:text-slate-400">
        程序就在安装包里，只监听 127.0.0.1；桌宠、托盘与系统通知由它管。装好后不需要再开这个网页。
      </p>
      <a
        href="/api/shell-release/download"
        download={name}
        className="mt-3 inline-block rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs text-slate-600 hover:border-blue-300 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300 dark:hover:border-blue-700"
      >
        下载桌面壳
      </a>
    </div>
  );
}
