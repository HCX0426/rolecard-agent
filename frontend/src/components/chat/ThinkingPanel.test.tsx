// @vitest-environment jsdom
import { render } from "@testing-library/react";

import { describe, expect, it } from "vitest";

import ThinkingPanel from "./ThinkingPanel";

/**
 * 冒烟第 9 项读的是 `document.querySelector("details").hasAttribute("open")`。
 * 这一支问的是那个读法**能不能读到 React 写的展开态** —— 量的是 attribute 与
 * property 会不会分叉，与模型、时序都无关，所以放在 jsdom 里而不是真机里赌。
 *
 * 为什么要专门问：如果 `open={true}` 只落 DOM property、不落 attribute，那条判据就
 * **永远不可能绿**（恒假红），而它镜像的那条「结束后已折叠」就**永远绿**（恒真绿）——
 * 本仓原话："一个从不失败的检查比没有检查更坏"。
 */
describe("ThinkingPanel 的展开态落在 attribute 还是 property", () => {
  it("defaultOpen=true 时 hasAttribute('open') 为真（冒烟那条判据读得动）", () => {
    const { container } = render(<ThinkingPanel text="想一下" defaultOpen />);
    const el = container.querySelector("details") as HTMLDetailsElement;
    expect(el.open).toBe(true);
    expect(el.hasAttribute("open")).toBe(true);
  });

  it("defaultOpen=false 时 attribute 不在（折叠那一半同样读得动）", () => {
    const { container } = render(<ThinkingPanel text="想一下" defaultOpen={false} />);
    const el = container.querySelector("details") as HTMLDetailsElement;
    expect(el.open).toBe(false);
    expect(el.hasAttribute("open")).toBe(false);
  });

  it("文本为空时整个面板不渲染（判据拿不到元素 ≠ 面板坏了）", () => {
    const { container } = render(<ThinkingPanel text="   " defaultOpen />);
    expect(container.querySelector("details")).toBeNull();
  });
});
