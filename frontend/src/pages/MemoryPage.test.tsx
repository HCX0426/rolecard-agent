// @vitest-environment jsdom
//
// MemoryPage（顶层「记忆」页签）接线测试。用例随组件 **整体迁来** SettingsPage.test.tsx
// （2026-10-30 IA：记忆三卡出设置）—— 断言与桩一字未动，只换了 render 目标与住所；
// 读侧桩同步精简：本页挂载时只拉 /api/roles、/api/settings/memory(?) 与
// /api/settings/runtime 三个端点，其余分支不再陪跑。
// 主动开口可写卡那一半从「页签拆分」用例里拆来（运行环境只读那一半留在 SettingsPage.test）。

import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
    consolidateMemory: vi.fn(),
  },
}));

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, api: apiMock };
});

import MemoryPage from "./MemoryPage";

beforeEach(() => {
  vi.clearAllMocks();
  apiMock.get.mockImplementation(async (url: string) => {
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
    if (url === "/api/settings/memory" || url.startsWith("/api/settings/memory?"))
      return {
        enabled: false,
        role_id: null,
        content: "原始记忆",
        items: [{ id: 7, text: "原始记忆", source: "chat", pinned: false, hit_count: 2, last_hit_at: null, created_at: null }],
        active_count: 1,
        limit: 200,
        over_limit: false,
      };
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
                // 的可观察证据（`R102-19` 的守卫，见下面那条用例）。
                choices: ["1", "3", "7", "14"],
              },
            ],
          },
        ],
      };
    }
    return {};
  });
});

function memScopeSelect(): HTMLSelectElement {
  const selects = screen.getAllByRole("combobox") as HTMLSelectElement[];
  const sel = selects.find((s) => s.textContent?.includes("全局"));
  if (!sel) throw new Error("找不到记忆作用域下拉");
  return sel;
}

/**
 * 等到按钮**真的可点**再交回去。
 *
 * 为什么要等：面板的数据是异步载入的，`render()` 之后立刻拿到的往往是禁用态那一下，
 * `fireEvent.click` 点上去什么都不发生，于是一屏"没调用"的红 —— 测的是竞态，不是行为。
 * 为什么每轮**重新查**元素：`Button` 在禁用时会套一层带说明文字的容器，从禁用翻成可用
 * 时那个 `<button>` 节点是被换掉的，攥着旧引用等下去只会永远 `disabled === true`。
 */
async function enabledButton(name: string): Promise<HTMLButtonElement> {
  let btn!: HTMLButtonElement;
  await waitFor(() => {
    btn = screen.getByRole("button", { name }) as HTMLButtonElement;
    expect(btn.disabled).toBe(false);
  });
  return btn;
}

describe("MemoryPage 切换记忆作用域二次确认（useConfirm）", () => {
  it("有未保存修改时切换作用域弹确认框，点『确认』才切换", async () => {
    const { fireEvent } = await import("@testing-library/react");
    render(<MemoryPage />);
    // 等记忆加载完，草稿文本框显示原文后再改脏（否则异步加载会覆盖改动）
    const ta = (await screen.findByDisplayValue("原始记忆")) as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "改了" } });
    const select = memScopeSelect();
    expect(select.value).toBe("");
    fireEvent.change(select, { target: { value: "r1" } });
    expect(await screen.findByText("切换作用域将丢弃未保存的修改")).toBeTruthy();
    fireEvent.click(screen.getByText("确认"));
    await waitFor(() => expect(memScopeSelect().value).toBe("r1"));
  });

  it("点『取消』不切换作用域", async () => {
    const { fireEvent } = await import("@testing-library/react");
    render(<MemoryPage />);
    // 等记忆加载完、草稿文本框可编辑且显示原文后再改脏（否则命中 disabled 文本框或异步加载覆盖）
    const ta = (await screen.findByDisplayValue("原始记忆")) as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "改了" } });
    const select = memScopeSelect();
    fireEvent.change(select, { target: { value: "r1" } });
    expect(await screen.findByText("切换作用域将丢弃未保存的修改")).toBeTruthy();
    fireEvent.click(screen.getByText("取消"));
    await new Promise((r) => setTimeout(r, 20));
    expect(memScopeSelect().value).toBe("");
  });
});

