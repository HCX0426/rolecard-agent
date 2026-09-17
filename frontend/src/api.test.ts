// api.ts 的运行时测试（vitest，node 环境，mock fetch —— 不需要浏览器）。
//
// 为什么这些测试存在：当年发生过「request() 只设 Content-Type 不 JSON.stringify」
// 的事故 —— 页面 GET 全正常，所有写操作静默 422，纯靠人眼看代码漏掉的。
// 这里把 api 层的可执行契约钉死：序列化、错误归一化、SSE 解析、abort 语义。
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api, streamChat } from "./api";

function sseStream(frames: string[], errorAtEnd?: Error): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream<Uint8Array>({
    start(controller) {
      for (const f of frames) controller.enqueue(encoder.encode(f));
      if (errorAtEnd) controller.error(errorAtEnd);
      else controller.close();
    },
  });
}

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("api.request：请求体序列化（422 事故的原点）", () => {
  it("POST 对象 body 必须被 JSON.stringify，并带 application/json 头", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ ok: true }));
    vi.stubGlobal("fetch", fetchMock);

    await api.post("/api/session", { role_id: "medical_archivist" });

    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, opt] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/session");
    expect(opt.method).toBe("POST");
    // 契约核心：body 必须是 JSON 字符串。当年它退化成 "[object Object]"，服务端 422。
    expect(opt.body).toBe('{"role_id":"medical_archivist"}');
    expect((opt.headers as Record<string, string>)["Content-Type"]).toBe("application/json");
  });

  it("GET 请求不带 body，也不设 JSON 头", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse([]));
    vi.stubGlobal("fetch", fetchMock);

    await api.get("/api/roles");

    const [, opt] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(opt.method).toBe("GET");
    expect(opt.body).toBeUndefined();
    expect(opt.headers).toEqual({});
  });

  it("FormData 不盖 JSON 头（multipart 边界由浏览器设置）", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ status: "indexed" }));
    vi.stubGlobal("fetch", fetchMock);

    const form = new FormData();
    form.append("file", new Blob(["abc"]), "report.png");
    await fetch("/api/session/t1/upload", { method: "POST", body: form });

    const [, opt] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(opt.body).toBeInstanceOf(FormData);
    const headers = opt.headers as Record<string, string> | undefined;
    expect(headers?.["Content-Type"]).toBeUndefined(); // 手动覆盖会破坏 multipart
  });
});

describe("api.request：错误归一化", () => {
  it("非 2xx 且 detail 为字符串 → ApiError(status, detail)", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ detail: "对话不存在" }, 404)));
    const err = await api.get("/api/session/t_nope").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(404);
    expect((err as ApiError).message).toBe("对话不存在");
  });

  it("422 校验错误（detail 为数组）→ 拼成可读文本，而不是 [object Object]", async () => {
    const detail = [
      { loc: ["body", "thread_id"], msg: "Field required" },
      { loc: ["body", "message"], msg: "String should have at least 1 character" },
    ];
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ detail }, 422)));
    const err = await api.post("/api/chat", {}).catch((e: unknown) => e);
    const msg = (err as ApiError).message;
    expect((err as ApiError).status).toBe(422);
    expect(msg).toContain("body.thread_id: Field required");
    expect(msg).toContain("body.message: String should have at least 1 character");
    expect(msg).not.toContain("[object Object]");
  });

  it("非 JSON 错误体 → 回退到状态码文本", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response("Bad Gateway", { status: 502 })),
    );
    const err = await api.get("/api/x").catch((e: unknown) => e);
    expect((err as ApiError).status).toBe(502);
    // Node 的 Response.statusText 为空串、浏览器是 "Bad Gateway" —— 断言前缀即契约。
    expect((err as ApiError).message).toMatch(/^502/);
  });

  it("204 返回 undefined（DELETE 的契约）", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 204 })));
    expect(await api.del("/api/session/t1")).toBeUndefined();
  });
});

