// @vitest-environment jsdom
//
// RolesPage（角色卡管理）接线测试。重点：删除角色卡是破坏性操作，
// 走 useConfirm 二次确认 —— 点确认才 DELETE，点取消不调。
// 2026-10-30 起本页还托管「工作区」卡（原设置页的任务目录，授权跟角色走）。

import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
    // 工作区卡的三个专用方法（api 层的便捷封装，不走 api.get）：
    // 从前 MemoryPanel 也调它们，但旧桩没给、异常被 catch 吞成"加载中"——没测试看着就一直缺。
    getWorkspaceDir: vi.fn(),
    setWorkspaceDir: vi.fn(),
    clearWorkspaceDir: vi.fn(),
    browseTree: vi.fn(),
  },
}));

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, api: apiMock };
});

import RolesPage from "./RolesPage";

const ROLE = {
  role_id: "r1",
  role_name: "测试角色",
  system_prompt: "",
  temperature: 0.7,
  model_name: "",
  tool_whitelist: [] as string[],
  knowledge_scopes: [] as string[],
  is_builtin: false,
  description: "",
  reachout_enabled: false,
  exemplars: [] as { rowId: number; user: string; assistant: string }[],
};

beforeEach(() => {
  vi.clearAllMocks();
  // 工作区卡挂载即读生效目录（不走 api.get，直接钉方法返回值）。
  apiMock.getWorkspaceDir.mockResolvedValue({ path: "D:/wk", overridden: true });
  apiMock.get.mockImplementation(async (url: string) => {
    if (url === "/api/roles") return [ROLE];
    if (url === "/api/tools/catalog") return { kernel: [], domains: {} };
    if (url === "/api/settings/models") return { default: "", providers: [], fallbacks: [] };
    if (url === "/api/knowledge/scopes") return { scopes: [] };
    return {};
  });
});

describe("RolesPage 删除角色卡二次确认（useConfirm）", () => {
  it("点『删除』弹确认框，点『确认删除』才调用 DELETE", async () => {
    const { fireEvent } = await import("@testing-library/react");
    render(<RolesPage />);
    await screen.findByText("测试角色");
    fireEvent.click(screen.getByText("删除"));
    expect(await screen.findByText("删除这个角色卡？")).toBeTruthy();
    fireEvent.click(screen.getByText("确认删除"));
    await waitFor(() => expect(apiMock.del).toHaveBeenCalledWith("/api/roles/r1"));
  });

  it("点『取消』不调用 DELETE", async () => {
    const { fireEvent } = await import("@testing-library/react");
    render(<RolesPage />);
    await screen.findByText("测试角色");
    fireEvent.click(screen.getByText("删除"));
    expect(await screen.findByText("删除这个角色卡？")).toBeTruthy();
    fireEvent.click(screen.getByText("取消"));
    await new Promise((r) => setTimeout(r, 20));
    expect(apiMock.del).not.toHaveBeenCalled();
  });
});

describe("知识作用域 chips：只有 ✕ 删（2026-10-29 误删修复的回归钉）", () => {
  it("点区块标题与 chip 的名字都不删；点某个 chip 的 ✕ 只删那一个", async () => {
    // 根因：整块曾被一个 <label> 包住 —— 浏览器把"点这里任何非控件处"转发给 label 里
    // 第一个可标注后代，正是第一个 chip 的 ✕。用户报"没点 X、点了那一带，每点一次删一个"。
    // 现在这块是 <div>；这条用例钉住"点空白不删 + 点 ✕ 精确删那一个"两半。
    const WITH_SCOPES = { ...ROLE, knowledge_scopes: ["alpha", "beta"] };
    apiMock.get.mockImplementation(async (url: string) => {
      if (url === "/api/roles") return [WITH_SCOPES];
      if (url === "/api/tools/catalog") return { kernel: [], domains: {} };
      if (url === "/api/settings/models") return { default: "", providers: [], fallbacks: [] };
      if (url === "/api/knowledge/scopes") return { scopes: [] };
      return {};
    });
    const { fireEvent } = await import("@testing-library/react");
    render(<RolesPage />);
    await screen.findByText("测试角色");
    fireEvent.click(screen.getByText("编辑"));
    await screen.findByText("alpha");

    // ① 点标题、点 chip 的名字本体 —— 两个作用域都得原样在。
    // （标题用整句钉：页头副标题里也含"知识作用域"四个字，裸词正则两边都命中。）
    fireEvent.click(screen.getByText(/声明可检索的范围/));
    expect(screen.getByText("alpha")).toBeTruthy();
    expect(screen.getByText("beta")).toBeTruthy();
    fireEvent.click(screen.getByText("alpha"));
    expect(screen.getByText("alpha")).toBeTruthy();
    expect(screen.getByText("beta")).toBeTruthy();

    // ② 点 ✕ 只删那一个，另一个不动（"每点一次删一个"的反面是这个）。
    //    断言钉 chip 层（✕ 按钮 + chip 名字消失）而不是裸文本：作用域名会留在
    //    "从已有作用域添加"的下拉里（可选列表 = 现存角色声明并集，这条本来就是设计）。
    fireEvent.click(screen.getByTitle("移除作用域 alpha"));
    expect(screen.queryByTitle("移除作用域 alpha")).toBeNull();
    expect(screen.getByTitle("移除作用域 beta")).toBeTruthy();
  });
});

