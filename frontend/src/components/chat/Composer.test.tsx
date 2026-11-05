// @vitest-environment jsdom
import { createRef } from "react";
import { fireEvent, render } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import Composer from "./Composer";

/**
 * 输入框的两条用户诉求（2026-11-05）各自钉住：
 *   ① 「回答我时输入框还可以输入」——从前 `disabled={busy}` 把整框锁死；
 *   ② 「没输入文字时上拉按键也要可以上拉」——从前拉高的只是**上限**，空框按内容
 *      收高，拉完纹丝不动，看起来像坏了。
 *
 * 两条都是"从不失败就没意义"的那类：判据读 DOM 属性/内联样式，不读注释。
 */
function makeProps(over: Partial<React.ComponentProps<typeof Composer>> = {}) {
  const inputRef = createRef<HTMLTextAreaElement>();
  return {
    props: {
      input: "",
      setInput: vi.fn(),
      busy: false,
      enhancing: false,
      pendingImage: null,
      setPendingImage: vi.fn(),
      ctxUsed: 0,
      ctxBudget: 0,
      ctxPct: 0,
      droppedThisTurn: null,
      inputRef,
      onSubmit: vi.fn(),
      onStop: vi.fn(),
      onEnhance: vi.fn(),
      onStatus: vi.fn(),
      ...over,
    } satisfies React.ComponentProps<typeof Composer>,
    inputRef,
  };
}

beforeEach(() => {
  localStorage.clear();
});

describe("生成中输入框不再被锁（用户 2026-11-05：回答时也要能打字）", () => {
  it("busy=true 时 textarea 可用，打字照样进 setInput", () => {
    const { props } = makeProps({ busy: true });
    const { container } = render(<Composer {...props} />);
    const box = container.querySelector("textarea") as HTMLTextAreaElement;
    expect(box.disabled).toBe(false);
    fireEvent.change(box, { target: { value: "下一句的草稿" } });
    expect(props.setInput).toHaveBeenCalledWith("下一句的草稿");
  });

  it("busy=true 时 Enter **不发送**，也不被吞（浏览器照常插换行，草稿在攒着）", () => {
    const { props } = makeProps({ busy: true, input: "攒了一半" });
    const { container } = render(<Composer {...props} />);
    const box = container.querySelector("textarea") as HTMLTextAreaElement;
    const event = fireEvent.keyDown(box, { key: "Enter", shiftKey: false });
    expect(props.onSubmit).not.toHaveBeenCalled();
    // fireEvent 返回 false = defaultPrevented 被调用过；这里要它**没被**拦
    expect(event).toBe(true);
  });

  it("空闲时 Enter 照旧发送（别把老行为改没了）", () => {
    const { props } = makeProps({ input: "发这句" });
    const { container } = render(<Composer {...props} />);
    const box = container.querySelector("textarea") as HTMLTextAreaElement;
    fireEvent.keyDown(box, { key: "Enter", shiftKey: false });
    expect(props.onSubmit).toHaveBeenCalledOnce();
  });
});

describe("输入框：默认最小；松手停在那；跨会话不记高度", () => {
  /** 顶边把手（role=separator）：pointerdown 起、pointermove 跟、pointerup 落。 */
  function handle(container: HTMLElement) {
    return container.querySelector('[role="separator"]') as HTMLElement;
  }

  it("拉过并松手 ⇒ 高度**停在放手那一档**（用户 2026-11-05：「我在那松手就放那」）", () => {
    // 上一版把这条写成了「松手回到贴内容」，正是被否掉的行为。现在钉回来：
    // 起手贴内容（最小）→ 拉着跟手 → 松手不回弹，停在你放的那一档。
    const { props, inputRef } = makeProps({ input: "" });
    const { container } = render(<Composer {...props} />);
    expect(inputRef.current?.style.height).toBe("0px"); // 起手：贴内容（jsdom scrollHeight=0）
    const h = handle(container);
    fireEvent.pointerDown(h, { clientY: 400, pointerId: 1 });
    fireEvent.pointerMove(h, { clientY: 260, pointerId: 1 }); // 上拉 140
    const dragged = (inputRef.current?.style.height ?? "0").replace("px", "");
    expect(Number(dragged)).toBeGreaterThan(0); // 拉了必须看得见动
    fireEvent.pointerUp(h, { pointerId: 1 });
    expect(inputRef.current?.style.height).toBe(`${dragged}px`); // 松手：停在那
  });

  it("默认最小 = **跨会话不记高度**，localStorage 那份只是上限", () => {
    // 用户："对话框咋默认是拉开的，默认是最小的就行了"——上一把把拖过的值落盘成
    // 常驻高度，每次重开都撑着。那份值现在只能是**上限**：重挂后空框贴内容，
    // maxHeight 里仍留着它。判据钉住"落盘的是上限、非常驻高度"这条分界。
    localStorage.setItem("chat.inputMaxH", "200");
    const { props, inputRef } = makeProps({ input: "" });
    const { container } = render(<Composer {...props} />);
    expect(inputRef.current?.style.maxHeight).toBe("200px"); // 上限：记着
    expect(inputRef.current?.style.height).toBe("0px"); // 高度：贴内容，没被撑开
    expect(handle(container)).not.toBeNull();
  });

  it("没拉过（无 localStorage）时空框就是贴内容", () => {
    const { props, inputRef } = makeProps({ input: "" });
    render(<Composer {...props} />);
    expect(inputRef.current?.style.maxHeight).toBe("160px");
    expect(inputRef.current?.style.height).toBe("0px");
  });
});
