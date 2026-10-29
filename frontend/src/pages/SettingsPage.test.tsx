// @vitest-environment jsdom
//
// SettingsPage（设置页）接线测试。2026-10-30 IA 迁出后本页只装环境与凭据
// （模型 / 服务 / 关于 / 运行环境 / 审计）：记忆族用例随「记忆」页签迁往
// MemoryPage.test.tsx，主动开口可写卡的用例同去 —— 这里保留运行环境那侧的
// "只读并指向写点"断言（单写点纪律的两半，各自住在写点所在页与只读展示页）。
// （设置页子页签常驻挂载 + hidden —— jsdom 查询不看 CSS，各面板同屏可查。）

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
  },
}));

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, api: apiMock };
});

import SettingsPage from "./SettingsPage";

beforeEach(() => {
  vi.clearAllMocks();
  apiMock.get.mockImplementation(async (url: string) => {
    if (url === "/api/settings/models") return { default: "", providers: [], fallbacks: [] };
    if (url === "/api/plugins") return [];
    if (url === "/api/roles") {
      return [
        {
          role_id: "r1",
          role_name: "角色1",
          system_prompt: "",
          temperature: 0.7,
          model_name: "",
          tool_whitelist: [],
          knowledge_scopes: [],
          is_builtin: false,
          description: "",
          reachout_enabled: false,
          exemplars: [],
        },
      ];
    }
    if (url === "/api/sessions") return [];
    if (url === "/api/knowledge/scopes") return { scopes: [] };
    if (url === "/api/services") return { services: [] };
    if (url === "/api/mcp/servers") return { servers: [], effective_count: 0 };
    if (url === "/api/settings/runtime") {
      return {
        note: "",
        groups: [
          {
            key: "reachout",
            label: "主动开口",
            items: [
              {
                // 真实后端：`key` 是 **env 名**，`field` 才是 Settings 字段名（保存按它）。
                // 以前这份 stub 把 key 写成了小写 field 名，于是"运行环境只读"那条判定
                // （比 key）在生产上从未命中，用例却一直是绿的。
                key: "REACHOUT_ENABLED",
                field: "reachout_enabled",
                label: "全局总闸",
                value: "1",
                default: "1",
                changed: false,
                overridden: false,
                override_value: null,
                kind: "bool",
                choices: null,
              },
              {
                key: "REACHOUT_MERGE_DAYS",
                field: "reachout_merge_days",
                label: "收件箱合并窗口（天）",
                value: "3",
                default: "1",
                changed: true,
                overridden: true,
                override_value: "3",
                kind: "int",
                // 14 只在 payload 里、不在组件那份出厂兜底中：它是"下拉真的跟着后端清单走"
                // 的可观察证据（`R102-19` 的守卫，见 MemoryPage.test 那条用例）。
                choices: ["1", "3", "7", "14"],
              },
            ],
          },
        ],
      };
    }
    if (url.startsWith("/api/audit")) return [];
    return {};
  });
});

describe("SettingsPage 页签结构（Batch 5 拆帖；2026-10-30 记忆三卡迁出后）", () => {
  function renderPage() {
    return render(
      <SettingsPage onOpenChat={() => {}} theme="light" onToggleTheme={() => {}} />,
    );
  }

  it("页签栏含「关于与系统状态」，不再有『通用』与『记忆与任务目录』", async () => {
    renderPage();
    expect(await screen.findByRole("button", { name: "关于与系统状态" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "记忆与任务目录" })).toBeNull();
    expect(screen.queryByRole("button", { name: "通用" })).toBeNull();
  });

  it("主动开口两行在运行环境里只读并指向「记忆」页签（单写点在那边）", async () => {
    renderPage();
    // 只读指针跟写点走：2026-10-30 起写点是顶层「记忆」页签的主动开口卡
    expect((await screen.findAllByText("在「记忆」页签修改")).length).toBe(2);
  });

  it("关于与系统状态页含 外观 / 关于 / 系统状态 三块", async () => {
    renderPage();
    expect(await screen.findByText("外观")).toBeTruthy();
    expect(screen.getByText("关于")).toBeTruthy();
    expect(screen.getByText("系统状态")).toBeTruthy();
  });
});

