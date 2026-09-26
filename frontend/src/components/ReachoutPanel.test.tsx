// @vitest-environment jsdom
//
// ReachoutPanel（角色主动开口收件箱）的接线测试。
//
// 钉的是两代症状：
//   1. 2026-09-19「角色主动找我，我却回不了、也点不开历史」—— 条目必须点得进该角色的
//      主动会话（thread_id 由后端给，不由前端拼），且**标记已读失败也不拦跳转**；
//   2. 收件箱"一天开口 5 次就是 5 行，历史糊成流水账" —— 同角色在一个时间窗口里的开口
//      折成一行（窗口宽 `merge_days` 由列表一起带回，不在前端写死），但**有未读的不折**。

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    getReachouts: vi.fn(),
    markAllReachoutsRead: vi.fn(),
    markReachoutRead: vi.fn(),
  },
}));

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, api: apiMock };
});

import ReachoutPanel from "./ReachoutPanel";
import type { ReachoutsPage, ReachoutRow } from "../api";

/** 相对"现在"的时间戳：折叠判的是**距今多久**，写死日期会让用例随日历漂移。 */
function ago(hours: number): string {
  return new Date(Date.now() - hours * 3_600_000).toISOString().slice(0, 19).replace("T", " ");
}

function row(over: Partial<ReachoutRow> = {}): ReachoutRow {
  return {
    id: 1,
    role_id: "general_assistant",
    role_name: "通用助手",
    text: "今天腰还酸吗？",
    state: "unread",
    created_at: ago(2),
    thread_id: "s_proactive_general_assistant",
    ...over,
  };
}

function page(overrides: Partial<ReachoutsPage> = {}): ReachoutsPage {
  return { items: [row()], unread: 1, merge_days: 1, ...overrides };
}

function renderPanel(onOpenThread = vi.fn(), onUnreadChange = vi.fn()) {
  const view = render(
    <ReachoutPanel
      open
      onClose={() => {}}
      onUnreadChange={onUnreadChange}
      onOpenThread={onOpenThread}
    />,
  );
  // 同一个用例里要换配置重画两次（比窗口宽度）：第一次不卸载的话两个抽屉叠在 DOM 里，
  // "找不到 4 条"这种断言就变成在比空气。
  return { onOpenThread, onUnreadChange, unmount: view.unmount };
}

beforeEach(() => {
  vi.clearAllMocks();
  apiMock.getReachouts.mockResolvedValue(page());
  apiMock.markAllReachoutsRead.mockResolvedValue(page({ items: [], unread: 0 }));
});

describe("ReachoutPanel 收件箱", () => {
  it("条目显示「打开对话并回复」，点击跳进该角色的主动会话", async () => {
    const { onOpenThread, onUnreadChange } = renderPanel();
    fireEvent.click(await screen.findByText("今天腰还酸吗？"));
    // 跳转发生在"标完未读"那个 await 之后，所以要等一拍再断言。
    await waitFor(() => expect(onOpenThread).toHaveBeenCalledWith("s_proactive_general_assistant"));
    expect(apiMock.markAllReachoutsRead).toHaveBeenCalled();
    expect(onUnreadChange).toHaveBeenCalledWith(0); // 红点当场归零，不等下一轮轮询
  });

  it("标已读失败照样跳转（红点下一轮轮询自然校正）", async () => {
    apiMock.markAllReachoutsRead.mockRejectedValue(new Error("500"));
    const { onOpenThread } = renderPanel();
    fireEvent.click(await screen.findByText("今天腰还酸吗？"));
    await waitFor(() => expect(onOpenThread).toHaveBeenCalledWith("s_proactive_general_assistant"));
  });

  it("没有主动会话的老消息：只标记已读，不给死链接", async () => {
    apiMock.getReachouts.mockResolvedValue(
      page({ items: [row({ thread_id: null, text: "很久以前那条" })] }),
    );
    apiMock.markReachoutRead.mockResolvedValue(page({ items: [], unread: 0 }));
    const { onOpenThread } = renderPanel();
    // 标着"标记已读"而不是"打开对话并回复"（点了不会把人送进一个不存在的会话）。
    const old = await screen.findByText("很久以前那条");
    expect(screen.getByText("标记已读")).toBeTruthy();
    fireEvent.click(old);
    await waitFor(() => expect(apiMock.markReachoutRead).toHaveBeenCalledWith(1));
    expect(onOpenThread).not.toHaveBeenCalled();
  });

  it("空收件箱不报错", async () => {
    apiMock.getReachouts.mockResolvedValue({ items: [], unread: 0 });
    renderPanel();
    expect(await screen.findByText("还没有角色主动找过你")).toBeTruthy();
  });

  it("抽屉关着时不发请求（红点靠轮询，不靠打开界面才去问）", () => {
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

describe("收件箱折叠（角色 × 时间桶）", () => {
  const fourToday = () =>
    [1, 2, 3, 4].map((n) => row({ id: n, text: `第 ${n} 次开口`, state: "read", created_at: ago(n) }));

  it("同角色一天内四条已读 → 折成一行，带条数与展开箭头", async () => {
    apiMock.getReachouts.mockResolvedValue(page({ items: fourToday(), unread: 0 }));
    renderPanel();
    const header = await screen.findByRole("button", { name: /通用助手/ });
    expect(header.textContent).toContain("4 条");
    // 折起来时只露"最新一条"的摘要，不是一口气四行
    expect(screen.queryByText("第 4 次开口")).toBeNull();
    expect(screen.getByText("第 1 次开口")).toBeTruthy();
  });

  it("展开看每一条，再点收起", async () => {
    apiMock.getReachouts.mockResolvedValue(page({ items: fourToday(), unread: 0 }));
    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: /4 条/ }));
    expect(await screen.findByText("第 4 次开口")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /4 条/ }));
    expect(screen.queryByText("第 4 次开口")).toBeNull();
  });

  it("有未读就不折 —— 折叠只为收旧消息，不藏新消息", async () => {
    apiMock.getReachouts.mockResolvedValue(
      page({ items: [...fourToday(), row({ id: 9, text: "刚开口的那条" })], unread: 1 }),
    );
    renderPanel();
    expect(await screen.findByText("刚开口的那条")).toBeTruthy();
    expect(screen.getByText("第 4 次开口")).toBeTruthy(); // 已读那摞也摊开（同一角色）
    expect(screen.getByText(/5 条/)).toBeTruthy(); // 一摞五行，不是五行各占一格
  });

  it("窗口宽度按后端给的 merge_days：7 天窗口里四条只算一摞", async () => {
    // 刻意用 1/2/3/4 天前：正好 7 天的那条会落进下一个桶（边界本来就是"每 7 天一摞"的语义）
    const spread = (days: number[]) =>
      days.map((d) => row({ id: d, text: `${d} 天前那条`, state: "read", created_at: ago(d * 24) }));
    apiMock.getReachouts.mockResolvedValue(
      page({ merge_days: 7, items: spread([1, 2, 3, 4]), unread: 0 }),
    );
    const first = renderPanel();
    expect(await screen.findByRole("button", { name: /4 条/ })).toBeTruthy();
    first.unmount();

    apiMock.getReachouts.mockResolvedValue(
      page({ merge_days: 1, items: spread([1, 2, 3, 4]), unread: 0 }),
    );
    renderPanel();
    // 同样的四条换成"按天"就是一天一摞，每摞一条 → 不折（一条不值得折）
    expect(await screen.findAllByText(/天前那条/)).toHaveLength(4);
    expect(screen.queryByText(/4 条/)).toBeNull();
  });

  it("不同角色不合并（分组键含 role_id）", async () => {
    apiMock.getReachouts.mockResolvedValue(
      page({
        unread: 0,
        items: [
          row({ id: 1, state: "read", text: "通用助手的那条" }),
          row({
            id: 2,
            state: "read",
            role_id: "archivist",
            role_name: "医学档案管理员",
            text: "管理员的那条",
            thread_id: "s_proactive_archivist",
          }),
        ],
      }),
    );
    renderPanel();
    // 各一条 → 不成摞（没有"N 条"的组头），两行直接摊开
    expect(await screen.findByText("通用助手的那条")).toBeTruthy();
    expect(screen.getByText("管理员的那条")).toBeTruthy();
    expect(screen.queryByText(/2 条/)).toBeNull();
  });
});

