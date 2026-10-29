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
    if (url === "/api/domains") return [{ id: "health", display_name: "健康" }];
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

describe("DataPage 领域列表的失败要有出口（2026-10-30 交互审计的回归钉）", () => {
  it("读不到报原因 + 给重试；『正在加载领域…』只在真的还在加载时出现", async () => {
    // 旧实现 catch(() => undefined)：失败后那行"正在加载领域…"成了永久文案，
    // 失败与"没数据"不可区分 —— 用户无从知道该等还是该修。
    apiMock.get.mockImplementation(async (url: string) => {
      // 两个域：域胶囊条只在 >1 个域时渲染，单域界面不出现"健康"两个字。
      if (url === "/api/domains")
        return [
          { id: "health", display_name: "健康" },
          { id: "study", display_name: "学习" },
        ];
      if (url.startsWith("/api/records")) return { items: [], total: 0 };
      return {};
    });
    apiMock.get.mockRejectedValueOnce(new Error("500")); // 只挂载那一趟失败
    apiMock.listDomainRecords.mockResolvedValue([]);
    const { fireEvent } = await import("@testing-library/react");
    render(<DataPage />);
    expect(await screen.findByText(/领域列表没读到：500/)).toBeTruthy();
    expect(screen.queryByText("正在加载领域…")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByText("健康")).toBeTruthy();
    expect(screen.queryByText(/领域列表没读到/)).toBeNull();
  });
});

describe("通用域分页（2026-10-30 交互审计的回归钉）", () => {
  it("多取一条探下一页：一屏 20 条、翻页带 offset、换域重挂翻页归零", async () => {
    // 后端 limit/offset 是加法（响应仍是数组）；前端拿 21 条探边界，整表渲染不再无限长。
    apiMock.get.mockImplementation(async (url: string) => {
      if (url === "/api/domains")
        return [
          { id: "health", display_name: "健康" },
          { id: "study", display_name: "学习" },
        ];
      if (url.startsWith("/api/records")) return { items: [], total: 0 };
      return {};
    });
    apiMock.listDomainRecords.mockImplementation(
      async (_domain: string, page?: { limit?: number; offset?: number }) => {
        const off = page?.offset ?? 0;
        const end = Math.min(off + (page?.limit ?? 10_000), 25);
        return Array.from({ length: Math.max(0, end - off) }, (_, i) => ({
          id: `r${off + i}`,
          domain: "study",
          label: `第 ${off + i} 条`,
          value_text: null,
          value_num: off + i,
          unit: null,
          note: null,
          created_at: "2026-01-01 00:00:00",
        }));
      },
    );
    const { fireEvent } = await import("@testing-library/react");
    render(<DataPage />);
    fireEvent.click(await screen.findByText("学习"));
    expect(await screen.findByText(/第 1–20 条/)).toBeTruthy();
    expect(screen.getByText(/还有更多/)).toBeTruthy();
    expect(screen.queryByText(/第 21–25 条/)).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() =>
      expect(apiMock.listDomainRecords).toHaveBeenLastCalledWith("study", {
        limit: 21,
        offset: 20,
      }),
    );
    expect(await screen.findByText(/第 21–25 条/)).toBeTruthy();
    // 末页：下一页置灰（探不到第 26 条）。
    expect(
      (screen.getByRole("button", { name: "下一页" }) as HTMLButtonElement).disabled,
    ).toBe(true);
  });
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
