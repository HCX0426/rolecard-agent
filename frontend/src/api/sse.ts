// SSE 对话流：事件协议 + `postSse` 公共体 + `streamChat` / `streamEdit` 两个入口。
//
// 从 `api/index.ts` 按主题拆出（快照 P3-1 第三刀）。拆前先量过耦合：`ChatEvent` 的全部
// 出现都在这一段里面，`postSse` 与两条流对外只依赖 `lib/dataSource` 与 `lib/stream`，
// **与"契约类型 + `api` 对象"那两大块零交叉引用** —— 所以这一刀是整块搬，不是切断依赖。
// `index.ts` 以 `export … from "./sse"` 再导出：住址换在实现里，接口面留在 index，
// 所有 `from "../api"` 的消费者一字未动（它们按目录 index 解析，本来就不认文件名）。
//
// 事件协议与 api/chat.py 一一对应；前端永远以 message_replace / 权威文本为最终真相。
import { apiBase, authHeaders } from "../lib/dataSource";
import { parseSseFrame, splitSseFrames } from "../lib/stream";

// ---- SSE 对话流 ----------------------------------------------------------------
// 事件协议与 api/chat.py 一一对应；前端永远以 message_replace / 权威文本为最终真相。

export type ChatEvent =
  | { type: "start"; role: { role_id: string; role_name: string } }
  | { type: "token"; text: string }
  | { type: "thinking"; text: string }
  | { type: "message_replace"; text: string }
  | { type: "context_trimmed"; dropped: number; kept: number }
  // ENGI-36 B：这一轮实际由云端后端答的话（请求的是本地档、回退链静默降级）。只带后端名，
  // 不带内容/端点/凭据。每轮最多一条；正常本地或正常云端都不会发。
  | { type: "answered_by"; backend: string }
  | { type: "tool_call"; name: string; args: Record<string, unknown> }
  | { type: "tool_result"; name: string; content: string }
  | { type: "error"; detail: string }
  // `stopped` 是后端对"这一轮是用户叫停的"的记账（#18）。它有两个读者：流式那半截由
  // `lib/stream` 的 reduce 带到 live 气泡上（reload 失败时的兜底）；落库的半句经
  // `MessageRow.stopped` 随回放展示（R26-13 尾）。客户端自己按的停另有 `signal.aborted`。
  | { type: "end"; stopped?: boolean };

/** POST 一条 SSE 请求，把响应流按帧交给 `onEvent` —— `streamChat` / `streamEdit` 的公共体。
 *
 *  为什么抽出来（2026-10-04 审查快照"streamChat/streamEdit 逐行复制 55 行 SSE 循环"）：
 *  两条各带一份 fetch-失败处理 + `!res.ok` 详情提取 + 读流循环，共 55 行**逐行相同** ——
 *  改一处不改另一处的症状是"编辑那条路的错误处理渐渐跟对话那条不一样"，而且没人会发现
 *  （两边跑起来都"看着正常"）。现在帧切分与解析（`lib/stream.ts` 的可测纯函数）只有一处。
 *
 *  语义合并自两份原件：中止（`AbortError`）**不是**错误 —— 静默结束并补发 `end`，
 *  由调用方做收尾（回放 checkpoint 拿到已生成的部分）；HTTP 非 2xx 时尽力读 `detail`
 *  给用户一句话，读不出就退回 `HTTP <code>`。
 */
async function postSse(
  path: string,
  body: unknown,
  onEvent: (ev: ChatEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  let res: Response;
  try {
    res = await fetch(`${apiBase()}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify(body),
      signal, // 用户点「停止」→ controller.abort()，这里会以 AbortError 结束
    });
  } catch (e) {
    if ((e as Error).name !== "AbortError") {
      onEvent({ type: "error", detail: (e as Error).message });
    }
    onEvent({ type: "end" });
    return;
  }
  if (!res.ok || !res.body) {
    let detail = `HTTP ${res.status}`;
    try {
      detail = ((await res.json()) as { detail?: string }).detail || detail;
    } catch {
      /* keep */
    }
    onEvent({ type: "error", detail });
    onEvent({ type: "end" });
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      // 帧切分与解析走 lib/stream.ts 的纯函数（可测）：半帧留在缓冲里，
      // 坏帧被消化成"这一帧没有事件"而不是抛异常中断整条流。
      const { frames, rest } = splitSseFrames(buf);
      buf = rest;
      for (const frame of frames) {
        const ev = parseSseFrame(frame);
        if (ev) onEvent(ev as ChatEvent);
      }
    }
  } catch (e) {
    // 中断不是错误：静默结束，由调用方做收尾（回放 checkpoint 拿到已生成的部分）
    if ((e as Error).name !== "AbortError") {
      onEvent({ type: "error", detail: (e as Error).message });
    }
  }
}

/** 编辑一条自己发过的消息并从那里重新生成（SSE 事件流与 streamChat 完全一致 —— 同一个 `postSse`）。 */
export async function streamEdit(
  threadId: string,
  messageId: string,
  content: string,
  onEvent: (ev: ChatEvent) => void,
  signal?: AbortSignal,
  image?: string | null, // 重新生成/编辑时保留原图（多模态传图，2026-09-18）
): Promise<void> {
  await postSse(
    `/api/session/${threadId}/messages/edit`,
    { message_id: messageId, content, ...(image ? { image } : {}) },
    onEvent,
    signal,
  );
}

export async function streamChat(
  threadId: string,
  message: string,
  onEvent: (ev: ChatEvent) => void,
  signal?: AbortSignal,
  image?: string | null, // 多模态传图：data URL（None = 纯文本）
): Promise<void> {
  await postSse(
    "/api/chat",
    { thread_id: threadId, message, ...(image ? { image } : {}) },
    onEvent,
    signal,
  );
}
