// lib/stream.ts 的纯函数测试（node 环境，不需要 DOM）。
//
// 这些函数直接从 ChatPage 的 SSE 回调里抽出来 —— 抽出来的**唯一目的**就是让下面这些
// 边界条件能被断言，而不是靠人工点页面：
//   · 一个 chunk 里含多帧 / 半帧跨 chunk（帧切分最经典的 bug 来源）；
//   · 坏帧不该中断整条流（原来 JSON.parse 裸调用，一个坏帧会让整轮回答消失）；
//   · tool_result 与 tool_call 的配对（同名多次调用、乱序、丢帧）；
//   · message_replace 是**覆盖**而不是追加（守卫改写/角色缺失兜底都走这条）。

import { describe, expect, it } from "vitest";
import {
  describeTrim,
  newLiveBubble,
  parseSseFrame,
  reduceChatEvent,
  splitSseFrames,
  type LiveBubble,
} from "./stream";

// ------------------------------------------------------------------ 帧切分

describe("splitSseFrames", () => {
  it("一次取出多帧，且把尾巴留在缓冲里", () => {
    const { frames, rest } = splitSseFrames("data: a\n\ndata: b\n\ndata: half");
    expect(frames).toEqual(["data: a", "data: b"]);
    expect(rest).toBe("data: half");
  });

  it("半帧跨 chunk：先到的部分不会被解析，补齐后一次性取出", () => {
    const first = splitSseFrames("data: {\"ty");
    expect(first.frames).toEqual([]);
    expect(first.rest).toBe("data: {\"ty");

    const second = splitSseFrames(first.rest + "pe\":\"token\"}\n\n");
    expect(second.frames).toEqual(['data: {"type":"token"}']);
    expect(second.rest).toBe("");
  });

  it("宽容 CRLF 分隔符（反代可能改写行尾）", () => {
    const { frames, rest } = splitSseFrames("data: a\r\n\r\ndata: b");
    expect(frames).toEqual(["data: a"]);
    expect(rest).toBe("data: b");
  });

  it("空输入不产生帧", () => {
    expect(splitSseFrames("")).toEqual({ frames: [], rest: "" });
  });
});

// ------------------------------------------------------------------ 帧解析

describe("parseSseFrame", () => {
  it("取出 data 行里的 JSON", () => {
    expect(parseSseFrame('data: {"type":"token","text":"hi"}')).toEqual({
      type: "token",
      text: "hi",
    });
  });

  it("坏 JSON 不抛异常，而是当作没有事件（否则一个坏帧毁掉整轮回答）", () => {
    expect(parseSseFrame("data: {not json")).toBeNull();
  });

  it("非对象 / 缺 type 的载荷一律忽略", () => {
    expect(parseSseFrame("data: 42")).toBeNull();
    expect(parseSseFrame('data: {"text":"x"}')).toBeNull();
  });

  it("没有 data 行的帧（心跳/注释）返回 null", () => {
    expect(parseSseFrame(": keep-alive")).toBeNull();
  });
});

// ------------------------------------------------------------------ 事件归约

const bubble = (over: Partial<LiveBubble> = {}): LiveBubble => ({
  ...newLiveBubble(),
  ...over,
});

