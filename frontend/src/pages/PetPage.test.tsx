// @vitest-environment jsdom
//
// PetPage（桌宠：一张色片 + 一句气泡）的接线测试。
//
// 钉的是"驻留件不能骗人"这一族：未读合并成 +N、气泡会自己淡出、后端连不上时必须说出来
// 而不是安静地停在上一次的内容上（后者是这类小窗最容易被忽略的失效形态 —— 它看起来还在，
// 其实已经哑了）。
//
// 另一半钉的是"通知不能变成轰炸"：开机/重连时积压的那批一律不算新到，只有快照之间真的
// 变大 id 的那一条才拍系统通知；以及没有主动会话可跳的老消息不能去要点跳转（死链）。

import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { apiMock, streamChatMock, streamEditMock } = vi.hoisted(() => ({
  apiMock: {
    getReachouts: vi.fn(),
    markAllReachoutsRead: vi.fn(),
    proactiveThread: vi.fn(),
    get: vi.fn(),
    post: vi.fn(),
  },
  // 桌宠的回话复用对话页那份 SSE 归约，所以这里也换掉 `streamChat`（不是另写一套流）。
  streamChatMock: vi.fn(),
  // 「重新生成这一轮」也复用对话页那条通道（`POST /api/session/{tid}/messages/edit`）。
  streamEditMock: vi.fn(),
}));

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, api: apiMock, streamChat: streamChatMock, streamEdit: streamEditMock };
});

import PetPage from "./PetPage";
import type { ReachoutsPage, ReachoutRow } from "../api";
import type { ShellBridge } from "../lib/shell";

function row(id: number, text: string, state = "unread") {
  return {
    id,
    role_id: "wan",
    role_name: "苏晚晴",
    text,
    state,
    created_at: "2026-09-19 20:14:00",
    thread_id: "s_proactive_wan",
  };
}

function page(items: ReturnType<typeof row>[], unread = items.length): ReachoutsPage {
  // 这份 map 由后端算（`list_reachouts`），测试里按同样的口径造出来当"服务端的回答"。
  const byRole: Record<string, number> = {};
  for (const r of items) if (r.state === "unread") byRole[r.role_id] = (byRole[r.role_id] ?? 0) + 1;
  return { items, unread, unread_by_role: byRole } as ReachoutsPage;
}

/** 装上"壳"：桌宠的系统通知、悬停展开与"点气泡拉起控制台"都以它存在为前提。
 *  不装的时候就是 B/S —— 同一个组件、少两样能力，其余行为必须一模一样。 */
function withShell(contentVisible = true, voice = false): ShellBridge & {
  notify: ReturnType<typeof vi.fn>;
  openSession: ReturnType<typeof vi.fn>;
  movePetBy: ReturnType<typeof vi.fn>;
  onPetContentVisible: ReturnType<typeof vi.fn>;
  onPetVoice: ReturnType<typeof vi.fn>;
} {
  const shell = {
    backendUrl: () => Promise.resolve("http://127.0.0.1:8000"),
    backendReachable: () => Promise.resolve(true),
    notify: vi.fn(),
    openSession: vi.fn(),
    setPetExpanded: vi.fn().mockResolvedValue(true),
    movePetBy: vi.fn(),
    petDragEnd: vi.fn(),
    petReveal: vi.fn(),
    petRetuck: vi.fn(),
    petHitTest: vi.fn(),
    petHotRects: vi.fn(),
    petContentVisible: vi.fn().mockResolvedValue(contentVisible),
    onPetContentVisible: vi.fn(),
    petVoiceEnabled: vi.fn().mockResolvedValue(voice),
    onPetVoice: vi.fn(),
    onRequestOpenThread: vi.fn(),
    ollamaOwner: vi.fn().mockResolvedValue({ managed: false, pid: null, binary: null }),
    startOllama: vi.fn(),
    stopOllama: vi.fn(),
    pickDirectory: vi.fn().mockResolvedValue(null),
  };
  window.rolecardShell = shell;
  return shell;
}

/** 系统 TTS 替身：jsdom 两个都没有（`speechSynthesis` / `SpeechSynthesisUtterance`）。 */
function installTts(): { speak: ReturnType<typeof vi.fn>; cancel: ReturnType<typeof vi.fn> } {
  const speak = vi.fn();
  const cancel = vi.fn();
  Object.defineProperty(window, "speechSynthesis", {
    value: { speak, cancel },
    configurable: true,
  });
  (window as unknown as { SpeechSynthesisUtterance: unknown }).SpeechSynthesisUtterance = class {
    text: string;
    lang = "";
    rate = 1;
    constructor(text: string) {
      this.text = text;
    }
  };
  return { speak, cancel };
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.useFakeTimers();
  delete window.rolecardShell;
  apiMock.getReachouts.mockResolvedValue(page([row(3, "外头降温了，穿上外套。"), row(2, "旧的一条"), row(1, "更早的一条")]));
  apiMock.markAllReachoutsRead.mockResolvedValue(page([], 0));
  // 面板会按 URL 打两种请求：会话历史（对象）与角色表（数组）。一刀切的 mock 会让
  // `roles.find` 在一个"看着像历史"的对象上炸掉 —— 按 URL 分派才反映真实的两个端点。
  apiMock.get.mockImplementation(async (url: string) =>
    url === "/api/roles"
      ? [{ role_id: "wan", role_name: "苏晚晴", model_name: "" }]
      : { messages: [], total: 0, limit: 8, truncated: false },
  );
  apiMock.post.mockResolvedValue({ thread_id: "s_proactive_wan" });
  // 默认"这条主动会话还不存在"（清空抽屉 / 从没被找过的角色都是这个答案）。
  apiMock.proactiveThread.mockResolvedValue({ thread_id: null, role_id: "wan" });
  streamChatMock.mockImplementation(
    async (_tid: string, _msg: string, onEvent: (e: unknown) => void) => {
      onEvent({ type: "token", text: "好呀" });
      onEvent({ type: "end" });
    },
  );
});

afterEach(() => {
  vi.useRealTimers();
});

/** 把挂载 + 首次轮询的 Promise 都跑完（fake timers 下必须手动 flush）。 */
async function mount() {
  const view = render(<PetPage />);
  await act(async () => {
    await Promise.resolve();
  });
  return view;
}

/** 走一次轮询：让 mock 里换掉的新快照被读到。 */
async function poll() {
  await act(async () => {
    vi.advanceTimersByTime(10_000);
    for (let i = 0; i < 4; i += 1) await Promise.resolve();
  });
}

