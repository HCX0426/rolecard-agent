// @vitest-environment jsdom
/**
 * 同步向导的**接线**（M7 四屏 + M8 方向档）。`lib/sync.test.ts` 已经钉过"打哪儿、送什么"，
 * 这里测那一屏一屏走得通不通，六件各有坏法的事：
 *
 *   1. 第一屏默认上传方向、逐条合并，「暂不同步」不发出任何请求；
 *   2. 下载方向：整份替换以禁用态呈现并说明原因（不是悄悄消失）；
 *   3. **取消勾选的类目不出现在请求里**（不勾的保留对端现有数据）；
 *   4. 冲突裁决：记忆给三档、卡只给两档，默认档随方向翻转；
 *   5. **界面语义要翻译成后端语义**：选"保留本机版本"发出去的必须是 `mine` ——
 *      直传的话后端收到的是看不懂的键、按默认保留接收侧处理，用户的选择被静默丢弃；
 *   6. 对账模式（登录后带着未决冲突进来）：一个选择两端各搬各的（apply 与 pull 各一发）。
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import SyncWizard from "./SyncWizard";
import { save as saveDataSource, type DataSource } from "../lib/dataSource";

const CLOUD: DataSource = {
  mode: "cloud",
  base: "https://cloud.test:8123",
  user: "u1",
  secret: "pw",
};

const PLAN = {
  counts: { only_local: 4, only_remote: 7, same: 12, conflicts: 2, skipped: 1 },
  by_kind: {
    card: { only_local: 1, same: 2, conflicts: 1 },
    memory: { only_local: 2, conflicts: 1 },
    thread: { only_local: 1, same: 9, skipped: 1 },
    reachout: { only_local: 1 },
  },
  remote_counts: { card: 3, memory: 5 },
  skipped: [{ kind: "thread", ident: "s_x", reason: "这条会话里有附图或文件，附件这一版不传" }],
  conflicts: [
    {
      kind: "memory",
      ident: "uid-1",
      mine: { at: "2026-09-25 10:00:00", preview: "用户住在上海，跑步习惯是每周三次。" },
      theirs: { at: "2026-09-26 10:00:00", preview: "用户住在上海，最近改成了每周五次。" },
    },
    {
      kind: "card",
      ident: "e莉希雅",
      mine: { at: "2026-09-20 08:00:00", preview: "爱莉希雅 · 本机改过的人设" },
      theirs: { at: "2026-09-21 08:00:00", preview: "爱莉希雅 · 云端改过的人设" },
    },
  ],
  only_local: [],
};

const APPLIED = {
  sent: 4,
  mode: "merge",
  kinds: ["card", "thread", "memory", "reachout"],
  conflicts_left: 0,
  remote: { written: { card: 1, memory: 2, reachout: 1 }, skipped: {}, errors: [] },
};

const PULLED = {
  pulled: 6,
  conflicts_left: 0,
  local: { written: { memory: 4, reachout: 2 }, skipped: {}, errors: [] },
};

function stubRoutes(bodies: Record<string, unknown>) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: unknown) => {
      const url = String(input);
      const hit = Object.keys(bodies).find((key) => url.includes(key));
      return new Response(JSON.stringify(hit ? bodies[hit] : {}), { status: 200 });
    }),
  );
}

function sentTo(path: string): Record<string, unknown> {
  const calls = vi.mocked(fetch).mock.calls as unknown as [string, RequestInit][];
  const call = calls.filter(([url]) => String(url).includes(path)).at(-1);
  expect(call, `没有发过 ${path}`).toBeTruthy();
  return JSON.parse(String(call![1].body)) as Record<string, unknown>;
}

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  vi.unstubAllGlobals();
  saveDataSource(CLOUD);
});

describe("SyncWizard", () => {
  it("第一屏：方向默认上传、档位默认逐条合并，「暂不同步」不发出任何请求", () => {
    stubRoutes({});
    render(<SyncWizard onClose={vi.fn()} />);
    expect(screen.getByRole("heading", { name: "数据同步" })).toBeTruthy();
    const directions = screen.getByRole("radiogroup", { name: "同步方向" });
    expect(directions.textContent).toContain("上传");
    expect(directions.textContent).toContain("下载");
    const modes = screen.getAllByRole("radio") as HTMLInputElement[];
    expect(modes.map((r) => r.checked)).toEqual([true, false, false]);
    fireEvent.click(screen.getByRole("button", { name: "暂不同步" }));
    expect(vi.mocked(fetch)).not.toHaveBeenCalled();
  });

  it("下载方向：整份替换以禁用态呈现并说明原因，不悄悄消失", () => {
    stubRoutes({});
    render(<SyncWizard onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "下载云端 → 本机" }));
    const modes = screen.getAllByRole("radio") as HTMLInputElement[];
    expect(modes[2].disabled).toBe(true);
    expect(screen.getByText("下载方向不可用")).toBeTruthy();
    expect(screen.getByText(/如需清除本机数据，请使用删除功能/)).toBeTruthy();
  });

  it("上传流：预检四格随方向翻转，取消勾选的类目真的不在请求里", async () => {
    stubRoutes({ "/api/sync/plan": PLAN, "/api/sync/apply": APPLIED });
    render(<SyncWizard onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "下一步：查看差异" }));
    expect(await screen.findByRole("heading", { name: "差异确认" })).toBeTruthy();
    expect(screen.getByText("本机独有 · 将上传")).toBeTruthy();
    expect(screen.getByText("云端独有 · 保留")).toBeTruthy();
    expect(screen.getByText(/这条会话里有附图或文件/)).toBeTruthy();
    fireEvent.click(screen.getByLabelText("同步会话"));
    fireEvent.click(screen.getByLabelText("同步主动消息"));
    fireEvent.click(screen.getByRole("button", { name: /开始同步/ }));
    await vi.waitFor(() => {
      expect(sentTo("/api/sync/apply").kinds).toEqual(["card", "memory"]);
    });
    expect(sentTo("/api/sync/apply").mode).toBe("merge");
  });

  it("冲突裁决：记忆给三档、卡只给两档；界面选择必须翻译成后端语义", async () => {
    stubRoutes({ "/api/sync/plan": PLAN, "/api/sync/apply": APPLIED });
    render(<SyncWizard onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "下一步：查看差异" }));
    await screen.findByRole("heading", { name: "差异确认" });
    fireEvent.click(screen.getByRole("button", { name: "处理 2 项冲突" }));
    expect(await screen.findByText("冲突 1 / 2 · 记忆")).toBeTruthy();
    // 记忆：三个版本可选；卡：只有两档（身份即 id，两版并存不适用）
    expect(screen.getAllByRole("radio")).toHaveLength(3);
    fireEvent.click(screen.getByRole("button", { name: "下一项" }));
    expect(await screen.findByText("冲突 2 / 2 · 角色卡")).toBeTruthy();
    expect(screen.getAllByRole("radio")).toHaveLength(2);
    fireEvent.click(screen.getByText("保留本机版本"));
    fireEvent.click(screen.getByRole("button", { name: "完成" }));
    await screen.findByRole("heading", { name: "差异确认" });
    expect(screen.getByText("0 项未确认")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /开始同步/ }));
    await vi.waitFor(() => {
      expect(sentTo("/api/sync/apply").resolutions).toEqual({ "card:e莉希雅": "mine" });
    });
  });

  it("下载流：打的是 /api/sync/pull，预检列名翻转", async () => {
    stubRoutes({ "/api/sync/plan": PLAN, "/api/sync/pull": PULLED });
    render(<SyncWizard onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "下载云端 → 本机" }));
    fireEvent.click(screen.getByRole("button", { name: "下一步：查看差异" }));
    await screen.findByRole("heading", { name: "差异确认" });
    expect(screen.getByText("云端独有 · 将下载")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /开始同步/ }));
    await vi.waitFor(() => expect(sentTo("/api/sync/pull").kinds).toHaveLength(4));
    expect(await screen.findByRole("heading", { name: "同步完成" })).toBeTruthy();
    // 计数被 <b> 切成多段，按整格文本查
    expect(screen.getByRole("dialog").textContent).toContain("已上传 0 项，下载 6 项");
  });

  it("对账模式：一个选择两端各搬各的（apply 与 pull 各一发）", async () => {
    stubRoutes({ "/api/sync/apply": APPLIED, "/api/sync/pull": PULLED });
    render(
      <SyncWizard
        onClose={vi.fn()}
        initialConflicts={[
          {
            kind: "memory",
            ident: "uid-1",
            mine: { at: "2026-09-25 10:00:00", preview: "本机版本" },
            theirs: { at: "2026-09-26 10:00:00", preview: "云端版本" },
          },
        ]}
      />,
    );
    expect(await screen.findByText("冲突 1 / 1 · 记忆")).toBeTruthy();
    fireEvent.click(screen.getByText("保留两个版本"));
    fireEvent.click(screen.getByRole("button", { name: "应用选择" }));
    await vi.waitFor(() => {
      expect(sentTo("/api/sync/apply").resolutions).toEqual({ "memory:uid-1": "both" });
      expect(sentTo("/api/sync/pull").resolutions).toEqual({ "memory:uid-1": "both" });
    });
    await vi.waitFor(() => {
      expect(screen.getByRole("dialog").textContent).toContain("已上传 4 项，下载 6 项");
    });
  });

  it("完成屏两句必说的话一句都不能少", async () => {
    stubRoutes({ "/api/sync/plan": PLAN, "/api/sync/apply": APPLIED });
    render(<SyncWizard onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "下一步：查看差异" }));
    await screen.findByRole("heading", { name: "差异确认" });
    fireEvent.click(screen.getByRole("button", { name: /开始同步/ }));
    expect(await screen.findByRole("heading", { name: "同步完成" })).toBeTruthy();
    expect(screen.getByText(/向量索引未随数据迁移/)).toBeTruthy();
    expect(screen.getByText(/同步为复制操作，非迁移/)).toBeTruthy();
  });

  it("对面拒了某几条：完成屏要数出来，而 200 与「一条都没落」必须分得开", async () => {
    // `R102-24` 的界面那一半：写入端把坏掉的条目折进 errors 之后照样回 200，
    // 而向导从前只累加 written/skipped —— 于是"每张卡都没写进去"看起来就是"今天没卡要搬"。
    const rejected = {
      ...APPLIED,
      remote: {
        written: { memory: 2 },
        skipped: {},
        errors: [
          { kind: "card", ident: "e莉希雅", error: "ValidationError: tool_whitelist" },
          { kind: "card", ident: "docs", error: "ValidationError: tool_whitelist" },
        ],
      },
    };
    stubRoutes({ "/api/sync/plan": PLAN, "/api/sync/apply": rejected });
    render(<SyncWizard onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "下一步：查看差异" }));
    await screen.findByRole("heading", { name: "差异确认" });
    fireEvent.click(screen.getByRole("button", { name: /开始同步/ }));
    expect(await screen.findByRole("heading", { name: "同步完成" })).toBeTruthy();
    expect(screen.getByRole("dialog").textContent).toContain("另有 2 项对面没收下");
  });

  it("对面拒了：出声，但不许跳到完成屏（没写成就不该有「同步完成」）", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: unknown) =>
        String(input).includes("/api/sync/apply")
          ? new Response(JSON.stringify({ detail: "连不上 https://cloud.test:8123。" }), {
              status: 502,
            })
          : new Response(JSON.stringify(PLAN), { status: 200 }),
      ),
    );
    render(<SyncWizard onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "下一步：查看差异" }));
    await screen.findByRole("heading", { name: "差异确认" });
    fireEvent.click(screen.getByRole("button", { name: /开始同步/ }));
    expect(await screen.findByText(/连不上/)).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "同步完成" })).toBeNull();
  });

  it("全收下了：那句「没收下」不许出现（它不是常驻文案）", async () => {
    stubRoutes({ "/api/sync/plan": PLAN, "/api/sync/apply": APPLIED });
    render(<SyncWizard onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "下一步：查看差异" }));
    await screen.findByRole("heading", { name: "差异确认" });
    fireEvent.click(screen.getByRole("button", { name: /开始同步/ }));
    expect(await screen.findByRole("heading", { name: "同步完成" })).toBeTruthy();
    expect(screen.getByRole("dialog").textContent).not.toContain("没收下");
  });
});
