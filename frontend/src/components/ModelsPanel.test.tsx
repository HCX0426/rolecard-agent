// @vitest-environment jsdom
//
// 模型页（凭据组卡片 + 添加抽屉 + 探测确认卡）的接线测试。
//
// 这一页的价值全在"说准"三件事，所以断言都钉在它们身上：
//   1. **key 只出现一次**（组头），模型行不许各带一份；
//   2. **三态徽章**：`视觉 ?` 可点且点下去是那张写清代价的确认卡，`✗` 不可点（它是结论不是入口）；
//   3. **门禁与红线**：没测连通过不许加入；图片只在"全部测试"那一支才发给供应商。
// 破坏性动作（删除）走 useConfirm，取消时一个请求都不发。

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
    getLocalService: vi.fn(),
  },
}));

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, api: apiMock };
});

import { ModelsPanel } from "./ModelsPanel";

const SF_GROUP = {
  id: "siliconflow",
  provider: "siliconflow",
  label: "硅基流动",
  base_url: "https://api.siliconflow.cn/v1",
  style: "openai",
  needs_key: true,
  has_key: true,
  key_masked: "sk-…wixj",
  models: [
    {
      name: "sf",
      model: "deepseek-ai/DeepSeek-V4-Flash",
      num_ctx: null,
      supports_vision: null,
      supports_tools: true,
      used_by: ["chat", "embedding"],
      is_default: false,
    },
    {
      name: "sf-vl",
      model: "Qwen/Qwen3-VL-30B-A3B-Instruct",
      num_ctx: null,
      supports_vision: false,
      supports_tools: null,
      used_by: ["chat"],
      is_default: true,
    },
  ],
};

const OLLAMA_GROUP = {
  id: "ollama",
  provider: "ollama",
  label: "本地 Ollama",
  base_url: "http://localhost:11434",
  style: "native",
  needs_key: false,
  has_key: false,
  key_masked: null,
  models: [
    {
      name: "qwen3-vl-8b",
      model: "qwen3-vl:8b",
      num_ctx: 8192,
      supports_vision: true,
      supports_tools: true,
      used_by: [],
      is_default: false,
    },
  ],
};

const CATALOG = [
  { id: "ollama", label: "本地 Ollama", needs_key: "0", base_url_hint: "", style: "native" },
  {
    id: "siliconflow",
    label: "硅基流动",
    needs_key: "1",
    base_url_hint: "https://api.siliconflow.cn/v1",
    style: "openai",
  },
];

const SETTINGS = { default: "sf-vl", fallbacks: ["sf"], providers: [SF_GROUP, OLLAMA_GROUP] };

beforeEach(() => {
  vi.clearAllMocks();
  apiMock.get.mockImplementation(async (url: string) => {
    if (url === "/api/settings/models") return SETTINGS;
    if (url === "/api/settings/model-providers") return { providers: CATALOG };
    return {};
  });
  apiMock.getLocalService.mockResolvedValue({
    base_url: "http://localhost:11434",
    running: true,
    resident: [],
    resident_bytes: 0,
    pinned: false,
    is_local: false,
    model: "sf-vl",
  });
});

describe("分组卡片：一屏读懂凭据 / 模型 / 谁在用", () => {
  it("key 只在组头出现一次，模型行不再各带凭据", async () => {
    render(<ModelsPanel />);
    expect(await screen.findByText("硅基流动")).toBeTruthy();
    // 组头那一行同时给出端点与掩码 key
    expect(screen.getByText(/key 已存 sk-…wixj/)).toBeTruthy();
    // 整个页面里掩码只出现一次（拆层前是每行一份）
    expect(screen.getAllByText(/sk-…wixj/)).toHaveLength(1);
    // 本地组明确说"无需 key"，不是"未配置"那种吓人的口径
    expect(screen.getByText(/本机程序，无需 key/)).toBeTruthy();
  });

  it("used_by 只读回显，并提供去服务页调整的动作", async () => {
    const onOpenServices = vi.fn();
    render(<ModelsPanel onOpenServices={onOpenServices} />);
    await screen.findByText("硅基流动");
    expect(screen.getAllByText("对话")).toHaveLength(2);
    expect(screen.getByText("嵌入")).toBeTruthy();
    expect(screen.getByText(/这家还用于：嵌入/)).toBeTruthy();
    expect(screen.getByText("第 1 位")).toBeTruthy(); // sf-vl 是对话默认
    fireEvent.click(screen.getAllByText(/在服务页调整用途/)[0]);
    expect(onOpenServices).toHaveBeenCalled();
  });

  it("没被任何服务引用的行显示「未使用」，而不是假装用于对话", async () => {
    render(<ModelsPanel />);
    await screen.findByText("本地 Ollama");
    expect(screen.getByText("未使用")).toBeTruthy();
  });

  it("一个供应商都没有时给空态与添加入口", async () => {
    apiMock.get.mockImplementation(async (url: string) => {
      if (url === "/api/settings/models")
        return { default: null, fallbacks: [], providers: [] };
      if (url === "/api/settings/model-providers") return { providers: CATALOG };
      return {};
    });
    render(<ModelsPanel />);
    expect(await screen.findByText(/还没有配置任何模型/)).toBeTruthy();
    expect(screen.getAllByText("+ 添加模型").length).toBeGreaterThan(0);
  });

  it("加载中显示骨架，不显示空态文案（免得以为没配）", () => {
    apiMock.get.mockImplementation(() => new Promise(() => {})); // 永不 resolve
    render(<ModelsPanel />);
    expect(screen.queryByText(/还没有配置任何模型/)).toBeNull();
    expect(document.querySelectorAll(".animate-pulse").length).toBeGreaterThan(0);
  });
});

