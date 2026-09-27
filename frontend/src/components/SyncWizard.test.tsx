// @vitest-environment jsdom
/**
 * 上行同步四屏的**接线**（M7）。`lib/sync.test.ts` 已经钉过"打哪儿、送什么"，
 * 这里只测那一屏一屏走得通不通，五件各有坏法的事：
 *
 *   1. 第一屏默认停在「逐条合并」，且「这次先不带」是一个正经出口（不请求任何东西）；
 *   2. 第二屏的数来自对面的计划，四类各有一个勾；
 *   3. **取消勾选的那一类不出现在上行请求里**（用户 09-27：不勾选的就用云端）；
 *   4. 冲突屏：记忆给三档、卡只给两档（"都留"对卡讲不通），跳过的默认是按对面的留着；
 *   5. 完成屏那三句话一句都不能少 —— 少了"索引是重建的"，以后检索质量对不上没人知道为什么。
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

function sentBody(): Record<string, unknown> {
  const call = vi.mocked(fetch).mock.calls.at(-1) as [string, RequestInit];
  return JSON.parse(String(call[1].body)) as Record<string, unknown>;
}

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  vi.unstubAllGlobals();
  saveDataSource(CLOUD);
});

describe("SyncWizard", () => {
  it("第一屏：三档齐、默认停在逐条合并，「这次先不带」不发出任何请求", () => {
    stubRoutes({});
    render(<SyncWizard onClose={vi.fn()} />);
    expect(screen.getByRole("heading", { name: "要把本机这份带过去吗？" })).toBeTruthy();
    const radios = screen.getAllByRole("radio") as HTMLInputElement[];
    expect(radios.map((r) => r.checked)).toEqual([true, false, false]);
    fireEvent.click(screen.getByRole("button", { name: "这次先不带" }));
    expect(vi.mocked(fetch)).not.toHaveBeenCalled();
  });

  it("第二屏：四格读数来自计划，四类各有一个勾，跳过的原因摆出来", async () => {
    stubRoutes({ "/api/sync/plan": PLAN });
    render(<SyncWizard onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "下一步：看差异" }));
    expect(await screen.findByRole("heading", { name: "差异看完了" })).toBeTruthy();
    expect(screen.getByText("本机独有 · 会过去")).toBeTruthy();
    expect(screen.getByText("冲突 · 要你挑")).toBeTruthy();
    expect(screen.getAllByRole("checkbox")).toHaveLength(4);
    expect(screen.getByText(/这条会话里有附图或文件/)).toBeTruthy();
  });

  it("取消勾选的那一类，真的不在上行请求里", async () => {
    stubRoutes({ "/api/sync/plan": PLAN, "/api/sync/apply": APPLIED });
    render(<SyncWizard onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "下一步：看差异" }));
    await screen.findByRole("heading", { name: "差异看完了" });
    fireEvent.click(screen.getByLabelText("同步会话"));
    fireEvent.click(screen.getByLabelText("同步主动消息"));
    fireEvent.click(screen.getByRole("button", { name: /开始上行/ }));
    await vi.waitFor(() => expect(sentBody().kinds).toEqual(["card", "memory"]));
    expect(sentBody().mode).toBe("merge");
  });

  it("冲突屏：记忆给三档，卡只给两档；挑完两条才回得去", async () => {
    stubRoutes({ "/api/sync/plan": PLAN, "/api/sync/apply": APPLIED });
    render(<SyncWizard onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "下一步：看差异" }));
    await screen.findByRole("heading", { name: "差异看完了" });
    fireEvent.click(screen.getByRole("button", { name: "先处理 2 条冲突" }));
    expect(await screen.findByText("冲突 1 / 2 · 一条记忆")).toBeTruthy();
    expect(screen.getAllByRole("radio")).toHaveLength(3);
    fireEvent.click(screen.getByRole("button", { name: "下一条" }));
    expect(await screen.findByText("冲突 2 / 2 · 一条角色卡")).toBeTruthy();
    // 这一类"两份都留"讲不通（身份就是那一个 id），所以第三档压根不出现
    expect(screen.getAllByRole("radio")).toHaveLength(2);
    fireEvent.click(screen.getByText("保留本机这份"));
    fireEvent.click(screen.getByRole("button", { name: "就这么定" }));
    await screen.findByRole("heading", { name: "差异看完了" });
    // 文本被 <b> 切成两段，所以按那一段本身查（整句正则匹配不到任何一个元素）
    expect(screen.getByText("0 条没挑")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /开始上行/ }));
    await vi.waitFor(() => expect(sentBody().resolutions).toEqual({
      "memory:uid-1": "theirs",
      "card:e莉希雅": "mine",
    }));
  });

  it("完成屏：三句话一句都不能少", async () => {
    stubRoutes({ "/api/sync/plan": PLAN, "/api/sync/apply": APPLIED });
    render(<SyncWizard onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "下一步：看差异" }));
    await screen.findByRole("heading", { name: "差异看完了" });
    fireEvent.click(screen.getByRole("button", { name: /开始上行/ }));
    expect(await screen.findByRole("heading", { name: "上行完成" })).toBeTruthy();
    expect(screen.getByText(/已带过去 4 项/)).toBeTruthy();
    expect(screen.getByText(/向量索引没有搬/)).toBeTruthy();
    expect(screen.getByText(/一个字都没改/)).toBeTruthy();
  });

  it("对面拒了：出声，但不许跳到完成屏（没写成就不该有「上行完成」四个字）", async () => {
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
    fireEvent.click(screen.getByRole("button", { name: "下一步：看差异" }));
    await screen.findByRole("heading", { name: "差异看完了" });
    fireEvent.click(screen.getByRole("button", { name: /开始上行/ }));
    expect(await screen.findByText(/连不上/)).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "上行完成" })).toBeNull();
  });
});
