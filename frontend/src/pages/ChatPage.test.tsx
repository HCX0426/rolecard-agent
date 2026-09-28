// @vitest-environment jsdom
//
// ChatPage 的**接线**测试：把 SSE 事件序列喂进去，断言界面上真的出现了该出现的东西。
//
// 为什么不能只测纯函数：`lib/stream.ts` 的归约再正确，只要组件把事件接错地方
// （用错状态、忘了清空、提示挂在会被覆盖的地方）用户看到的还是错的。这一层测的正是
// "事件 → 界面"的这段线。项目里出过的「写操作静默 422」就是同一类问题的前科：
// 单元逻辑都对，接错了没人知道。
//
// 覆盖三件此前完全没测的事：
//   1. 流式 token 真的逐字出现在气泡里，message_replace 真的**覆盖**它；
//   2. 工具调用卡片从"执行中"变成结果；
//   3. 上下文裁剪提示真的渲染出来（而且挂在不会被回放覆盖的位置）。

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ChatEvent } from "../api";
import { ToastProvider } from "../components/Toast";

// `vi.mock` 的工厂会被**提升到文件顶部**，因此它引用的变量必须同样被提升 ——
// 用 `vi.hoisted` 而不是普通 `const`，否则报 "Cannot access before initialization"。
const { apiMock, streamChatMock } = vi.hoisted(() => ({
  apiMock: {
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    setModelSampling: vi.fn(),
    del: vi.fn(),
    // 开口回话会把主动开口的红点一次标读（09-23 那条口径在控制台这一侧的接线）。
    markAllReachoutsRead: vi.fn(),
    extractRecord: vi.fn(),
    distillSession: vi.fn(),
  },
  streamChatMock: vi.fn(),
}));

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, api: apiMock, streamChat: streamChatMock };
});

// 必须在 vi.mock 之后导入（vi.mock 是提升的，这里只是可读性上的分组）。
import ChatPage from "./ChatPage";

/** 服务端会返回的历史（checkpoint 回放）。每个测试自己设，模拟"这一轮之后服务端的真相"。 */
let replay: {
  id?: string;
  role: string;
  content: string;
  name?: string;
  reasoning?: string;
  stopped?: boolean;
}[] = [];

/** 一份最小可用的后端应答集：会话列表 / 角色 / 模型设置 / 历史回放 / 上下文预算。 */
function stubMountCalls(
  contextOverride: { trimmed: number; kept: number; budget: number } = {
    trimmed: 0,
    kept: 0,
    budget: 24000,
  },
) {
  apiMock.get.mockImplementation(async (url: string) => {
    if (url === "/api/sessions") return [];
    if (url === "/api/roles") return [];
    if (url === "/api/settings/models") return { default: "local", providers: [], fallbacks: [] };
    if (url === "/api/settings/model-providers") return { providers: [] };
    if (url.includes("/messages")) return { messages: replay, total: replay.length, limit: 500, truncated: false };
    if (url.endsWith("/context")) return contextOverride;
    if (url.startsWith("/api/session/")) return { model_name: null };
    return {};
  });
  apiMock.markAllReachoutsRead.mockResolvedValue({ items: [], unread: 0 });
  apiMock.post.mockImplementation(async (url: string) => {
    if (url === "/api/session") return { thread_id: "s_test" };
    return {};
  });
}

/** 让 streamChat 依次回调给定事件（模拟一次完整的流）。 */
function scriptedStream(events: ChatEvent[]) {
  streamChatMock.mockImplementation(
    async (_tid: string, _msg: string, onEvent: (e: ChatEvent) => void) => {
      for (const e of events) onEvent(e);
    },
  );
}

async function sendMessage(text: string) {
  const { fireEvent } = await import("@testing-library/react");
  fireEvent.change(screen.getByPlaceholderText(/输入消息/), { target: { value: text } });
  fireEvent.click(screen.getByRole("button", { name: "发送" }));
}

beforeEach(() => {
  vi.clearAllMocks();
  replay = [];
  stubMountCalls();
});