describe("角色级后端选择（用户 2026-09-25：角色级 > 设置里那份优先级）", () => {
  it("下拉按全局优先级排、标出第一档，只列参与对话的模型", async () => {
    // 供应商分组的原始顺序故意打乱：这里要看的不是"有哪些后端"，而是"我在盖过谁"。
    apiMock.get.mockImplementation(async (url: string) => {
      if (url === "/api/roles") return [ROLE];
      // 编辑表单会渲染工具白名单那两块（形状是 `{kernel, domains}`，`tools` 那个键不存在）。
      if (url === "/api/tools/catalog") return { kernel: [], domains: {} };
      if (url === "/api/settings/models")
        return {
          default: "c-first",
          fallbacks: ["b-second", "a-third"],
          providers: [
            {
              models: [
                { name: "a-third", used_by: ["chat"] },
                { name: "embed-only", used_by: ["embedding"] },
                { name: "c-first", used_by: ["chat"] },
                { name: "b-second", used_by: ["chat"] },
              ],
            },
          ],
        };
      if (url === "/api/knowledge/scopes") return { scopes: [] };
      return {};
    });
    const { fireEvent } = await import("@testing-library/react");
    render(<RolesPage />);
    await screen.findByText("测试角色");
    fireEvent.click(screen.getByText("编辑"));
    // 2026-10-29：那段"顺序按服务页优先级排"的说明从 label 里挪到了输入框下面独占一行
    // （修"长注释把两列输入框顶歪"）；label 的可及名字现在 = "模型" + 那行短说明。
    const select = (await screen.findByLabelText(/角色级 —— 留空/)) as HTMLSelectElement;
    expect([...select.options].map((o) => o.value)).toEqual([
      "",
      "c-first",
      "b-second",
      "a-third",
    ]);
    expect(select.options[0].textContent).toBe("跟随全局优先级");
    expect(select.options[1].textContent).toContain("全局第一档");
  });
});

describe("工作区（2026-10-30 搬进每张角色卡内部，每个角色一份）", () => {
  it("在角色卡的编辑区里，读的是**这个角色**的那份目录；点「浏览…」开目录抽屉", async () => {
    apiMock.browseTree.mockResolvedValue({
      path: "D:/wk",
      parent: "D:/",
      entries: [{ name: "子目录", is_dir: true, size: null }],
      truncated: false,
    });
    const { fireEvent } = await import("@testing-library/react");
    render(<RolesPage />);
    await screen.findByText("测试角色");

    // 关键回归钉：**先得进入这张卡的编辑区**才看得到工作区 —— 从前它是一张
    // 页面底部的全局卡，现在它是"这张卡的一个属性"。不进编辑区就看不到。
    expect(screen.queryByDisplayValue("D:/wk")).toBeNull();
    fireEvent.click(screen.getByText("编辑"));

    expect(await screen.findByDisplayValue("D:/wk")).toBeTruthy();
    // per-role：读的是"这个角色"那一份，不是全局默认。
    expect(apiMock.getWorkspaceDir).toHaveBeenCalledWith("r1");
    fireEvent.click(screen.getByRole("button", { name: "浏览…" }));
    expect(await screen.findByText("选择工作区目录")).toBeTruthy();
    expect(screen.getByText("子目录")).toBeTruthy();
  });

  it("保存与清除都带着 role_id（否则改的会是全局默认那份）", async () => {
    apiMock.setWorkspaceDir.mockResolvedValue({ path: "D:/wk", overridden: true });
    apiMock.clearWorkspaceDir.mockResolvedValue({ path: "D:/default", overridden: false });
    const { fireEvent } = await import("@testing-library/react");
    const { within } = await import("@testing-library/react");
    render(<RolesPage />);
    await screen.findByText("测试角色");
    fireEvent.click(screen.getByText("编辑"));

    const input = (await screen.findByDisplayValue("D:/wk")) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "D:/new" } });
    // 「保存」在角色卡编辑表单里另有一枚，所以要把范围收进**工作区这一块**。
    // 定位方式：用工作区输入框所在的容器（它和按钮在同一行）。
    const wsRow = input.closest("div.flex.items-center.gap-2");
    expect(wsRow).toBeTruthy();
    fireEvent.click(within(wsRow as HTMLElement).getByRole("button", { name: "保存" }));
    await waitFor(() =>
      expect(apiMock.setWorkspaceDir).toHaveBeenCalledWith("D:/new", "r1"),
    );

    fireEvent.click(within(wsRow as HTMLElement).getByRole("button", { name: "清除" }));
    await waitFor(() =>
      expect(apiMock.clearWorkspaceDir).toHaveBeenCalledWith("r1"),
    );
  });
});