describe("MemoryPage 页头（注入总闸是这页的英雄元素）", () => {
  it("总闸住在页头而不在卡里；挂载读完显示当前注入状态", async () => {
    render(<MemoryPage />);
    expect(await screen.findByRole("button", { name: "记忆注入：已关闭" })).toBeTruthy();
    // 页头副标题说明"一份事实、一条注入路径"，并把两件事的去处指清楚
    // （2026-10-30：工作区进了每张角色卡、主动开口总闸去了角色卡页 —— 都不在记忆页签）。
    expect(screen.getByText(/对话、主动开口、桌宠共用这一份/)).toBeTruthy();
    expect(screen.getByText(/总闸在「角色卡」页/)).toBeTruthy();
    expect(screen.getByText(/工作区.*在每张角色卡里/)).toBeTruthy();
  });
});

describe("记忆卡逐条列表（事实面是条目）", () => {
  function renderPage() {
    return render(<MemoryPage />);
  }

  it("列出条目并显示来源、层与命中次数", async () => {
    renderPage();
    // 条目行的存在用元信息那一行证明（来源 + 层 + 命中次数只有渲染出的条目才会出现）。
    // 层标签（记忆分层，2026-10-30）：旧数据没有 category 字段时按长期记忆显示。
    expect(await screen.findByText(/对话 · 长期记忆 · 用过 2 次/)).toBeTruthy();
  });

  it("「记住」提交一条并采用返回的 payload", async () => {
    apiMock.post.mockResolvedValue({
      enabled: false, role_id: null, content: "- 新的", items: [
        { id: 8, text: "新的", source: "manual", pinned: false, hit_count: 0, last_hit_at: null, created_at: null },
      ], active_count: 1, limit: 200, over_limit: false,
    });
    renderPage();
    fireEvent.change(await screen.findByPlaceholderText(/手动记一条事实/), { target: { value: "新的" } });
    fireEvent.click(screen.getByRole("button", { name: "记住" }));
    // 带 category（记忆分层，2026-10-30）：新增落在**当前看的那一层**，
    // 否则在"项目记忆"板块里加一条，它会悄悄落到长期记忆。
    await waitFor(() =>
      expect(apiMock.post).toHaveBeenCalledWith("/api/settings/memory/item", {
        text: "新的",
        category: "longterm",
      }),
    );
    expect(await screen.findByText("新的")).toBeTruthy();
  });

  it("分板块：切到某一层后读 URL 带 category、新增落在那一层", async () => {
    apiMock.get.mockImplementation(async (url: string) => {
      if (url.startsWith("/api/settings/memory")) {
        return {
          enabled: true,
          role_id: null,
          category: url.includes("category=project") ? "project" : null,
          content: "",
          items: [],
          counts: { longterm: 3, session: 1, project: 2, self: 0 },
          active_count: 6,
          limit: 200,
          over_limit: false,
          extract_turns: 12,
        };
      }
      if (url === "/api/roles") return [{ role_id: "r1", role_name: "爱莉希雅" }];
      if (url === "/api/settings/runtime")
        return { groups: [{ items: [{ field: "memory_inject_categories", value: "longterm,self" }] }] };
      return {};
    });
    renderPage();
    // 板块标签上带条数（后端一次给齐 counts，不用前端再发 N 次请求自己数）。
    expect(await screen.findByText(/长期记忆 3/)).toBeTruthy();
    expect(screen.getByText(/项目记忆 2/)).toBeTruthy();

    fireEvent.click(screen.getByText(/项目记忆 2/));
    await waitFor(() =>
      expect(apiMock.get).toHaveBeenCalledWith("/api/settings/memory?category=project"),
    );

    // 在这一层里新增 → category 是 project
    apiMock.post.mockResolvedValue({
      enabled: true, role_id: null, category: "project", content: "- 项目的一条",
      items: [{ id: 9, text: "项目的一条", source: "manual", pinned: false, hit_count: 0, importance: 1, last_hit_at: null, created_at: null }],
      counts: { longterm: 3, session: 1, project: 3, self: 0 },
      active_count: 7, limit: 200, over_limit: false, extract_turns: 12,
    });
    fireEvent.change(await screen.findByPlaceholderText(/手动记一条事实/), { target: { value: "项目的一条" } });
    fireEvent.click(screen.getByRole("button", { name: "记住" }));
    await waitFor(() =>
      expect(apiMock.post).toHaveBeenCalledWith("/api/settings/memory/item", {
        text: "项目的一条",
        category: "project",
      }),
    );
  });

  it("钉住走 PATCH；删除要先二次确认，确认后才真删", async () => {
    apiMock.patch.mockResolvedValue({
      enabled: false, role_id: null, content: "原始记忆",
      items: [{ id: 7, text: "原始记忆", source: "chat", pinned: true, hit_count: 2, last_hit_at: null, created_at: null }],
      active_count: 1, limit: 200, over_limit: false,
    });
    apiMock.del.mockResolvedValue({
      enabled: false, role_id: null, content: "", items: [], active_count: 0, limit: 200, over_limit: false,
    });
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "钉住" }));
    await waitFor(() => expect(apiMock.patch).toHaveBeenCalledWith("/api/settings/memory/item/7", { pinned: true }));

    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    expect(await screen.findByText(/会从记忆里删除/)).toBeTruthy();
    expect(apiMock.del).not.toHaveBeenCalled();
    fireEvent.click(await screen.findByRole("button", { name: "确认删除" }));
    await waitFor(() => expect(apiMock.del).toHaveBeenCalledWith("/api/settings/memory/item/7"));
  });

  it("「要紧程度」那一档走 PATCH：0 是一个真值，回读也要显示成「次要」", async () => {
    apiMock.patch.mockResolvedValue({
      enabled: false, role_id: null, content: "原始记忆",
      items: [
        {
          id: 7, text: "原始记忆", source: "chat", pinned: false, hit_count: 2,
          importance: 0, last_hit_at: null, created_at: null,
        },
      ],
      active_count: 1, limit: 200, over_limit: false,
    });
    renderPage();
    const tier = await screen.findByLabelText(/要紧程度/);
    // 只数这一个 select 里的档（整页还有角色下拉，`getAllByRole("option")` 会连它一起捞）。
    expect(Array.from((tier as HTMLSelectElement).options).map((o) => o.textContent)).toEqual([
      "次要",
      "一般",
      "要紧",
    ]);
    fireEvent.change(tier, { target: { value: "0" } });
    await waitFor(() =>
      expect(apiMock.patch).toHaveBeenCalledWith("/api/settings/memory/item/7", { importance: 0 }),
    );
    // 后端回什么就显示什么：0 若被当成"没给"而退回默认档，用户点"次要"会看到"一般"。
    await waitFor(() =>
      expect((screen.getByLabelText(/要紧程度/) as HTMLSelectElement).value).toBe("0"),
    );
  });
});