describe("reduceChatEvent", () => {
  it("token 逐段累加", () => {
    let b = bubble();
    b = reduceChatEvent(b, { type: "token", text: "你" }).bubble;
    b = reduceChatEvent(b, { type: "token", text: "好" }).bubble;
    expect(b.text).toBe("你好");
  });

  it("message_replace 覆盖而不是追加", () => {
    const b = reduceChatEvent(bubble({ text: "半句违规内容" }), {
      type: "message_replace",
      text: "抱歉，这项内容超出了我的职责范围。",
    }).bubble;
    expect(b.text).toBe("抱歉，这项内容超出了我的职责范围。");
  });

  it("tool_call → tool_result 把卡片标记完成", () => {
    let b = reduceChatEvent(bubble(), { type: "tool_call", name: "query_health_record" }).bubble;
    expect(b.tools).toEqual([
      { id: 1, name: "query_health_record", status: "running", content: "" },
    ]);
    b = reduceChatEvent(b, {
      type: "tool_result",
      name: "query_health_record",
      content: "6.0 mm",
    }).bubble;
    expect(b.tools).toEqual([
      { id: 1, name: "query_health_record", status: "ok", content: "6.0 mm" },
    ]);
  });

  it("tool_call 带入参数：工具卡能显示'过程'（搜了什么词）", () => {
    let b = reduceChatEvent(bubble(), {
      type: "tool_call",
      name: "web_search",
      args: { query: "北京天气" },
    }).bubble;
    expect(b.tools[0].args).toEqual({ query: "北京天气" });
    // 结果回填时入参保留（spread 更新）
    b = reduceChatEvent(b, { type: "tool_result", name: "web_search", content: "晴" }).bubble;
    expect(b.tools[0].args).toEqual({ query: "北京天气" });
  });

  it("同名工具连续调用：结果配到**最近一个**未完成的卡片", () => {
    let b = bubble();
    b = reduceChatEvent(b, { type: "tool_call", name: "t" }).bubble;
    b = reduceChatEvent(b, { type: "tool_call", name: "t" }).bubble;
    b = reduceChatEvent(b, { type: "tool_result", name: "t", content: "第一次" }).bubble;
    expect(b.tools.map((x) => x.status)).toEqual(["running", "ok"]);
    expect(b.tools[1].content).toBe("第一次");
  });

  it("结果先到（乱序/丢帧）：补一张已完成卡片，而不是丢掉结果", () => {
    const b = reduceChatEvent(bubble(), {
      type: "tool_result",
      name: "t",
      content: "结果",
    }).bubble;
    expect(b.tools).toEqual([{ id: 1, name: "t", status: "ok", content: "结果" }]);
  });

  it("error：追加一行 [错误]，并把仍在执行的卡片标红", () => {
    let b = reduceChatEvent(bubble({ text: "开头" }), { type: "tool_call", name: "t" }).bubble;
    const r = reduceChatEvent(b, { type: "error", detail: "模型调用失败" });
    expect(r.bubble.text).toBe("开头\n[错误] 模型调用失败");
    expect(r.bubble.tools[0].status).toBe("error");
    expect(r.meta.errored).toBe(true);
  });

  it("context_trimmed 只进旁路信息，不污染回答正文", () => {
    const r = reduceChatEvent(bubble({ text: "回答" }), {
      type: "context_trimmed",
      dropped: 12,
      kept: 6,
    });
    expect(r.bubble.text).toBe("回答");
    expect(r.bubble.tools).toEqual([]);
    expect(r.meta.trimmed).toEqual({ dropped: 12, kept: 6 });
  });

  it("thinking 逐段累加，且不污染回答文本", () => {
    let b = bubble();
    b = reduceChatEvent(b, { type: "thinking", text: "先想：" }).bubble;
    b = reduceChatEvent(b, { type: "thinking", text: "查一下" }).bubble;
    b = reduceChatEvent(b, { type: "token", text: "回答" }).bubble;
    expect(b.thinking).toBe("先想：查一下");
    expect(b.text).toBe("回答");
    // message_replace 覆盖回答但不冲掉思考（两条独立通道）
    const r = reduceChatEvent(b, { type: "message_replace", text: "权威回答" });
    expect(r.bubble.thinking).toBe("先想：查一下");
    expect(r.bubble.text).toBe("权威回答");
  });

  it("start 带回角色摘要；end 不改动气泡（收尾由调用方做）", () => {
    const started = reduceChatEvent(bubble(), {
      type: "start",
      role: { role_id: "r1", role_name: "通用助手" },
    });
    expect(started.meta.role?.role_name).toBe("通用助手");
    const ended = reduceChatEvent(bubble({ text: "x" }), { type: "end" });
    expect(ended.bubble.text).toBe("x");
  });

  it("end 把「这一轮是被叫停的」带到气泡上（#18 的 End.stopped）", () => {
    const stopped = reduceChatEvent(bubble({ text: "说到一半" }), { type: "end", stopped: true });
    expect(stopped.bubble.stopped).toBe(true);
    // 正常收尾要能**覆盖**掉上一轮留下的 true：气泡对象会被复用，只认 true 就会一直挂着"已停止"。
    const clean = reduceChatEvent(bubble({ text: "说完了", stopped: true }), {
      type: "end",
      stopped: false,
    });
    expect(clean.bubble.stopped).toBe(false);
    // 旧后端/旧壳不带这个字段 = 没被叫停，不是"被叫停了"。
    expect(reduceChatEvent(bubble(), { type: "end" }).bubble.stopped).toBe(false);
  });

  it("归约是纯函数：不改动传入的气泡对象", () => {
    const before = bubble({ text: "a" });
    const snapshot = JSON.stringify(before);
    reduceChatEvent(before, { type: "token", text: "b" });
    expect(JSON.stringify(before)).toBe(snapshot);
  });
});

// ------------------------------------------------------------------ 文案

describe("describeTrim", () => {
  it("没裁就不出提示", () => {
    expect(describeTrim(0, 10)).toBe("");
  });

  it("给出条数，并明确「记录没有丢」（避免用户以为对话被删了）", () => {
    const text = describeTrim(12, 6);
    expect(text).toContain("12");
    expect(text).toContain("6");
    expect(text).toContain("没有丢失");
  });
});
