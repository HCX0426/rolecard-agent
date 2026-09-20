// @vitest-environment jsdom
//
// PetPage（桌宠：一张色片 + 一句气泡）的接线测试。
//
// 钉的是"驻留件不能骗人"这一族：未读合并成 +N、气泡会自己淡出、后端连不上时必须说出来
// 而不是安静地停在上一次的内容上（后者是这类小窗最容易被忽略的失效形态 —— 它看起来还在，
// 其实已经哑了）。
//
// 另一半钉的是"通知不能变成轰炸"：开机/重连时积压的那批一律不算新到，只有快照之间真的
// 变大 id 的那一条才拍系统通知；以及没有主动会话可跳的老消息不能去要点跳转（死链）。

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
import type { ReachoutsPage, ReachoutRow } from "../api";
import type { ShellBridge } from "../lib/shell";

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

/** 装上"壳"：桌宠的系统通知与"点气泡拉起控制台"两件事都以它存在为前提。
 *  不装的时候就是 B/S —— 同一个组件、少两样能力，其余行为必须一模一样。 */
function withShell(): ShellBridge & { notify: ReturnType<typeof vi.fn>; openSession: ReturnType<typeof vi.fn> } {
  const shell = {
    backendUrl: () => Promise.resolve("http://127.0.0.1:8000"),
    backendReachable: () => Promise.resolve(true),
    notify: vi.fn(),
    openSession: vi.fn(),
    onRequestOpenThread: vi.fn(),
    ollamaOwner: vi.fn().mockResolvedValue({ managed: false, pid: null, binary: null }),
    startOllama: vi.fn(),
    stopOllama: vi.fn(),
    pickDirectory: vi.fn().mockResolvedValue(null),
  };
  window.rolecardShell = shell;
  return shell;
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.useFakeTimers();
  delete window.rolecardShell;
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

/** 走一次轮询：让 mock 里换掉的新快照被读到。 */
async function poll() {
  await act(async () => {
    vi.advanceTimersByTime(10_000);
    for (let i = 0; i < 4; i += 1) await Promise.resolve();
  });
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

  it("挂载时那批已存在的消息不拍系统通知（首次快照只立基线）", async () => {
    const shell = withShell();
    await mount();
    await poll();
    expect(shell.notify).not.toHaveBeenCalled();
  });

  it("轮询到新到的一条才拍通知，且只拍一次", async () => {
    const shell = withShell();
    await mount();
    apiMock.getReachouts.mockResolvedValue(page([row(4, "给你带了桂花糕。"), row(3, "外头降温了，穿上外套。")]));
    await poll();
    expect(shell.notify).toHaveBeenCalledTimes(1);
    expect(shell.notify).toHaveBeenCalledWith("苏晚晴", "给你带了桂花糕。", "s_proactive_wan");
    // 换一个新数组装同样的内容再轮询一次：证明"不再弹"靠的是基线，不是 React 认出了同一个引用。
    apiMock.getReachouts.mockResolvedValue(page([row(4, "给你带了桂花糕。"), row(3, "外头降温了，穿上外套。")]));
    await poll();
    expect(shell.notify).toHaveBeenCalledTimes(1);
  });

  it("新到但已是已读的那条不拍通知", async () => {
    const shell = withShell();
    await mount();
    apiMock.getReachouts.mockResolvedValue(page([row(4, "自己看过的话", "read")]));
    await poll();
    expect(shell.notify).not.toHaveBeenCalled();
  });

  it("点气泡把那条主动会话交给壳打开（壳负责把控制台拉到前台）", async () => {
    const shell = withShell();
    await mount();
    fireEvent.click(screen.getByText("外头降温了，穿上外套。"));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(shell.openSession).toHaveBeenCalledWith("s_proactive_wan");
  });

  it("没有主动会话可跳的老消息：只标已读，不求跳转", async () => {
    const shell = withShell();
    const legacy = { ...row(9, "很早以前的一句话"), thread_id: null } as ReachoutRow;
    apiMock.getReachouts.mockResolvedValue({ items: [legacy], unread: 1 } as ReachoutsPage);
    await mount();
    fireEvent.click(screen.getByText("很早以前的一句话"));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(apiMock.markRoleReachoutsRead).toHaveBeenCalledWith("wan");
    expect(shell.openSession).not.toHaveBeenCalled();
  });
});
