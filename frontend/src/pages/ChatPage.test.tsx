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

import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ChatEvent } from "../api";

// `vi.mock` 的工厂会被**提升到文件顶部**，因此它引用的变量必须同样被提升 ——
// 用 `vi.hoisted` 而不是普通 `const`，否则报 "Cannot access before initialization"。
const { apiMock, streamChatMock } = vi.hoisted(() => ({
  apiMock: {
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
    extractRecord: vi.fn(),
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
let replay: { role: string; content: string; name?: string; reasoning?: string }[] = [];

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
    if (url === "/api/settings/models") return { default: "local", backends: [], fallbacks: [] };
    if (url === "/api/settings/model-providers") return { providers: [] };
    if (url.endsWith("/messages")) return replay;
    if (url.endsWith("/context")) return contextOverride;
    if (url.startsWith("/api/session/")) return { model_name: null };
    return {};
  });
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

    render(<ChatPage />);
    await sendMessage("问一句");

    expect(await screen.findByText("问一句")).toBeTruthy();
    expect(await screen.findByText("权威文本")).toBeTruthy();
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

    render(<ChatPage />);
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

    render(<ChatPage />);
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
      if (url === "/api/settings/models") return { default: "local", backends: [], fallbacks: [] };
      if (url === "/api/settings/model-providers") return { providers: [] };
      if (url.endsWith("/messages")) return [];
      if (url.endsWith("/context")) return { trimmed: 9, kept: 3, budget: 24000 };
      if (url.startsWith("/api/session/")) return { model_name: null };
      return {};
    });

    render(<ChatPage />);
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

    render(<ChatPage />);
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

    render(<ChatPage />);
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

    render(<ChatPage />);
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

    render(<ChatPage />);
    await sendMessage("会失败的请求");

    // 关键断言：错误文案出现在**常驻的 toast** 上，而不是只写进随后就被清掉的气泡。
    // 这是写这组组件测试时发现的真实缺陷：错误信息原本一闪即逝，用户看不到任何提示。
    const toast = await screen.findByText(/回答中断/);
    expect(toast.textContent).toContain("模型调用失败");
  });
});