describe("记忆卡「整理记忆」（一次模型调用，只写标记）", () => {
  const item = (id: number, text: string) => ({
    id,
    text,
    source: "chat",
    pinned: false,
    hit_count: 1,
    last_hit_at: null,
    created_at: null,
  });

  /** 记忆面板的读侧桩：两条活跃条目（够整理）。 */
  function stubMemory(items: ReturnType<typeof item>[]) {
    const payload = {
      enabled: true,
      role_id: null,
      content: items.map((i) => `- ${i.text}`).join("\n"),
      items,
      active_count: items.length,
      limit: 200,
      over_limit: false,
    };
    apiMock.get.mockImplementation(async (url: string) => {
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
      if (url === "/api/settings/memory" || url.startsWith("/api/settings/memory?")) {
        return items.length ? { ...payload, items } : { ...payload, items: [], active_count: 0 };
      }
      return {};
    });
  }

  it("两条以上才可点：整理后列表刷成响应里那份，并报出条数与成本", async () => {
    stubMemory([item(1, "用户养了一只猫"), item(2, "用户的猫叫米")]);
    apiMock.consolidateMemory.mockResolvedValue({
      report: {
        added: 0,
        updated: 0,
        merged: 1,
        invalidated: 0,
        noop: 0,
        skipped: 0,
        detail: "",
        tokens: 456,
        before: 2,
        after: 1,
      },
      turns_since: 0,
      enabled: true,
      role_id: null,
      content: "- 用户养了一只叫米的猫",
      items: [item(3, "用户养了一只叫米的猫")],
      active_count: 1,
      limit: 200,
      over_limit: false,
    });

    render(<MemoryPage />);
    // 记忆是**异步**载入的：`mem` 到位前按钮一直是禁用态，直接点等于点空气（第一次写就踩了）。
    fireEvent.click(await enabledButton("整理记忆"));
    await waitFor(() => expect(apiMock.consolidateMemory).toHaveBeenCalledWith(undefined));
    expect(await screen.findByText("用户养了一只叫米的猫")).toBeTruthy();
    expect(screen.queryByText("用户养了一只猫")).toBeNull();
    // 「没删任何一行」是这句提示的重点：整理只写失效标记
    expect(await screen.findByText(/合并 1 组/)).toBeTruthy();
    expect(screen.getByText(/没删任何一行/)).toBeTruthy();
    expect(screen.getByText(/花掉 456 个 token/)).toBeTruthy();
  });

  it("作用域切到某角色时，整理的是那个桶（不是全局那份）", async () => {
    stubMemory([item(1, "它记得的猫"), item(2, "猫叫米")]);
    apiMock.consolidateMemory.mockResolvedValue({
      report: {
        added: 0,
        updated: 0,
        merged: 1,
        invalidated: 0,
        noop: 0,
        skipped: 0,
        detail: "",
        tokens: null,
        before: 2,
        after: 1,
      },
      turns_since: 0,
      enabled: true,
      role_id: "r1",
      content: "- 它记得一只叫米的猫",
      items: [item(3, "它记得一只叫米的猫")],
      active_count: 1,
      limit: 200,
      over_limit: false,
    });

    render(<MemoryPage />);
    const sel = await waitFor(() => {
      const found = (screen.getAllByRole("combobox") as HTMLSelectElement[]).find((s) =>
        s.textContent?.includes("全局"),
      );
      if (!found) throw new Error("还没渲染出作用域下拉");
      return found;
    });
    fireEvent.change(sel, { target: { value: "r1" } });
    fireEvent.click(await enabledButton("整理记忆"));
    await waitFor(() => expect(apiMock.consolidateMemory).toHaveBeenCalledWith("r1"));
    // 模型没报 usage 时不编一个数：提示里没有 tokens，也不能显示 0
    expect(await screen.findByText(/合并 1 组/)).toBeTruthy();
    expect(screen.queryByText(/个 token/)).toBeNull();
  });

  it("只有一条时禁用并说明原因（而不是一个点了没反应的按钮）", async () => {
    stubMemory([item(1, "只有一条")]);
    render(<MemoryPage />);
    const btn = (await screen.findByText("整理记忆")).closest("button") as HTMLButtonElement;
    await waitFor(() => expect(btn.disabled).toBe(true));
    expect(screen.getByText("至少两条活跃记忆才有可整理的")).toBeTruthy();
    expect(apiMock.consolidateMemory).not.toHaveBeenCalled();
  });

  it("整理失败把后端原文显示出来（里面有「去哪开」）", async () => {
    stubMemory([item(1, "用户养了一只猫"), item(2, "用户的猫叫米")]);
    apiMock.consolidateMemory.mockRejectedValue(
      new Error("跨会话记忆当前是关闭的 —— 先打开它再整理。"),
    );
    render(<MemoryPage />);
    fireEvent.click(await enabledButton("整理记忆"));
    expect(await screen.findByText(/整理失败：跨会话记忆当前是关闭的/)).toBeTruthy();
  });
});

