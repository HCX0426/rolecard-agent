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

describe("上拉把手拉的是**高度**，不只是上限（空框也要看得见拉了）", () => {
  it("拉过（localStorage 有值）之后，空输入框的高度 = 拉的那份", () => {
    // jsdom 里 scrollHeight 恒 0：从前的写法 `min(scrollHeight, cap)` 会算出 0 ——
    // 于是"用户拉高了、框还是那么矮"正是这条判据要拦的形状。
    localStorage.setItem("chat.inputMaxH", "200");
    const { props, inputRef } = makeProps({ input: "" });
    render(<Composer {...props} />);
    expect(inputRef.current).not.toBeNull();
    expect(inputRef.current?.style.height).toBe("200px");
    expect(inputRef.current?.style.maxHeight).toBe("200px");
  });

  it("没拉过（无 localStorage）时空框不被撑高（默认按内容，回落旧行为）", () => {
    const { props, inputRef } = makeProps({ input: "" });
    render(<Composer {...props} />);
    expect(inputRef.current?.style.maxHeight).toBe("160px");
    expect(inputRef.current?.style.height).toBe("0px");
  });
});
