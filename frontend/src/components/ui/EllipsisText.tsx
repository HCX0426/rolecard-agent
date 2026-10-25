import { useEffect, useRef, useState, type ReactNode } from "react";

/** 长文本契约的原语（2026-10-25 重设计）：**名字/标题可能很长**的挂点统一用它。

 *  为什么单独一个组件而不是各处贴 `truncate`：截断有两半——`min-w-0`（让 flex
 *  子项可以被压缩）+ `truncate`（省略号）+ `title`（鼠标悬停看全文）。三件缺一
 *  就出事故：`min-w-0` 缺 = flex 容器被撑爆（实测：工具条的角色按钮把整条撑出
 *  屏幕）；`title` 缺 = 用户永远看不到全名。三件绑进一个组件，这个语义就只有
 *  一处实现。

 *  `detect`（默认开）：内容是否真的被截断由 ResizeObserver 现测——截断时才挂
 *  `title`。挂不挂 title 有讲究：**没被截断的短名字挂 title 是噪音**（鼠标悬上去
 *  弹一个和可见文本一模一样的气泡）。
 */
export default function EllipsisText({
  children,
  className = "",
  title,
  detect = false,
  maxClass = "",
  as: Tag = "span",
}: {
  children: ReactNode;
  className?: string;
  /** 显式的 title（优先于自动检测）。传 undefined 且 detect=true 时自动判断。 */
  title?: string;
  /** 开启"截断了才显示 title"的自动检测（需要 measurement，默认关）。 */
  detect?: boolean;
  /** 最大宽度类（如 max-w-40）——truncate 的前提是有界。 */
  maxClass?: string;
  as?: "span" | "div";
}) {
  const ref = useRef<HTMLElement | null>(null);
  const [clipped, setClipped] = useState(false);

  useEffect(() => {
    if (!detect || !ref.current) return;
    // jsdom 没有 ResizeObserver（测试环境）：降级成"总是给 title"——宁可多一个
    // 悬停提示，也不要整棵组件树在测试里炸掉（那会让 45 条用例一起红）。
    if (typeof ResizeObserver === "undefined") {
      setClipped(true);
      return;
    }
    const el = ref.current;
    const update = () => setClipped(el.scrollWidth > el.clientWidth);
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, [detect, children]);

  const showTitle = title ?? (detect && clipped ? textOf(children) : undefined);

  return (
    <Tag
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      ref={ref as any}
      title={showTitle}
      className={`min-w-0 truncate ${maxClass} ${className}`}
    >
      {children}
    </Tag>
  );
}

function textOf(node: ReactNode): string {
  if (node == null || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textOf).join("");
  const el = node as { props?: { children?: ReactNode } };
  return el.props ? textOf(el.props.children) : "";
}
