// @vitest-environment jsdom
//
// TimelineDrawer（事件簿抽屉，设计稿 §6）的接线测试。
//
// 这条轴是只读的，所以这里钉的全是"别骗人 + 别把用户送进死路"那一族：
//   * 措辞只能到"它哪天记下 / 更正了什么"（记忆条目的 created_at 是被记下的时间，不是
//     事情发生的时间）；
//   * 「失效不删」要真的看得见 —— 旧事实划掉摆在那儿，而不是凭空不见了；
//   * 没有会话可去的行不给链接（后端已经把 thread_id 回落成 null，前端不许自己拼一个）；
//   * 分页是**追加**，游标由后端给；换角色 / 换筛选要重发，旧响应不许污染新页。

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { apiMock } = vi.hoisted(() => ({ apiMock: { get: vi.fn() } }));

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, api: apiMock };
});

import TimelineDrawer from "./TimelineDrawer";
import type { TimelineEvent, TimelinePage } from "../api";

const ROLE = { role_id: "elysia", role_name: "爱莉希雅" };

/** 本机日子的时间戳：写死日期会随日历漂移，而分组判的就是"距今几天"。 */
function day(offsetDays: number, hm = "09:00"): string {
  const d = new Date();
  d.setDate(d.getDate() - offsetDays);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${hm}:00`;
}

/** `thread_id` 默认 null：只有主动开口与会话锚点才有可跳的线程，由用例自己给。 */
function ev(over: Partial<TimelineEvent> = {}): TimelineEvent {
  return {
    kind: "reachout",
    at: day(0, "20:14"),
    text: "刚路过琴房，想起你说周三要练琴。",
    verb: null,
    from_text: null,
    thread_id: null,
    ref_id: 1,
    ...over,
  };
}

function page(items: TimelineEvent[], over: Partial<TimelinePage> = {}): TimelinePage {
  return { role_id: ROLE.role_id, items, next_cursor: null, truncated: false, ...over };
}

function renderDrawer(onOpenThread = vi.fn()) {
  const view = render(
    <TimelineDrawer open role={ROLE} onClose={() => {}} onOpenThread={onOpenThread} />,
  );
  return { onOpenThread, unmount: view.unmount };
}

beforeEach(() => {
  vi.clearAllMocks();
  apiMock.get.mockResolvedValue(page([]));
});

describe("TimelineDrawer 事件簿", () => {
  it("打开就读这一页：按天分组，四类各说各的话", async () => {
    apiMock.get.mockResolvedValue(
      page([
        ev({ thread_id: "s_proactive_elysia" }),
        ev({ kind: "memory", text: "用户每周三晚上练琴", ref_id: 2, at: day(0, "18:02") }),
        ev({
          kind: "memory_correct",
          verb: "correct",
          text: "用户下周搬到杭州居住",
          from_text: "用户住在上海",
          ref_id: 3,
          at: day(0, "18:02"),
        }),
        ev({
          kind: "thread",
          verb: "start",
          text: "腰疼怎么缓解",
          thread_id: "s_1",
          ref_id: 4,
          at: day(1, "21:31"),
        }),
      ]),
    );
    renderDrawer();
    await waitFor(() => expect(screen.getByText("今天")).toBeTruthy());
    expect(apiMock.get).toHaveBeenCalledWith("/api/roles/elysia/timeline?limit=30");
    expect(screen.getByText("昨天")).toBeTruthy();
    expect(screen.getByText(/刚路过琴房/)).toBeTruthy();
    expect(screen.getByText("记下：用户每周三晚上练琴")).toBeTruthy();
    expect(screen.getByText(/更正为：用户下周搬到杭州居住/)).toBeTruthy();
    expect(screen.getByText(/开始聊：腰疼怎么缓解/)).toBeTruthy();
    for (const badge of ["主动", "记忆", "更正", "对话"]) {
      expect(screen.getAllByText(badge).length).toBeGreaterThan(0);
    }
  });

  it("被作废的旧事实是划掉摆着，不是不见了（「失效不删」第一次被用户看见）", async () => {
    apiMock.get.mockResolvedValue(
      page([
        ev({
          kind: "memory_correct",
          verb: "correct",
          text: "用户下周搬到杭州居住",
          from_text: "用户住在上海",
        }),
      ]),
    );
    renderDrawer();
    const old = await waitFor(() => screen.getByText("用户住在上海"));
    expect(old.className).toContain("line-through");
  });

  it("整理时只作废、没写替代：说「作废了一条事实」，不编一条「改成了」", async () => {
    apiMock.get.mockResolvedValue(
      page([ev({ kind: "memory_correct", verb: "correct", text: "", from_text: "旧的一条" })]),
    );
    renderDrawer();
    await waitFor(() => expect(screen.getByText("作废了一条事实")).toBeTruthy());
  });

  it("「只看」换了就按 kinds 重发；记忆那一档把「更正」一起带上", async () => {
    renderDrawer();
    fireEvent.change(screen.getByTitle("只看某一类"), { target: { value: "memory" } });
    await waitFor(() =>
      expect(apiMock.get).toHaveBeenCalledWith(
        "/api/roles/elysia/timeline?limit=30&kinds=memory,memory_correct",
      ),
    );
  });

  it("加载更多：带后端给的游标，且是追加不是替换", async () => {
    apiMock.get
      .mockResolvedValueOnce(page([ev({ text: "第一条" })], { next_cursor: "c|reachout|1" }))
      .mockResolvedValueOnce(page([ev({ text: "更早的一条", ref_id: 0 })]));
    renderDrawer();
    await waitFor(() => expect(screen.getByText("第一条")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: /加载更多/ }));
    await waitFor(() =>
      expect(apiMock.get).toHaveBeenLastCalledWith(
        "/api/roles/elysia/timeline?limit=30&before=c%7Creachout%7C1",
      ),
    );
    expect(screen.getByText("第一条")).toBeTruthy();
    expect(screen.getByText("更早的一条")).toBeTruthy();
  });

  it("有会话才给跳转，没有就不给死链（thread_id 由后端回落成 null）", async () => {
    const { onOpenThread } = renderDrawer();
    apiMock.get.mockResolvedValue(
      page([
        ev({ text: "能点开的", thread_id: "s_proactive_elysia" }),
        ev({ text: "没有会话的", ref_id: 2 }),
      ]),
    );
    fireEvent.change(screen.getByTitle("只看某一类"), { target: { value: "reachout" } });
    await waitFor(() => expect(screen.getByText(/能点开的/)).toBeTruthy());
    fireEvent.click(screen.getByText(/能点开的/));
    expect(onOpenThread).toHaveBeenCalledWith("s_proactive_elysia");
    // 没有 thread_id 的那条是纯文本，不是按钮（后面那个 → 只属于可跳转的行）。
    expect(screen.getByText("没有会话的").tagName).not.toBe("BUTTON");
  });

  it("空态说清「怎么才会有东西」，而不是留一块空白", async () => {
    renderDrawer();
    await waitFor(() =>
      expect(screen.getByText(/还没有关于它的事 —— 聊几句/)).toBeTruthy(),
    );
  });

  it("读不到就报原因，而不是安静地停在一块空白上", async () => {
    apiMock.get.mockRejectedValueOnce(new Error("500"));
    renderDrawer();
    await waitFor(() => expect(screen.getByText(/事件簿没读到：500/)).toBeTruthy());
  });

  it("扫到上限就如实标「只扫到最近的一段」，不承诺总量", async () => {
    apiMock.get.mockResolvedValue(page([ev()], { truncated: true }));
    renderDrawer();
    await waitFor(() => expect(screen.getByText(/只扫到最近的一段/)).toBeTruthy());
  });

  it("换角色 = 按新角色重发（旧角色的行不许留在屏上）", async () => {
    apiMock.get.mockResolvedValue(page([ev({ text: "爱莉希雅的事" })]));
    const view = render(
      <TimelineDrawer open role={ROLE} onClose={() => {}} onOpenThread={vi.fn()} />,
    );
    await waitFor(() => expect(screen.getByText("爱莉希雅的事")).toBeTruthy());
    apiMock.get.mockResolvedValue(page([ev({ text: "助手的事" })]));
    view.rerender(
      <TimelineDrawer
        open
        role={{ role_id: "general_assistant", role_name: "通用助手" }}
        onClose={() => {}}
        onOpenThread={vi.fn()}
      />,
    );
    await waitFor(() =>
      expect(apiMock.get).toHaveBeenLastCalledWith(
        "/api/roles/general_assistant/timeline?limit=30",
      ),
    );
    await waitFor(() => expect(screen.getByText("助手的事")).toBeTruthy());
    expect(screen.queryByText("爱莉希雅的事")).toBeNull();
  });
});
