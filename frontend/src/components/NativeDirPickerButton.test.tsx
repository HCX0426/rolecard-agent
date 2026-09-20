// @vitest-environment jsdom
//
// NativeDirPickerButton（D②-6 系统目录选择器）的接线测试。
//
// 钉的是"能力不存在时不许装作存在"：浏览器里没有壳，这个按钮就**整个不出现**，
// 而不是出现一个点了没反应的框。另外两条是选择结果的语义：取消 = null 不该被当成
// 一次失败，挑到的路径只交给调用方（保存仍然是另一件事）。

import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { NativeDirPickerButton } from "./NativeDirPickerButton";
import type { ShellBridge } from "../lib/shell";

function withShell(pick: ShellBridge["pickDirectory"]) {
  const shell = {
    backendUrl: () => Promise.resolve("http://127.0.0.1:8000"),
    backendReachable: () => Promise.resolve(true),
    openSession: vi.fn(),
    notify: vi.fn(),
    onRequestOpenThread: vi.fn(),
    ollamaOwner: vi.fn(),
    startOllama: vi.fn(),
    stopOllama: vi.fn(),
    pickDirectory: vi.fn().mockImplementation(pick),
  } as unknown as ShellBridge & { pickDirectory: ReturnType<typeof vi.fn> };
  window.rolecardShell = shell;
  return shell;
}

beforeEach(() => {
  vi.clearAllMocks();
});

afterEach(() => {
  delete window.rolecardShell;
});

describe("NativeDirPickerButton", () => {
  it("浏览器（B/S）里没有壳：整个按钮不出现", async () => {
    const onPicked = vi.fn();
    render(<NativeDirPickerButton onPicked={onPicked} />);
    await Promise.resolve();
    expect(screen.queryByText("系统选目录…")).toBeNull();
  });

  it("挑到的路径交给调用方", async () => {
    const shell = withShell(async () => "D:\\my-tasks");
    const onPicked = vi.fn();
    render(<NativeDirPickerButton onPicked={onPicked} />);
    fireEvent.click(screen.getByText("系统选目录…"));
    await vi.waitFor(() => expect(onPicked).toHaveBeenCalledWith("D:\\my-tasks"));
    expect(shell.pickDirectory).toHaveBeenCalledTimes(1);
  });

  it("取消对话框不是失败：什么都不改，也不报错", async () => {
    withShell(async () => null);
    const onPicked = vi.fn();
    render(<NativeDirPickerButton onPicked={onPicked} />);
    fireEvent.click(screen.getByText("系统选目录…"));
    await Promise.resolve();
    await Promise.resolve();
    expect(onPicked).not.toHaveBeenCalled();
    expect(screen.queryByText(/没打开/)).toBeNull();
  });

  it("桥报错时说一句人话", async () => {
    withShell(async () => {
      throw new Error("dialog 不可用");
    });
    render(<NativeDirPickerButton onPicked={vi.fn()} />);
    fireEvent.click(screen.getByText("系统选目录…"));
    expect(await screen.findByText(/系统对话框没打开：dialog 不可用/)).toBeTruthy();
  });
});
