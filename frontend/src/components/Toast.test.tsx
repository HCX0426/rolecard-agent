// @vitest-environment jsdom
//
// Toast 全局化（批次 4）的接线测试：验证 ToastProvider + useToast 这一条新契约——
// 任意组件 push 的 toast 由根上的 ToastStack 统一渲染（视口级 fixed）、点击可关、
// 且未挂载 Provider 时 useToast 返回 no-op 不崩溃（真实应用总在 App 根挂 Provider）。

import { render, screen, fireEvent } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ToastProvider, useToast, ToastStack } from "./Toast";

function Pusher({ label }: { label: string }) {
  const { push } = useToast();
  return (
    <button onClick={() => push(`hello-${label}`, "ok")}>push-{label}</button>
  );
}

describe("ToastProvider + useToast（全局栈）", () => {
  it("任意子组件 push 的 toast 由根上 ToastStack 统一渲染，点击关闭即移除", async () => {
    render(
      <ToastProvider>
        <Pusher label="a" />
      </ToastProvider>,
    );
    // 初始无 toast
    expect(screen.queryByText(/hello-/)).toBeNull();

    fireEvent.click(screen.getByText("push-a"));
    const toast = await screen.findByText("hello-a");
    expect(toast).toBeTruthy();

    // 点击 toast 即关掉
    fireEvent.click(toast);
    await screen.findByText("push-a"); // 按钮还在
    expect(screen.queryByText("hello-a")).toBeNull();
  });

  it("多条 toast 叠加且各自独立", async () => {
    render(
      <ToastProvider>
        <Pusher label="a" />
        <Pusher label="b" />
      </ToastProvider>,
    );
    fireEvent.click(screen.getByText("push-a"));
    fireEvent.click(screen.getByText("push-b"));
    expect(await screen.findByText("hello-a")).toBeTruthy();
    expect(await screen.findByText("hello-b")).toBeTruthy();
  });

  it("未挂载 Provider 时 useToast 返回 no-op：渲染不崩溃、也不弹出 toast", () => {
    // 直接渲染子组件（无 Provider）—— 模拟单测场景；应静默不崩，且不渲染任何 toast。
    render(<Pusher label="x" />);
    expect(screen.getByText("push-x")).toBeTruthy();
    expect(screen.queryByText(/hello-/)).toBeNull();
  });

  it("空文本被忽略（不渲染）", () => {
    function EmptyPusher() {
      const { push } = useToast();
      push("", "info");
      return <div>rendered</div>;
    }
    render(
      <ToastProvider>
        <EmptyPusher />
      </ToastProvider>,
    );
    expect(screen.getByText("rendered")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /点击关闭/ })).toBeNull();
  });

  it("ToastStack 视口级 fixed 定位（不要求调用方 relative）", () => {
    const { container } = render(<ToastStack toasts={[{ id: 1, text: "x", tone: "warn" }]} />);
    const stack = container.querySelector("div");
    expect(stack?.className).toContain("fixed");
    expect(stack?.className).toContain("z-[60]");
  });
});
