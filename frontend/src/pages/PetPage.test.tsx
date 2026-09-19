// @vitest-environment jsdom
//
// PetPage（桌宠：一张色片 + 一句气泡）的接线测试。
//
// 钉的是"驻留件不能骗人"这一族：未读合并成 +N、气泡会自己淡出、后端连不上时必须说出来
// 而不是安静地停在上一次的内容上（后者是这类小窗最容易被忽略的失效形态 —— 它看起来还在，
// 其实已经哑了）。

import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { apiMock } = vi.hoisted(() => ({
  apiMock: { getReachouts: vi.fn(), markRoleReachoutsRead: vi.fn() },
}));

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, api: apiMock };
});

import PetPage from "./PetPage";
import type { ReachoutsPage } from "../api";

function row(id: number, text: string, state = "unread") {
  return {
    id,
    role_id: "wan",
    role_name: "苏晚晴",
    text,
    state,
    created_at: "2026-09-19 20:14:00",
    thread_id: "s_proactive_wan",
  };
}

function page(items: ReturnType<typeof row>[], unread = items.length): ReachoutsPage {
  return { items, unread } as ReachoutsPage;
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.useFakeTimers();
  apiMock.getReachouts.mockResolvedValue(page([row(3, "外头降温了，穿上外套。"), row(2, "旧的一条"), row(1, "更早的一条")]));
  apiMock.markRoleReachoutsRead.mockResolvedValue(page([], 0));
});

afterEach(() => {
  vi.useRealTimers();
});

/** 把挂载 + 首次轮询的 Promise 都跑完（fake timers 下必须手动 flush）。 */
async function mount() {
  const view = render(<PetPage />);
  await act(async () => {
    await Promise.resolve();
  });
  return view;
}

describe("PetPage 桌宠", () => {
  it("只显示最新一条，其余折成 +N；色片用角色名首字", async () => {
    await mount();
    expect(screen.getByText("外头降温了，穿上外套。")).toBeTruthy();
    expect(screen.queryByText("旧的一条")).toBeNull();
    expect(screen.getByText("+2")).toBeTruthy();
    expect(screen.getByTitle("苏晚晴")).toBeTruthy();
    expect(screen.getByText("苏")).toBeTruthy();
  });

  it("点气泡 = 标该角色已读，气泡随即收起", async () => {
    await mount();
    fireEvent.click(screen.getByText("外头降温了，穿上外套。"));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(apiMock.markRoleReachoutsRead).toHaveBeenCalledWith("wan");
    expect(screen.queryByText("外头降温了，穿上外套。")).toBeNull();
  });

  it("气泡 30 秒后自己淡出（驻留件不长期戳在桌面上）", async () => {
    await mount();
    expect(screen.getByText("外头降温了，穿上外套。")).toBeTruthy();
    await act(async () => {
      vi.advanceTimersByTime(30_500);
    });
    expect(screen.queryByText("外头降温了，穿上外套。")).toBeNull();
  });

  it("后端连不上时明说，而不是继续显示上一次的消息", async () => {
    apiMock.getReachouts.mockRejectedValue(new Error("connection refused"));
    await mount();
    expect(screen.getByText("连不上本地服务")).toBeTruthy();
    expect(screen.queryByText("外头降温了，穿上外套。")).toBeNull();
  });

  it("一条消息都没有时只剩色片，不弹空气泡", async () => {
    apiMock.getReachouts.mockResolvedValue(page([]));
    await mount();
    expect(screen.getByText("助")).toBeTruthy(); // 没消息时回落到默认名"助手"
    expect(screen.queryByText("+2")).toBeNull();
  });
});
