/** 对话页用到的内联 SVG 图标（原散在 ChatPage 里，抽出以便复用与统一风格）。
 *
 * 为什么不用 emoji：缺彩色字体的环境（部分 Linux/容器）emoji 会渲染成方框。
 * 为什么保留 ChatPage 原有的线性风格：这些形状已是既有视觉的一部分，换风格等于改设计。
 */
const ICON = {
  width: 13,
  height: 13,
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 2,
  strokeLinecap: "round",
  strokeLinejoin: "round",
} as const;

export const IconUser = () => (
  <svg {...ICON}>
    <path d="M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM4 21c0-4 3.6-6 8-6s8 2 8 6" />
  </svg>
);

export const IconModel = () => (
  <svg {...ICON}>
    <rect x="6" y="6" width="12" height="12" rx="2" />
    <path d="M10 3v3M14 3v3M10 18v3M14 18v3M3 10h3M3 14h3M18 10h3M18 14h3" />
  </svg>
);

export const IconClip = () => (
  <svg {...ICON}>
    <path d="M8 12l6.5-6.5a3 3 0 0 1 4.2 4.2L11 17.4a5 5 0 0 1-7.1-7.1L11 3.2" />
  </svg>
);

/** 发送（上箭头）——嵌在输入框内的图标按钮（WorkBuddy 式）。 */
export const IconSend = () => (
  <svg {...ICON} className="h-4 w-4">
    <path d="M12 19V6M12 6l-5.5 5.5M12 6l5.5 5.5" />
  </svg>
);

/** 停止（方块）——生成中替换发送按钮。 */
export const IconStop = () => (
  <svg {...ICON} fill="currentColor" stroke="none" className="h-3.5 w-3.5">
    <rect x="7" y="7" width="10" height="10" rx="1.5" />
  </svg>
);

/** 增强提示词（四角星）。 */
export const IconSparkle = () => (
  <svg {...ICON} className="h-3.5 w-3.5">
    <path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8L12 3z" />
  </svg>
);