describe("记忆卡「每 N 轮自动提取」那一档（会自己花钱的行为必须能在界面上关）", () => {
  /** 记忆 payload 的读侧桩；`turns` 用 getter 现取，才能模拟"保存之后读回新值"。 */
  function stubMemory(getTurns: () => number) {
    apiMock.get.mockImplementation(async (url: string) => {
      if (url === "/api/settings/memory" || url.startsWith("/api/settings/memory?")) {
        return {
          enabled: true,
          role_id: null,
          content: "",
          items: [],
          active_count: 0,
          limit: 200,
          over_limit: false,
          extract_turns: getTurns(),
        };
      }
      return {};
    });
  }

  function cadenceSelect(): HTMLSelectElement {
    const found = (screen.getAllByRole("combobox") as HTMLSelectElement[]).find((s) =>
      [...s.options].some((o) => o.textContent?.includes("每 12 轮")),
    );
    if (!found) throw new Error("找不到自动提取节奏下拉");
    return found;
  }

  it("按有效值回显当前档位，改成 0 = 关掉自动那条（并读回一次新值）", async () => {
    let turns = 12;
    stubMemory(() => turns);
    apiMock.put.mockImplementation(async () => {
      turns = 0; // 后端收下"关"之后，有效值就变 0 了
      return {};
    });
    render(<MemoryPage />);
    const sel = cadenceSelect();
    await waitFor(() => expect(sel.value).toBe("12"));

    fireEvent.change(cadenceSelect(), { target: { value: "0" } });
    await waitFor(() =>
      expect(apiMock.put).toHaveBeenCalledWith("/api/settings/memory", { extract_turns: 0 }),
    );
    expect(await screen.findByText(/已关掉自动提取/)).toBeTruthy();
  });

  it(".env 里设成档位外的数（8）时原样回显，不假装是「每 12 轮」", async () => {
    stubMemory(() => 8);
    render(<MemoryPage />);
    await waitFor(() => expect(cadenceSelect().value).toBe("8"));
    expect(cadenceSelect().selectedOptions[0].textContent).toContain("每 8 轮");
  });
});