describe("ChatPage 流式渲染", () => {
  it("流结束后以服务端回放为准：用户消息与权威文本都在", async () => {
    // 关键设计契约：**checkpoint 是唯一真相**。流过程中的乐观气泡（含 message_replace
    // 的覆盖结果）在收尾时整体让位给服务端历史 —— 所以断言最终 DOM 里出现的是服务端的
    // 权威文本，而不是流式过程中拼出来的那份。
    replay = [
      { role: "user", content: "问一句" },
      { role: "assistant", content: "权威文本" },
    ];
    scriptedStream([
      { type: "start", role: { role_id: "general_assistant", role_name: "通用助手" } },
      { type: "token", text: "你" },
      { type: "token", text: "好" },
      { type: "message_replace", text: "权威文本" },
      { type: "end" },
    ]);

    render(<ToastProvider><ChatPage /></ToastProvider>);
    await sendMessage("问一句");

    expect(await screen.findByText("问一句")).toBeTruthy();
    expect(await screen.findByText("权威文本")).toBeTruthy();
  });

  it("在控制台发一句话 = 她那些主动开口都看过了（红点跟着清）", async () => {
    // 用户在 09-23 定的口径是"进入对话界面/在回话 = 都看过了"。桌宠面板与抽屉早就走
    // `read-all` 了，控制台这一侧漏接的症状是：我明明在这儿聊，铃铛上别的角色还在闪。
    replay = [{ role: "user", content: "在的" }];
    scriptedStream([{ type: "end" }]);
    render(<ToastProvider><ChatPage /></ToastProvider>);
    await sendMessage("在的");
    await waitFor(() => expect(apiMock.markAllReachoutsRead).toHaveBeenCalledOnce());
  });

  it("回放里带 stopped 标记的那条回答，刷新后仍显示『被叫停』（R26-13 尾）", async () => {
    // 标记随 checkpoint 落库：这句提示原先活在页面 state 上，一刷新就丢 —— 而
    // "跟着历史走"才是它本来该有的性质（与 reasoning 随消息回放是同一条道理）。
    replay = [
      { role: "user", content: "讲个长故事" },
      { role: "assistant", content: "说到一半的", stopped: true },
    ];
    scriptedStream([{ type: "end" }]);
    render(<ToastProvider><ChatPage /></ToastProvider>);
    await sendMessage("讲个长故事");
    expect(await screen.findByText(/这一轮是被叫停的/)).toBeTruthy();
    // 全局的旧提示已删：只有按轮那一份，不许同一个事实说两遍。
    expect(screen.getAllByText(/这一轮是被叫停的/)).toHaveLength(1);
  });

  it("正常收尾的回答不显示『被叫停』（不冤枉一句完整的话）", async () => {
    replay = [
      { role: "user", content: "问" },
      { role: "assistant", content: "完整的回答" },
    ];
    scriptedStream([{ type: "end" }]);
    render(<ToastProvider><ChatPage /></ToastProvider>);
    await sendMessage("问");
    expect(await screen.findByText("完整的回答")).toBeTruthy();
    expect(screen.queryByText(/这一轮是被叫停的/)).toBeNull();
  });

  it("工具调用先显示执行中，结果到达后显示内容", async () => {
    let seenRunning = false;
    streamChatMock.mockImplementation(
      async (_tid: string, _msg: string, onEvent: (e: ChatEvent) => void) => {
        onEvent({ type: "tool_call", name: "query_health_record", args: {} });
        // 此时组件应已渲染出"执行中…"
        await Promise.resolve();
        seenRunning = !!document.body.textContent?.includes("执行中");
        onEvent({
          type: "tool_result",
          name: "query_health_record",
          content: "6.0 mm【未经人工校验】",
        });
        onEvent({ type: "end" });
      },
    );

    render(<ToastProvider><ChatPage /></ToastProvider>);
    await sendMessage("查一下");

    await waitFor(() => expect(streamChatMock).toHaveBeenCalledOnce());
    expect(seenRunning).toBe(true);
  });

  it("context_trimmed 会在输入框上方渲染提示，并说明记录没有丢", async () => {
    replay = [
      { role: "user", content: "长会话里的一问" },
      { role: "assistant", content: "答" },
    ];
    scriptedStream([
      { type: "token", text: "答" },
      { type: "context_trimmed", dropped: 12, kept: 6 },
      { type: "end" },
    ]);

    render(<ToastProvider><ChatPage /></ToastProvider>);
    await sendMessage("长会话里的一问");

    const notice = await screen.findByText(/早期对话已折叠/);
    expect(notice.textContent).toContain("12");
    expect(notice.textContent).toContain("6");
    expect(notice.textContent).toContain("没有丢失");
    // 提示挂在消息区之外、不会被回放替换；而且可以关掉（不是永久挡在输入框上）
    expect(screen.getByRole("button", { name: "知道了" })).toBeTruthy();
  });

  it("刷新后的会话（checkpoint 里 trimmed>0）同样显示提示", async () => {
    stubMountCalls({ trimmed: 9, kept: 3, budget: 24000 });
    apiMock.get.mockImplementation(async (url: string) => {
      if (url === "/api/sessions") return [{ thread_id: "s_old", title: "旧会话", role_id: "r" }];
      if (url === "/api/roles") return [];
      if (url === "/api/settings/models") return { default: "local", providers: [], fallbacks: [] };
      if (url === "/api/settings/model-providers") return { providers: [] };
      if (url.endsWith("/messages")) return { messages: [], total: 0, limit: 500, truncated: false };
      if (url.endsWith("/context")) return { trimmed: 9, kept: 3, budget: 24000 };
      if (url.startsWith("/api/session/")) return { model_name: null };
      return {};
    });

    render(<ToastProvider><ChatPage /></ToastProvider>);
    const { fireEvent } = await import("@testing-library/react");
    // 从侧栏点开历史会话
    fireEvent.click(await screen.findByText("旧会话"));

    expect(await screen.findByText(/早期对话已折叠/)).toBeTruthy();
  });

  it("thinking 事件渲染为可折叠的思考面板（AI IDE 式）", async () => {
    replay = [
      { role: "user", content: "带思考的一问" },
      { role: "assistant", content: "权威回答" },
    ];
    scriptedStream([
      { type: "thinking", text: "先想：这个问题涉及知识库。" },
      { type: "token", text: "权威回答" },
      { type: "end" },
    ]);

    render(<ToastProvider><ChatPage /></ToastProvider>);
    await sendMessage("带思考的一问");

    await waitFor(() => expect(streamChatMock).toHaveBeenCalledOnce());
    expect(screen.queryByText(/权威回答/)).toBeTruthy();
  });

  it("回答结束后思考过程**仍然保留**：回放的助手消息带 reasoning 就要再渲染一次", async () => {
    // 一轮结束前端会用 checkpoint 回放整体替换消息区、live 气泡被清空；
    // 思考若只存在 live 气泡里就会消失 —— 所以回放数据里的 reasoning 必须再渲染。
    replay = [
      { role: "user", content: "一问" },
      {
        role: "assistant",
        content: "回答正文",
        reasoning: "回放出来的思考：先比较大小再下结论。",
      },
    ];
    scriptedStream([{ type: "token", text: "回答正文" }, { type: "end" }]);

    render(<ToastProvider><ChatPage /></ToastProvider>);
    await sendMessage("一问");

    // 面板出现，且里面是回放出来的那段思考
    const summary = await screen.findByText(/^思考过程/);
    expect(summary).toBeTruthy();
    expect(await screen.findByText(/回放出来的思考/)).toBeTruthy();
    expect(screen.queryByText(/回答正文/)).toBeTruthy();
  });

  it("回放的思考面板默认折叠（翻历史时主体是回答），可点开且不带省略号", async () => {
    replay = [
      {
        role: "assistant",
        content: "答",
        reasoning: "想过了",
      },
    ];
    scriptedStream([{ type: "token", text: "答" }, { type: "end" }]);

    render(<ToastProvider><ChatPage /></ToastProvider>);
    await sendMessage("问");

    // 单步思考：直接是「思考过程」面板（不套「过程」外层），默认折叠
    const summary = await screen.findByText(/^思考过程/);
    const details = summary.closest("details");
    expect(details?.hasAttribute("open")).toBe(false);
    summary.click();
    expect(await screen.findByText(/想过了/)).toBeTruthy();
  });

  it("error 事件让用户真的看得到提示（气泡会被回放冲掉，所以必须走 toast）", async () => {
    replay = [{ role: "user", content: "会失败的请求" }]; // 服务端只存下了用户那一句
    scriptedStream([
      { type: "tool_call", name: "t", args: {} },
      { type: "error", detail: "模型调用失败，请稍后重试或换一种问法。" },
      { type: "end" },
    ]);

    render(<ToastProvider><ChatPage /></ToastProvider>);
    await sendMessage("会失败的请求");

    // 关键断言：错误文案出现在**常驻的 toast** 上，而不是只写进随后就被清掉的气泡。
    // 这是写这组组件测试时发现的真实缺陷：错误信息原本一闪即逝，用户看不到任何提示。
    const toast = await screen.findByText(/回答中断/);
    expect(toast.textContent).toContain("模型调用失败");
  });

  it("切对话要退出删除模式（勾选属于上一个对话，不能跟着过来）", async () => {
    // 勾选的是**消息 id**，而 id 属于某一个对话：在 A 里勾两条再切到 B，横幅还写着
    // "已选 2 条"、复选框却全空；点"删除所选"会把 A 的 id 发给 B（后端 404）。
    replay = [
      { role: "user", content: "一问" },
      { role: "assistant", content: "答" },
    ];
    scriptedStream([{ type: "token", text: "答" }, { type: "end" }]);
    apiMock.get.mockImplementation(async (url: string) => {
      if (url === "/api/sessions") return [{ thread_id: "s_old", title: "旧会话", role_id: "r" }];
      if (url === "/api/roles") return [];
      if (url === "/api/settings/models") return { default: "local", providers: [], fallbacks: [] };
      if (url === "/api/settings/model-providers") return { providers: [] };
      if (url.includes("/messages")) return { messages: replay, total: replay.length, limit: 500, truncated: false };
      if (url.endsWith("/context")) return { trimmed: 0, kept: 0, budget: 24000 };
      return { model_name: null };
    });

    render(<ToastProvider><ChatPage /></ToastProvider>);
    await sendMessage("一问");

    const { fireEvent } = await import("@testing-library/react");
    fireEvent.click(screen.getByRole("button", { name: "删除对话" }));
    expect(await screen.findByRole("button", { name: "退出删除模式" })).toBeTruthy();

    fireEvent.click(await screen.findByText("旧会话"));

    // 切过去之后必须已退出删除模式（勾选、编辑态一起清掉）
    await waitFor(() => expect(screen.queryByRole("button", { name: "退出删除模式" })).toBeNull());
    expect(screen.getByRole("button", { name: "删除对话" })).toBeTruthy();
  });
});

