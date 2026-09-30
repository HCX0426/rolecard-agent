// @vitest-environment jsdom
//
// ui/Button 的禁用态约定：`disabledHint` 只在真的禁用时出现，且必须出现在 DOM 里。
//
// 为什么单独钉这一条：全站「暂时不可用」的控件都指望这个出口说话（口径见
// `docs/archive/模型接入设计稿.md` §4）。而原生 `title` 在禁用按钮上 Chromium 根本不弹 ——
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