describe("审计面板：展开键是后端下发的唯一 id（R102-17）", () => {
  it("两行同 (ts, action)：点一行只开那一行，不再连坐多行同开", async () => {
    // 旧实现拿 `(ts, action)` 当展开键 —— 同一秒同动作的行会一起开。这条把那个形状
    // 原样摆出来：只有"键 = 后端下发的唯一 id"的实现能通过。
    const base = apiMock.get.getMockImplementation()!;
    apiMock.get.mockImplementation(async (url: string) => {
      if (url === "/api/audit?limit=200") {
        return [
          { id: 101, ts: "2026-10-02 03:12:42", actor: "u1", action: "set_model",
            target: "elysia", detail_json: '{"who": "甲的详情"}' },
          { id: 102, ts: "2026-10-02 03:12:42", actor: "u1", action: "set_model",
            target: "elysia", detail_json: '{"who": "乙的详情"}' },
        ];
      }
      return base(url);
    });
    render(<SettingsPage onOpenChat={() => {}} theme="light" onToggleTheme={() => {}} />);
    const btnA = (await screen.findByText('{"who": "甲的详情"}')).closest("button");
    const btnB = screen.getByText('{"who": "乙的详情"}').closest("button");
    fireEvent.click(btnA!);

    // 展开的只有甲：甲的 JSON 进了 <pre>，而乙那一格仍是按钮（旧实现会两格同开）。
    expect(await screen.findAllByText(/甲的详情/, { selector: "pre" })).toHaveLength(1);
    expect(screen.getByText('{"who": "乙的详情"}').closest("button")).toBe(btnB);
    expect(screen.queryByText(/乙的详情/, { selector: "pre" })).toBeNull();
  });
});

// `S-8`：运行环境页那一格「她此刻为什么静默」。它读的是收件箱那份负载里的 `quiet`
// （后端算的），前端不自己推时间 —— 这一格的全部意义是让人信，猜的时刻一次不准就再也不信。
describe("运行环境页的「她此刻为什么静默」", () => {
  const quietRow = {
    role_id: "elysia",
    role_name: "爱莉希雅",
    why: "距上次说话不足 66 分钟",
    next_ok_at: new Date(Date.now() + 40 * 60_000).toISOString(),
    streak: 1,
    unread: 1,
  };

  const runtimeWithInterval = {
    note: "",
    groups: [
      {
        key: "reachout",
        label: "主动开口",
        items: [
          {
            key: "REACHOUT_INTERVAL_MINUTES",
            field: "reachout_interval_minutes",
            label: "开口间隔（分钟）",
            value: "60",
            default: "60",
            changed: true,
            overridden: true,
            override_value: "60",
            kind: "int",
            choices: null,
          },
        ],
      },
    ],
  };

  function stubReachouts(quiet: unknown[] | undefined) {
    const base = apiMock.get.getMockImplementation()!;
    apiMock.get.mockImplementation(async (url: string) => {
      if (url === "/api/reachouts") return { items: [], unread: 0, quiet };
      if (url === "/api/settings/runtime") return runtimeWithInterval;
      return base(url);
    });
    // 保存也要回一份完整的 payload：面板拿它重刷草稿，给 undefined 就是 `p.groups.map` 炸。
    apiMock.put.mockResolvedValue(runtimeWithInterval);
  }

  it("挂在「主动开口」那一组下面，一句原因 + 下一次大约几点", async () => {
    stubReachouts([quietRow]);
    render(
      <SettingsPage onOpenChat={() => {}} theme="light" onToggleTheme={() => {}} />,
    );
    const box = await screen.findByTestId("quiet-status");
    expect(box.textContent).toContain("爱莉希雅 静默中");
    expect(box.textContent).toContain("距上次说话不足 66 分钟");
    expect(box.textContent).toContain("下一次大约");
    // 只挂一组：给「联网」「静默时段」那些组也各画一行，就成了同一句话在页面上到处复读
    expect(screen.getAllByTestId("quiet-status")).toHaveLength(1);
  });

  it("后端没给 quiet（老后端 / 没人开主动资格）时不画空壳", async () => {
    stubReachouts(undefined);
    render(
      <SettingsPage onOpenChat={() => {}} theme="light" onToggleTheme={() => {}} />,
    );
    expect((await screen.findAllByText("主动开口")).length).toBeGreaterThan(0);
    expect(screen.queryByTestId("quiet-status")).toBeNull();
  });

  it("改了「开口间隔」保存后要重读那一格 —— 那句分钟数与时刻都是它算的", async () => {
    stubReachouts([quietRow]);
    render(
      <SettingsPage onOpenChat={() => {}} theme="light" onToggleTheme={() => {}} />,
    );
    await screen.findByTestId("quiet-status");
    const input = screen.getByDisplayValue("60");
    fireEvent.change(input, { target: { value: "90" } });
    // 各页签是"同时渲染 + CSS 隐藏"，所以按钮名要挑唯一的那个：运行环境的保存键
    // 带着改动条数（保存（1 项修改）），别的页签只写"保存"。
    fireEvent.click(screen.getByRole("button", { name: /保存（/ }));
    await waitFor(() =>
      expect(apiMock.put).toHaveBeenCalledWith(
        "/api/settings/runtime",
        // 运行环境页发的是 **env 名**（REACHOUT_INTERVAL_MINUTES），「记忆」页签那个
        // 单写点发的是 field 名 —— 两个写点两套键，抄错的那一边看起来总是"绿的假用例"。
        expect.objectContaining({ values: { REACHOUT_INTERVAL_MINUTES: "90" } }),
      ),
    );
    const calls = apiMock.get.mock.calls.filter((c) => c[0] === "/api/reachouts").length;
    expect(calls).toBeGreaterThanOrEqual(2);
  });
});