describe("ChatPage 深链打开会话（收件箱「打开对话并回复」）", () => {
  it("按普通会话路径载入指定线程，并把深链意图回销", async () => {
    // 主动消息现在落在"该角色的主动会话"里，跳进来的目的就是接着谈 —— 所以它必须走
    // 普通会话那条载入路径（历史 / 明细 / 上下文预算），而不是另造一套"主动消息视图"。
    replay = [{ role: "assistant", content: "今天腰还酸吗？" }];
    const onUsed = vi.fn();
    render(
      <ChatPage deepThread="s_proactive_general_assistant" onDeepThreadUsed={onUsed} />,
    );
    await waitFor(() => expect(onUsed).toHaveBeenCalled());
    expect(apiMock.get).toHaveBeenCalledWith(
      "/api/session/s_proactive_general_assistant/messages",
    );
    expect(await screen.findByText("今天腰还酸吗？")).toBeTruthy();
  });
});

describe("ChatPage 删除二次确认（useConfirm）", () => {
  function stubWithSession() {
    apiMock.get.mockImplementation(async (url: string) => {
      if (url === "/api/sessions") return [{ thread_id: "s1", title: "会话A", role_id: "r" }];
      if (url === "/api/roles") return [];
      if (url === "/api/settings/models") return { default: "local", providers: [], fallbacks: [] };
      if (url === "/api/settings/model-providers") return { providers: [] };
      if (url.endsWith("/messages")) return { messages: [], total: 0, limit: 500, truncated: false };
      if (url.endsWith("/context")) return { trimmed: 0, kept: 0, budget: 24000 };
      if (url.startsWith("/api/session/")) return { model_name: null };
      return {};
    });
  }

  it("删除会话：点 ✕ 弹确认框，点『删除』才调用 DELETE", async () => {
    const { fireEvent } = await import("@testing-library/react");
    stubWithSession();

    render(<ToastProvider><ChatPage /></ToastProvider>);
    fireEvent.click(await screen.findByText("✕"));
    expect(await screen.findByText("删除这个对话？")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    await waitFor(() => expect(apiMock.del).toHaveBeenCalledWith("/api/session/s1"));
  });

  it("删除会话：点『取消』不调用 DELETE", async () => {
    const { fireEvent } = await import("@testing-library/react");
    stubWithSession();

    render(<ToastProvider><ChatPage /></ToastProvider>);
    fireEvent.click(await screen.findByText("✕"));
    expect(await screen.findByText("删除这个对话？")).toBeTruthy();
    fireEvent.click(screen.getByText("取消"));
    await new Promise((r) => setTimeout(r, 20));
    expect(apiMock.del).not.toHaveBeenCalled();
  });

  it("多选删除消息：勾选后点『删除所选』弹确认框，点『确认删除』才调用 DELETE", async () => {
    const { fireEvent } = await import("@testing-library/react");
    replay = [
      { id: "m1", role: "user", content: "一问" },
      { id: "m2", role: "assistant", content: "答" },
    ];
    scriptedStream([{ type: "token", text: "答" }, { type: "end" }]);

    render(<ToastProvider><ChatPage /></ToastProvider>);
    await sendMessage("一问");
    // 消息已渲染（用户问 + 助手答）
    expect(await screen.findByText("一问")).toBeTruthy();
    // 进入多选删除模式（无会话时，只有这个『删除对话』按钮）
    fireEvent.click(screen.getByRole("button", { name: "删除对话" }));
    // 进入选择模式后，每条消息左侧出现勾选框（这也是 selectMode 生效的信号）
    const boxes = await screen.findAllByRole("checkbox");
    expect(boxes.length).toBeGreaterThan(0);
    // 删除模式下铅笔必须收掉：它（-left-9）与勾选框（-left-7）叠在同一片像素上，而
    // opacity-0 的元素照样吃点击 —— 真机实测勾不上（09-28 浏览器探针抓出来的）。
    expect(screen.queryAllByLabelText("编辑并重答")).toEqual([]);
    fireEvent.click(boxes[0]);
    fireEvent.click(await screen.findByRole("button", { name: "删除所选" }));
    expect(await screen.findByText("删除选中的消息？")).toBeTruthy();
    fireEvent.click(screen.getByText("确认删除"));
    await waitFor(() =>
      expect(apiMock.post).toHaveBeenCalledWith(
        "/api/session/s_test/messages/delete",
        expect.any(Object),
      ),
    );
  });
});

describe("ChatPage 模型菜单能力位徽章（Batch 6：supports_tools）", () => {
  function stubBackends(rows: Record<string, unknown>[], groupProvider = "openai") {
    // 用例按"行"写最省事，这里统一包成一个凭据组：契约只有分组这一个视图，
    // 参与对话 = used_by 含 chat（不再有 usage 字段可筛）。
    const providers = [
      {
        id: "g", provider: groupProvider, label: "OpenAI 兼容", base_url: null,
        style: groupProvider === "ollama" ? "native" : "openai",
        needs_key: true, has_key: true, key_masked: "sk-…x",
        models: rows.map((r) => ({
          name: r.name, model: r.model, num_ctx: r.num_ctx ?? null,
          supports_vision: r.supports_vision ?? null, supports_tools: r.supports_tools ?? null,
          repeat_penalty: r.repeat_penalty ?? null,
          frequency_penalty: r.frequency_penalty ?? null,
          presence_penalty: r.presence_penalty ?? null,
          used_by: ["chat"], is_default: false,
        })),
      },
    ];
    apiMock.get.mockImplementation(async (url: string) => {
      if (url === "/api/sessions") return [];
      if (url === "/api/roles") return [];
      if (url === "/api/settings/models") return { default: "local", providers, fallbacks: [] };
      if (url.endsWith("/messages")) return { messages: [], total: 0, limit: 500, truncated: false };
      if (url.endsWith("/context")) return { trimmed: 0, kept: 0, budget: 24000 };
      if (url.startsWith("/api/session/")) return { model_name: null };
      return {};
    });
  }

  async function openModelMenu() {
    const { fireEvent } = await import("@testing-library/react");
    render(<ToastProvider><ChatPage /></ToastProvider>);
    fireEvent.click(await screen.findByTitle(/切换本对话使用的模型/));
  }

  it("supports_tools=true 的后端在模型菜单显示「工具」徽章（纯展示能力位）", async () => {
    stubBackends([
      { name: "with_tools", provider: "openai", model: "gpt-x", supports_vision: false, supports_tools: true },
    ]);
    await openModelMenu();
    expect(await screen.findByText("gpt-x")).toBeTruthy();
    expect(screen.getByText("工具")).toBeTruthy();
  });

  it("supports_tools=false 的后端不显示「工具」徽章", async () => {
    stubBackends([
      { name: "no_tools", provider: "openai", model: "gpt-y", supports_vision: false, supports_tools: false },
    ]);
    await openModelMenu();
    // 菜单已开：该后端行可见，但没有「工具」徽章（避免把能力位这条事实复制到前端做 disable）
    expect(await screen.findByText("gpt-y")).toBeTruthy();
    expect(screen.queryByText("工具")).toBeNull();
  });

  it("采样那一栏：云端行根本没有「重复惩罚」，点档位走 PATCH", async () => {
    stubBackends([{ name: "sf", model: "deepseek-x" }]);
    await openModelMenu();
    fireEvent.click(await screen.findByTitle(/采样惩罚/));
    expect(await screen.findByText("频率惩罚")).toBeTruthy();
    expect(screen.findByText("存在惩罚")).toBeTruthy();
    // OpenAI 兼容体没有 repeat_penalty 这个标准字段：那一栏对云端**不出现**，
    // 而不是出现而后端 400 —— 看得见却存不进去的控件比没有控件更糟。
    expect(screen.queryByText("重复惩罚")).toBeNull();
    apiMock.setModelSampling.mockResolvedValue({
      name: "sf", repeat_penalty: null, frequency_penalty: 0.1, presence_penalty: null,
    });
    // 两行的档位数字重名（频率 0.1 与存在 0.1），按 DOM 序取第一行 = 频率惩罚。
    fireEvent.click(screen.getAllByRole("button", { name: "0.1" })[0]);
    await waitFor(() =>
      expect(apiMock.setModelSampling).toHaveBeenCalledWith("sf", { frequency_penalty: 0.1 }),
    );
  });

  it("本地（Ollama）行多一栏「重复惩罚」，徽章在已设时改口", async () => {
    stubBackends([{ name: "local", model: "qwen3-vl:8b", repeat_penalty: 1.2 }], "ollama");
    await openModelMenu();
    // 徽章只说"至少一栏不是默认"，具体值在面板里逐栏回显。
    expect(await screen.findByText("采样 ·已设 ▾")).toBeTruthy();
    fireEvent.click(screen.getByTitle(/采样惩罚/));
    expect(await screen.findByText("重复惩罚")).toBeTruthy();
    expect(screen.getAllByRole("button", { name: "1.2" }).length).toBeGreaterThan(0);
  });
});

describe("ChatPage 跟着服务端走（在桌宠上回一句，切回控制台就该看到）", () => {
  const THREAD = "s_x";

  /** 一条会话 + 一个"服务端的真相"。`bump()` 模拟**别处**（桌宠面板）往同一条线程里写字。 */
  function stubLiveSession() {
    let server: { id?: string; role: string; content: string }[] = [
      { id: "m1", role: "user", content: "第一句" },
    ];
    apiMock.get.mockImplementation(async (url: string) => {
      if (url === "/api/sessions")
        return [
          {
            thread_id: THREAD,
            title: "旧的",
            role_id: "ga",
            role_name: "通用助手",
            updated_at: "2026-09-26 04:00:00",
            agent_mode: "chat",
            is_proactive: false,
            is_blank: false,
          },
        ];
      if (url === "/api/roles") return [];
      if (url === "/api/settings/models") return { default: "local", providers: [], fallbacks: [] };
      if (url === "/api/settings/model-providers") return { providers: [] };
      if (url.includes("/messages")) {
        const probe = url.includes("limit=1");
        return {
          messages: probe ? server.slice(-1) : [...server],
          total: server.length,
          limit: probe ? 1 : 500,
          truncated: false,
        };
      }
      if (url.endsWith("/context")) return { trimmed: 0, kept: 0, budget: 24000 };
      if (url.startsWith(`/api/session/${THREAD}`)) return { model_name: null, agent_mode: "chat" };
      return {};
    });
    return {
      bump: () => {
        server = [...server, { id: "m2", role: "assistant", content: "她在桌宠上答的那句" }];
      },
      /** 全量重读的次数（探针是 ?limit=1，不算）。 */
      fullReads: () =>
        apiMock.get.mock.calls.filter(
          ([u]) => String(u).startsWith(`/api/session/${THREAD}/messages`) && !String(u).includes("limit=1"),
        ).length,
    };
  }

  async function mountAndOpen() {
    const h = stubLiveSession();
    render(
      <ToastProvider>
        <ChatPage />
      </ToastProvider>,
    );
    await vi.waitFor(() => expect(screen.getByText("旧的")).toBeTruthy());
    fireEvent.click(screen.getByText("旧的"));
    await vi.waitFor(() => expect(screen.getByText("第一句")).toBeTruthy());
    return h;
  }

  it("服务端条数一变就重读并画上新的那句；数字没变就一次都不重读", async () => {
    vi.useFakeTimers();
    try {
      const h = await mountAndOpen();
      const before = h.fullReads();
      await vi.advanceTimersByTimeAsync(12_000);
      // 什么都没变的 12 秒里：只发探针，不重读、不刷界面（那会清掉滚动位置与勾选状态）
      expect(h.fullReads()).toBe(before);

      h.bump();
      await vi.advanceTimersByTimeAsync(6_000);
      expect(h.fullReads()).toBeGreaterThan(before);
      expect(screen.getByText("她在桌宠上答的那句")).toBeTruthy();
    } finally {
      vi.useRealTimers();
    }
  });
});

/** 09-27 真库形状（`s_proactive_elysia`）：一问 + 她的答 + 一小时后她**主动**又开口的那句，
 * 三条同在一轮里。从前渲染面一段只留一个回答 ⇒ 先写的那句被后写的覆盖（用户在对话界面
 * 里看不见它，而桌宠气泡读的是原始消息列表，所以只有这一侧丢），而那一栏「耗时」取的是
 * 段首的用户消息 → 段尾的回答 ⇒ 她的主动开口被算成「这条回答耗时 62 分 34 秒」。
 * 两条症状一起钉。 */
describe("ChatPage 把主动开口画成单独一条（不吞上一问的答、不算跨一小时的耗时）", () => {
  const THREAD = "s_proactive_elysia";

  function stubProactiveThread() {
    const server = [
      { id: "e8c2", role: "user", content: "想你了", ts: "2026-09-27 12:30:31" },
      { id: "5761", role: "assistant", content: "这话一说出口", ts: "2026-09-27 12:30:34" },
      { id: "2f67", role: "assistant", content: "三月末的风", ts: "2026-09-27 13:33:05" },
    ];
    apiMock.get.mockImplementation(async (url: string) => {
      if (url === "/api/sessions")
        return [
          {
            thread_id: THREAD,
            title: "爱莉希雅 · 主动找你",
            role_id: "elysia",
            role_name: "爱莉希雅",
            updated_at: "2026-09-27 13:33:05",
            agent_mode: "chat",
            is_proactive: true,
            is_blank: false,
          },
        ];
      if (url === "/api/roles") return [];
      if (url === "/api/settings/models") return { default: "local", providers: [], fallbacks: [] };
      if (url === "/api/settings/model-providers") return { providers: [] };
      if (url.includes("/messages")) {
        const probe = url.includes("limit=1");
        return {
          messages: probe ? server.slice(-1) : [...server],
          total: server.length,
          limit: probe ? 1 : 500,
          truncated: false,
        };
      }
      if (url.endsWith("/context")) return { trimmed: 0, kept: 0, budget: 24000 };
      if (url.startsWith(`/api/session/${THREAD}`)) return { model_name: null, agent_mode: "chat" };
      return {};
    });
  }

  it("两句都在屏幕上，而「耗时」只跟着真的那一问", async () => {
    stubProactiveThread();
    render(
      <ToastProvider>
        <ChatPage />
      </ToastProvider>,
    );
    await vi.waitFor(() => expect(screen.getByText("爱莉希雅 · 主动找你")).toBeTruthy());
    fireEvent.click(screen.getByText("爱莉希雅 · 主动找你"));
    await vi.waitFor(() => expect(screen.getByText("这话一说出口")).toBeTruthy());
    expect(screen.getByText("三月末的风")).toBeTruthy();
    // 12:30:31 → 12:30:34 = 3 秒；那 62 分 34 秒是"她一小时后又开口"，不是这条回答花了这么久。
    expect(screen.getByText(/耗时 3 秒/)).toBeTruthy();
    expect(screen.queryByText(/耗时 \d+ 分/)).toBeNull();
  });

  it("「她主动说的」角标只挂在主动那句上（R26-40 ①）", async () => {
    stubProactiveThread();
    render(
      <ToastProvider>
        <ChatPage />
      </ToastProvider>,
    );
    await vi.waitFor(() => expect(screen.getByText("爱莉希雅 · 主动找你")).toBeTruthy());
    fireEvent.click(screen.getByText("爱莉希雅 · 主动找你"));
    await vi.waitFor(() => expect(screen.getByText("三月末的风")).toBeTruthy());

    // 这一段两句话都是助手说的，但只有一句是她**主动**说的（另一句前面有提问）。
    expect(screen.getAllByText("她主动说的")).toHaveLength(1);
    // **位置也要对**：光数个数咬不住"标到了回答那一段"（那也只有一个）。
    // 每一轮那个容器是 `div.group`（`key={turn.key}` 那个），角标必须落在主动那句里。
    const proactive = screen.getByText("三月末的风").closest(".group");
    const answered = screen.getByText("这话一说出口").closest(".group");
    expect(proactive?.textContent).toContain("她主动说的");
    expect(answered?.textContent).not.toContain("她主动说的");
  });
});

/** R26-38：桌宠那一轮**正在生成**的那半句要在对话界面里同步显出来。
 *
 * 量过的账（副本库 + 自起后端一轮 419 字的回答）：他那句 0.21 秒就可读，她那句要
 * 10.49 秒才进检查点，界面那个 5 秒网格把它推到 15.0 秒 —— 落后 12.1 秒里有 7.6 秒
 * 是"她正在说"这件事完全不可见。所以这里钉三件事：那一格画得出来、拍子真的收紧了、
 * 落地之后不留重影。 */
describe("ChatPage 镜像「她正在说的那半句」", () => {
  const THREAD = "s_m";

  /** 一条会话，服务端的"在飞那句"由测试自己拨。 */
  function stubMirror() {
    let server: { id?: string; role: string; content: string }[] = [
      { id: "m1", role: "user", content: "在桌宠上问的那句" },
    ];
    let inflight: { text: string } | null = null;
    let probes = 0;
    let turns = 0;
    apiMock.get.mockImplementation(async (url: string) => {
      if (url === "/api/sessions") {
        return [
          {
            thread_id: THREAD,
            title: "她那条",
            role_id: "ga",
            role_name: "通用助手",
            updated_at: "2026-09-26 04:00:00",
            agent_mode: "chat",
            is_proactive: true,
            is_blank: false,
          },
        ];
      }
      if (url === "/api/roles") return [];
      if (url === "/api/settings/models") return { default: "local", providers: [], fallbacks: [] };
      if (url === "/api/settings/model-providers") return { providers: [] };
      if (url.endsWith("/turn")) {
        turns += 1;
        return { inflight };
      }
      if (url.includes("/messages")) {
        const probe = url.includes("limit=1");
        if (probe) probes += 1;
        return {
          messages: probe ? server.slice(-1) : [...server],
          total: server.length,
          limit: probe ? 1 : 500,
          truncated: false,
          inflight,
        };
      }
      if (url.endsWith("/context")) return { trimmed: 0, kept: 0, budget: 24000 };
      if (url.startsWith(`/api/session/${THREAD}`)) return { model_name: null, agent_mode: "chat" };
      return {};
    });
    return {
      /** 她在说：登记里已经有这段了，但还没进检查点。 */
      speaking: (text: string) => {
        inflight = { text };
      },
      /** 说完了：整句落进历史，登记清空。 */
      committed: (text: string) => {
        inflight = null;
        server = [...server, { id: "m2", role: "assistant", content: text }];
      },
      /** 贵的那一读（`?limit=1`）打了几次。 */
      probeCount: () => probes,
      /** 便宜的那一读（`/turn`）打了几次。 */
      turnCount: () => turns,
    };
  }

  async function mountAndOpen() {
    const h = stubMirror();
    render(
      <ToastProvider>
        <ChatPage />
      </ToastProvider>,
    );
    await vi.waitFor(() => expect(screen.getByText("她那条")).toBeTruthy());
    fireEvent.click(screen.getByText("她那条"));
    await vi.waitFor(() => expect(screen.getByText("在桌宠上问的那句")).toBeTruthy());
    return h;
  }

  it("她在说的那半句 0.8 秒内就画出来，一个字还没投送时也有那一格", async () => {
    vi.useFakeTimers();
    try {
      const h = await mountAndOpen();
      // 前提：静默时那一格不存在（不是"画了个空的"）
      await vi.advanceTimersByTimeAsync(3_000);
      expect(screen.queryByTestId("inflight-mirror")).toBeNull();

      h.speaking("");
      await vi.advanceTimersByTimeAsync(1_000);
      expect(screen.getByTestId("inflight-mirror").textContent).toContain("她在说");

      h.speaking("今天想先把那几份报告");
      await vi.advanceTimersByTimeAsync(1_000);
      expect(screen.getByTestId("inflight-mirror").textContent).toContain("今天想先把那几份报告");
    } finally {
      vi.useRealTimers();
    }
  });

  it("整句落地后那一格收掉，不许和已提交的那条同时在场", async () => {
    vi.useFakeTimers();
    try {
      const h = await mountAndOpen();
      h.speaking("理一理，晚上再去跑步。");
      await vi.advanceTimersByTimeAsync(1_500);
      expect(screen.getByTestId("inflight-mirror")).toBeTruthy();

      h.committed("理一理，晚上再去跑步。");
      await vi.advanceTimersByTimeAsync(1_500);
      expect(screen.getByText("理一理，晚上再去跑步。")).toBeTruthy();
      expect(screen.queryByTestId("inflight-mirror")).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it("两路探针是分工：便宜那路每拍一次，贵那路只在到点与落地时付", async () => {
    vi.useFakeTimers();
    try {
      const h = await mountAndOpen();
      const turns0 = h.turnCount();
      const probes0 = h.probeCount();
      await vi.advanceTimersByTimeAsync(4_000);
      // 0.8 秒一拍：4 秒里便宜那路该问 4~5 次，贵那路最多一次（它 4.8 秒才轮到一次）
      expect(h.turnCount() - turns0).toBeGreaterThanOrEqual(4);
      expect(h.probeCount() - probes0).toBeLessThanOrEqual(1);

      // 她一说起来，贵那路不必等到点：落地那一拍立刻补一次，把真消息换进来
      h.speaking("说了一半");
      await vi.advanceTimersByTimeAsync(1_500);
      const beforeLanding = h.probeCount();
      h.committed("说了一半");
      await vi.advanceTimersByTimeAsync(1_500);
      expect(h.probeCount()).toBeGreaterThan(beforeLanding);
      expect(screen.queryByTestId("inflight-mirror")).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("ChatPage 提取精华（对话 → 该角色的记忆）", () => {
  /** 一份"有历史的会话"：头部那个按钮只有在**当前会话且读到了消息**时才该可点。 */
  function stubConversation(messages: { role: string; content: string }[]) {
    apiMock.get.mockImplementation(async (url: string) => {
      if (url === "/api/sessions") {
        return [{ thread_id: "s_d", title: "聊过的", role_id: "general_assistant", role_name: "通用助手", updated_at: "t" }];
      }
      if (url === "/api/roles") {
        return [{ role_id: "general_assistant", role_name: "通用助手", model_name: "" }];
      }
      if (url === "/api/settings/models") return { default: "local", providers: [], fallbacks: [] };
      if (url.endsWith("/messages")) {
        return { messages, total: messages.length, limit: 500, truncated: false };
      }
      if (url.endsWith("/context")) return { trimmed: 0, kept: 0, budget: 24000 };
      if (url.startsWith("/api/session/")) return { model_name: null, agent_mode: "chat" };
      return {};
    });
  }

  const report = {
    added: 2,
    similar: 0,
    updated: 0,
    merged: 0,
    invalidated: 0,
    noop: 0,
    skipped: 0,
    detail: "",
    tokens: 321,
    before: null,
    after: null,
  };

  /**
   * 打开这会话并点头部那个按钮 —— 但**每轮重新查**按钮再判断能不能点。
   *
   * 两个坑都在这里：历史是异步载入的（早一步点在禁用态上，等于点了空气）；而 `Button`
   * 禁用时会多套一层带说明的容器，翻成可用时那个 `<button>` 节点是**被换掉的**，攥着旧
   * 引用等下去只会永远 `disabled === true`。
   */
  async function openConversationAndClickDistill() {
    render(
      <ToastProvider>
        <ChatPage deepThread="s_d" onDeepThreadUsed={() => {}} />
      </ToastProvider>,
    );
    let btn!: HTMLButtonElement;
    await waitFor(() => {
      const found = screen.getByText("提取精华").closest("button");
      expect(found).not.toBeNull();
      btn = found as HTMLButtonElement;
      expect(btn.disabled).toBe(false);
    });
    fireEvent.click(btn);
    return btn;
  }

  it("点按钮打这一会话的提取端点，并把条数与成本说清楚", async () => {
    stubConversation([
      { role: "user", content: "我搬到苏州住了半年" },
      { role: "assistant", content: "苏州不错" },
    ]);
    apiMock.distillSession.mockResolvedValue({ report, turns_since: 0 });

    await openConversationAndClickDistill();

    await waitFor(() => expect(apiMock.distillSession).toHaveBeenCalledWith("s_d"));
    // 报的是"进了谁的记忆 + 几条 + 花多少"，不复读抽出来的事实（那归记忆卡看）
    expect(await screen.findByText(/已提取进通用助手的记忆：新增 2 条/)).toBeTruthy();
    expect(screen.getByText(/321 tokens/)).toBeTruthy();
  });

  it("像重复的那几条只提示、不自动合并，并把「整理记忆」指给用户", async () => {
    stubConversation([
      { role: "user", content: "我搬到苏州住了半年" },
      { role: "assistant", content: "苏州不错" },
    ]);
    apiMock.distillSession.mockResolvedValue({
      report: { ...report, added: 1, similar: 3 },
      turns_since: 0,
    });

    await openConversationAndClickDistill();

    expect(
      await screen.findByText(/新增 1 条 · 用去 321 tokens（另有 3 条字面上看着像同一件事/)
    ).toBeTruthy();
  });

  it("什么都没抽到时说清楚，而不是静默", async () => {
    stubConversation([
      { role: "user", content: "今天天气不错" },
      { role: "assistant", content: "是啊" },
    ]);
    apiMock.distillSession.mockResolvedValue({
      report: { ...report, added: 0, noop: 1 },
      turns_since: 0,
    });

    await openConversationAndClickDistill();
    expect(await screen.findByText(/没有值得新记的事实/)).toBeTruthy();
  });

  it("后端说「记忆是关的」→ 原文显示出来（里面带着去哪开）", async () => {
    stubConversation([
      { role: "user", content: "一问" },
      { role: "assistant", content: "一答" },
    ]);
    apiMock.distillSession.mockRejectedValue(
      new Error("跨会话记忆当前是关闭的 —— 先在「设置 → 记忆与任务目录」打开它。"),
    );

    await openConversationAndClickDistill();
    expect(await screen.findByText(/记忆与任务目录/)).toBeTruthy();
  });

  it("空对话时按钮禁用并说明「先聊几句」（不是点了没反应的死按钮）", async () => {
    stubConversation([]);
    render(
      <ToastProvider>
        <ChatPage deepThread="s_d" onDeepThreadUsed={() => {}} />
      </ToastProvider>,
    );
    const btn = (await screen.findByText("提取精华")).closest("button") as HTMLButtonElement;
    await waitFor(() => expect(btn.disabled).toBe(true));
    expect(screen.getByText("先聊几句再提取")).toBeTruthy();
  });
});

describe("ChatPage 换角色 = 进那条角色自己的对话（09-26：一条线程只属于一个说话人）", () => {
  const ROLES = [
    { role_id: "ga", role_name: "通用助手", is_builtin: true },
    { role_id: "ly", role_name: "玲", is_builtin: false },
  ];

  /** 一条已有会话（属于通用助手）+ 两个角色。换人之后要落到 `s_proactive_ly`。 */
  async function mountWithSession() {
    apiMock.get.mockImplementation(async (url: string) => {
      if (url === "/api/sessions")
        return [
          {
            thread_id: "s_old",
            title: "旧的",
            role_id: "ga",
            role_name: "通用助手",
            updated_at: "2026-09-26 04:00:00",
            agent_mode: "chat",
          },
        ];
      if (url === "/api/roles") return ROLES;
      if (url === "/api/settings/models") return { default: "local", providers: [], fallbacks: [] };
      if (url === "/api/settings/model-providers") return { providers: [] };
      if (url.endsWith("/messages")) return { messages: [], total: 0, limit: 500, truncated: false };
      if (url.endsWith("/context")) return { trimmed: 0, kept: 0, budget: 24000 };
      if (url.startsWith("/api/session/")) return { model_name: null, agent_mode: "chat" };
      return {};
    });
    apiMock.post.mockImplementation(async (url: string) =>
      url === "/api/session/proactive" ? { thread_id: "s_proactive_ly", role_id: "ly" } : { thread_id: "s_new" },
    );
    render(
      <ToastProvider>
        <ChatPage />
      </ToastProvider>,
    );
    await waitFor(() => expect(screen.getByText("旧的")).toBeTruthy());
  }

  function pickRole(name: string) {
    fireEvent.click(screen.getByTitle(/换个说话的人/));
    fireEvent.click(screen.getByRole("button", { name: new RegExp(name) }));
  }

  it("点另一个角色：ensure 她那一条并载入，**不再把当前线程改挂到她名下**", async () => {
    await mountWithSession();
    pickRole("玲");

    await waitFor(() =>
      expect(apiMock.post).toHaveBeenCalledWith("/api/session/proactive", { role_id: "ly" }),
    );
    // 载入走的是普通会话那条路径（历史 / 明细），不是另造一套视图
    expect(apiMock.get).toHaveBeenCalledWith("/api/session/s_proactive_ly/messages");
    // 这一条才是这次改法的实质：旧实现是一次 PATCH role_id，等于把两个说话人的话混进同一份历史
    const rolePatches = apiMock.patch.mock.calls.filter(
      ([, body]) => body && "role_id" in (body as Record<string, unknown>),
    );
    expect(rolePatches).toEqual([]);
  });

  it("这一轮还在跑的时候不许换人 —— 换线程会把屏幕上的那一轮清空", async () => {
    await mountWithSession();
    streamChatMock.mockImplementation(() => new Promise<void>(() => undefined));
    await sendMessage("还没说完的一句");

    pickRole("玲");
    await waitFor(() => expect(screen.getByText(/这一轮还在跑/)).toBeTruthy());
    expect(apiMock.post).not.toHaveBeenCalledWith("/api/session/proactive", expect.anything());
  });
});

describe("ChatPage 侧栏：每个角色一条固定线 + 临时话题批量清理（09-26）", () => {
  const ROLES = [
    { role_id: "ga", role_name: "通用助手", is_builtin: true },
    { role_id: "ly", role_name: "玲", is_builtin: false },
  ];

  function session(
    thread_id: string,
    title: string | null,
    role_id: string,
    extra: Record<string, unknown> = {},
  ) {
    return {
      thread_id,
      title,
      role_id,
      role_name: ROLES.find((r) => r.role_id === role_id)?.role_name ?? role_id,
      updated_at: "2026-09-26 04:00:00",
      agent_mode: "chat",
      ...extra,
    };
  }

  async function mountSidebar() {
    apiMock.get.mockImplementation(async (url: string) => {
      if (url === "/api/sessions")
        return [
          // 玲的固定线（已经有对话）
          session("s_proactive_ly", "玲 · 主动找你", "ly", { is_proactive: true, is_blank: false }),
          // 一条一个字都没写过的临时线程：不该再出现在列表里（旧实现靠"有没有标题"判，
          // 重命名过的空线程与深链刚建的线程都会被骗过去）
          session("s_blank", null, "ga", { is_proactive: false, is_blank: true }),
          session("s_t1", "这个是啥", "ga", { is_proactive: false, is_blank: false }),
          session("s_t2", "你好", "ga", { is_proactive: false, is_blank: false }),
        ];
      if (url === "/api/roles") return ROLES;
      if (url === "/api/settings/models") return { default: "local", providers: [], fallbacks: [] };
      if (url === "/api/settings/model-providers") return { providers: [] };
      if (url.endsWith("/messages")) return { messages: [], total: 0, limit: 500, truncated: false };
      if (url.endsWith("/context")) return { trimmed: 0, kept: 0, budget: 24000 };
      if (url.startsWith("/api/session/")) return { model_name: null, agent_mode: "chat" };
      return {};
    });
    apiMock.post.mockImplementation(async (url: string) =>
      url === "/api/session/proactive" ? { thread_id: "s_proactive_ga", role_id: "ga" } : { thread_id: "s_new" },
    );
    render(
      <ToastProvider>
        <ChatPage unreadByRole={{ ly: 3 }} />,
      </ToastProvider>,
    );
    await waitFor(() => expect(screen.getByText("这个是啥")).toBeTruthy());
  }

  it("「她们」一栏每个角色一行：没线的那个也列着，且固定行不给删除只给清空", async () => {
    await mountSidebar();
    // 通用助手一条线都还没有 —— 照样列着，副标题告诉你点下去会发生什么
    expect(screen.getByText("还没开始 · 点一下就在这里")).toBeTruthy();
    // 未读徽章是铃铛那次轮询的同一份数（不是自己再算一遍）
    expect(screen.getByTitle("3 条她主动找你，还没读")).toBeTruthy();
    // 空白线程不进列表
    expect(screen.queryByText("新对话")).toBeNull();
    // 删除按钮只属于那两条临时话题；固定行上一个都没有（清空另算）
    expect(screen.getAllByTitle("删除对话")).toHaveLength(2);
    expect(screen.getByTitle(/清空与玲的对话/)).toBeTruthy();
    // 「删除对话」那两类按钮都长在临时话题行上；固定行一个都没有（只有上面那个「清空」）
    expect(screen.queryByTitle("删除这个对话？")).toBeNull();
  });

  it("角色一多：抽屉里给搜索框，少的时候不给；筛完点谁都进她那条线", async () => {
    const many = Array.from({ length: 7 }, (_, i) => ({
      role_id: `r${i}`,
      role_name: `角色${i}号`,
      is_builtin: false,
    }));
    apiMock.get.mockImplementation(async (url: string) => {
      if (url === "/api/sessions") return [];
      if (url === "/api/roles") return many;
      if (url === "/api/settings/models") return { default: "local", providers: [], fallbacks: [] };
      if (url === "/api/settings/model-providers") return { providers: [] };
      if (url.includes("/messages")) return { messages: [], total: 0, limit: 500, truncated: false };
      if (url.endsWith("/context")) return { trimmed: 0, kept: 0, budget: 24000 };
      return {};
    });
    apiMock.post.mockResolvedValue({ thread_id: "s_proactive_r5", role_id: "r5" });
    render(
      <ToastProvider>
        <ChatPage />
      </ToastProvider>,
    );
    await waitFor(() => expect(screen.getByText("还没有角色，也还没有对话")).toBeTruthy());

    fireEvent.click(screen.getByTitle(/换个说话的人/));
    const box = await screen.findByLabelText("搜角色");
    fireEvent.change(box, { target: { value: "5" } });
    expect(screen.getByRole("button", { name: /角色5号/ })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /角色1号/ })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /角色5号/ }));
    await waitFor(() => expect(apiMock.post).toHaveBeenCalledWith("/api/session/proactive", { role_id: "r5" }));
  });

  it("角色不多时不出现搜索框（三五个角色不必为过滤付一个控件）", async () => {
    await mountSidebar();
    fireEvent.click(screen.getByTitle(/换个说话的人/));
    expect(screen.queryByLabelText("搜角色")).toBeNull();
  });

  it("点固定行 = 幂等 ensure 出她那条线再打开", async () => {
    await mountSidebar();
    fireEvent.click(screen.getByText("还没开始 · 点一下就在这里"));
    await waitFor(() => expect(apiMock.post).toHaveBeenCalledWith("/api/session/proactive", { role_id: "ga" }));
    expect(apiMock.get).toHaveBeenCalledWith("/api/session/s_proactive_ga/messages");
  });

  it("批量清理：全选一次点完，删的是逐条走已有端点，不新开后端口子", async () => {
    await mountSidebar();
    apiMock.del.mockResolvedValue(undefined);

    fireEvent.click(screen.getByText("批量清理"));
    fireEvent.click(screen.getByText("全选"));
    fireEvent.click(screen.getByRole("button", { name: "删除 2" }));
    const confirmBtn = await screen.findByRole("button", { name: "删除", hidden: true });
    fireEvent.click(confirmBtn);

    await waitFor(() => expect(apiMock.del).toHaveBeenCalledTimes(2));
    const ids = apiMock.del.mock.calls.map(([url]) => url);
    expect(ids).toContain("/api/session/s_t1");
    expect(ids).toContain("/api/session/s_t2");
    // 固定线绝对不在这批里
    expect(ids).not.toContain("/api/session/s_proactive_ly");
  });
});
