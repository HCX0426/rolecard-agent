// 「用系统对话框选目录」按钮（D②-6）。
//
// 为什么单独一个组件：这条能力**只在桌面壳里存在**，浏览器里没有。判空放在渲染入口
// （`!shell` 直接不出现）比"出现但点了没反应"诚实，也让"壳里才有"这件事只写一遍。
//
// 路径是用户在原生对话框里挑的，页面从头到尾**不提供一个路径参数** —— 所以这不是一条
// "网页能指定任意路径"的口子。挑完只填进草稿：保存仍然是用户按下去的那一下。
import { useState } from "react";

import { shellBridge } from "../lib/shell";

export function NativeDirPickerButton({
  disabled,
  onPicked,
}: {
  disabled?: boolean;
  onPicked: (path: string) => void;
}) {
  const [err, setErr] = useState("");
  const shell = shellBridge();
  if (!shell) return null;

  async function pick() {
    setErr("");
    const bridge = shellBridge(); // 事件回调里重新取一次：闭包不继承上面那次判空
    if (!bridge) return;
    try {
      const picked = await bridge.pickDirectory();
      if (picked) onPicked(picked); // 取消 = null：什么都不改，也不报错
    } catch (e) {
      setErr(`系统对话框没打开：${(e as Error).message}`);
    }
  }

  return (
    <>
      <button
        onClick={() => void pick()}
        disabled={disabled}
        className="shrink-0 rounded-lg border border-slate-200 px-3 py-1.5 text-xs text-slate-600 hover:border-blue-300 disabled:opacity-50 dark:border-slate-600 dark:text-slate-300"
      >
        系统选目录…
      </button>
      {err && <span className="text-xs text-red-600 dark:text-red-400">{err}</span>}
    </>
  );
}
