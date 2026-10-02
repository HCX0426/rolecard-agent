// @vitest-environment jsdom
//
// ShellReleaseCard（设置→通用 的桌面壳安装包卡）的接线测试。
//
// 这张卡唯一需要钉的行为是**不许说谎**：没有产物时整卡不渲染（而不是留一个点了没反应的
// 按钮），有产物时下载链接必须指向后端那个「只给最新产物」的端点，且体积/构建日如实显示。
// 拉取失败同样静默 —— 它是可选入口，不该在通用页上占一句报错。

import { act, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { apiMock } = vi.hoisted(() => ({ apiMock: { getShellRelease: vi.fn() } }));

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, api: apiMock };
});

import { ShellReleaseCard } from "./ShellReleaseCard";
import type { ShellRelease } from "../api";

const MB = 1024 * 1024;

function release(overrides: Partial<ShellRelease> = {}): ShellRelease {
  return {
    available: true,
    configured: true,
    file_name: "rolecard-agent-0.1.0-x64.exe",
    size_bytes: 181 * MB,
    built_at: "2026-09-20T02:00:00+00:00",
    ...overrides,
  };
}

/** 挂载并把那次 fetch 的 microtask 跑完（组件没有 loading 态，等一次刷新就到位）。 */
async function mount() {
  render(<ShellReleaseCard />);
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("ShellReleaseCard", () => {
  it("没有产物时整卡不渲染", async () => {
    apiMock.getShellRelease.mockResolvedValue({ available: false, configured: true });
    await mount();
    expect(screen.queryByText("桌面壳（Windows 桌宠）")).toBeNull();
    expect(screen.queryByText("下载桌面壳")).toBeNull();
  });

  it("有产物时显示文件名、体积与构建日", async () => {
    apiMock.getShellRelease.mockResolvedValue(release());
    await mount();
    expect(screen.getByText(/rolecard-agent-0\.1\.0-x64\.exe/)).toBeTruthy();
    expect(screen.getByText(/181.0 MB/)).toBeTruthy();
    expect(screen.getByText(/构建于/)).toBeTruthy();
  });

  it("下载链接指向后端那个「只给最新产物」的端点，并带上文件名", async () => {
    apiMock.getShellRelease.mockResolvedValue(release());
    await mount();
    const link = screen.getByText("下载桌面壳");
    expect(link.getAttribute("href")).toBe("/api/shell-release/download");
    expect(link.getAttribute("download")).toBe("rolecard-agent-0.1.0-x64.exe");
  });

  it("超过 1 GB 的产物按 GB 显示（不写成 1500 MB）", async () => {
    apiMock.getShellRelease.mockResolvedValue(release({ size_bytes: 2.5 * 1024 * MB }));
    await mount();
    expect(screen.getByText(/2\.5 GB/)).toBeTruthy();
  });

  it("后端没给构建日时不出现「构建于」这半句", async () => {
    apiMock.getShellRelease.mockResolvedValue(release({ built_at: undefined }));
    await mount();
    expect(screen.getByText("下载桌面壳")).toBeTruthy();
    expect(screen.queryByText(/构建于/)).toBeNull();
  });

  it("拉取失败时静默不显示", async () => {
    apiMock.getShellRelease.mockRejectedValue(new Error("boom"));
    await mount();
    expect(screen.queryByText("下载桌面壳")).toBeNull();
  });
});