describe("记忆卡：改一条 / 合两条（S-3 界面那半）", () => {
  const item = (id: number, text: string, extra: Record<string, unknown> = {}) => ({
    id,
    text,
    source: "chat",
    pinned: false,
    hit_count: 1,
    importance: 1,
    last_hit_at: null,
    created_at: null,
    ...extra,
  });
  const THREE = {
    enabled: true,
    role_id: null,
    content: "",
    items: [
      item(1, "用户住在上海"),
      item(2, "用户在上海一家医院工作"),
      item(3, "用户养了一只猫", { pinned: true }),
    ],
    active_count: 3,
    limit: 200,
    over_limit: false,
  };
  const payload = (items: unknown[]) => ({ ...THREE, items, active_count: items.length });

  beforeEach(() => {
    // 只换记忆那一条分支，其余端点沿用外层 beforeEach 那份。
    const base = apiMock.get.getMockImplementation() as (url: string) => Promise<unknown>;
    apiMock.get.mockImplementation(async (url: string) =>
      url.startsWith("/api/settings/memory") ? THREE : base(url),
    );
  });

  it("「改」把那一行换成输入框，保存走 PATCH 且带上新句子", async () => {
    apiMock.patch.mockResolvedValue(payload([item(1, "用户住在上海，做金融")]));
    render(<MemoryPage />);
    const editButtons = await screen.findAllByRole("button", { name: "改" });
    fireEvent.click(editButtons[0]);
    const box = (await screen.findByLabelText("改这条记忆")) as HTMLInputElement;
    expect(box.value).toBe("用户住在上海"); // 预填的是原文，不是空框
    fireEvent.change(box, { target: { value: "用户住在上海，做金融" } });
    // 「保存」这一页有好几个（整段覆写、行内编辑都叫这名），所以要限定在本行里找。
    fireEvent.click(within(box.closest("li") as HTMLElement).getByRole("button", { name: "保存" }));
    await waitFor(() =>
      expect(apiMock.patch).toHaveBeenCalledWith("/api/settings/memory/item/1", {
        text: "用户住在上海，做金融",
      }),
    );
  });

  it("一个字没改就点保存：不发这次写（保存后回到常态行）", async () => {
    render(<MemoryPage />);
    const editButtons = await screen.findAllByRole("button", { name: "改" });
    fireEvent.click(editButtons[0]);
    const editBox = (await screen.findByLabelText("改这条记忆")) as HTMLInputElement;
    fireEvent.click(within(editBox.closest("li") as HTMLElement).getByRole("button", { name: "保存" }));
    await waitFor(() => expect(apiMock.patch).not.toHaveBeenCalled());
    expect(screen.queryByLabelText("改这条记忆")).toBeNull();
  });

  it("勾两条 → 预填兜底句 → 合并走 POST，留下的是先勾的那条", async () => {
    apiMock.post.mockResolvedValue(payload([item(1, "用户住在上海；用户在上海一家医院工作")]));
    render(<MemoryPage />);
    const picks = await screen.findAllByLabelText("选择这条记忆用于合并");
    fireEvent.click(picks[0]);
    fireEvent.click(picks[1]);
    const merged = (await screen.findByLabelText("合并后的句子")) as HTMLTextAreaElement;
    expect(merged.value).toBe("用户住在上海；用户在上海一家医院工作");
    fireEvent.click(screen.getByRole("button", { name: /合并（留 1 条）/ }));
    await waitFor(() =>
      expect(apiMock.post).toHaveBeenCalledWith("/api/settings/memory/item/1/merge/2", {
        text: "用户住在上海；用户在上海一家医院工作",
      }),
    );
  });

  it("只许两条：勾第三条时挤掉最早那条，于是留下的是第 2、3 条", async () => {
    apiMock.post.mockResolvedValue(payload([item(2, "合并结果")]));
    render(<MemoryPage />);
    const picks = await screen.findAllByLabelText("选择这条记忆用于合并");
    fireEvent.click(picks[0]);
    fireEvent.click(picks[1]);
    fireEvent.click(picks[2]);
    const merged = (await screen.findByLabelText("合并后的句子")) as HTMLTextAreaElement;
    expect(merged.value).toBe("用户在上海一家医院工作；用户养了一只猫");
    fireEvent.click(screen.getByRole("button", { name: /合并（留 1 条）/ }));
    await waitFor(() => expect(apiMock.post).toHaveBeenCalledWith("/api/settings/memory/item/2/merge/3", expect.anything()));
  });

  it("先勾没钉住的、后勾钉住的：要说清钉住状态不跟着搬", async () => {
    render(<MemoryPage />);
    const picks = await screen.findAllByLabelText("选择这条记忆用于合并");
    fireEvent.click(picks[0]); // 1 号：未钉住
    fireEvent.click(picks[2]); // 3 号：钉住
    expect(await screen.findByText(/钉住状态不跟着搬/)).toBeTruthy();
  });
});

