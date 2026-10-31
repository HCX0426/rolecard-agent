// @vitest-environment jsdom
/**
 * 流式渲染的**稳态**基准（R1031-31，2026-10-31）。
 *
 * 为什么要有第二个基准（`turns.bench.test.tsx` 量的是首渲染）：
 * 那一支量"打开一条长会话要多久"，触发线是 >3s。本条量的是**另一个东西**：
 * 流式期间**每收到一个 token，那次提交里实际渲染了多久**。
 *
 * 那个数才是 R1031-31 的判据。症状不是"打开会话卡"，而是"她一边打字，屏幕上
 * 那一千条历史跟着一起重渲"——jsdom 里测不出"卡不卡"（它没有帧），但
 * **React.Profiler 给得出每次提交的真实渲染耗时**：历史行没被 memo 掉 ⇒ 每 token
 * 都要重渲 N 行，耗时随历史长度线性涨；memo 生效 ⇒ 每次提交只渲染在飞气泡，
 * 耗时与历史长度无关。**同一份代码、两种历史长度，两条读数一比就知道有没有被 memo。**
 *
 * 为什么用 Profiler 而不是往 TurnRow 里插计数探针：探针要改被测物（产品代码），
 * 而这个仓库的纪律是"判据不许改被测物"（同 benchmark 尺子与真库副本那条）——
 * Profiler 是 React 自带的观察面，零产品代码改动。
 *
 * 跑法：`BENCH=1 npx vitest run src/bench/streaming.bench.test.tsx`
 * 默认跳过（不占日常测试时长）—— 与 `turns.bench` 同款约定（用常量 `RUN` 而非
 * `process.env`，前端 tsconfig 的 types 只有 vite/client）。
 */
import { Profiler } from "react";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import type { ChatEvent } from "../api";
import { ToastProvider } from "../components/Toast";

// 批次 J 把 Markdown 改成懒加载后，coverage 模式下 lazy 的模块解析会慢过断言那一拍
// （同 ChatPage.test.tsx 的注释）：全套用例开始前先 await 一次这个动态 import。
beforeAll(async () => {
  await import("../components/Markdown");
});

const { apiMock, streamChatMock } = vi.hoisted(() => ({
  apiMock: {
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    setModelSampling: vi.fn(),
    del: vi.fn(),
    markAllReachoutsRead: vi.fn(),
    markRoleReachoutsRead: vi.fn(),
    extractRecord: vi.fn(),
    distillSession: vi.fn(),
  },
  streamChatMock: vi.fn(),
}));

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, api: apiMock, streamChat: streamChatMock };
});

import ChatPage from "../pages/ChatPage";

// 开关用显式常量而非 process.env（见文件头注释）。
const RUN = false;

/** 流式 token 数：一句话的典型长度。 */
const TOKENS = 60;

/** 服务端回放的历史：一问一答 N 轮。 */
function replayOf(rounds: number) {
  const out: { role: string; content: string }[] = [];
  for (let i = 0; i < rounds; i++) {
    out.push({ role: "user", content: `问 ${i}` });
    out.push({ role: "assistant", content: `答 ${i}` });
  }
  return out;
}

/** 每次提交的渲染耗时（ms）。Profiler 是 React 自带观察面，零产品代码改动。 */
type Commit = { actualDuration: number };

function stubApi(replay: { role: string; content: string }[]) {
  apiMock.get.mockImplementation(async (url: string) => {
    // 会话列表要有**一条**：没有它，挂载时不会去开任何会话，历史也就永远不载入
    // （history 只在 selectSession 之后才有 —— ChatPage 的 selectSession 才拉
    // /messages）。这一条是本基准能测到"长历史"的前提。
    if (url === "/api/sessions") return [{ thread_id: "s_test", title: "老会话", updated_at: "" }];
    if (url === "/api/roles") return [];
    if (url === "/api/settings/models") return { default: "local", providers: [], fallbacks: [] };
    if (url === "/api/settings/model-providers") return { providers: [] };
    if (url.includes("/messages")) {
      return { messages: replay, total: replay.length, limit: 500, truncated: false };
    }
    if (url.endsWith("/context")) return { trimmed: 0, kept: 0, budget: 24000 };
    if (url.startsWith("/api/session/")) return { model_name: null };
    return {};
  });
  apiMock.markAllReachoutsRead.mockResolvedValue({ items: [], unread: 0 });
  apiMock.markRoleReachoutsRead.mockResolvedValue({ items: [], unread: 0 });
  apiMock.post.mockImplementation(async (url: string) => {
    if (url === "/api/session") return { thread_id: "s_test" };
    return {};
  });
}

