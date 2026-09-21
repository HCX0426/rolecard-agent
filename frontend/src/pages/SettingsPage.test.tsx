// @vitest-environment jsdom
//
// SettingsPage（设置页）接线测试。重点：切换记忆作用域时若有未保存修改，走 useConfirm
// 二次确认 —— 点确认才切换并丢弃草稿，点取消不切换。

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
                choices: ["1", "3", "7"],
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

describe("SettingsPage 切换记忆作用域二次确认（useConfirm）", () => {
  it("有未保存修改时切换作用域弹确认框，点『确认』才切换", async () => {
    const { fireEvent } = await import("@testing-library/react");
    render(<SettingsPage onOpenChat={() => {}} theme="light" onToggleTheme={() => {}} />);
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
    render(<SettingsPage onOpenChat={() => {}} theme="light" onToggleTheme={() => {}} />);
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

describe("SettingsPage 页签拆分（Batch 5：记忆与任务目录 / 关于与系统状态）", () => {
  function renderPage() {
    return render(
      <SettingsPage onOpenChat={() => {}} theme="light" onToggleTheme={() => {}} />,
    );
  }

  it("页签栏含两个新帖，且不再有『通用』", async () => {
    renderPage();
    expect(await screen.findByRole("button", { name: "记忆与任务目录" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "关于与系统状态" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "通用" })).toBeNull();
  });

  it("主动开口总闸与收件箱窗口只在「记忆与任务目录」可写；运行环境里只读并指向该页签", async () => {
    const { fireEvent } = await import("@testing-library/react");
    renderPage();
    // 单写点：记忆与任务目录里的可写开关（stub 的 reachout_enabled 生效值 = 1 → 已开启）
    expect(await screen.findByRole("button", { name: "已开启" })).toBeTruthy();
    // 两条都该只读并指回单写点：只有一条被标成只读 = 判定按错了字段（key vs field）
    expect((await screen.findAllByText("在「记忆与任务目录」页签修改")).length).toBe(2);
    // 窗口下拉按**生效值**选中（stub 给 3），而不是按出厂默认 1
    const select = (await screen.findByLabelText(/收件箱折叠窗口/)) as HTMLSelectElement;
    expect(select.value).toBe("3");
    fireEvent.change(select, { target: { value: "7" } });
    await waitFor(() =>
      expect(apiMock.put).toHaveBeenCalledWith(
        "/api/settings/runtime",
        expect.objectContaining({ values: { reachout_merge_days: "7" } }),
      ),
    );
    // 折叠窗口不是"建议改重启"的只读项：保存走同一条 runtime 覆盖通道，即时生效
    expect(screen.queryAllByRole("button", { name: "已关闭" })).toHaveLength(0);
  });

  it("关于与系统状态页含 外观 / 关于 / 系统状态 三块", async () => {
    renderPage();
    expect(await screen.findByText("外观")).toBeTruthy();
    expect(screen.getByText("关于")).toBeTruthy();
    expect(screen.getByText("系统状态")).toBeTruthy();
  });
});

describe("记忆卡逐条列表（事实面是条目）", () => {
  function renderPage() {
    return render(<SettingsPage />);
  }

  it("列出条目并显示来源与命中次数", async () => {
    renderPage();
    // 条目行的存在用元信息那一行证明（来源 + 命中次数只有渲染出的条目才会出现）。
expect(await screen.findByText(/对话 · 用过 2 次/)).toBeTruthy();
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
    await waitFor(() => expect(apiMock.post).toHaveBeenCalledWith("/api/settings/memory/item", { text: "新的" }));
    expect(await screen.findByText("新的")).toBeTruthy();
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
      if (url === "/api/settings/runtime") return { note: "", groups: [] };
      if (url.startsWith("/api/audit")) return [];
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

    render(<SettingsPage />);
    // 记忆是**异步**载入的：`mem` 到位前按钮一直是禁用态，直接点等于点空气（第一次写就踩了）。
    fireEvent.click(await enabledButton("整理记忆"));
    await waitFor(() => expect(apiMock.consolidateMemory).toHaveBeenCalledWith(undefined));
    expect(await screen.findByText("用户养了一只叫米的猫")).toBeTruthy();
    expect(screen.queryByText("用户养了一只猫")).toBeNull();
    // 「没删任何一行」是这句提示的重点：整理只写失效标记
    expect(await screen.findByText(/合并 1 组/)).toBeTruthy();
    expect(screen.getByText(/没删任何一行/)).toBeTruthy();
    expect(screen.getByText(/456 tokens/)).toBeTruthy();
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

    render(<SettingsPage />);
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
    expect(screen.queryByText(/tokens/)).toBeNull();
  });

  it("只有一条时禁用并说明原因（而不是一个点了没反应的按钮）", async () => {
    stubMemory([item(1, "只有一条")]);
    render(<SettingsPage />);
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
    render(<SettingsPage />);
    fireEvent.click(await enabledButton("整理记忆"));
    expect(await screen.findByText(/整理失败：跨会话记忆当前是关闭的/)).toBeTruthy();
  });
});
