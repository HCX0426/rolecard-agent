// @vitest-environment jsdom
//
// RolesPage（角色卡管理）接线测试。重点：删除角色卡是破坏性操作，
// 走 useConfirm 二次确认 —— 点确认才 DELETE，点取消不调。

import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
  },
}));

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, api: apiMock };
});

import RolesPage from "./RolesPage";

const ROLE = {
  role_id: "r1",
  role_name: "测试角色",
  system_prompt: "",
  temperature: 0.7,
  model_name: "",
  tool_whitelist: [] as string[],
  knowledge_scopes: [] as string[],
  is_builtin: false,
  description: "",
  reachout_enabled: false,
  exemplars: [] as { rowId: number; user: string; assistant: string }[],
};

beforeEach(() => {
  vi.clearAllMocks();
  apiMock.get.mockImplementation(async (url: string) => {
    if (url === "/api/roles") return [ROLE];
    if (url === "/api/tools/catalog") return { kernel: [], domains: {} };
    if (url === "/api/settings/models") return { default: "", providers: [], fallbacks: [] };
    if (url === "/api/knowledge/scopes") return { scopes: [] };
    return {};
  });
});

describe("RolesPage 删除角色卡二次确认（useConfirm）", () => {
  it("点『删除』弹确认框，点『确认删除』才调用 DELETE", async () => {
    const { fireEvent } = await import("@testing-library/react");
    render(<RolesPage />);
    await screen.findByText("测试角色");
    fireEvent.click(screen.getByText("删除"));
    expect(await screen.findByText("删除这个角色卡？")).toBeTruthy();
    fireEvent.click(screen.getByText("确认删除"));
    await waitFor(() => expect(apiMock.del).toHaveBeenCalledWith("/api/roles/r1"));
  });

  it("点『取消』不调用 DELETE", async () => {
    const { fireEvent } = await import("@testing-library/react");
    render(<RolesPage />);
    await screen.findByText("测试角色");
    fireEvent.click(screen.getByText("删除"));
    expect(await screen.findByText("删除这个角色卡？")).toBeTruthy();
    fireEvent.click(screen.getByText("取消"));
    await new Promise((r) => setTimeout(r, 20));
    expect(apiMock.del).not.toHaveBeenCalled();
  });
});

describe("角色级后端选择（用户 2026-09-25：角色级 > 设置里那份优先级）", () => {
  it("下拉按全局优先级排、标出第一档，只列参与对话的模型", async () => {
    // 供应商分组的原始顺序故意打乱：这里要看的不是"有哪些后端"，而是"我在盖过谁"。
    apiMock.get.mockImplementation(async (url: string) => {
      if (url === "/api/roles") return [ROLE];
      // 编辑表单会渲染工具白名单那两块（形状是 `{kernel, domains}`，`tools` 那个键不存在）。
      if (url === "/api/tools/catalog") return { kernel: [], domains: {} };
      if (url === "/api/settings/models")
        return {
          default: "c-first",
          fallbacks: ["b-second", "a-third"],
          providers: [
            {
              models: [
                { name: "a-third", used_by: ["chat"] },
                { name: "embed-only", used_by: ["embedding"] },
                { name: "c-first", used_by: ["chat"] },
                { name: "b-second", used_by: ["chat"] },
              ],
            },
          ],
        };
      if (url === "/api/knowledge/scopes") return { scopes: [] };
      return {};
    });
    const { fireEvent } = await import("@testing-library/react");
    render(<RolesPage />);
    await screen.findByText("测试角色");
    fireEvent.click(screen.getByText("编辑"));
    const select = (await screen.findByLabelText(/这个角色的对话用哪一个/)) as HTMLSelectElement;
    expect([...select.options].map((o) => o.value)).toEqual([
      "",
      "c-first",
      "b-second",
      "a-third",
    ]);
    expect(select.options[0].textContent).toBe("跟随全局优先级");
    expect(select.options[1].textContent).toContain("全局第一档");
  });
});
