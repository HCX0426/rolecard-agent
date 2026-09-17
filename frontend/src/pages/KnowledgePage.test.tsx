// @vitest-environment jsdom
//
// KnowledgePage 的接线测试，重点是**上传目录回收**这条破坏性流程：
//   检查（只读） → 看到清单与字节数 → 二次确认 → 执行 → 结果提示。
//
// 为什么值得测：这是前端唯一"点了就永久删用户文件"的按钮。它的安全性取决于 UI 把
// "盘点"和"执行"分成两步、且确认前展示将删什么 —— 这类承诺只有渲染出来才算数。

import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
    extractRecord: vi.fn(),
  },
}));

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, api: apiMock };
});

import KnowledgePage from "./KnowledgePage";

function stubMountCalls() {
  apiMock.get.mockImplementation(async (url: string) => {
    if (url === "/api/knowledge") {
      return [
        { scope: "health_reports", chunks: 12, embedder: "hash", sources: ["须知.md"] },
      ];
    }
    if (url === "/api/rag/metrics") {
      return {
        samples: 3,
        embedder: "hash",
        rerank_enabled: false,
        p50: { embed_ms: 1, vector_ms: 2, rerank_ms: 0, total_ms: 3 },
        p95: { embed_ms: 1, vector_ms: 2, rerank_ms: 0, total_ms: 3 },
        p99: { embed_ms: 1, vector_ms: 2, rerank_ms: 0, total_ms: 3 },
      };
    }
    return {};
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  stubMountCalls();
});

describe("KnowledgePage 知识库视图", () => {
  it("渲染作用域与检索延迟细分", async () => {
    render(<KnowledgePage />);
    expect(await screen.findByText("health_reports")).toBeTruthy();
    expect(await screen.findByText("检索延迟细分（ms）")).toBeTruthy();
    expect(screen.getByText("12 段")).toBeTruthy();
  });
});

describe("KnowledgePage 上传目录回收", () => {
  it("只读盘点：展示文件、大小与合计，但此时没有可点的『确认回收』之外的危险按钮", async () => {
    const { fireEvent } = await import("@testing-library/react");
    apiMock.get.mockImplementation(async (url: string) => {
      if (url === "/api/knowledge") return [];
      if (url === "/api/rag/metrics") return null;
      if (url === "/api/uploads/orphans") {
        return {
          orphans: [
            { name: "deadbeef_旧报告.txt", size: 2048, companion: false },
            { name: "deadbeef_旧报告.txt.parsed.txt", size: 100, companion: true },
          ],
          total_bytes: 2148,
          scanned: 5,
          referenced: 3,
        };
      }
      return {};
    });

    render(<KnowledgePage />);
    fireEvent.click(await screen.findByText("检查可回收文件"));

    expect(await screen.findByText(/扫描 5 个文件，其中 3 个被台账引用/)).toBeTruthy();
    expect(screen.getByText("deadbeef_旧报告.txt")).toBeTruthy();
    // "解析副本 · 100 B" 在同一个 span 里（两个文本节点，getByText 会拼接匹配）
    expect(screen.getByText(/解析副本 · 100 B/)).toBeTruthy();
    expect(screen.getByText("2.0 KB")).toBeTruthy(); // 主文件 2048 B
    // 合计 2148 B 出现在汇总段（数字被 <b> 隔开，用正则匹配所在段落）
    expect(screen.getByText(/2\.1 KB/)).toBeTruthy();
    // 二次确认出现在执行之前，而不是直接删
    expect(screen.getByRole("button", { name: "确认回收" })).toBeTruthy();
    expect(apiMock.post).not.toHaveBeenCalled();
  });

  it("没有孤儿时不出现确认按钮", async () => {
    const { fireEvent } = await import("@testing-library/react");
    apiMock.get.mockImplementation(async (url: string) => {
      if (url === "/api/knowledge") return [];
      if (url === "/api/rag/metrics") return null;
      if (url === "/api/uploads/orphans") {
        return { orphans: [], total_bytes: 0, scanned: 4, referenced: 4 };
      }
      return {};
    });

    render(<KnowledgePage />);
    fireEvent.click(await screen.findByText("检查可回收文件"));

    // 0 个孤儿：只显示汇总，不出现"确认回收"（"没有可回收的文件"是执行后的结果提示）
    expect(await screen.findByText(/扫描 4 个文件，其中 4 个被台账引用/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "确认回收" })).toBeNull();
    expect(apiMock.post).not.toHaveBeenCalled();
  });

  it("确认后调用执行端点，并回显删除数与释放量", async () => {
    const { fireEvent } = await import("@testing-library/react");
    apiMock.get.mockImplementation(async (url: string) => {
      if (url === "/api/knowledge") return [];
      if (url === "/api/rag/metrics") return null;
      if (url === "/api/uploads/orphans") {
        return {
          orphans: [{ name: "x.txt", size: 4096, companion: false }],
          total_bytes: 4096,
          scanned: 2,
          referenced: 1,
        };
      }
      return {};
    });
    apiMock.post.mockResolvedValue({
      deleted: 1,
      freed_bytes: 4096,
      scanned: 2,
      referenced: 1,
    });

    render(<KnowledgePage />);
    fireEvent.click(await screen.findByText("检查可回收文件"));
    fireEvent.click(await screen.findByRole("button", { name: "确认回收" }));

    await waitFor(() => expect(apiMock.post).toHaveBeenCalledWith("/api/uploads/cleanup", {}));
    expect(await screen.findByText(/已回收 1 个文件，释放 4.0 KB/)).toBeTruthy();
    // 执行成功后清单收起，避免误以为还能再删一次
    expect(screen.queryByRole("button", { name: "确认回收" })).toBeNull();
  });

  it("盘点失败给出可读提示而不是白屏", async () => {
    const { fireEvent } = await import("@testing-library/react");
    apiMock.get.mockImplementation(async (url: string) => {
      if (url === "/api/knowledge") return [];
      if (url === "/api/rag/metrics") return null;
      if (url === "/api/uploads/orphans") throw new Error("后端炸了");
      return {};
    });

    render(<KnowledgePage />);
    fireEvent.click(await screen.findByText("检查可回收文件"));
    expect(await screen.findByText(/盘点失败：后端炸了/)).toBeTruthy();
  });
});
