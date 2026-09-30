// @vitest-environment jsdom
//
// LocalServiceCard（设置→模型 的本地推理服务卡）的接线测试。
//
// 这张卡的价值全在「状态说得准 + 按钮按得对」：常驻（自己永远不让出显存）与「用完自动退出」
// 必须区分显示，否则用户会傻等一次不会来的释放；云端默认时不能给「常驻」按钮；失败句子来自
// 后端 detail，不能自己编一句「成功」。

import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    getLocalService: vi.fn(),
    pinLocalModel: vi.fn(),
    unloadLocalModel: vi.fn(),
  },
}));

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, api: apiMock };
});

import { LocalServiceCard } from "./LocalServiceCard";
import type { LocalServiceStatus } from "../api";

const PINNED = { name: "qwen3-vl:8b", size_bytes: 5795940924, expires_at: null, pinned: true };

function status(overrides: Partial<LocalServiceStatus> = {}): LocalServiceStatus {
  return {
    base_url: "http://localhost:11434",
    running: true,
    resident: [PINNED],
    resident_bytes: 5795940924,
    pinned: true,
    is_local: true,
    model: "qwen3-vl:8b",
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  delete window.rolecardShell;
  apiMock.getLocalService.mockResolvedValue(status());
  apiMock.unloadLocalModel.mockResolvedValue({ unloaded: ["qwen3-vl:8b"], skipped: [] });
  apiMock.pinLocalModel.mockResolvedValue({ model: "qwen3-vl:8b", resident: [] });
});

/** 装上"壳"：起停服务进程是壳的能力，浏览器里没有这两个按钮。 */
function withShell(owner: { managed: boolean; pid: number | null }) {
  const shell = {
    backendUrl: () => Promise.resolve("http://127.0.0.1:8000"),
    backendReachable: () => Promise.resolve(true),
    openSession: vi.fn(),
    notify: vi.fn(),
    setPetExpanded: vi.fn().mockResolvedValue(true),
    movePetBy: vi.fn(),
    onRequestOpenThread: vi.fn(),
    ollamaOwner: vi.fn().mockResolvedValue({ ...owner, binary: "C:\\ollama.exe" }),
    startOllama: vi.fn().mockResolvedValue({ ok: true }),
    stopOllama: vi.fn().mockResolvedValue({ ok: true }),
    pickDirectory: vi.fn().mockResolvedValue(null),
  };
  window.rolecardShell = shell;
  return shell;
}

