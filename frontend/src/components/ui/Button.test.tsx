// @vitest-environment jsdom
//
// ui/Button 的禁用态约定：`disabledHint` 只在真的禁用时出现，且必须出现在 DOM 里。
//
// 为什么单独钉这一条：全站「暂时不可用」的控件都指望这个出口说话（口径见
// 审计台账汇编·模型接入设计稿篇 §4）。而原生 `title` 在禁用按钮上 Chromium 根本不弹 ——
// 如果哪天有人把它改成 `title={hint}`，测试会红，而不是悄悄退回"灰着但没人知道为什么"。

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import Button from "./Button";

describe("ui/Button 的 disabledHint", () => {
  it("没禁用时不显示说明", () => {
    render(
      <Button disabledHint="不该出现">做点什么</Button>,
    );
    expect(screen.getByText("做点什么")).toBeTruthy();
    expect(screen.queryByText(/不该出现/)).toBeNull();
  });

  it("禁用时说明与按钮一起出现，且按钮真的点不动", () => {
    const onClick = vi.fn();
    render(
      <Button disabled disabledHint="先填 key 才能测连" onClick={onClick}>
        测试连接
      </Button>,
    );
    expect(screen.getByText(/先填 key 才能测连/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "测试连接" }));
    expect(onClick).not.toHaveBeenCalled();
  });

  it("ghost / danger 也有可见的禁用态（以前这两个变体灰都灰不出来）", () => {
    const { rerender } = render(
      <Button variant="ghost" disabled>
        删掉
      </Button>,
    );
    expect(screen.getByRole("button", { name: "删掉" }).className).toMatch(/disabled:/);
    rerender(
      <Button variant="danger" disabled>
        清空
      </Button>,
    );
    expect(screen.getByRole("button", { name: "清空" }).className).toMatch(/disabled:/);
  });
});

describe("ui/Button 的 icon 尺寸（方形图标按钮）", () => {
  it("icon 尺寸不带任何 padding —— padding 会把方按钮的内容盒吃光", () => {
    // 2026-10-30 实测：发送按钮曾用 `md` 尺寸（`px-4 py-2`）装一个 32px 方按钮，
    // 左右各 1rem 的 padding 吃完内容盒后，svg 作为 flex 子项被压成 **0px 宽** ——
    // 图标整个消失，发送按钮只剩一个纯色方块（用户报"图标咋蓝了"）。
    render(
      <Button variant="primary" size="icon" className="h-8 w-8" aria-label="发送">
        <svg />
      </Button>,
    );
    const cls = screen.getByRole("button", { name: "发送" }).className;
    expect(cls).toMatch(/\bp-0\b/);
    expect(cls).not.toMatch(/\bpx-4\b/);
    expect(cls).not.toMatch(/\bpy-2\b/);
  });

  it("primary 禁用时不写 inline background —— 否则禁用按钮仍是角色色、看着像能点", () => {
    // 内联 style 的优先级高于 `disabled:bg-slate-300`：从前 primary 的禁用态
    // 一直是强调色（--accent），"禁用了但看起来能用"正是要消灭的那一族。
    const { rerender } = render(
      <Button variant="primary" disabled aria-label="发送">
        发
      </Button>,
    );
    const btn = screen.getByRole("button", { name: "发送" }) as HTMLButtonElement;
    expect(btn.style.background).toBe("");
    expect(btn.className).toMatch(/disabled:bg-slate-300/);

    rerender(
      <Button variant="primary" aria-label="发送">
        发
      </Button>,
    );
    const on = screen.getByRole("button", { name: "发送" }) as HTMLButtonElement;
    expect(on.style.background).toBe("var(--accent)");
  });
});