// `S-8`：闸门那句"为什么静默"以前只在后端 `if …: continue` 里被丢掉，界面上只会表现成
// "她最近怎么不找我了"。现在它跟着收件箱那份负载一起回来（不另开一次轮询）。
describe("抽屉那一格「她此刻为什么静默」", () => {
  const quiet = [
    {
      role_id: "general_assistant",
      role_name: "通用助手",
      why: "距上次说话不足 66 分钟，她连着 1 条没被回已退避",
      next_ok_at: new Date(Date.now() + 40 * 60_000).toISOString(),
      streak: 1,
      unread: 1,
    },
    {
      role_id: "archivist",
      role_name: "医学档案管理员",
      why: "未读堆积已达上限",
      next_ok_at: null,
      streak: 0,
      unread: 2,
    },
  ];

  it("后端带了 quiet 就画一行，措辞是闸门原话 + 下一次大约几点", async () => {
    apiMock.getReachouts.mockResolvedValue(page({ quiet }));
    renderPanel();
    const box = await screen.findByTestId("quiet-status");
    expect(box.textContent).toContain("通用助手 静默中");
    expect(box.textContent).toContain("距上次说话不足 66 分钟");
    expect(box.textContent).toContain("下一次大约");
    // 给不出时刻的那种阻塞（要他回话）不编时刻
    expect(box.textContent).toContain("未读堆积已达上限");
    expect((box.textContent || "").split("下一次大约").length - 1).toBe(1);
  });

  it("没有 quiet（没人开主动资格 / 老后端）时不画空壳", async () => {
    renderPanel(); // 默认 stub 不带 quiet
    expect(await screen.findByText("今天腰还酸吗？")).toBeTruthy();
    expect(screen.queryByTestId("quiet-status")).toBeNull();
  });

  it("筛了「只看」某个角色，这一格也跟着只剩他 —— 否则筛一个人看三个人的状态", async () => {
    // 后端按 `role_id` 过滤 `quiet`（判据见 `reachout._page`），前端也要跟着只画筛中那个 ——
    // 两边各筛一半，症状就是"筛了一个人却看见三个人的状态"。
    apiMock.getReachouts.mockImplementation((role_id?: string) =>
      Promise.resolve(
        role_id
          ? page({ quiet: quiet.filter((q) => q.role_id === role_id) })
          : page({
              quiet,
              items: [
                row(),
                row({
                  id: 2,
                  role_id: "archivist",
                  role_name: "医学档案管理员",
                  text: "管理员的那条",
                }),
              ],
            }),
      ),
    );
    renderPanel();
    await screen.findByTestId("quiet-status"); // 等首轮异步加载画出来，否则 DOM 里还没有筛选下拉
    const select = document.querySelector("select");
    expect(select).toBeTruthy();
    fireEvent.change(select as HTMLSelectElement, { target: { value: "archivist" } });
    const box = await screen.findByTestId("quiet-status");
    expect(box.textContent).toContain("未读堆积已达上限");
    expect(box.textContent).not.toContain("通用助手");
  });
});
