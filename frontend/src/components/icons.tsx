/** 全项目共用的内联 SVG 图标 —— **唯一出处**（2026-10-24 UI 打磨：此前对话页一套
 * （`chat/icons.tsx`，13px/2.0）、左侧导航一套（`App.tsx` 内联，16px/1.8），两套参数
 * 各写一遍、两处各长各的。合并后风格仍是两档尺寸，但**参数与形状只有一个家**。）
 *
 * 为什么不用图标库：全项目共十几个形状，一个依赖换不来收益；为什么不用 emoji：缺
 * 彩色字体的环境（部分 Linux/容器）emoji 会渲染成方框（见下方历史注释）。
 *
 * 约定：stroke 线性风格，`currentColor` 上色，尺寸由各图标自带（13 = 行内小标，
 * 16 = 导航/工具栏）。新图标加在这里，别再在页面里内联 `<svg>`。
 */

const iconAttrs = (width: number, strokeWidth: number) =>
  ({
    width,
    height: width,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth,
    strokeLinecap: "round",
    strokeLinejoin: "round",
  }) as const;

const INLINE = iconAttrs(13, 2); // 行内小标（输入框 / 消息工具行）
const NAV = iconAttrs(16, 1.8); // 导航与工具栏

// ---- 对话页（行内 13px）-------------------------------------------------

/** 用户。 */
export const IconUser = () => (
  <svg {...INLINE}>
    <path d="M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM4 21c0-4 3.6-6 8-6s8 2 8 6" />
  </svg>
);

/** 模型（芯片）。 */
export const IconModel = () => (
  <svg {...INLINE}>
    <rect x="6" y="6" width="12" height="12" rx="2" />
    <path d="M10 3v3M14 3v3M10 18v3M14 18v3M3 10h3M3 14h3M18 10h3M18 14h3" />
  </svg>
);

export const IconClip = () => (
  <svg {...INLINE}>
    <path d="M8 12l6.5-6.5a3 3 0 0 1 4.2 4.2L11 17.4a5 5 0 0 1-7.1-7.1L11 3.2" />
  </svg>
);

/** 图片（照片）——输入框「附加图片」按钮（多模态传图，2026-09-18）。 */
export const IconImage = () => (
  <svg {...INLINE}>
    <rect x="3" y="4" width="18" height="16" rx="2" />
    <circle cx="9" cy="10" r="1.6" />
    <path d="M3 17l5-5 4 4 4-4 5 5" />
  </svg>
);

/** 发送（上箭头）——嵌在输入框内的图标按钮（紧凑 IDE 式）。 */
export const IconSend = () => (
  <svg {...INLINE} className="h-4 w-4">
    <path d="M12 19V6M12 6l-5.5 5.5M12 6l5.5 5.5" />
  </svg>
);

/** 停止（方块）——生成中替换发送按钮。 */
export const IconStop = () => (
  <svg {...INLINE} fill="currentColor" stroke="none" className="h-3.5 w-3.5">
    <rect x="7" y="7" width="10" height="10" rx="1.5" />
  </svg>
);

/** 增强提示词（四角星）。 */
export const IconSparkle = () => (
  <svg {...INLINE} className="h-3.5 w-3.5">
    <path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8L12 3z" />
  </svg>
);

// ---- 左侧导航与抽屉入口（16px / 1.8，原 App.tsx 内联那一套）-----------------

export const IconChat = () => (
  <svg {...NAV}>
    <path d="M4 5h16v11H8l-4 3z" />
  </svg>
);

export const IconData = () => (
  <svg {...NAV}>
    <path d="M4 20h16M8 20v-6M13 20V7M18 20v-9" />
  </svg>
);

export const IconKnowledge = () => (
  <svg {...NAV}>
    <path d="M5 4h13v16H5zM8 4v16M11 9h4M11 13h4" />
  </svg>
);

export const IconRoles = () => (
  <svg {...NAV}>
    <path d="M3 6h18v12H3zM7 10h5M7 14h3M16 13.5a2 2 0 1 0 0-4 2 2 0 0 0 0 4z" />
  </svg>
);

export const IconPlugins = () => (
  <svg {...NAV}>
    <path d="M9 3v5M15 3v5M6 8h12v5a6 6 0 0 1-12 0V8ZM12 19v2" />
  </svg>
);

export const IconSettings = () => (
  <svg {...NAV}>
    <path d="M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z" />
    <path d="M4 12h2M18 12h2M12 4v2M12 18v2M6.5 6.5l1.4 1.4M16.1 16.1l1.4 1.4M17.5 6.5l-1.4 1.4M7.9 16.1l-1.4 1.4" />
  </svg>
);

/** 命令审批（盾 + 对勾）——侧栏抽屉入口（2026-10-24 从 ⚙️ emoji 换来）。 */
export const IconApproval = () => (
  <svg {...NAV}>
    <path d="M12 3 5 6v5c0 4.5 3 7.5 7 9 4-1.5 7-4.5 7-9V6l-7-3z" />
    <path d="m9.5 12 1.8 1.8L15 10" />
  </svg>
);

/** 主动消息（铃）——侧栏抽屉入口（同上，从 🔔 换来）。 */
export const IconBell = () => (
  <svg {...NAV}>
    <path d="M6 9a6 6 0 0 1 12 0c0 5 2 6 2 6H4s2-1 2-6z" />
    <path d="M10.5 19a1.8 1.8 0 0 0 3 0" />
  </svg>
);

/** 目录（任务目录树里的文件夹标记）。 */
export const IconFolder = ({ size = 12 }: { size?: number }) => (
  <svg {...iconAttrs(size, 1.8)} className="mr-1 inline-block align-[-1px]">
    <path d="M3 6h6l2 2h10v11H3z" />
  </svg>
);
