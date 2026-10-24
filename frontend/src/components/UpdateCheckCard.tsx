// 「检查更新」卡（设计稿《更新机制与OCR随包》§3.1 的界面半边），挂在设置→关于。
//
// 为什么要有它：装机版没有版本提示，"装了旧版不知道有新版"是 2026-10-13 真实发生的
// 事故形状（用户装着 0.4.0，v0.4.1 发布三天无人知晓）。判据全在后端
// （core/update_check.py，壳与网页读同一份），这里只照实画三种读数：
//   · 有更新 → 强调条 + 下载/前往发布页的链接（用户自己点，绝不自动下载）；
//   · 没更新 → 一行灰字"已是最新"；
//   · 问不到（disabled/none+error）→ 如实说"没问到"，**不伪装成"已是最新"** ——
//     把"检查失败"画成"一切正常"是静默失效的防线，比没有防线更贵（本仓反复的那条）。
// 拉不到数据（端点本身挂了）时整卡不渲染，与 ShellReleaseCard 同一口径。
import { useCallback, useEffect, useState } from "react";

import { api, type UpdateCheck } from "../api";

export function UpdateCheckCard() {
  const [info, setInfo] = useState<UpdateCheck | null>(null);
  const [checking, setChecking] = useState(false);

  const load = useCallback(async () => {
    setChecking(true);
    try {
      setInfo(await api.getUpdateCheck());
    } catch {
      /* 端点挂了就不显示这张卡：它是可选入口，不该在关于页留一句报错占位 */
    } finally {
      setChecking(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (!info) return null;

  const verdict = info.has_update
    ? { tone: "text-amber-700 dark:text-amber-400", text: `发现新版本 ${info.latest}` }
    : info.source === "disabled"
      ? { tone: "text-slate-400 dark:text-slate-500", text: "更新检查已关闭（UPDATE_CHECK_ENABLED=0）" }
      : info.error
        ? { tone: "text-slate-400 dark:text-slate-500", text: `没问到最新版本：${info.error}` }
        : { tone: "text-slate-400 dark:text-slate-500", text: `已是最新（${info.current}）` };

  return (
    <div className="rounded-xl border border-slate-200 bg-white p-5 dark:border-slate-700 dark:bg-slate-800">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium text-slate-900 dark:text-slate-100">更新</h3>
        <button
          onClick={() => void load()}
          disabled={checking}
          className="rounded-lg border border-slate-200 px-2.5 py-1 text-xs text-slate-600 hover:border-blue-300 disabled:opacity-50 dark:border-slate-600 dark:text-slate-300 dark:hover:border-blue-700"
        >
          {checking ? "检查中…" : "重新检查"}
        </button>
      </div>
      <p className={`mt-1.5 text-xs ${verdict.tone}`}>{verdict.text}</p>
      <p className="mt-1 text-xs text-slate-400 dark:text-slate-500">
        当前版本 {info.current}
        {info.source === "release_dir" && "（和本机托管的安装包比）"}
        {info.source === "github" && "（和 GitHub 发布比）"}
      </p>
      {info.has_update && info.url && (
        <a
          href={info.url}
          {...(info.url.startsWith("/") ? {} : { target: "_blank", rel: "noreferrer" })}
          className="mt-3 inline-block rounded-lg border border-amber-300 bg-amber-50 px-3 py-1.5 text-xs text-amber-800 hover:bg-amber-100 dark:border-amber-700 dark:bg-amber-950 dark:text-amber-300 dark:hover:bg-amber-900"
        >
          {info.source === "release_dir" ? "下载新版安装包" : "前往发布页"}
        </a>
      )}
      {info.has_update && (
        <p className="mt-1.5 text-11 text-slate-400 dark:text-slate-500">
          下载后运行安装包即可覆盖升级；你的数据（对话、知识库、设置）都在安装目录之外，不会被动。
        </p>
      )}
    </div>
  );
}
