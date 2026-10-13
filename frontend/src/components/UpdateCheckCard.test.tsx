// @vitest-environment jsdom
//
// UpdateCheckCard（设置→关于 的检查更新卡）的接线测试（设计稿《更新机制与OCR随包》§3.1）。
//
// 这张卡唯一需要钉的行为是**不许把"问不到"画成"一切正常"**：
//   · 有更新 → 显示新版本号 + 指向 url 的下载/发布页链接；
//   · 没更新 → "已是最新"；
//   · disabled / error → 如实说关闭/没问到，且**绝不**出现"发现新版本"；
//   · 端点挂了 → 整卡不渲染（可选入口不占报错位）。

import { act, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { apiMock } = vi.hoisted(() => ({ apiMock: { getUpdateCheck: vi.fn() } }));

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, api: apiMock };
});

import { UpdateCheckCard } from "./UpdateCheckCard";
import type { UpdateCheck } from "../api";

function info(overrides: Partial<UpdateCheck> = {}): UpdateCheck {
  return {
    current: "0.4.1",
    latest: null,
    has_update: false,
    source: "github",
    url: null,
    file_name: null,
    size_bytes: null,
    error: null,
    ...overrides,
  };
}

async function mount() {
  render(<UpdateCheckCard />);
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("UpdateCheckCard", () => {
  it("有更新：显示新版本号，链接指向后端的 url", async () => {
    apiMock.getUpdateCheck.mockResolvedValue(
      info({
        latest: "0.5.0",
        has_update: true,
        url: "https://github.com/o/r/releases/tag/v0.5.0",
      }),
    );
    await mount();
    expect(screen.getByText(/发现新版本 0\.5\.0/)).toBeTruthy();
    const link = screen.getByText("前往发布页");
    expect(link.getAttribute("href")).toBe("https://github.com/o/r/releases/tag/v0.5.0");
    // 外站链接必须带 noreferrer（新标签打开是刻意设计，但别把来源泄给外站）
    expect(link.getAttribute("rel")).toBe("noreferrer");
  });

  it("本地托管源有更新：链接走站内下载端点，不带 target=_blank", async () => {
    apiMock.getUpdateCheck.mockResolvedValue(
      info({
        source: "release_dir",
        latest: "0.9.0",
        has_update: true,
        url: "/api/shell-release/download",
        file_name: "rolecard-agent-0.9.0-x64.exe",
      }),
    );
    await mount();
    const link = screen.getByText("下载新版安装包");
    expect(link.getAttribute("href")).toBe("/api/shell-release/download");
    expect(link.getAttribute("target")).toBeNull();
  });

  it("没更新：如实『已是最新』，不渲染任何下载链接", async () => {
    apiMock.getUpdateCheck.mockResolvedValue(info({ latest: "0.4.1" }));
    await mount();
    expect(screen.getByText(/已是最新（0\.4\.1）/)).toBeTruthy();
    expect(screen.queryByText("前往发布页")).toBeNull();
    expect(screen.queryByText("下载新版安装包")).toBeNull();
  });

  it("disabled：说更新检查已关闭，不伪装成『已是最新』", async () => {
    apiMock.getUpdateCheck.mockResolvedValue(info({ source: "disabled" }));
    await mount();
    expect(screen.getByText(/更新检查已关闭/)).toBeTruthy();
    expect(screen.queryByText(/已是最新/)).toBeNull();
  });

  it("问不到（error）：把原因带出来，不伪装成『已是最新』", async () => {
    apiMock.getUpdateCheck.mockResolvedValue(
      info({ source: "none", error: "问 GitHub 失败：TimeoutError: x" }),
    );
    await mount();
    expect(screen.getByText(/没问到最新版本/)).toBeTruthy();
    expect(screen.queryByText(/已是最新/)).toBeNull();
    expect(screen.queryByText(/发现新版本/)).toBeNull();
  });

  it("端点挂了：整卡不渲染", async () => {
    apiMock.getUpdateCheck.mockRejectedValue(new Error("boom"));
    await mount();
    expect(screen.queryByText("更新")).toBeNull();
  });

  it("重新检查按钮真的再打一次端点", async () => {
    apiMock.getUpdateCheck.mockResolvedValue(info());
    await mount();
    expect(apiMock.getUpdateCheck).toHaveBeenCalledTimes(1);
    await act(async () => {
      screen.getByText("重新检查").click();
      await Promise.resolve();
    });
    expect(apiMock.getUpdateCheck).toHaveBeenCalledTimes(2);
  });
});
