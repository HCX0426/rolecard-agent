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
    if (url === "/api/tools/catalog") return { tools: [] };
    if (url === "/api/settings/models") return { default: "", backends: [], fallbacks: [] };
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