describe("PetPage 桌宠", () => {
  it("只显示最新一条，其余折成 +N；形象那块走素材渲染器（默认包）", async () => {
    await mount();
    expect(screen.getByText("外头降温了，穿上外套。")).toBeTruthy();
    expect(screen.queryByText("旧的一条")).toBeNull();
    expect(screen.getByText("+2")).toBeTruthy();
    expect(screen.getByTitle(/^苏晚晴/)).toBeTruthy();
    // 形象：有素材就画 spritesheet（默认包在 public/pets/default，构建时进 dist）。
    expect(screen.getByTestId("pet-sprite")).toBeTruthy();
  });

  it("点气泡 = 进入对话即都算读过（read-all），气泡随即收起", async () => {
    await mount();
    fireEvent.click(screen.getByText("外头降温了，穿上外套。"));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(apiMock.markAllReachoutsRead).toHaveBeenCalled();
    expect(screen.queryByText("外头降温了，穿上外套。")).toBeNull();
  });

  it("气泡 30 秒后自己收起（驻留件不长期戳在桌面上；不做淡出，见 PetPage 那条注释）", async () => {
    await mount();
    expect(screen.getByText("外头降温了，穿上外套。")).toBeTruthy();
    await act(async () => {
      vi.advanceTimersByTime(30_500);
    });
    expect(screen.queryByText("外头降温了，穿上外套。")).toBeNull();
  });

  it("后端连不上时明说，而不是继续显示上一次的消息", async () => {
    apiMock.getReachouts.mockRejectedValue(new Error("connection refused"));
    await mount();
    expect(screen.getByText("连不上本地服务")).toBeTruthy();
    expect(screen.queryByText("外头降温了，穿上外套。")).toBeNull();
  });

  it("一条消息都没有时只剩形象，不弹空气泡", async () => {
    apiMock.getReachouts.mockResolvedValue(page([]));
    await mount();
    expect(screen.getByTestId("pet-sprite")).toBeTruthy(); // 没消息时形象照常驻留
    expect(screen.queryByText("+2")).toBeNull();
  });

  it("挂载时那批已存在的消息不拍系统通知（首次快照只立基线）", async () => {
    const shell = withShell();
    await mount();
    await poll();
    expect(shell.notify).not.toHaveBeenCalled();
  });

  it("轮询到新到的一条才拍通知，且只拍一次", async () => {
    const shell = withShell();
    await mount();
    apiMock.getReachouts.mockResolvedValue(page([row(4, "给你带了桂花糕。"), row(3, "外头降温了，穿上外套。")]));
    await poll();
    expect(shell.notify).toHaveBeenCalledTimes(1);
    expect(shell.notify).toHaveBeenCalledWith("苏晚晴", "给你带了桂花糕。", "s_proactive_wan");
    // 换一个新数组装同样的内容再轮询一次：证明"不再弹"靠的是基线，不是 React 认出了同一个引用。
    apiMock.getReachouts.mockResolvedValue(page([row(4, "给你带了桂花糕。"), row(3, "外头降温了，穿上外套。")]));
    await poll();
    expect(shell.notify).toHaveBeenCalledTimes(1);
  });

  it("新到但已是已读的那条不拍通知", async () => {
    const shell = withShell();
    await mount();
    apiMock.getReachouts.mockResolvedValue(page([row(4, "自己看过的话", "read")]));
    await poll();
    expect(shell.notify).not.toHaveBeenCalled();
  });

  it("朗读默认关着：新到一条只拍通知，一声不出", async () => {
    const tts = installTts();
    const shell = withShell(); // voice 默认 false
    await mount();
    apiMock.getReachouts.mockResolvedValue(page([row(4, "给你带了桂花糕。"), row(3, "外头降温了，穿上外套。")]));
    await poll();
    expect(shell.notify).toHaveBeenCalledTimes(1);
    expect(tts.speak).not.toHaveBeenCalled();
  });

  it("朗读开着：新到的那条读出来（念的是原文，不是截断的预览）", async () => {
    const tts = installTts();
    withShell(true, true);
    await mount();
    await act(async () => {
      await Promise.resolve(); // 等 pull 到的旗子落进 ref
    });
    apiMock.getReachouts.mockResolvedValue(page([row(4, "给你带了桂花糕。"), row(3, "外头降温了，穿上外套。")]));
    await poll();
    expect(tts.speak).toHaveBeenCalledTimes(1);
    const spoken = tts.speak.mock.calls[0][0] as { text: string };
    expect(spoken.text).toBe("给你带了桂花糕。");
  });

  it("朗读开着但「显示消息内容」关着：仍然一声不出（声音同样会泄露内容）", async () => {
    const tts = installTts();
    withShell(false, true); // 内容藏起来、语音开着
    await mount();
    await act(async () => {
      await Promise.resolve();
    });
    apiMock.getReachouts.mockResolvedValue(page([row(4, "给你带了桂花糕。"), row(3, "外头降温了，穿上外套。")]));
    await poll();
    expect(tts.speak).not.toHaveBeenCalled();
  });

  it("点气泡把那条主动会话交给壳打开（壳负责把控制台拉到前台）", async () => {
    const shell = withShell();
    await mount();
    fireEvent.click(screen.getByText("外头降温了，穿上外套。"));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(shell.openSession).toHaveBeenCalledWith("s_proactive_wan");
  });

  it("没有主动会话可跳的老消息：只标已读，不求跳转", async () => {
    const shell = withShell();
    const legacy = { ...row(9, "很早以前的一句话"), thread_id: null } as ReachoutRow;
    apiMock.getReachouts.mockResolvedValue({ items: [legacy], unread: 1 } as ReachoutsPage);
    await mount();
    fireEvent.click(screen.getByText("很早以前的一句话"));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(apiMock.markAllReachoutsRead).toHaveBeenCalled();
    expect(shell.openSession).not.toHaveBeenCalled();
  });
});

