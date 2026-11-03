import { useEffect, useRef } from "react";

export type MenuEntry = { label: string; disabled?: boolean; run: () => void };

/**
 * 桌宠面板的右键菜单 —— **页面自绘，不走 Electron 原生菜单**。
 *
 * 为什么不原生：这扇窗是"无边框 + 透明 + `screen-saver` 置顶"，而独立的原生菜单窗会牵焦点
 * 与置顶（这两处在这扇窗上都是量出来的坑，见 `shell/main/windows.ts` 的 `applyPetPrefs`）；
 * 更要紧的是「重新生成 / 停止」本来就是页面里的动作，放原生菜单还得再走一趟 IPC 回页面。
 * 自绘这一版在浏览器里直接开 `#/pet` 也照样能用。
 *
 * `data-pet-ui` 不是给测试用的钩子：根节点的 `dragStart` / `rootClick` 都先问 `insideUi`，
 * 没有它，点菜单那一下会被当成"点了桌宠本体"而把面板收起。
 */
export default function PetContextMenu({
  x,
  y,
  entries,
  onClose,
}: {
  x: number;
  y: number;
  entries: MenuEntry[];
  onClose: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    // 点外面 / Esc / 窗口失焦都收。用捕获阶段：面板里那些 handler 先看到事件也无所谓，
    // 这一层只负责"落在菜单外就关"，不拦任何默认行为。
    const away = (event: PointerEvent) => {
      if (!ref.current?.contains(event.target as Node)) onClose();
    };
    const key = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("pointerdown", away, true);
    window.addEventListener("keydown", key);
    window.addEventListener("blur", onClose);
    return () => {
      window.removeEventListener("pointerdown", away, true);
      window.removeEventListener("keydown", key);
      window.removeEventListener("blur", onClose);
    };
  }, [onClose]);

  // 桌宠画布只有 380×520，菜单贴在右下角会整块溢出 ⇒ 按可视区夹一次（宽度/高度按实测上限估）。
  const left = Math.max(4, Math.min(x, window.innerWidth - 176));
  const top = Math.max(4, Math.min(y, window.innerHeight - 120));

  return (
    <div
      ref={ref}
      data-pet-ui="menu"
      role="menu"
      className="fixed z-50 w-44 rounded-lg border border-slate-200 bg-surface py-1 text-11 shadow-lg"
      style={{ left, top }}
    >
      {entries.map((entry) => (
        <button
          key={entry.label}
          role="menuitem"
          type="button"
          disabled={entry.disabled}
          onClick={() => {
            entry.run();
            onClose();
          }}
          // hover 高亮浅色端是 bg-blue-50 + text-blue-700（accent 的浅洗 + 暗 accent 字）。
          // 深色端从前写 `dark:enabled:hover:bg-slate-700/60` —— 双重翻转的残留（上一轮清
          // 841 条时我的正则 `dark:[a-z-]*slate` 不吃 `enabled:hover:` 这类冒号修饰，恰好
          // 漏掉这条）：--color-slate-700 = var(--bg-ink) 在 .dark 下是近白亮字色，于是
          // "深色下更深一档"实际铺出**近白一块**，配 accent 暗字实测对比 **1.61**（真鼠标
          // hover 取证，build/probe_dark_red_hover.cjs）。改成与全站深色端同一惯例
          // （dark:bg-blue-900/30 ×8 处、dark:hover:text-blue-400 ×6 处）：accent 深洗 +
          // accent 亮字，两轴一起翻，浅色端一个像素没动。
          className="block w-full px-2.5 py-1 text-left text-slate-600 enabled:hover:bg-blue-50 enabled:hover:text-blue-700 disabled:text-slate-300 dark:enabled:hover:bg-blue-900/30 dark:enabled:hover:text-blue-400"

        >
          {entry.label}
        </button>
      ))}
    </div>
  );
}
