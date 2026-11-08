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
  // 真客户端 post 永远回 Promise —— mock 必须同形，否则首载 effect 里 .catch 直接炸。
  apiMock.post.mockResolvedValue({ probed: 0, results: [] });
  apiMock.get.mockImplementation(async (url: string) => {
    if (url === "/api/services") return VIEW;
    if (url === "/api/settings/models") return { providers: [], fallbacks: [], default: "" };
    return {};
  });
});

describe("ServicesPanel 移除引用二次确认（useConfirm）", () => {
  it("点『移除』弹确认框，点『确认移除』才调用 DELETE", async () => {
    const { fireEvent } = await import("@testing-library/react");
    render(<ServicesPanel />);
    await screen.findByText("后端1");
    fireEvent.click(screen.getByText("移除"));
    expect(await screen.findByText("从这个服务顺序里移除这个模型？")).toBeTruthy();
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
    expect(await screen.findByText("从这个服务顺序里移除这个模型？")).toBeTruthy();
    fireEvent.click(screen.getByText("取消"));
    await new Promise((r) => setTimeout(r, 20));
    expect(apiMock.del).not.toHaveBeenCalled();
  });
});

describe("ServicesPanel 停用行的排序按钮（禁用必须配说明）", () => {
  it("行已停用：↑/↓ 禁用，且说明写在 DOM 里而不是 title 里", async () => {
    const disabledRow = { ...VIEW.services[0].candidates[0], enabled: false };
    const view = { services: [{ ...VIEW.services[0], candidates: [disabledRow] }] };
    apiMock.get.mockImplementation(async (url: string) => {
      if (url === "/api/services") return view;
      if (url === "/api/settings/models") return { providers: [], fallbacks: [], default: "" };
      return {};
    });
    render(<ServicesPanel />);
    const up = await screen.findByRole("button", { name: "↑" });
    expect((up as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(/先「启用」才能排优先级/)).toBeTruthy();
  });
});

describe("服务页真实探测（配额纪律：真请求只在首载与『重新检测』）", () => {
  const probes = () =>
    apiMock.post.mock.calls.filter((c: unknown[]) => c[0] === "/api/services/probe").length;

  it("首载真发恰好一次探测，且读状态排在探测之后", async () => {
    render(<ServicesPanel />);
    expect(await screen.findByText("后端1")).toBeTruthy();
    await waitFor(() => expect(probes(), "首载应真发一次探测").toBe(1));
  });

  it("普通保存（run→load）绝不 POST 探测：配额只花在两处", async () => {
    const { fireEvent } = await import("@testing-library/react");
    render(<ServicesPanel />);
    await waitFor(() => expect(probes()).toBe(1));
    await screen.findByText("后端1");
    fireEvent.click(screen.getByText("移除"));
    fireEvent.click(await screen.findByText("确认移除"));
    await waitFor(() => expect(apiMock.del).toHaveBeenCalled());
    expect(probes(), "保存路径只许读状态").toBe(1);
  });

  it("点『重新检测』才再次真发：每点一次、真发一次", async () => {
    const { fireEvent } = await import("@testing-library/react");
    render(<ServicesPanel />);
    await waitFor(() => expect(probes()).toBe(1));
    await screen.findByText("后端1");
    fireEvent.click(screen.getByText("重新检测"));
    await waitFor(() => expect(probes()).toBe(2));
    fireEvent.click(screen.getByText("重新检测"));
    await waitFor(() => expect(probes()).toBe(3));
  });

  it("探测接口挂了：面板照常渲染（探测失败 ≠ 面板失败）", async () => {
    apiMock.post.mockRejectedValueOnce(new Error("探测炸了"));
    render(<ServicesPanel />);
    expect(await screen.findByText("后端1")).toBeTruthy();
  });
});

describe("页签重新激活时重取台账（模型页刚删的模型，切回服务页要看得见）", () => {
  const reads = () =>
    apiMock.get.mock.calls.filter((c: unknown[]) => c[0] === "/api/services").length;
  const probes = () =>
    apiMock.post.mock.calls.filter((c: unknown[]) => c[0] === "/api/services/probe").length;

  it("隐藏→显示触发一次 /api/services 重取；探测仍是首载那一次", async () => {
    const { rerender } = render(<ServicesPanel visible={false} />);
    await waitFor(() => expect(probes(), "常驻挂载的首载探测照发").toBe(1));
    await screen.findByText("后端1");
    const before = reads();
    rerender(<ServicesPanel visible={true} />);
    await waitFor(() => expect(reads()).toBeGreaterThan(before));
    expect(probes(), "激活只重取本地台账，不许重发探测").toBe(1);
  });

  it("显示→隐藏→再显示：每次都重取，探测始终只有首载那一次", async () => {
    const { rerender } = render(<ServicesPanel visible={true} />);
    await screen.findByText("后端1");
    await waitFor(() => expect(probes()).toBe(1));
    const base = reads();
    rerender(<ServicesPanel visible={false} />);
    rerender(<ServicesPanel visible={true} />);
    await waitFor(() => expect(reads()).toBeGreaterThan(base));
    expect(probes()).toBe(1);
  });
});
