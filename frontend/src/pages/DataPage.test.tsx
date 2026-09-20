// @vitest-environment jsdom
//
// DataPage（领域数据管理）接线测试。重点：删除报告/指标/记录是破坏性操作，
// 走 useConfirm 二次确认 —— 点确认才 DELETE，点取消不调。这里覆盖健康域的"删除报告"。

import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
    listDomainRecords: vi.fn(),
    deleteDomainRecord: vi.fn(),
  },
}));

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, api: apiMock };
});

import DataPage from "./DataPage";

beforeEach(() => {
  vi.clearAllMocks();
  apiMock.get.mockImplementation(async (url: string) => {
    if (url === "/api/plugins") return [{ plugin_id: "health", display_name: "健康", enabled: true }];
    if (url.startsWith("/api/records")) {
      return {
        items: [
          { report_id: "rep1", check_time: "2026-01-01", report_type: "血检", institution: "", note: "", indices: [] },
        ],
        total: 1,
      };
    }
    return {};
  });
  apiMock.listDomainRecords.mockResolvedValue([]);
  apiMock.deleteDomainRecord.mockResolvedValue({});
});

describe("DataPage 删除报告二次确认（useConfirm）", () => {
  it("点『删除报告』弹确认框，点『确认删除』才调用 DELETE", async () => {
    const { fireEvent } = await import("@testing-library/react");
    render(<DataPage />);
    fireEvent.click(await screen.findByText("删除报告"));
    expect(await screen.findByText("删除整份报告？")).toBeTruthy();
    fireEvent.click(screen.getByText("确认删除"));
    await waitFor(() => expect(apiMock.del).toHaveBeenCalledWith("/api/records/report/rep1"));
  });

  it("点『取消』不调用 DELETE", async () => {
    const { fireEvent } = await import("@testing-library/react");
    render(<DataPage />);
    fireEvent.click(await screen.findByText("删除报告"));
    expect(await screen.findByText("删除整份报告？")).toBeTruthy();
    fireEvent.click(screen.getByText("取消"));
    await new Promise((r) => setTimeout(r, 20));
    expect(apiMock.del).not.toHaveBeenCalled();
  });
});
