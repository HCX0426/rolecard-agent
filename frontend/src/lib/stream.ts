/**
 * SSE 帧解析与对话事件归约 —— 从 ChatPage / api.ts 里抽出来的**纯函数**。
 *
 * ## 为什么要抽出来
 *
 * 这两件事此前都内联在组件与 fetch 循环里，**无法测试**：
 *   - 帧切分（`\n\n` 边界、一次 chunk 里多帧、半帧跨 chunk）；
 *   - 事件归约（token 累加、tool_call/tool_result 配对、message_replace 覆盖、error 追加）。
 *
 * 而它们恰恰是"看起来在工作、边界处出错"的高发区：上一轮项目里就出过一次
 * 「所有写操作静默 422」的事故（见 `api/index.ts` 里那段注释），根因就是序列化层没有测试。
 * 抽成纯函数之后，边界条件可以用一行断言钉住，不需要渲染整个页面。
 *
 * 副作用（setState）留在组件里；这里只做"输入 → 输出"的映射。
 */

/** 「这一轮被中途停止」那句话 —— 前台的 live 气泡与回放的按轮标记共用一份（R26-13 尾），
 *  跟 `describeTrim` 同一条理由：界面文案与测试断言必须说同一句话。
 *  措辞按 C 类改过（09-29）：原来那句「被叫停的 —— 上面那半截停在哪儿就是哪儿」是
 *  本仓自己的口吻，读起来像开发者在打趣；界面只需要陈述两件事：停了、上面那些是真的。 */
export const STOP_HINT = "已停止生成：以上是这一轮已经完成的部分。";

// 与 `api/sse.ts` 的 ChatEvent 保持结构一致（此处只依赖用到的那几个字段，避免循环依赖）。
export interface ChatEventLike {
  type: string;
  text?: string;
  name?: string;
  content?: string;
  detail?: string;
  dropped?: number;
  kept?: number;
  args?: Record<string, unknown>;
  role?: { role_id: string; role_name: string };
  /** end 事件带回的"这一轮是用户叫停的"（后端 `End(stopped=…)`，#18）。 */
  stopped?: boolean;
}

export interface ToolStep {
  /** 稳定 React key：追加时按位置分配（1 起），tool_result 命中更新时原样保留（M7）。 */
  id: number;
  name: string;
  status: "running" | "ok" | "error";
  content: string;
  /** 工具入参摘要（tool_call 带来）：让"过程"可见——搜了什么词、抓了哪个地址。 */
  args?: Record<string, unknown>;
}

/** 流式回答的临时气泡：token 逐段进入，message_replace 用权威文本覆盖。 */
export interface LiveBubble {
  text: string;
  /** 思考模型的推理过程（thinking 事件逐段累加；非思考模型恒为空串）。 */
  thinking: string;
  tools: ToolStep[];
  streaming: boolean;
  /** 这一轮被用户叫停过：屏幕上那半截是**停下来的**，不是说完的（#18 的 `End.stopped`）。
   *  前台气泡拿它显示 `STOP_HINT`；reload 之后由 checkpoint 里的 `MessageRow.stopped` 接棒。 */
  stopped?: boolean;
}

/** 从事件里额外要收集的旁路信息（不进入气泡本体）。 */
export interface StreamMeta {
  /** 历史被上下文预算裁剪时的条数（0/undefined = 没裁）。 */
  trimmed?: { dropped: number; kept: number };
  /** start 事件带回的角色摘要。 */
  role?: { role_id: string; role_name: string };
  /** 收到过 error 事件（用于收尾时决定提示语气）。 */
  errored?: boolean;
  /** 错误详情原文。调用方在流结束后要用它给用户一句能看的话。 */
  errorDetail?: string;
}

export interface ReducedFrame {
  bubble: LiveBubble;
  meta: StreamMeta;
}

/**
 * 把缓冲区分成"完整帧"与"剩余半帧"。
 *
 * SSE 的帧分隔符是空行；服务端（api/chat.py 的 `sse()`）固定写 `\n\n`。
 * 关键点：**半帧必须留在缓冲区里**，否则一个 chunk 正好切在帧中间时会解析失败 —— 这是
 * 流式解析最典型的 bug，且只在网络分片恰好对齐时才复现。
 *
 * 顺带宽容 `\r\n\r\n`：反代/代理偶尔会改写行尾，而多认一种分隔符的成本是零。
 */
export function splitSseFrames(buffer: string): { frames: string[]; rest: string } {
  const frames: string[] = [];
  let rest = buffer;
  for (;;) {
    const lf = rest.indexOf("\n\n");
    const crlf = rest.indexOf("\r\n\r\n");
    let idx = -1;
    let sepLen = 2;
    if (lf >= 0 && (crlf < 0 || lf <= crlf)) {
      idx = lf;
      sepLen = 2;
    } else if (crlf >= 0) {
      idx = crlf;
      sepLen = 4;
    }
    if (idx < 0) break;
    frames.push(rest.slice(0, idx));
    rest = rest.slice(idx + sepLen);
  }
  return { frames, rest };
}