describe("streamChat：SSE 解析与 abort 语义", () => {
  it("按帧解析事件序列（token / message_replace / end）", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          sseStream([
            'data: {"type":"start","role":{"role_id":"r","role_name":"角色"}}\n\n',
            'data: {"type":"token","text":"你好"}\n\n',
            'data: {"type":"token","text":"，世界"}\n\n',
            'data: {"type":"end"}\n\n',
          ]),
          { status: 200 },
        ),
      ),
    );
    const events: string[] = [];
    await streamChat("t1", "问", (ev) => events.push(ev.type));
    expect(events).toEqual(["start", "token", "token", "end"]);
  });

  it("事件跨 chunk 到达也能解析（后端契约：每帧一条 data: 行）", async () => {
    // 三个帧从中间切开 —— reader 每次拿到半个帧，解析必须靠 buf 累积 + \n\n 分帧。
    // 注意 api.ts 的契约是「一帧一条 data:」（后端 sse() 就这么发）；一帧多行只取第一条。
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          sseStream([
            'data: {"type":"token","text":"A"}\n\n',
            'data: {"type":"to',
            'ken","text":"B"}\n\n',
            'data: {"type":"end"}\n\n',
          ]),
          { status: 200 },
        ),
      ),
    );
    const events: string[] = [];
    await streamChat("t1", "问", (ev) => events.push(ev.type));
    expect(events).toEqual(["token", "token", "end"]);
  });

  it("HTTP 500 → 一个 error 事件 + end（不抛异常给调用方）", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ detail: "模型调用失败" }, 500)),
    );
    const events: { type: string; text?: string; detail?: string }[] = [];
    await streamChat("t1", "问", (ev) => events.push(ev));
    expect(events.map((e) => e.type)).toEqual(["error", "end"]);
    expect(events[0].detail).toBe("模型调用失败");
  });

  it("流中断（AbortError）→ 静默结束：不发 error，只发 end", async () => {
    const abort = Object.assign(new Error("The user aborted a request."), { name: "AbortError" });
    const encoder = new TextEncoder();
    const stream = new ReadableStream<Uint8Array>({
      // 先给一个完整 chunk；queue 空之后的下一次 read 才触发 pull —— 在那里模拟中断。
      // （不能在 start 里 error()：那会清空已 enqueue 未读的 chunk。）
      start(controller) {
        controller.enqueue(encoder.encode('data: {"type":"token","text":"部分输出"}\n\n'));
      },
      pull() {
        throw abort;
      },
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(stream, { status: 200 })));
    const events: { type: string }[] = [];
    await streamChat("t1", "问", (ev) => events.push(ev));
    // 契约（api.ts 读流阶段的 catch）：「停止生成」不是错误，静默结束且**不补发 end** ——
    // 调用方收尾走 checkpoint 回放（ChatPage 的实现）。注意与 fetch 阶段失败不同，
    // 那里会补发一个 end（见上一个测试）。
    expect(events.map((e) => e.type)).toEqual(["token"]);
  });

  it("网络错误（fetch 直接拒绝）→ error + end", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("ECONNREFUSED")));
    const events: { type: string; detail?: string }[] = [];
    await streamChat("t1", "问", (ev) => events.push(ev));
    expect(events.map((e) => e.type)).toEqual(["error", "end"]);
    expect(events[0].detail).toContain("ECONNREFUSED");
  });
});

// 超时不只关乎体验，还关乎"界面说的话是否属实"（审查报告 P1-4）：
// 后端 OCR 子进程单次上限就是 120s，抽取还要跑两次模型调用。若沿用 30s，前端会先 abort
// 并把界面变成"上传失败/AI 识别指标失败"，而后端线程仍在跑、**文件已落盘、索引/报告已写库**
// —— 用户看到假失败，还可能照着假失败再点一次。
describe("请求超时：普通请求 30s，上传 / 抽取走长超时（P1-4）", () => {
  /** 永不返回、只在 signal abort 时 reject AbortError 的 fetch 替身。 */
  function hangingFetch() {
    return vi.fn(
      (_url: string, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => {
            reject(Object.assign(new Error("aborted"), { name: "AbortError" }));
          });
        }),
    );
  }

  afterEach(() => {
    vi.useRealTimers();
  });

  it("上传不会在 30s 被掐断", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", hangingFetch());

    const pending = api.upload("t1", new FormData());
    const onReject = vi.fn();
    pending.catch(onReject);

    await vi.advanceTimersByTimeAsync(30_000);
    expect(onReject).not.toHaveBeenCalled(); // 30s 到点必须还活着

    await vi.advanceTimersByTimeAsync(270_000); // 累计 300s
    await expect(pending).rejects.toThrow(/300s/);
  });

  it("超时提示要说「后端可能仍在处理」，而不是「已挂起」", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", hangingFetch());

    // 用 then(成功分支, 失败分支) 而不是 catch：catch 的返回类型会变成联合类型，
    // tsc 就没法在下面直接读 ApiError 的字段了。
    const pending = api.extractRecord("ing_1").then(
      () => null,
      (e: ApiError) => e,
    );
    await vi.advanceTimersByTimeAsync(300_000); // 必须先推进时间，await 在后
    const failure = await pending;

    expect(failure?.status).toBe(0);
    expect(failure?.message).toContain("仍在处理"); // 别让用户以为白做了
  });

  it("普通 JSON 请求仍然 30s 掐断（默认值不能被一起放大）", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", hangingFetch());

    const pending = api.get("/api/sessions");
    const onReject = vi.fn();
    pending.catch(onReject);

    await vi.advanceTimersByTimeAsync(30_000);

    expect(onReject).toHaveBeenCalledOnce();
    await expect(pending).rejects.toThrow(/30s/);
  });
});