describe("能力徽章三态与探测确认卡", () => {
  it("? 可点（没测过），点开那张写清代价的确认卡", async () => {
    render(<ModelsPanel />);
    const unknown = await screen.findByText("视觉 ?");
    fireEvent.click(unknown);
    expect(await screen.findByText(/探测 deepseek-ai\/DeepSeek-V4-Flash 的能力？/)).toBeTruthy();
    // 代价必须在点之前就说清：几次调用 + 图片会不会离开本机
    expect(screen.getByText(/消耗配额/)).toBeTruthy();
    expect(screen.getByText(/16×16 的测试图片/)).toBeTruthy();
    expect(screen.getByText("只测工具")).toBeTruthy();
    expect(screen.getByText("全部测试")).toBeTruthy();
  });

  it("✗ 与 ✓ 是结论不是入口（不可点）", async () => {
    render(<ModelsPanel />);
    const settled = await screen.findByText("视觉 ✗");
    expect(settled.closest("button")).toBeNull();
    for (const badge of screen.getAllByText("工具 ✓")) expect(badge.closest("button")).toBeNull();
  });

  it("「只测工具」= 一次调用、不外发图片；结论写回能力位", async () => {
    apiMock.post.mockResolvedValue({
      reachable: true,
      detail: "",
      model_listed: true,
      tools: true,
      vision: null,
      vision_source: "not-tested",
      calls_used: 1,
      models: [],
    });
    apiMock.patch.mockResolvedValue({});
    render(<ModelsPanel />);
    fireEvent.click(await screen.findByText("视觉 ?"));
    fireEvent.click(screen.getByText("只测工具"));
    await waitFor(() =>
      expect(apiMock.post).toHaveBeenCalledWith(
        "/api/settings/models/probe",
        expect.objectContaining({ provider_id: "siliconflow", test_tools: true }),
      ),
    );
    const body = apiMock.post.mock.calls.find((c) => c[1]?.test_tools !== undefined)?.[1];
    expect(body.test_vision).toBe(false); // 红线：这一支绝不上传图片
    await waitFor(() =>
      expect(apiMock.patch).toHaveBeenCalledWith(
        "/api/settings/models/sf/capabilities",
        { supports_tools: true },
      ),
    );
  });

  it("「全部测试」才允许把图片发出去", async () => {
    apiMock.post.mockResolvedValue({
      reachable: true,
      detail: "",
      model_listed: true,
      tools: false,
      vision: true,
      vision_source: "uploaded-image",
      calls_used: 2,
      models: [],
    });
    render(<ModelsPanel />);
    fireEvent.click(await screen.findByText("视觉 ?"));
    fireEvent.click(screen.getByText("全部测试"));
    await waitFor(() =>
      expect(apiMock.post).toHaveBeenCalledWith(
        "/api/settings/models/probe",
        expect.objectContaining({ test_tools: true, test_vision: true }),
      ),
    );
    await waitFor(() =>
      expect(apiMock.patch).toHaveBeenCalledWith(
        "/api/settings/models/sf/capabilities",
        { supports_tools: false, supports_vision: true },
      ),
    );
  });

  it("探测失败后徽章仍是 ?：后端 detail 原样显示，不自己编文案", async () => {
    apiMock.post.mockResolvedValue({
      reachable: false,
      detail: "ConnectError: 连不上",
      model_listed: null,
      tools: null,
      vision: null,
      vision_source: "not-tested",
      calls_used: 0,
      models: [],
    });
    render(<ModelsPanel />);
    fireEvent.click(await screen.findByText("视觉 ?"));
    fireEvent.click(screen.getByText("只测工具"));
    expect(await screen.findByText(/连不上/)).toBeTruthy();
    expect(apiMock.patch).not.toHaveBeenCalled();
  });
});