/**
 * 从一个帧里取出事件对象。取不到（心跳/注释帧/半帧）返回 null —— **不抛异常**。
 *
 * 为什么不能抛：调用方在 `for await` 的读取循环里，抛出去会中断整条流。
 * 一个坏帧不该让整轮回答消失。`JSON.parse` 的失败在这里被消化成"这一帧没有事件"。
 */
export function parseSseFrame(frame: string): ChatEventLike | null {
  const line = frame.split("\n").find((l) => l.startsWith("data: "));
  if (!line) return null;
  try {
    const parsed: unknown = JSON.parse(line.slice(6));
    if (parsed && typeof parsed === "object" && typeof (parsed as ChatEventLike).type === "string") {
      return parsed as ChatEventLike;
    }
    return null;
  } catch {
    return null;
  }
}

/**
 * 归约一个事件到「气泡 + 旁路信息」。保持既有行为不变（这是重构，不是改行为）：
 *
 *   - `token`          累加到文本；
 *   - `message_replace` 用权威文本**覆盖**（不是追加）—— 守卫改写/角色缺失兜底都走这里；
 *   - `tool_call`      追加一张"执行中"卡片；
 *   - `tool_result`    把**最近的同名执行中**卡片标记完成；找不到就补一张（事件乱序/丢帧时兜底）；
 *   - `error`          追加一行 `[错误] …`，并把仍在执行的卡片标红；
 *   - `context_trimmed` 只记旁路信息，不污染回答正文。
 */
export function reduceChatEvent(bubble: LiveBubble, ev: ChatEventLike): ReducedFrame {
  const meta: StreamMeta = {};
  let next = bubble;

  switch (ev.type) {
    case "start":
      if (ev.role) meta.role = ev.role;
      break;
    case "token":
      next = { ...bubble, text: bubble.text + (ev.text ?? "") };
      break;
    case "message_replace":
      // 权威文本只覆盖**回答**；思考过程是另一条通道，保留不冲掉。
      next = { ...bubble, text: ev.text ?? "" };
      break;
    case "thinking":
      next = { ...bubble, thinking: bubble.thinking + (ev.text ?? "") };
      break;
    case "tool_call":
      next = {
        ...bubble,
        // 工具卡片只增不重排，按当前长度分配 id 即稳定且可测（新气泡从 1 起）。
        tools: [
          ...bubble.tools,
          {
            id: bubble.tools.length + 1,
            name: ev.name ?? "?",
            status: "running",
            content: "",
            args: ev.args,
          },
        ],
      };
      break;
    case "tool_result": {
      const tools = [...bubble.tools];
      let done = false;
      for (let i = tools.length - 1; i >= 0; i -= 1) {
        if (tools[i].name === ev.name && tools[i].status === "running") {
          // 展开保留 id（React key 稳定），只覆盖结果相关字段。
          tools[i] = { ...tools[i], name: ev.name ?? "?", status: "ok", content: ev.content ?? "" };
          done = true;
          break;
        }
      }
      next = done
        ? { ...bubble, tools }
        : {
            ...bubble,
            // 结果先到（丢帧）：补一张已完成卡片，id 同样按位置分配。
            tools: [
              ...tools,
              { id: tools.length + 1, name: ev.name ?? "?", status: "ok", content: ev.content ?? "" },
            ],
          };
      break;
    }
    case "error":
      meta.errored = true;
      meta.errorDetail = ev.detail ?? "";
      next = {
        ...bubble,
        text: `${bubble.text}\n[错误] ${ev.detail ?? ""}`,
        tools: bubble.tools.map((t) => (t.status === "running" ? { ...t, status: "error" as const } : t)),
      };
      break;
    case "context_trimmed":
      meta.trimmed = { dropped: ev.dropped ?? 0, kept: ev.kept ?? 0 };
      break;
    case "end":
      // 后端每轮都发 `stopped`（正常收尾是 false），所以这里**照实覆盖**而不是只认 true ——
      // 否则一个复用出去的气泡对象会带着上一轮的"已停止"。这个事实的两个读者：live 气泡
      // 在 reload 接棒前显示它（reload 失败时它就是兜底）；落库的半句经 `MessageRow.stopped`
      // 随回放展示（R26-13 尾）。meta 不再另抄一份 —— 填了没人读的字段是台账里的老问题。
      next = { ...bubble, stopped: Boolean(ev.stopped) };
      break;
    default:
      break; // 未知类型由调用方收尾
  }

  return { bubble: next, meta };
}

/** 空气泡（一轮开始时）。 */
export function newLiveBubble(): LiveBubble {
  return { text: "", thinking: "", tools: [], streaming: true };
}

/** 上下文裁剪的人话描述 —— 界面与测试共用同一份措辞。 */
export function describeTrim(dropped: number, kept: number): string {
  if (dropped <= 0) return "";
  return `已折叠早期对话：这一轮送给模型的是最近 ${kept} 条（更早的 ${dropped} 条超出上下文预算）。对话记录没有丢失，往上翻还在。`;
}
