// @vitest-environment jsdom
//
// ServicesPanel（设置→服务：运行时状态 + 各服务引用的后端）接线测试。
// 重点：移除引用是破坏性操作，走 useConfirm 二次确认 —— 点确认才 DELETE，点取消不调。

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

import { ServicesPanel } from "./ServicesPanel";

const VIEW = {
  services: [
    {
      key: "ocr",
      title: "OCR",
      hint: "",
      effective: null,
      effective_kind: null,
      degraded_from: null,
      readonly: false,
      candidates: [
        {
          id: "e1",
          label: "后端1",
          kind: "local",
          available: true,
          reason: "",
          enabled: true,
          builtin: false,
          key_masked: null,
          base_url: null,
          model: null,
          ref_backend: null,
          stale: false,
          order: 0,
        },
      ],
    },
  ],
};

beforeEach(() => {
  vi.clearAllMocks();
  apiMock.get.mockImplementation(async (url: string) => {
    if (url === "/api/services") return VIEW;
    if (url === "/api/settings/models") return { backends: [], fallbacks: [], default: "" };
    return {};
  });
});

describe("ServicesPanel 移除引用二次确认（useConfirm）", () => {
  it("点『移除』弹确认框，点『确认移除』才调用 DELETE", async () => {
    const { fireEvent } = await import("@testing-library/react");
    render(<ServicesPanel />);
    await screen.findByText("后端1");
    fireEvent.click(screen.getByText("移除"));
    expect(await screen.findByText("从本服务移除该后端引用？")).toBeTruthy();
    fireEvent.click(screen.getByText("确认移除"));
    await waitFor(() =>
      expect(apiMock.del).toHaveBeenCalledWith("/api/services/ocr/endpoints/e1"),
    );
  });

  it("点『取消』不调用 DELETE", async () => {
    const { fireEvent } = await import("@testing-library/react");
    render(<ServicesPanel />);
    await screen.findByText("后端1");
    fireEvent.click(screen.getByText("移除"));
    expect(await screen.findByText("从本服务移除该后端引用？")).toBeTruthy();
    fireEvent.click(screen.getByText("取消"));
    await new Promise((r) => setTimeout(r, 20));
    expect(apiMock.del).not.toHaveBeenCalled();
  });
});