describe("LocalServiceCard", () => {
  it("显示地址与驻留模型，并标出「常驻」", async () => {
    render(<LocalServiceCard />);
    const line = await screen.findByText(/合计 5\.4 GB/);
    expect(line.textContent).toBe("qwen3-vl:8b 5.4 GB（常驻） · 合计 5.4 GB");
    expect(screen.getByText("http://localhost:11434")).toBeTruthy();
  });

  it("常驻按钮走 pin，成功后重读状态", async () => {
    render(<LocalServiceCard />);
    fireEvent.click(await screen.findByText("预热 / 常驻默认模型"));
    expect(apiMock.pinLocalModel).toHaveBeenCalled();
    expect(await screen.findByText(/已把 qwen3-vl:8b 常驻显存/)).toBeTruthy();
    expect(apiMock.getLocalService).toHaveBeenCalledTimes(2);
  });

  it("释放显存调用 unload，并把没卸掉的点名", async () => {
    apiMock.unloadLocalModel.mockResolvedValue({ unloaded: ["a:1"], skipped: ["b:2"] });
    render(<LocalServiceCard />);
    fireEvent.click(await screen.findByText("释放显存（卸载模型）"));
    expect(await screen.findByText(/已释放 a:1（未释放：b:2）/)).toBeTruthy();
  });

  it("卸载失败时把后端句子原样显示，不伪装成功", async () => {
    apiMock.unloadLocalModel.mockRejectedValue(new Error("释放失败：连不上本地推理服务"));
    render(<LocalServiceCard />);
    fireEvent.click(await screen.findByText("释放显存（卸载模型）"));
    expect(await screen.findByText("释放失败：连不上本地推理服务")).toBeTruthy();
    expect(screen.queryByText(/已释放/)).toBeNull();
  });

  it("云端默认模型：状态与卸载照样可见，但不给「常驻」按钮", async () => {
    apiMock.getLocalService.mockResolvedValue(status({ is_local: false, model: "deepseek-chat" }));
    render(<LocalServiceCard />);
    expect(await screen.findByText("释放显存（卸载模型）")).toBeTruthy();
    expect(screen.queryByText("预热 / 常驻默认模型")).toBeNull();
  });

  it("「没在跑」与「在跑但没驻留」是两句话", async () => {
    apiMock.getLocalService.mockResolvedValue(
      status({ running: false, resident: [], resident_bytes: 0, pinned: false }),
    );
    const { unmount } = render(<LocalServiceCard />);
    expect(await screen.findByText(/Ollama 没起来/)).toBeTruthy();
    // 按钮不再消失，而是禁用 + 说清怎么恢复（藏起来等于让人不知道有这能力）。
    const unload = screen.getByRole("button", { name: /释放显存/ });
    expect((unload as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(/显存里现在没有模型/)).toBeTruthy();
    const pin = screen.getByRole("button", { name: /预热 \/ 常驻/ });
    expect((pin as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(/先把它起起来才能预热/)).toBeTruthy();
    unmount();

    apiMock.getLocalService.mockResolvedValue(status({ resident: [], resident_bytes: 0 }));
    render(<LocalServiceCard />);
    expect(await screen.findByText(/冷加载/)).toBeTruthy();
    expect(screen.queryByText(/没起来/)).toBeNull();
    // 服务在跑：预热可点；仍没驻留 → 释放照旧禁用并说明
    expect((screen.getByRole("button", { name: /预热 \/ 常驻/ }) as HTMLButtonElement).disabled).toBe(
      false,
    );
    expect((screen.getByRole("button", { name: /释放显存/ }) as HTMLButtonElement).disabled).toBe(
      true,
    );
  });

  it("默认模型不在显存里时点名（显存被别的模型占着的真实场景）", async () => {
    apiMock.getLocalService.mockResolvedValue(
      status({
        resident: [{ name: "other:7b", size_bytes: 1024, expires_at: null, pinned: false }],
        resident_bytes: 1024,
        pinned: false,
      }),
    );
    render(<LocalServiceCard />);
    const line = await screen.findByText(/不在显存/);
    expect(line.textContent).toContain("other:7b 0.0 GB");
    expect(line.textContent).toContain("默认 qwen3-vl:8b 不在显存");
  });

  it("壳里 + 没在跑：给「启动 Ollama」，点了就问壳", async () => {
    apiMock.getLocalService.mockResolvedValue(
      status({ running: false, resident: [], resident_bytes: 0, pinned: false }),
    );
    const shell = withShell({ managed: false, pid: null });
    render(<LocalServiceCard />);
    fireEvent.click(await screen.findByText("启动 Ollama"));
    expect(shell.startOllama).toHaveBeenCalled();
    expect(await screen.findByText(/已拉起本机 Ollama/)).toBeTruthy();
  });

  it("运行中但不是本应用起的：不给「停止 Ollama」，也不冒充有 pid", async () => {
    withShell({ managed: false, pid: null });
    render(<LocalServiceCard />);
    expect(await screen.findByText(/合计 5\.4 GB/)).toBeTruthy();
    expect(screen.queryByText(/停止 Ollama/)).toBeNull();
    expect(screen.queryByText(/由本应用启动/)).toBeNull();
  });

  it("本应用起的：状态里点名归属，并给「停止 Ollama」", async () => {
    const shell = withShell({ managed: true, pid: 4321 });
    render(<LocalServiceCard />);
    const line = await screen.findByText(/由本应用启动（pid 4321）/);
    expect(line.textContent).toContain("合计 5.4 GB");
    fireEvent.click(screen.getByText("停止 Ollama（本应用起的）"));
    expect(shell.stopOllama).toHaveBeenCalled();
    expect(await screen.findByText(/已停掉本应用起的 Ollama/)).toBeTruthy();
  });

  it("浏览器（B/S）里不给起停按钮，只说清为什么没有", async () => {
    render(<LocalServiceCard />);
    expect(await screen.findByText(/合计 5\.4 GB/)).toBeTruthy();
    expect(screen.queryByText(/停止 Ollama/)).toBeNull();
    expect(screen.queryByText("启动 Ollama")).toBeNull();
    expect(screen.getByText(/起停 Ollama 进程属于桌面壳/)).toBeTruthy();
  });

  it("云端默认模型时不提桌面壳（那台机器上根本没有本地进程可起）", async () => {
    apiMock.getLocalService.mockResolvedValue(status({ is_local: false, model: "deepseek-chat" }));
    render(<LocalServiceCard />);
    expect(await screen.findByText("释放显存（卸载模型）")).toBeTruthy();
    expect(screen.queryByText(/起停 Ollama 进程属于桌面壳/)).toBeNull();
  });

  it("壳说不能起时，原样转达它的理由", async () => {
    apiMock.getLocalService.mockResolvedValue(
      status({ running: false, resident: [], resident_bytes: 0, pinned: false }),
    );
    const shell = withShell({ managed: false, pid: null });
    shell.startOllama.mockResolvedValue({ ok: false, reason: "没找到 ollama 可执行文件。" });
    render(<LocalServiceCard />);
    fireEvent.click(await screen.findByText("启动 Ollama"));
    expect(await screen.findByText("没找到 ollama 可执行文件。")).toBeTruthy();
    expect(screen.queryByText(/已拉起本机 Ollama/)).toBeNull();
  });
});