describe("PetPage 点开面板（§7：想回话不用开控制台）", () => {
  /** 色片：点击开合面板的目标。 */
  function sprite(): Element {
    return screen.getByTitle(/点开看你们最近聊了什么/);
  }

  /** enter/leave 现在挂在根节点上（色片的父级）—— 悬停只负责"把趴着的拉出来"。 */
  function hoverZone(): Element {
    return sprite().parentElement as Element;
  }

  /** 点开面板 → 把历史请求的 Promise 跑完。 */
  async function open(shell: ShellBridge) {
    fireEvent.click(sprite());
    await act(async () => {
      for (let i = 0; i < 6; i += 1) await Promise.resolve();
    });
    return shell;
  }

  it("点一下色片：请壳展开，并把这条主动会话的最近几条摊开", async () => {
    const shell = withShell();
    apiMock.get.mockImplementation(async (url: string) =>
      url === "/api/roles"
        ? [{ role_id: "wan", role_name: "苏晚晴", model_name: "" }]
        : {
            messages: [
              { role: "assistant", content: "外头降温了，穿上外套。" },
              { role: "user", content: "好，你也穿点" },
              { role: "tool", content: "工具返回的一堆东西" },
            ],
            total: 3,
            limit: 8,
            truncated: false,
          },
    );
    await mount();
    await open(shell);

    expect(shell.setPetExpanded).toHaveBeenCalledWith(true);
    expect(apiMock.get).toHaveBeenCalledWith("/api/session/s_proactive_wan/messages?limit=30");
    expect(screen.getByText(/好，你也穿点/)).toBeTruthy();
    // 工具行不进这块小面板：它是对话页的过程细节，摆在桌面上只是噪音
    expect(screen.queryByText(/工具返回/)).toBeNull();
    // 展开态取代气泡（同一条内容摆两遍只是噪音）
    expect(screen.queryByText("+2")).toBeNull();
  });

  it("别处那一轮正在说时这块面板也镜像出来；她不说了就收掉（R26-38）", async () => {
    vi.useFakeTimers();
    try {
      const shell = withShell();
      // 服务端的"在飞"是唯一真相源：这里只拨它，面板该跟着长出来、跟着收掉。
      let inflight: { text: string } | null = { text: "我正说着的一截" };
      apiMock.get.mockImplementation(async (url: string) =>
        url === "/api/roles"
          ? [{ role_id: "wan", role_name: "苏晚晴", model_name: "" }]
          : {
              messages: [{ role: "user", content: "控制台那边问的一句" }],
              total: 1,
              limit: 30,
              truncated: false,
              inflight,
            },
      );
      await mount();
      await open(shell);
      // 用 getBy 不用 findBy：`open()` 已经把那次读的 Promise 跑完了，而假计时器下
      // `findBy*` 的轮询等的是真定时器，会白等到超时（这条在同一天咬过对话页的用例）。
      const box = screen.getByTestId("pet-inflight-mirror");
      expect(box.textContent).toContain("我正说着的一截");
      // 措辞不许替我们断言来源：登记里没有"谁发的"这一项 —— 09-29 改文案时把它写成了
      // "那一轮是控制台发起的"，就是这条用例挡下来的（那一扇窗可能根本没开：还有别的
      // 标签页、脚本调用、B/S 那台）。所以只说"不是这里发起的"。
      expect(box.textContent).toContain("不是这里发起的");
      expect(box.textContent).not.toContain("控制台");
      expect(box.textContent).not.toContain("桌宠");

      inflight = null; // 她那句落地
      await vi.advanceTimersByTimeAsync(3_400); // 下一拍（UNREAD_POLL_MS = 3s）
      await act(async () => {
        for (let i = 0; i < 6; i += 1) await Promise.resolve();
      });
      expect(screen.queryByTestId("pet-inflight-mirror")).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it("一个字都还没投送时那一格也在：空串是「她在打字」，不是「没有这回事」", async () => {
    const shell = withShell();
    apiMock.get.mockImplementation(async (url: string) =>
      url === "/api/roles"
        ? [{ role_id: "wan", role_name: "苏晚晴", model_name: "" }]
        : {
            messages: [{ role: "user", content: "那边问的" }],
            total: 1,
            limit: 30,
            truncated: false,
            inflight: { text: "" },
          },
    );
    await mount();
    await open(shell);
    expect(screen.getByTestId("pet-inflight-mirror").textContent).toContain("对方在说");
  });

  it("我说的靠右、对方说的靠左（09-26 用户：像对话界面那样才有对话感）", async () => {
    const shell = withShell();
    apiMock.get.mockImplementation(async (url: string) =>
      url === "/api/roles"
        ? [{ role_id: "wan", role_name: "苏晚晴", model_name: "" }]
        : {
            messages: [
              { role: "assistant", content: "外头降温了，穿上外套。" },
              { role: "user", content: "好，你也穿点" },
            ],
            total: 2,
            limit: 8,
            truncated: false,
          },
    );
    await mount();
    await open(shell);

    // jsdom 不把 Tailwind 排成版，所以这里量的是"那条规则挂上了没有"；真实位置在真浏览器
    // 里另外量过一次（我那一行的左边界确实落在它那一行的右边，见本轮验收）。
    const mine = screen.getByLabelText("我说") as HTMLElement;
    const theirs = screen.getByLabelText("对方说") as HTMLElement;
    expect(mine.tagName).toBe("P");
    expect(mine.className).toContain("ml-auto");
    expect(mine.className).toContain("max-w-[86%]");
    expect(theirs.className).not.toContain("ml-auto");
    // 靠右不等于把"谁说的"标掉：这么小的面板里位置与颜色不足以分辨（见 Speaker 那条注释）
    expect(mine.textContent).toBe("好，你也穿点");
    // 说话人**不再印成可见的"你：/它："**（用户 09-26："每次对话都有个它：，你：，这是不需要的"），
    // 但标注没有消失 —— 它降级进了 `aria-label`（上面那两个 getByLabelText 就是它）。
    expect(screen.queryByText("你：")).toBeNull();
    expect(screen.queryByText("它：")).toBeNull();
  });

  it("面板里的字选得中（根节点那块 select-none 不越界到面板里）", async () => {
    // 用户 09-26："为什么桌宠框那里我不可以长按鼠标左键选中文字"。`select-none` 挂在根节点
    // 本是为了拖宠物时不拉出橡皮筋选区，但它把"把她说的话选出来复制"一起禁了。
    const shell = withShell();
    await mount();
    await open(shell);
    const list = document.querySelector(
      "[data-pet-ui='panel'] [class*='overflow-y-auto']",
    ) as HTMLElement;
    expect(list.className).toContain("select-text");
    // 根节点照旧是不选的：那条规则管的是宠物本体，不是面板
    const root = list.closest("[class*='select-none']") as HTMLElement;
    expect(root).toBeTruthy();
  });

  it("面板只画最近一截时，必须说出「上面还有 N 条」（09-26 用户：这不该是一个东西吗）", async () => {
    // 那条会话真库实量 44 条，面板按 `PANEL_MESSAGES` 只取最近一截。以前它一声不吭，
    // 于是驻留件看着就像"你们的全部对话"—— 差的就是这一行声明。
    const shell = withShell();
    apiMock.get.mockImplementation(async (url: string) =>
      url === "/api/roles"
        ? [{ role_id: "wan", role_name: "苏晚晴", model_name: "" }]
        : {
            messages: [
              { id: "m1", role: "user", content: "好，你也穿点" },
              { id: "m2", role: "assistant", content: "外头降温了，穿上外套。" },
            ],
            total: 44,
            limit: 30,
            truncated: true,
          },
    );
    await mount();
    await open(shell);

    const more = screen.getByRole("button", { name: /上面还有 42 条/ });
    fireEvent.click(more);
    // 点的就是"同一条会话的完整版"：跳的是这条主动会话自己的 thread_id，不是另开一条
    expect(shell.openSession).toHaveBeenCalledWith("s_proactive_wan");
  });

  it("全部都在屏上了就不许再喊「上面还有 N 条」", async () => {
    const shell = withShell();
    apiMock.get.mockImplementation(async (url: string) =>
      url === "/api/roles"
        ? [{ role_id: "wan", role_name: "苏晚晴", model_name: "" }]
        : {
            messages: [
              { id: "m1", role: "user", content: "好，你也穿点" },
              { id: "m2", role: "assistant", content: "外头降温了，穿上外套。" },
            ],
            total: 2,
            limit: 30,
            truncated: false,
          },
    );
    await mount();
    await open(shell);
    expect(screen.queryByText(/上面还有/)).toBeNull();
  });

  it("控制台那边往同一条会话写了新消息：面板开着时会在下一次轮询自己跟上", async () => {
    // 用户 09-26 报的原话："我在对话界面对话时，桌宠打开的消息却不会更新" —— 历史原先只在
    // **展开那一下**读一次，3 秒轮询只刷收件箱那份，于是面板停在打开它的那一瞬间。
    const shell = withShell();
    let server = [
      { id: "m1", role: "user", content: "好，你也穿点" },
      { id: "m2", role: "assistant", content: "外头降温了，穿上外套。" },
    ];
    let total = 2;
    apiMock.get.mockImplementation(async (url: string) => {
      if (url === "/api/roles") return [{ role_id: "wan", role_name: "苏晚晴", model_name: "" }];
      return { messages: server, total, limit: 30, truncated: false };
    });
    await mount();
    await open(shell);
    expect(screen.getByText(/外头降温了/)).toBeTruthy();

    // 控制台上她又答了一句（同一张 checkpoint，同一个 thread_id）
    server = [...server, { id: "m3", role: "assistant", content: "记得把阳台那盆也搬进来。" }];
    total = 3;
    await poll();
    await act(async () => {
      for (let i = 0; i < 8; i += 1) await Promise.resolve();
    });
    expect(screen.getByText(/记得把阳台那盆/)).toBeTruthy();
    // 顶上那句"上面还有 N 条"跟着新的 total 走，不是停在第一次那份
    expect(screen.queryByText(/上面还有/)).toBeNull();
  });

  it("自己这一轮还在流的时候，后台刷新不许插一脚（重影那一族）", async () => {
    const shell = withShell();
    let server = [{ id: "m1", role: "user", content: "好，你也穿点" }];
    apiMock.get.mockImplementation(async (url: string) =>
      url === "/api/roles"
        ? [{ role_id: "wan", role_name: "苏晚晴", model_name: "" }]
        : { messages: server, total: server.length, limit: 30, truncated: false },
    );
    streamChatMock.mockImplementation(
      (_tid: string, _msg: string, _onEvent: (e: unknown) => void) => new Promise<void>(() => undefined),
    );
    await mount();
    await open(shell);

    const box = screen.getByPlaceholderText(/跟苏晚晴说一句/) as HTMLTextAreaElement;
    fireEvent.change(box, { target: { value: "我发一条" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    await act(async () => {
      for (let i = 0; i < 4; i += 1) await Promise.resolve();
    });

    // 流还没落地，服务端"多出来"的那一条不能盖到屏幕上：它会被 `handoff` 在收尾时一次性交出来
    server = [...server, { id: "m9", role: "assistant", content: "这条现在还不该出现" }];
    await poll();
    await act(async () => {
      for (let i = 0; i < 8; i += 1) await Promise.resolve();
    });
    expect(screen.queryByText(/这条现在还不该出现/)).toBeNull();
  });

  it("把「画了像素的那几块」报给壳，形状没变不重复发，卸载时收回（§12.3）", async () => {
    const shell = withShell();
    const send = shell.petHotRects as unknown as ReturnType<typeof vi.fn>;
    const { unmount } = await mount();
    expect(shell.petHitTest).toHaveBeenLastCalledWith(true);
    await act(async () => {
      vi.advanceTimersByTime(300);
    });
    const rects = send.mock.calls[0][0] as number[][];
    expect(rects.length).toBeGreaterThanOrEqual(1); // 至少有色片那一块
    expect(rects[0]).toHaveLength(4);
    // jsdom 没有排版，量出来必然是 0 —— 这里能钉的是"形状报出去了、都是有限整数"，
    // 真实尺寸那一半由壳那边的样式位探针在真机上量（见审计 §12.3）。
    expect(rects.flat().every((n) => Number.isInteger(n))).toBe(true);

    const before = send.mock.calls.length;
    await act(async () => {
      vi.advanceTimersByTime(1_200);
    });
    expect(send).toHaveBeenCalledTimes(before); // 形状没变 ⇒ 不发消息

    unmount();
    expect(shell.petHitTest).toHaveBeenLastCalledWith(false);
  });

  it("浏览器里直接开这一页（没有壳）：不报像素块，也不报错", async () => {
    const { container } = await mount();
    expect(() => {
      fireEvent.mouseOver(container.querySelector(".h-full") as Element);
      fireEvent.mouseOver(sprite());
    }).not.toThrow();
  });

  it("悬停只负责把趴着的半只拉出来：不改窗口大小，也就没有「把自己挪出光标」的自激", async () => {
    const shell = withShell();
    await mount();
    fireEvent.mouseEnter(hoverZone());
    await act(async () => {
      for (let i = 0; i < 4; i += 1) await Promise.resolve();
    });
    expect(shell.petReveal).toHaveBeenCalledTimes(1);
    expect(shell.setPetExpanded).not.toHaveBeenCalled(); // 悬停不再弹面板
    expect(screen.queryByText("苏晚晴")).toBeNull();
  });

  it("面板改成点击开合；鼠标走开不关它（关它是 ✕ 或再点一下色片）", async () => {
    const shell = withShell();
    await mount();
    fireEvent.click(sprite());
    await act(async () => {
      for (let i = 0; i < 6; i += 1) await Promise.resolve();
    });
    expect(shell.setPetExpanded).toHaveBeenLastCalledWith(true);
    expect(screen.getByText("苏晚晴")).toBeTruthy();

    fireEvent.mouseLeave(hoverZone());
    await act(async () => {
      vi.advanceTimersByTime(3_000);
      for (let i = 0; i < 4; i += 1) await Promise.resolve();
    });
    expect(shell.setPetExpanded).not.toHaveBeenCalledWith(false); // 走开不算关

    fireEvent.click(screen.getByRole("button", { name: "收起面板" }));
    await act(async () => {
      for (let i = 0; i < 4; i += 1) await Promise.resolve();
    });
    expect(shell.setPetExpanded).toHaveBeenLastCalledWith(false);
  });

  it("指针离开一段宽限之后才让壳把它趴回去，中途回来就取消", async () => {
    const shell = withShell();
    await mount();
    fireEvent.mouseLeave(hoverZone());
    await act(async () => {
      vi.advanceTimersByTime(400);
    });
    expect(shell.petRetuck).not.toHaveBeenCalled(); // 还在宽限期里
    fireEvent.mouseEnter(hoverZone()); // 回来了 ⇒ 那个定时器作废
    await act(async () => {
      vi.advanceTimersByTime(3_000);
    });
    expect(shell.petRetuck).not.toHaveBeenCalled();
    fireEvent.mouseLeave(hoverZone());
    await act(async () => {
      vi.advanceTimersByTime(1_000);
    });
    expect(shell.petRetuck).toHaveBeenCalledTimes(1);
  });

  it("抽屉被清空之后仍然读得到历史：线程 id 问后端，不从投递记录倒推（2026-09-23）", async () => {
    const shell = withShell();
    apiMock.getReachouts.mockResolvedValue({ items: [], unread: 0 } as ReachoutsPage);
    apiMock.proactiveThread.mockResolvedValue({ thread_id: "s_proactive_wan", role_id: "wan" });
    const seen: string[] = [];
    apiMock.get.mockImplementation(async (url: string) => {
      seen.push(url);
      if (url === "/api/roles") return [{ role_id: "wan", role_name: "苏晚晴", model_name: "" }];
      return {
        messages: [{ id: "m1", role: "assistant", content: "今天腰还酸吗？", created_at: "" }],
        total: 1,
        limit: 8,
        truncated: false,
      };
    });
    await mount();
    await open(shell);
    // 这条链比别的多一跳：先问"这条线在不在"，拿到 id 才去读历史。fake timers 下
    // `findByText` 那种轮询等不出来，手动把微任务排干再直接查 DOM。
    await act(async () => {
      for (let i = 0; i < 12; i += 1) await Promise.resolve();
    });
    expect(apiMock.proactiveThread).toHaveBeenCalledWith("wan");
    expect(seen.some((u) => u.includes("s_proactive_wan/messages"))).toBe(true);
    expect(screen.getByText("今天腰还酸吗？")).toBeTruthy();
  });

  it("还没有主动会话：面板说清楚，并且一次请求都不发", async () => {
    const shell = withShell();
    apiMock.getReachouts.mockResolvedValue({
      items: [{ ...row(1, "很早以前的一句话"), thread_id: null }],
      unread: 1,
    } as ReachoutsPage);
    await mount();
    await open(shell);
    expect(screen.getByText(/还没有你们的对话/)).toBeTruthy();
    // 角色表照拉（面板顶上要用），但**没有会话就不去读历史**：不给一个不存在的线程发请求。
    expect(apiMock.get).not.toHaveBeenCalledWith(
      expect.stringMatching(/\/messages(\?|$)/),
    );
  });

  it("历史读不到就明说（面板不能停在上一次的内容上装作没事）", async () => {
    const shell = withShell();
    apiMock.get.mockRejectedValue(new Error("500"));
    await mount();
    await open(shell);
    expect(screen.getByText(/历史没读到：500/)).toBeTruthy();
  });

  it("浏览器里没有壳：面板照样画，只是不去调那个不存在的方法", async () => {
    // 面板会按 URL 打两种请求：会话历史（对象）与角色表（数组）。一刀切的 mock 会让
  // `roles.find` 在一个"看着像历史"的对象上炸掉 —— 按 URL 分派才反映真实的两个端点。
  apiMock.get.mockImplementation(async (url: string) =>
    url === "/api/roles"
      ? [{ role_id: "wan", role_name: "苏晚晴", model_name: "" }]
      : { messages: [], total: 0, limit: 8, truncated: false },
  );
    await mount();
    fireEvent.click(sprite());
    await act(async () => {
      for (let i = 0; i < 4; i += 1) await Promise.resolve();
    });
    expect(window.rolecardShell).toBeUndefined();
    expect(screen.queryByText(/读取中/)).toBeNull(); // 空历史就真的是空，不卡在"读取中"
    expect(screen.getByText("苏晚晴")).toBeTruthy();
  });
});


describe("PetPage 拖桌宠（手动拖，因为 CSS 拖拽区会吞掉悬停事件）", () => {
  function sprite(): Element {
    return screen.getByTitle(/点开看你们最近聊了什么/);
  }

  it("按下后移动：把**增量**交给壳，方向与步长都对", async () => {
    const shell = withShell();
    await mount();
    const el = sprite();
    fireEvent.pointerDown(el, { screenX: 500, screenY: 400, pointerId: 1 });
    fireEvent.pointerMove(el, { screenX: 512, screenY: 395, pointerId: 1 });
    expect(shell.movePetBy).toHaveBeenCalledWith(12, -5);
    fireEvent.pointerMove(el, { screenX: 512, screenY: 380, pointerId: 1 });
    expect(shell.movePetBy).toHaveBeenLastCalledWith(0, -15); // 第二次是相对上一点，不是相对起点

    fireEvent.pointerUp(el, { pointerId: 1 });
    const ended = shell.petDragEnd as ReturnType<typeof vi.fn>;
    expect(ended).toHaveBeenCalledTimes(1); // 松手那一刻才让壳判吸边（§7.6）
    shell.movePetBy.mockClear();
    ended.mockClear();
    fireEvent.pointerMove(el, { screenX: 900, screenY: 900, pointerId: 1 });
    expect(shell.movePetBy).not.toHaveBeenCalled(); // 抬起之后不再拖
    expect(ended).not.toHaveBeenCalled(); // 也不再重复报"放手了"
  });

  it("浏览器里没有壳：拖不动也不报错（这一页在 B/S 下只是调试入口）", async () => {
    await mount();
    const el = sprite();
    fireEvent.pointerDown(el, { screenX: 10, screenY: 10, pointerId: 1 });
    fireEvent.pointerMove(el, { screenX: 40, screenY: 10, pointerId: 1 });
    fireEvent.pointerUp(el, { pointerId: 1 }); // 放手也不去找那个不存在的 petDragEnd
    expect(screen.getByTitle(/^苏晚晴/)).toBeTruthy();
  });

  it("拖完松手补的那一下 click 不算点击：面板不弹（弹了就等于把刚吸上去的又拉回屏内）", async () => {
    const shell = withShell();
    const expanded = shell.setPetExpanded as unknown as ReturnType<typeof vi.fn>;
    await mount();
    const el = sprite();
    fireEvent.pointerDown(el, { screenX: 500, screenY: 400, pointerId: 1 });
    fireEvent.pointerMove(el, { screenX: 520, screenY: 400, pointerId: 1 });
    fireEvent.pointerUp(el, { pointerId: 1 });
    fireEvent.click(el); // 浏览器在拖完之后照样会补一个 click
    expect(expanded).not.toHaveBeenCalledWith(true);

    // 而原地按一下（没有位移）就是开面板
    fireEvent.pointerDown(el, { screenX: 520, screenY: 400, pointerId: 1 });
    fireEvent.pointerUp(el, { pointerId: 1 });
    fireEvent.click(el);
    expect(expanded).toHaveBeenLastCalledWith(true);
  });

  it("按一下（没拖动）只发一次指令：不会先收再开那样跳一下", async () => {
    const shell = withShell();
    const expanded = shell.setPetExpanded as unknown as ReturnType<typeof vi.fn>;
    await mount();
    const el = sprite();
    fireEvent.pointerDown(el, { screenX: 500, screenY: 400, pointerId: 1 });
    fireEvent.pointerUp(el, { pointerId: 1 });
    fireEvent.click(el);
    expect(expanded).toHaveBeenCalledTimes(1); // 只有"开"，没有按下时那次多余的"收"
    expect(expanded).toHaveBeenCalledWith(true);
  });

  it("旧壳没有 petDragEnd（新 dist 跑在旧安装包上）：照拖不误", async () => {
    const shell = withShell();
    delete shell.petDragEnd;
    await mount();
    const el = sprite();
    fireEvent.pointerDown(el, { screenX: 10, screenY: 10, pointerId: 1 });
    fireEvent.pointerMove(el, { screenX: 40, screenY: 10, pointerId: 1 });
    expect(shell.movePetBy).toHaveBeenCalledWith(30, 0);
    expect(() => fireEvent.pointerUp(el, { pointerId: 1 })).not.toThrow();
  });
});


describe("PetPage 在桌宠上回话（③：不进控制台就能聊）", () => {
  function sprite(): Element {
    return screen.getByTitle(/点开看你们最近聊了什么/);
  }

  async function expandPanel() {
    await mount();
    fireEvent.click(sprite());
    await act(async () => {
      for (let i = 0; i < 6; i += 1) await Promise.resolve();
    });
  }

  it("输入 + Enter：先把所有未读标掉，再走对话页同一套 streamChat，流完刷新历史", async () => {
    await expandPanel();
    const box = screen.getByPlaceholderText(/跟苏晚晴说一句/) as HTMLTextAreaElement;
    fireEvent.change(box, { target: { value: "我穿好了" } });
    fireEvent.keyDown(box, { key: "Enter", shiftKey: false, isComposing: false });
    await act(async () => {
      for (let i = 0; i < 8; i += 1) await Promise.resolve();
    });

    expect(apiMock.markAllReachoutsRead).toHaveBeenCalled();
    expect(streamChatMock).toHaveBeenCalledWith(
      "s_proactive_wan",
      "我穿好了",
      expect.any(Function),
      expect.anything(),
    );
    // 流完以服务端回放为准：历史被重新读了一次（展开一次 + 一轮结束一次）
    expect(apiMock.get).toHaveBeenCalledWith("/api/session/s_proactive_wan/messages?limit=30");
  });

  it("她正在回话时面板跟着滚到最新；往上翻历史则不拽回去", async () => {
    // 用户 2026-09-25 报的那条：面板只有 300px 高，新长出来的字一直在看不见的下面，
    // 而桌宠先前**根本没接**自动滚动（对话页接了）。这里钉的是接上之后那份规则：
    // 贴着底才跟，翻上去的手不被抢回去 —— 与 `useAutoScroll` 一字不差，因为用的就是它。
    let emit: ((e: unknown) => void) | null = null;
    let finish: (() => void) | null = null;
    streamChatMock.mockImplementation(
      (_tid: string, _msg: string, onEvent: (e: unknown) => void) =>
        new Promise<void>((resolve) => {
          emit = onEvent;
          finish = resolve;
        }),
    );
    await expandPanel();
    const list = document.querySelector(
      "[data-pet-ui='panel'] [class*='overflow-y-auto']",
    ) as HTMLElement;
    expect(list).toBeTruthy();
    // jsdom 不排版：几何与 scrollTop 都得自己造，否则"贴没贴底"这个判据根本算不出来。
    let scrollTop = 0;
    const geo = { scrollHeight: 900, clientHeight: 300 };
    Object.defineProperty(list, "scrollHeight", { configurable: true, get: () => geo.scrollHeight });
    Object.defineProperty(list, "clientHeight", { configurable: true, get: () => geo.clientHeight });
    Object.defineProperty(list, "scrollTop", {
      configurable: true,
      get: () => scrollTop,
      set: (v: number) => {
        scrollTop = v;
      },
    });
    // DOM 里 `scrollTo` 有两个重载，`vi.spyOn` 挑到的是 `(x, y)` 那个，而 `useAutoScroll`
    // 传的是 `{ top }` 的对象形态 —— 按对象形态收。
    const scrollTo = (opts: ScrollToOptions) => {
      if (typeof opts.top === "number") scrollTop = opts.top;
    };
    vi.spyOn(list, "scrollTo").mockImplementation(scrollTo as unknown as () => void);
    scrollTop = geo.scrollHeight - geo.clientHeight; // 起点：用户正贴着底看最新那一条

    const box = screen.getByPlaceholderText(/跟苏晚晴说一句/) as HTMLTextAreaElement;
    fireEvent.change(box, { target: { value: "喂" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    await act(async () => {
      for (let i = 0; i < 4; i += 1) await Promise.resolve();
    });

    // 每来一块就长一截，滚动条必须跟着到底。
    for (const [i, chunk] of ["第一块", "第二块", "第三块"].entries()) {
      geo.scrollHeight = 900 + i * 200;
      // eslint-disable-next-line no-await-in-loop
      await act(async () => {
        emit?.({ type: "token", text: chunk });
      });
      expect(scrollTop).toBe(geo.scrollHeight);
    }

    // 用户往上翻着找旧的一条：字还在长，但不该被拽回底部。
    scrollTop = 200;
    geo.scrollHeight = 1600;
    await act(async () => {
      emit?.({ type: "token", text: "第四块" });
    });
    expect(scrollTop).toBe(200);

    await act(async () => {
      emit?.({ type: "end" });
      finish?.();
      for (let i = 0; i < 6; i += 1) await Promise.resolve();
    });
  });

  it("她的思考过程在桌宠里也看得见（09-26 用户：和对话界面差不多吧）", async () => {
    // 实测背景（审计 R26-29）：本地档那十几~几十秒**全部**花在思考解码上，可见正文只有几十字。
    // 桌宠先前只画 `live.text`，于是那段时间屏幕上是一个不动的空泡 —— 现在挂上对话页
    // 同一个 `ThinkingPanel`（流式期间展开本身就是"她在打字"的信号，不再另叠省略号）。
    let emit: ((e: unknown) => void) | null = null;
    let finish: (() => void) | null = null;
    streamChatMock.mockImplementation(
      (_tid: string, _msg: string, onEvent: (e: unknown) => void) =>
        new Promise<void>((resolve) => {
          emit = onEvent;
          finish = resolve;
        }),
    );
    await expandPanel();
    const box = screen.getByPlaceholderText(/跟苏晚晴说一句/) as HTMLTextAreaElement;
    fireEvent.change(box, { target: { value: "喂" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    await act(async () => {
      for (let i = 0; i < 4; i += 1) await Promise.resolve();
    });

    await act(async () => {
      emit?.({ type: "thinking", text: "先别急着问他在干嘛，" });
    });
    const panel = screen.getByText("思考过程").closest("details") as HTMLDetailsElement;
    expect(panel.open).toBe(true); // 流式期间展开
    await act(async () => {
      emit?.({ type: "thinking", text: "先说今天累不累" });
    });
    expect(panel.textContent).toContain("先说今天累不累");
    // 正文一个字都还没有，但屏幕上已经在长东西 —— 所以那一行不该再补一个多余的省略号。
    const line = screen.getByLabelText("对方说") as HTMLElement;
    expect(line.textContent).toBe("");

    await act(async () => {
      emit?.({ type: "token", text: "你来啦" });
      emit?.({ type: "end" });
      finish?.();
      for (let i = 0; i < 8; i += 1) await Promise.resolve();
    });
    // 一轮结束、气泡交回服务端回放之后，这一格跟着 `live` 一起收掉：桌宠面板只画最近几条
    // 正文（`/api/session/{tid}/messages` 不带思考），要看完整的"过程"去控制台那扇对话页。
    expect(screen.queryByText("思考过程")).toBeNull();
  });

  it("Shift+Enter 是换行，不是发送", async () => {
    await expandPanel();
    const box = screen.getByPlaceholderText(/跟苏晚晴说一句/) as HTMLTextAreaElement;
    fireEvent.change(box, { target: { value: "第一行" } });
    fireEvent.keyDown(box, { key: "Enter", shiftKey: true, isComposing: false });
    await act(async () => {
      for (let i = 0; i < 6; i += 1) await Promise.resolve();
    });
    expect(streamChatMock).not.toHaveBeenCalled();
  });

  it("输入法组合期间的 Enter 不发送（否则打拼音打到一半就发出去了）", async () => {
    await expandPanel();
    const box = screen.getByPlaceholderText(/跟苏晚晴说一句/) as HTMLTextAreaElement;
    fireEvent.change(box, { target: { value: "chuan" } });
    fireEvent.keyDown(box, { key: "Enter", shiftKey: false, isComposing: true });
    await act(async () => {
      for (let i = 0; i < 6; i += 1) await Promise.resolve();
    });
    expect(streamChatMock).not.toHaveBeenCalled();
  });

  it("这条线还不存在时：先按确定 id 幂等地把它建出来，再发第一句", async () => {
    apiMock.getReachouts.mockResolvedValue({
      items: [{ ...row(1, "它的话"), thread_id: null }],
      unread: 1,
    } as ReachoutsPage);
    await expandPanel();
    const box = screen.getByPlaceholderText(/跟苏晚晴说一句/) as HTMLTextAreaElement;
    fireEvent.change(box, { target: { value: "第一句" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    await act(async () => {
      for (let i = 0; i < 8; i += 1) await Promise.resolve();
    });
    expect(apiMock.post).toHaveBeenCalledWith("/api/session/proactive", { role_id: "wan" });
    expect(streamChatMock.mock.calls[0][0]).toBe("s_proactive_wan");
  });

  it("走开不算关面板；显式收起也不打断正在跑的这一轮", async () => {
    const shell = withShell();
    let finish: (() => void) | null = null;
    streamChatMock.mockImplementation(
      (_tid: string, _msg: string, onEvent: (e: unknown) => void) =>
        new Promise<void>((resolve) => {
          onEvent({ type: "token", text: "正在想…" });
          finish = resolve;
        }),
    );
    await expandPanel();
    const box = screen.getByPlaceholderText(/跟苏晚晴说一句/) as HTMLTextAreaElement;
    fireEvent.change(box, { target: { value: "喂" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    await act(async () => {
      for (let i = 0; i < 4; i += 1) await Promise.resolve();
    });
    expect(screen.getByText("停止")).toBeTruthy();

    // 指针走开：面板不再被悬停驱动（那正是自激晃动的来源），也不会被趴回去顶掉
    fireEvent.mouseLeave(sprite().parentElement as Element);
    await act(async () => {
      vi.advanceTimersByTime(3_000);
      for (let i = 0; i < 4; i += 1) await Promise.resolve();
    });
    expect(shell.setPetExpanded).not.toHaveBeenCalledWith(false);
    expect(shell.petRetuck).toHaveBeenCalled(); // 但"回去"这件事照发 —— 壳那边见面板开着会不动
    const signal = streamChatMock.mock.calls[0][3] as AbortSignal;
    expect(signal.aborted).toBe(false); // 走开没掐掉这一轮

    // 显式收起（✕）也不 abort：收的只是那扇窗，不是这一轮
    fireEvent.click(screen.getByRole("button", { name: "收起面板" }));
    await act(async () => {
      for (let i = 0; i < 4; i += 1) await Promise.resolve();
    });
    expect(shell.setPetExpanded).toHaveBeenLastCalledWith(false);
    expect(signal.aborted).toBe(false);

    await act(async () => {
      finish?.();
      for (let i = 0; i < 6; i += 1) await Promise.resolve();
    });
  });

  it("一轮跑完只留一份：回放到手就把流式气泡交出去（先前是同一条回答画两遍）", async () => {
    // 回放里带上这一轮那两条 —— 这正是流结束那一刻 checkpoint 的真实形状。
    apiMock.get.mockImplementation(async (url: string) =>
      url === "/api/roles"
        ? [{ role_id: "wan", role_name: "苏晚晴", model_name: "" }]
        : {
            messages: [
              { role: "user", content: "我穿好了", id: "u1" },
              { role: "assistant", content: "好呀", id: "a1" },
            ],
            total: 2,
            limit: 8,
            truncated: false,
          },
    );
    await expandPanel();
    const box = screen.getByPlaceholderText(/跟苏晚晴说一句/) as HTMLTextAreaElement;
    fireEvent.change(box, { target: { value: "我穿好了" } });
    fireEvent.keyDown(box, { key: "Enter", shiftKey: false, isComposing: false });
    await act(async () => {
      for (let i = 0; i < 10; i += 1) await Promise.resolve();
    });
    // 「重影」就是这两行数不相等：气泡里挂着的与回放里新到的各画一遍。
    expect(screen.getAllByText("好呀")).toHaveLength(1);
    expect(screen.getAllByText("我穿好了")).toHaveLength(1);
  });

  it("还在流的时候重读历史，不许把正在长的那句收掉", async () => {
    let finish: (() => void) | null = null;
    streamChatMock.mockImplementation(
      (_tid: string, _msg: string, onEvent: (e: unknown) => void) =>
        new Promise<void>((resolve) => {
          onEvent({ type: "token", text: "正在想…" });
          finish = resolve;
        }),
    );
    await expandPanel();
    const box = screen.getByPlaceholderText(/跟苏晚晴说一句/) as HTMLTextAreaElement;
    fireEvent.change(box, { target: { value: "喂" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    await act(async () => {
      for (let i = 0; i < 4; i += 1) await Promise.resolve();
    });

    // 收起再展开：会再打一次历史重读，而这一轮还没落进回放里（mock 给的是空历史）。
    fireEvent.click(screen.getByRole("button", { name: "收起面板" }));
    await act(async () => {
      for (let i = 0; i < 4; i += 1) await Promise.resolve();
    });
    fireEvent.click(sprite());
    await act(async () => {
      for (let i = 0; i < 8; i += 1) await Promise.resolve();
    });
    expect(screen.getByText("正在想…")).toBeTruthy(); // 实时那句还在，没被一次空回放吃掉

    await act(async () => {
      finish?.();
      for (let i = 0; i < 8; i += 1) await Promise.resolve();
    });
  });
});

describe("PetPage 托盘的「显示消息内容」旗子（§7.2 第 4 条：别人站在背后读不到）", () => {
  const HIDDEN = "外头降温了，穿上外套。";

  function sprite(): Element {
    return screen.getByTitle(/^苏晚晴/);
  }

  /** 面板里那一句历史：默认 mock 是空历史，所以这里换成一条真内容。 */
  function withHistory(text: string) {
    apiMock.get.mockImplementation(async (url: string) =>
      url === "/api/roles"
        ? [{ role_id: "wan", role_name: "苏晚晴", model_name: "" }]
        : {
            messages: [{ role: "assistant", content: text, id: "m1" }],
            total: 1,
            limit: 8,
            truncated: false,
          },
    );
  }

  async function hover() {
    fireEvent.click(sprite());
    await act(async () => {
      for (let i = 0; i < 6; i += 1) await Promise.resolve();
    });
  }

  it("关掉之后：气泡不画原文，面板不读历史，只说'去哪读、怎么打开'", async () => {
    withShell(false);
    withHistory(HIDDEN);
    await mount();
    expect(screen.getByText("有新消息 · 内容已隐藏")).toBeTruthy();
    expect(screen.queryByText(HIDDEN)).toBeNull();

    await hover();
    expect(screen.getByText(/在控制台里读/)).toBeTruthy();
    expect(screen.getByText(/3 条未读/)).toBeTruthy();
    // 藏起来的东西不该只是"不画"：连请求都不发，页面里不留那份明文。
    expect(apiMock.get.mock.calls.some((c) => String(c[0]).includes("/messages"))).toBe(false);
    expect(screen.queryByText(HIDDEN)).toBeNull();
  });

  it("托盘当场改旗子：正开着的面板立刻把内容收掉", async () => {
    const shell = withShell(true);
    withHistory(HIDDEN);
    await mount();
    await hover();
    expect(screen.getByText(HIDDEN)).toBeTruthy();

    const push = shell.onPetContentVisible.mock.calls[0][0] as (v: boolean) => void;
    act(() => push(false));
    expect(screen.queryByText(HIDDEN)).toBeNull();
    expect(screen.getByText(/内容已隐藏/)).toBeTruthy();

    act(() => push(true));
    await act(async () => {
      for (let i = 0; i < 6; i += 1) await Promise.resolve();
    });
    expect(screen.getByText(HIDDEN)).toBeTruthy(); // 勾回来就又读回来，不用重开面板
  });

  it("旗子关掉时新到的消息只报'谁找你了'，toast 里不拍原文", async () => {
    const shell = withShell(false);
    await mount();
    apiMock.getReachouts.mockResolvedValue(page([row(4, "给你带了桂花糕。"), row(3, HIDDEN)]));
    await poll();
    expect(shell.notify).toHaveBeenCalledTimes(1);
    expect(shell.notify).toHaveBeenCalledWith("苏晚晴", "内容已隐藏", "s_proactive_wan");
  });

  it("旧壳没这能力（新 dist 跑在旧安装包上）：内容照画，不白屏也不自己藏起来", async () => {
    const shell = withShell(true);
    delete shell.petContentVisible;
    withHistory(HIDDEN);
    await mount();
    await hover();
    expect(screen.getByText(HIDDEN)).toBeTruthy();
  });
});

describe("PetPage 命中区与一次拖只收一次（三条都是量出来的，不是设想）", () => {
  function sprite(): HTMLElement {
    return screen.getByTitle(/点开看你们最近聊了什么/) as HTMLElement;
  }
  function root(): HTMLElement {
    return sprite().parentElement as HTMLElement;
  }
  /** 给色片一块真实的矩形：jsdom 默认什么都量成 0，而"落点离色片多远"正是判据。
   *  画布 560×520，色片 88 见方在下沿居中 → x 236..324，y 428..516。 */
  function stubSpriteRect(): void {
    const el = sprite();
    el.getBoundingClientRect = () =>
      ({
        x: 236, y: 428, left: 236, top: 428, right: 324, bottom: 516, width: 88, height: 88,
        toJSON: () => ({}),
      }) as DOMRect;
  }

  it("点色片旁边的透明边也算点它：悬停把宠物拉回屏内时，光标会落在那段带上", async () => {
    // 实测：从右边趴着点它，窗口往屏内滑 96px，色片从光标底下滑走，而光标还在窗口里。
    // handler 只挂在色片上就是"我点它，它跑了"。余量按壳能挪走多远给（`HIT_SLACK_X`）。
    const shell = withShell();
    const expanded = shell.setPetExpanded as unknown as ReturnType<typeof vi.fn>;
    await mount();
    stubSpriteRect();
    // 色片右沿外 56px：正是旧尺寸里那条透明带的位置。
    fireEvent.click(root(), { clientX: 380, clientY: 470 });
    expect(expanded).toHaveBeenLastCalledWith(true);
  });

  it("点画布远处的空白不算点桌宠：收起着不摊开，摊着的时候才是收起", async () => {
    // 用户 2026-09-24 报的：窗改成 560×520 的常驻画布之后，"边上"是左右各 180px、上面 280px。
    // 壳放行点击有宽限期（展开/收起后 500ms），那段时间落在这儿的点击会收到页面上 ——
    // 若按整块窗判就是"我明明没点到桌宠，消息框却弹出来了"。
    const shell = withShell();
    const expanded = shell.setPetExpanded as unknown as ReturnType<typeof vi.fn>;
    await mount();
    stubSpriteRect();
    fireEvent.click(root(), { clientX: 60, clientY: 60 });
    expect(expanded).not.toHaveBeenCalled();

    // 摊着的时候点空白 = 收起来（"点空白关闭"这条留着有用，不用去摸那个 ✕）。
    fireEvent.click(sprite(), { clientX: 280, clientY: 470 });
    expect(expanded).toHaveBeenLastCalledWith(true);
    fireEvent.click(root(), { clientX: 60, clientY: 60 });
    expect(expanded).toHaveBeenLastCalledWith(false);
  });

  it("面板里打字/点按钮不冒泡成「收起面板」", async () => {
    const shell = withShell();
    const expanded = shell.setPetExpanded as unknown as ReturnType<typeof vi.fn>;
    await mount();
    fireEvent.click(sprite()); // 开
    expect(expanded).toHaveBeenLastCalledWith(true);
    expanded.mockClear();
    fireEvent.click(screen.getByRole("textbox")); // 输入框自己的一下
    fireEvent.click(screen.getByTitle(/在控制台里打开这条会话/));
    expect(expanded).not.toHaveBeenCalled();
  });

  it("一次拖拽里连着多次 pointermove：只发一次「收起」", async () => {
    // React 状态要到下一次渲染才更新，只认 `expanded` 就会在一次拖里把"收起"发十几遍，
    // 而壳每一次都会重算落点 —— 读起来就是抖。
    const shell = withShell();
    const expanded = shell.setPetExpanded as unknown as ReturnType<typeof vi.fn>;
    await mount();
    fireEvent.click(sprite());
    expanded.mockClear();
    const el = root();
    fireEvent.pointerDown(el, { screenX: 500, screenY: 400, pointerId: 1 });
    for (const y of [392, 384, 376, 368]) {
      fireEvent.pointerMove(el, { screenX: 500, screenY: y, pointerId: 1 });
    }
    fireEvent.pointerUp(el, { pointerId: 1 });
    expect(expanded).toHaveBeenCalledTimes(1);
    expect(expanded).toHaveBeenCalledWith(false);
  });
});

describe("PetPage 状态行（M5 之② + R26-45：云端指示 + 为什么静默）", () => {
  const KEY = "rolecard.dataSource.v1";

  async function openPanel() {
    await mount();
    fireEvent.click(screen.getByTitle(/点开看你们最近聊了什么/));
    await act(async () => {
      for (let i = 0; i < 6; i += 1) await Promise.resolve();
    });
  }

  it("本机态：显示 ● 本机，不出现「云端」两个字", async () => {
    localStorage.removeItem(KEY);
    await openPanel();
    expect(screen.getByText("● 本机")).toBeTruthy();
    expect(screen.queryByText(/云端/)).toBeNull();
  });

  it("云端态：显示 ☁ 云端 · 账号", async () => {
    localStorage.setItem(
      KEY,
      JSON.stringify({ mode: "cloud", base: "https://cloud.test:8123", user: "u1", secret: "pw" }),
    );
    await openPanel();
    expect(screen.getByText("☁ 云端 · u1")).toBeTruthy();
  });

  it("静默原因挂在当前面板角色上，别的角色那句不画", async () => {
    localStorage.removeItem(KEY);
    apiMock.getReachouts.mockResolvedValue({
      items: [row(3, "外头降温了，穿上外套。")],
      unread: 1,
      unread_by_role: { wan: 1 },
      quiet: [
        { role_id: "wan", role_name: "苏晚晴", why: "距上次说话不足 66 分钟", next_ok_at: null, streak: 1, unread: 1 },
        { role_id: "other", role_name: "别的", why: "处于静默时段", next_ok_at: null, streak: 0, unread: 0 },
      ],
    } as ReachoutsPage);
    await openPanel();
    expect(screen.getByText(/距上次说话不足 66 分钟/)).toBeTruthy();
    expect(screen.queryByText(/静默时段/)).toBeNull();
  });
});

describe("PetPage 面板的右键菜单（09-26 用户选的形态：页内自绘，不用原生菜单）", () => {
  const HISTORY = [
    { id: "m1", role: "user", content: "你陪我嘛" },
    { id: "m2", role: "assistant", content: "当然陪着你呀" },
  ] as const;

  async function openWithHistory() {
    apiMock.get.mockImplementation(async (url: string) =>
      url === "/api/roles"
        ? [{ role_id: "wan", role_name: "苏晚晴", model_name: "" }]
        : { messages: [...HISTORY], total: 2, limit: 8, truncated: false },
    );
    await mount();
    fireEvent.click(screen.getByTitle(/点开看你们最近聊了什么/));
    await act(async () => {
      for (let i = 0; i < 6; i += 1) await Promise.resolve();
    });
  }

  function menuItems(): HTMLButtonElement[] {
    const menu = document.querySelector("[data-pet-ui='menu']");
    if (!menu) throw new Error("菜单没弹出来");
    return within(menu as HTMLElement).getAllByRole("menuitem") as HTMLButtonElement[];
  }

  it("右键她那一句：三项按顺序在，没选区时「复制选中的文字」置灰", async () => {
    await openWithHistory();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });

    fireEvent.contextMenu(screen.getByLabelText("对方说"));
    const items = menuItems();
    expect(items.map((b) => b.textContent)).toEqual([
      "复制选中的文字",
      "复制整条消息",
      "重新生成这一轮",
    ]);
    expect(items[0].disabled).toBe(true); // 屏幕上没有选区，就不给一个点了没动作的项
    expect(items[1].disabled).toBe(false);

    fireEvent.click(items[1]);
    expect(writeText).toHaveBeenCalledWith("当然陪着你呀");
    // 点完自己收：透明窗上留一块浮层就是噪音，而且它会挡住下面的桌面点击
    expect(document.querySelector("[data-pet-ui='menu']")).toBeNull();
  });

  it("重新生成走对话页那条 edit 通道，用的是触发这一轮的那句用户消息", async () => {
    await openWithHistory();
    streamEditMock.mockImplementation(async () => undefined);

    fireEvent.contextMenu(screen.getByLabelText("对方说"));
    fireEvent.click(menuItems()[2]);
    await act(async () => {
      for (let i = 0; i < 8; i += 1) await Promise.resolve();
    });

    expect(streamEditMock).toHaveBeenCalledWith(
      "s_proactive_wan",
      "m1",
      "你陪我嘛",
      expect.any(Function),
      expect.anything(),
      null,
    );
  });

  it("我那句没有可重问的靶子：右键它只给复制，不给「重新生成」", async () => {
    await openWithHistory();
    fireEvent.contextMenu(screen.getByLabelText("我说"));
    expect(menuItems().map((b) => b.textContent)).toEqual(["复制选中的文字", "复制整条消息"]);
  });

  it("正在流的那一句：多一项「停止这一轮生成」，而重问要等这一轮结束", async () => {
    streamChatMock.mockImplementation(
      (_tid: string, _msg: string, _onEvent: (e: unknown) => void) => new Promise<void>(() => undefined),
    );
    await openWithHistory();
    const box = screen.getByPlaceholderText(/跟苏晚晴说一句/) as HTMLTextAreaElement;
    fireEvent.change(box, { target: { value: "喂" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    await act(async () => {
      for (let i = 0; i < 4; i += 1) await Promise.resolve();
    });

    // 正在流的那一行没有 id（还没落库），所以它给"停止"而不是"重新生成"
    const hers = screen.getAllByLabelText("对方说");
    const live = hers[hers.length - 1] as HTMLElement;
    expect(live.getAttribute("data-mid")).toBeNull();
    fireEvent.contextMenu(live);
    expect(menuItems().map((b) => b.textContent)).toEqual([
      "复制选中的文字",
      "复制整条消息",
      "停止这一轮生成",
    ]);
  });
});