describe("作用域竞态守卫（2026-10-30 批次的回归钉）", () => {
  const ROLE_R1 = {
    role_id: "r1", role_name: "角色1", system_prompt: "", temperature: 0.7,
    model_name: "", tool_whitelist: [], knowledge_scopes: [], is_builtin: false,
    description: "", reachout_enabled: false, exemplars: [],
  };
  const ROLE_R2 = { ...ROLE_R1, role_id: "r2", role_name: "角色2" };
  const bucket = (roleId: string | null, content: string) => ({
    enabled: true, role_id: roleId, content, items: [],
    active_count: 0, limit: 200, over_limit: false, extract_turns: 12,
  });

  it("快速连切两个角色：慢到的旧桶响应整包作废，不许盖进新桶的草稿", async () => {
    // 旧实现无守卫：r1 的响应后到，"r1 的内容"填进标着 r2 的编辑器——此时点「保存」
    // 发出的 PUT 带 role_id=r2，A 桶的正文静默覆盖 B 桶（数据损坏，不只是显示错位）。
    let resolveR1!: () => void;
    apiMock.get.mockImplementation(async (url: string) => {
      if (url === "/api/roles") return [ROLE_R1, ROLE_R2];
      if (url === "/api/settings/memory?role_id=r1") {
        return new Promise((res) => {
          resolveR1 = () => res(bucket("r1", "角色1的记忆"));
        });
      }
      if (url === "/api/settings/memory?role_id=r2") return bucket("r2", "角色2的记忆");
      if (url === "/api/settings/memory") return bucket(null, "全局的记忆");
      return {};
    });
    render(<MemoryPage />);
    const sel = (await screen.findAllByRole("combobox") as HTMLSelectElement[]).find((s) =>
      s.textContent?.includes("全局"),
    )!;
    await screen.findByDisplayValue("全局的记忆");

    fireEvent.change(sel, { target: { value: "r1" } }); // 在途（手工放行）
    fireEvent.change(sel, { target: { value: "r2" } });
    expect(await screen.findByDisplayValue("角色2的记忆")).toBeTruthy();

    await act(async () => {
      resolveR1();
    });
    // 慢到的 r1 响应被令牌判过期：草稿仍是 r2 的，"保存"不可能再把 r1 的内容发进 r2 桶。
    expect(screen.queryByDisplayValue("角色1的记忆")).toBeNull();
    expect(screen.getByDisplayValue("角色2的记忆")).toBeTruthy();
  });

  it("角色作用域下改提取档位：读回的是该角色的桶，整屏不再被换成全局记忆", async () => {
    // 旧实现 PUT 后打裸全局 URL 读回：角色作用域下列表+原文草稿静默换成全局桶，
    // 而下拉还停在该角色 —— 屏上数据与作用域标签互相说谎。
    let turns = 12; // 后端收下"关"之后有效值变 0（照抄既有那条档位用例的活桩手法）
    apiMock.get.mockImplementation(async (url: string) => {
      if (url === "/api/roles") return [ROLE_R1];
      const scoped = url.includes("role_id=r1");
      return { ...bucket(scoped ? "r1" : null, scoped ? "角色1的记忆" : "全局的记忆"), extract_turns: turns };
    });
    apiMock.put.mockImplementation(async () => {
      turns = 0;
      return {};
    });
    render(<MemoryPage />);
    const sel = (await screen.findAllByRole("combobox") as HTMLSelectElement[]).find((s) =>
      s.textContent?.includes("全局"),
    )!;
    fireEvent.change(sel, { target: { value: "r1" } });
    await screen.findByDisplayValue("角色1的记忆");

    const cadence = (screen.getAllByRole("combobox") as HTMLSelectElement[]).find((s) =>
      [...s.options].some((o) => o.textContent?.includes("每 12 轮")),
    )!;
    fireEvent.change(cadence, { target: { value: "0" } });
    expect(await screen.findByText(/已关掉自动提取/)).toBeTruthy();
    expect(screen.getByDisplayValue("角色1的记忆")).toBeTruthy();
    expect(screen.queryByDisplayValue("全局的记忆")).toBeNull();
    expect(apiMock.get).toHaveBeenLastCalledWith("/api/settings/memory?role_id=r1");
  });
});

describe("记忆卡「清空」的二次确认（2026-10-30 交互审计的回归钉）", () => {
  it("点『清空』先弹确认，确认才 DELETE —— 一键抹整桶不该比删一条还随便", async () => {
    // 单条删除早有确认，整桶清空反而裸奔是不一致；收件箱/知识库的清空全都有确认。
    apiMock.del.mockResolvedValue({
      enabled: false, role_id: null, content: "", items: [], active_count: 0, limit: 200, over_limit: false,
    });
    render(<MemoryPage />);
    await screen.findByDisplayValue("原始记忆");
    fireEvent.click(screen.getByRole("button", { name: "清空" }));
    expect(await screen.findByText("清空全局记忆？")).toBeTruthy();
    expect(apiMock.del).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "确认清空" }));
    await waitFor(() => expect(apiMock.del).toHaveBeenCalledWith("/api/settings/memory"));
    expect(await screen.findByText("已清空")).toBeTruthy();
  });
});

// 主动开口总闸条的用例随组件**整体迁走**（2026-10-30 搬去角色卡页托管）：
// 现在住在 components/ReachoutMasterCard.test.tsx —— 这一页不再管主动开口。