describe("删除模型：破坏性动作要点名后果", () => {
  it("确认框里点名「这行还被嵌入用着」，取消则一个请求都不发", async () => {
    render(<ModelsPanel />);
    await screen.findByText("硅基流动");
    fireEvent.click(screen.getAllByText("删除")[0]);
    expect(await screen.findByText(/删除模型「sf」？/)).toBeTruthy();
    expect(screen.getByText(/嵌入」用着/)).toBeTruthy();
    fireEvent.click(screen.getByText("取消"));
    await new Promise((r) => setTimeout(r, 20));
    expect(apiMock.del).not.toHaveBeenCalled();
  });

  it("确认后才 DELETE", async () => {
    apiMock.del.mockResolvedValue({});
    render(<ModelsPanel />);
    fireEvent.click((await screen.findAllByText("删除"))[0]);
    fireEvent.click(await screen.findByText("确认删除"));
    await waitFor(() => expect(apiMock.del).toHaveBeenCalledWith("/api/settings/models/sf"));
  });
});

describe("添加抽屉：测连是门禁", () => {
  async function openDrawer() {
    render(<ModelsPanel />);
    fireEvent.click(await screen.findByText("+ 添加模型"));
  }

  it("〔加入〕的禁用说明按缺什么给什么：先模型名、再测连", async () => {
    await openDrawer();
    const add = (await screen.findByText("加入")).closest("button");
    expect(add?.hasAttribute("disabled")).toBe(true);
    expect(screen.getByText(/先选或填一个模型名/)).toBeTruthy();
    fireEvent.change(screen.getByPlaceholderText(/qwen3-vl:8b/), {
      target: { value: "glm-4" },
    });
    expect(screen.getByText(/先测试通过才能加入/)).toBeTruthy();
  });

  it("选了已配好的供应商就不问 key（凭据在组上）", async () => {
    await openDrawer();
    await screen.findByText("添加模型");
    expect(screen.queryByPlaceholderText(/api_key/)).toBeNull();
  });

  it("测连通过后填入模型名即可加入，走的是逐条 POST", async () => {
    apiMock.post.mockImplementation(async (url: string) => {
      if (url === "/api/settings/models/probe") {
        return {
          reachable: true, detail: "", model_listed: true, tools: null, vision: null,
          vision_source: "not-tested", calls_used: 0, models: [],
        };
      }
      if (url === "/api/settings/models") return { added: { name: "siliconflow-glm-4" } };
      return {};
    });
    await openDrawer();
    fireEvent.change(await screen.findByPlaceholderText(/qwen3-vl:8b/), {
      target: { value: "glm-4" },
    });
    fireEvent.click(screen.getByText("测试连接"));
    await screen.findByText(/连接通过/);
    fireEvent.click(screen.getByText("加入"));
    await waitFor(() =>
      expect(apiMock.post).toHaveBeenCalledWith(
        "/api/settings/models",
        expect.objectContaining({ provider_id: "siliconflow", model: "glm-4" }),
      ),
    );
    // 没探测能力 → 不写 capability（保持 `?`），新行以"未测过"入库
    expect(apiMock.patch).not.toHaveBeenCalled();
  });

  it("测连失败：给出后端的原因，加入仍然禁用", async () => {
    apiMock.post.mockResolvedValue({
      reachable: false,
      detail: "401（https://api.siliconflow.cn/v1/models）",
      model_listed: null,
      tools: null,
      vision: null,
      vision_source: "not-tested",
      calls_used: 0,
      models: [],
    });
    await openDrawer();
    fireEvent.change(await screen.findByPlaceholderText(/qwen3-vl:8b/), {
      target: { value: "glm-4" },
    });
    fireEvent.click(screen.getByText("测试连接"));
    expect(await screen.findByText(/连接失败/)).toBeTruthy();
    expect(screen.getByText(/401/)).toBeTruthy();
    expect(screen.getByText("加入").closest("button")?.hasAttribute("disabled")).toBe(true);
  });
});