/** 发一句话并把整轮流跑完；返回那次流式期间的提交读数。 */
async function measureStreaming(rounds: number): Promise<{ commits: Commit[]; ms: number }> {
  vi.clearAllMocks();
  stubApi(replayOf(rounds));

  const events: ChatEvent[] = [
    { type: "start", role: { role_id: "general_assistant", role_name: "通用助手" } },
  ];
  for (let i = 0; i < TOKENS; i++) events.push({ type: "token", text: `字${i}` });
  events.push({ type: "message_replace", text: "最终回答" });
  events.push({ type: "end" });

  streamChatMock.mockImplementation(
    async (_tid: string, _msg: string, onEvent: (e: ChatEvent) => void) => {
      for (const e of events) {
        // **每个 token 之间让出一次事件循环** —— 真流式就是这样：token 是从网络
        // 一个个到的，每到一次就是一次独立的 React 提交。若在 mock 里一口气把 60 个
        // token 全喂完，React 会把它们**批成一次**提交（同一个同步任务里的多次
        // setState 合并），量出来的就不是"每 token 一次重渲"了 —— 那是本基准要问的
        // 那个问题。await 一个宏任务即可把提交切开（0ms 定时器）。
        if (e.type === "token") await new Promise((r) => setTimeout(r, 0));
        onEvent(e);
      }
    },
  );

  const commits: Commit[] = [];
  render(
    <ToastProvider>
      <Profiler id="chat" onRender={(_id, _phase, actualDuration) => commits.push({ actualDuration })}>
        <ChatPage />
      </Profiler>
    </ToastProvider>,
  );

  // **先暖流一次**：会话是在第一次发送时才建出来的，历史也是那之后才载入
  // （selectSession 才会拉 /messages）。不暖流，测的就是"空会话第一次说话"——
  // 那条路上没有历史行可重渲，量出来的东西与本基准要问的问题无关。
  await sendAndStream();

  // 历史到位后等它渲染出来（"问 0" 是第一轮的用户气泡）
  await screen.findByText("问 0");

  // 清掉暖流与首屏那些提交，只留**下面这次**的。
  commits.length = 0;

  // 关键：**不要**把整轮包进一个 act() —— 那会把 60 个 token 的 setState 合成
  // 一次提交（React 在同一个 act 作用域里把同步更新批成一次）。真流式是"一个
  // token 一次提交"，所以这里每个 token 各自 act 一次：量到的就是真机上那种
  // "每来一个字，历史重渲一遍"的提交成本。
  const t0 = performance.now();
  await act(async () => {
    const input = screen.getByPlaceholderText(/输入消息/) as HTMLTextAreaElement;
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLTextAreaElement.prototype,
      "value",
    )?.set;
    setter?.call(input, "测一下");
    input.dispatchEvent(new Event("input", { bubbles: true }));
    screen.getByRole("button", { name: "发送" }).click();
  });
  // 流还没跑完（mock 里每个 token 之间让出事件循环），等它跑完。
  await waitFor(() => expect(streamChatMock).toHaveBeenCalled());
  await new Promise((r) => setTimeout(r, 50));
  return { commits, ms: performance.now() - t0 };
}

/** 在输入框里打一句话并点发送（走真实的受控输入路径）。 */
async function sendAndStream() {
  const input = screen.getByPlaceholderText(/输入消息/) as HTMLTextAreaElement;
  const setter = Object.getOwnPropertyDescriptor(
    window.HTMLTextAreaElement.prototype,
    "value",
  )?.set;
  setter?.call(input, "测一下");
  input.dispatchEvent(new Event("input", { bubbles: true }));
  screen.getByRole("button", { name: "发送" }).click();
}

beforeEach(() => {
  vi.clearAllMocks();
});

(RUN ? describe : describe.skip)("流式稳态", () => {
  it("历史越长，每次提交越贵 = 历史行没被 memo 掉", async () => {
    // 两档历史长度各测一次：**同一份代码、同一个 token 数**，只有历史长度不同。
    // 若两次的每次提交耗时**大致相同** ⇒ 历史行已被 memo（重渲被挡住了）；
    // 若**随历史长度线性涨** ⇒ 每 token 都在重渲那 N 行（R1031-31 的病灶）。
    const small = await measureStreaming(10);
    cleanup(); // 两档之间必须清 DOM：第二档的 findByText 会撞上第一档留下的同名节点
    const big = await measureStreaming(200);
    cleanup();

    const avg = (c: Commit[]) =>
      c.length ? c.reduce((s, x) => s + x.actualDuration, 0) / c.length : 0;
    const a1 = avg(small.commits);
    const a2 = avg(big.commits);

    console.info(
      `[bench] ${TOKENS} token：10 轮历史 → ${small.commits.length} 次提交，` +
        `均 ${a1.toFixed(2)}ms/次；200 轮历史 → ${big.commits.length} 次提交，` +
        `均 ${a2.toFixed(2)}ms/次；墙钟 ${small.ms.toFixed(0)}ms / ${big.ms.toFixed(0)}ms`,
    );
    console.info(
      `[bench] 判定：比值 ${(a2 / Math.max(a1, 0.001)).toFixed(2)}×` +
        `（≈1 ⇒ 历史行已 memo；≫1 ⇒ 每 token 重渲全部历史）`,
    );
    expect(big.commits.length).toBeGreaterThan(0);
  });
});
