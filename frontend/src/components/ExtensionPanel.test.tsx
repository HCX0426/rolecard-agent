// @vitest-environment jsdom
//
// ExtensionPanel（设置→扩展：MCP 接入 + 模型后端连通性自检）接线测试。
// 重点（对齐"界面合理"与后端安全语义）：
//   1. GET 出 server 列表；点"测试连接"→交通灯由测试中转绿、展开显示后端返回的工具；
//   2. "粘贴 JSON" 把 mcpServers 片段逐条 POST 到 /api/mcp/servers（含 id/url/headers）；
//   3. 空列表时显示引导空态。全离线（api mock）。

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    put: vi.fn(),
    del: vi.fn(),
  },
}));

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, api: apiMock };
});

import { ExtensionPanel } from "./ExtensionPanel";

const SRV = {
  id: "maps",
  display_name: "地图服务",
  transport: "http",
  url: "https://example.com/mcp",
  headers: { Authorization: "***" },
  enabled: true,
};

beforeEach(() => {
  vi.clearAllMocks();
  apiMock.get.mockResolvedValue({ servers: [SRV], effective_count: 1 });
  apiMock.post.mockResolvedValue({});
});

describe("ExtensionPanel", () => {
  it("渲染 server 列表：显示名 / url / 掩码头", async () => {
    render(<ExtensionPanel />);
    expect(await screen.findByText("地图服务")).toBeTruthy();
    expect(screen.getByText(/example\.com\/mcp/)).toBeTruthy();
    // 密钥值不外泄：显示 Authorization ****，不出现真实值
    expect(screen.getByText(/Authorization \*\*\*\*/)).toBeTruthy();
  });

  it("测试连接：转绿并展开显示工具", async () => {
    apiMock.post.mockImplementation((url: string) =>
      url.endsWith("/test")
        ? Promise.resolve({ id: "maps", ok: true, tool_count: 2, tools: ["geocode", "route"] })
        : Promise.resolve({}),
    );
    render(<ExtensionPanel />);
    await screen.findByText("地图服务");
    fireEvent.click(screen.getByText("测试连接"));
    await waitFor(() => expect(screen.getByText("geocode")).toBeTruthy());
    expect(screen.getByText("route")).toBeTruthy();
  });

  it("粘贴 JSON 片段逐条接入（POST 每条带 url）", async () => {
    render(<ExtensionPanel />);
    await screen.findByText("地图服务");
    fireEvent.click(screen.getByText("粘贴 JSON"));
    fireEvent.change(screen.getByPlaceholderText(/mcpServers/), {
      target: {
        value: JSON.stringify({ mcpServers: { weather: { url: "https://w.example/mcp", headers: { "x-key": "***" } } } }),
      },
    });
    fireEvent.click(screen.getByText("从 JSON 接入"));
    await waitFor(() =>
      expect(
        apiMock.post.mock.calls.some(
          ([u, b]) => u === "/api/mcp/servers" && (b as { id: string }).id === "weather",
        ),
      ).toBeTruthy(),
    );
  });

  it("空列表显示引导空态", async () => {
    apiMock.get.mockResolvedValue({ servers: [], effective_count: 0 });
    render(<ExtensionPanel />);
    expect(await screen.findByText(/还没有接入任何 MCP server/)).toBeTruthy();
  });
});

describe("ExtensionPanel 删除二次确认（useConfirm）", () => {
  it("点『删除』弹确认框，点『确认删除』才真正调用 DELETE", async () => {
    render(<ExtensionPanel />);
    await screen.findByText("地图服务");
    fireEvent.click(screen.getByText("删除"));
    expect(await screen.findByText("删除这个 MCP server？")).toBeTruthy();
    fireEvent.click(screen.getByText("确认删除"));
    await waitFor(() =>
      expect(apiMock.del).toHaveBeenCalledWith("/api/mcp/servers/maps"),
    );
  });

  it("点『取消』不调用 DELETE", async () => {
    render(<ExtensionPanel />);
    await screen.findByText("地图服务");
    fireEvent.click(screen.getByText("删除"));
    expect(await screen.findByText("删除这个 MCP server？")).toBeTruthy();
    fireEvent.click(screen.getByText("取消"));
    await new Promise((r) => setTimeout(r, 20));
    expect(apiMock.del).not.toHaveBeenCalled();
  });
});
