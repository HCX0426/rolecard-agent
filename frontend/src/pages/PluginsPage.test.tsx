// @vitest-environment jsdom
// 插件页启停提示（`R102-21`）：`tool_epoch` 是给报障对日志用的机器标识符 —— 它在接口
// 响应与审计里都有，但**不属于界面**。从前提示是"已启用（tool_epoch=42）"，把内部
// 递增号印给用户看。变异：把 `（tool_epoch=…）` 拼回提示 ⇒ 本条红。

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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

import PluginsPage from "./PluginsPage";

beforeEach(() => {
  vi.clearAllMocks();
  apiMock.get.mockImplementation(async (url: string) => {
    if (url === "/api/plugins") {
      return [
        { plugin_id: "health", display_name: "健康档案", enabled: false, config: {} },
      ];
    }
    if (url === "/api/tools/catalog") return { domains: { health: ["search_records"] } };
    return {};
  });
  apiMock.post.mockResolvedValue({ tool_epoch: 42 });
});

describe("插件启停", () => {
  it("提示读得懂、不带 tool_epoch 这样的机器标识符（R102-21）", async () => {
    render(<PluginsPage />);
    const sw = await screen.findByRole("switch", { name: "启用 健康档案" });
    fireEvent.click(sw);
    await waitFor(() =>
      expect(apiMock.post).toHaveBeenCalledWith("/api/plugins/health/toggle", { enabled: true }),
    );
    const status = await screen.findByText(/模型下次开口即用新清单/);
    expect(status.textContent).toContain("health → 已启用");
    expect(status.textContent).not.toContain("tool_epoch");
  });
});