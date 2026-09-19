// @vitest-environment jsdom
//
// ReachoutPanel（角色主动开口收件箱）的接线测试。
//
// 钉的是 2026-09-19 用户报的那个症状："角色主动找我，我却回不了、也点不开历史"。
// 修法的合同就两条：条目**点得进该角色的主动会话**（带后端给的 thread_id，不由前端拼），
// 而且**标记未读失败也不能拦住跳转** —— 用户要的是看到那条消息，红点自己会校正。

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    getReachouts: vi.fn(),
    markRoleReachoutsRead: vi.fn(),
    markReachoutRead: vi.fn(),
  },
}));

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, api: apiMock };
});

import ReachoutPanel from "./ReachoutPanel";
import type { ReachoutsPage } from "../api";

function page(overrides: Partial<ReachoutsPage> = {}): ReachoutsPage {
  return {
    items: [
      {
        id: 1,
        role_id: "general_assistant",
        role_name: "通用助手",
        text: "今天腰还酸吗？",
        state: "unread",
        created_at: "2026-09-19 20:14:00",
        thread_id: "s_proactive_general_assistant",
      },
    ],
    unread: 1,
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  apiMock.getReachouts.mockResolvedValue(page());
  apiMock.markRoleReachoutsRead.mockResolvedValue(page({ items: [], unread: 0 }));
});

describe("ReachoutPanel 收件箱", () => {
  it("条目显示「打开对话并回复」，点击跳进该角色的主动会话", async () => {
    const onOpenThread = vi.fn();
    const onUnreadChange = vi.fn();
    render(
      <ReachoutPanel
        open
        onClose={() => {}}
        onUnreadChange={onUnreadChange}
        onOpenThread={onOpenThread}
      />,
    );
    fireEvent.click(await screen.findByText("今天腰还酸吗？"));
    // 跳转发生在"标完未读"那个 await 之后，所以要等一拍再断言。
    await waitFor(() => expect(onOpenThread).toHaveBeenCalledWith("s_proactive_general_assistant"));
    expect(apiMock.markRoleReachoutsRead).toHaveBeenCalledWith("general_assistant");
    expect(onUnreadChange).toHaveBeenCalledWith(0); // 红点当场归零，不等下一轮轮询
  });

  it("标已读失败照样跳转（红点下一轮轮询自然校正）", async () => {
    apiMock.markRoleReachoutsRead.mockRejectedValue(new Error("500"));
    const onOpenThread = vi.fn();
    render(
      <ReachoutPanel
        open
        onClose={() => {}}
        onUnreadChange={() => {}}
        onOpenThread={onOpenThread}
      />,
    );
    fireEvent.click(await screen.findByText("今天腰还酸吗？"));
    await waitFor(() => expect(onOpenThread).toHaveBeenCalledWith("s_proactive_general_assistant"));
  });

  it("没有主动会话的老消息：只标记已读，不给死链接", async () => {
    apiMock.getReachouts.mockResolvedValue(
      page({ items: [{ ...page().items[0], thread_id: null, text: "很久以前那条" }] }),
    );
    apiMock.markReachoutRead.mockResolvedValue(page({ items: [], unread: 0 }));
    const onOpenThread = vi.fn();
    render(
      <ReachoutPanel
        open
        onClose={() => {}}
        onUnreadChange={() => {}}
        onOpenThread={onOpenThread}
      />,
    );
    // 标着"标记已读"而不是"打开对话并回复"（点了不会把人送进一个不存在的会话）。
    const old = await screen.findByText("很久以前那条");
    expect(screen.getByText("标记已读")).toBeTruthy();
    fireEvent.click(old);
    await waitFor(() => expect(apiMock.markReachoutRead).toHaveBeenCalledWith(1));
    expect(onOpenThread).not.toHaveBeenCalled();
  });

  it("空收件箱与关着时不发请求", async () => {
    apiMock.getReachouts.mockResolvedValue({ items: [], unread: 0 });
    render(
      <ReachoutPanel
        open
        onClose={() => {}}
        onUnreadChange={() => {}}
        onOpenThread={() => {}}
      />,
    );
    expect(await screen.findByText("还没有角色主动找过你")).toBeTruthy();
    apiMock.getReachouts.mockClear();
    render(
      <ReachoutPanel
        open={false}
        onClose={() => {}}
        onUnreadChange={() => {}}
        onOpenThread={() => {}}
      />,
    );
    expect(apiMock.getReachouts).not.toHaveBeenCalled();
  });
});
