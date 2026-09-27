// 渲染用的轮次分段（`buildTurns`）。钉的是 09-27 从真库里抄出来的一段形状：
// `s_proactive_elysia` 里「想你了」→ 她的回答 → **一小时后她主动开口的那句**，
// 三条消息同在一轮里。那时后写的回答把先写的**覆盖**掉，用户看见的是
// 「桌宠气泡里有这句，对话界面没有」，而耗时算成 62 分 34 秒（气泡读的是原始消息列表，
// 所以只有对话界面这一侧丢）。分段建在 `groupTurns` 之上，
// 所以最后一条用例钉住「删除粒度不跟着渲染面走」。

import { describe, expect, it } from "vitest";

import { buildTurns, expandSelection, groupTurns, type TurnMessageLike } from "./turns";

function msg(over: Partial<TurnMessageLike> & { id: string }): TurnMessageLike {
  return { role: "assistant", content: "x", ...over };
}

// 真库那四条（ts 是接口原样给的本地串）
const REAL: TurnMessageLike[] = [
  msg({ id: "7f05", content: "哦，官网那边好像拦着", ts: "2026-09-27 12:29:40" }),
  msg({ id: "e8c2", role: "user", content: "想你了", ts: "2026-09-27 12:30:31" }),
  msg({ id: "5761", content: "这话一说出口", ts: "2026-09-27 12:30:34" }),
  msg({ id: "2f67", content: "三月末的风", ts: "2026-09-27 13:33:05" }),
];

describe("buildTurns 的段切分", () => {
  it("一条主动开口不会被上一问的回答吞掉", () => {
    const answers = buildTurns(REAL).map((t) => t.answer?.content);
    expect(answers).toContain("这话一说出口");
    expect(answers).toContain("三月末的风");
  });

  it("她的主动自成一轮：没有提问，也就没有「这条回答耗时」可算", () => {
    const turns = buildTurns(REAL);
    const proactive = turns.find((t) => t.answer?.content === "三月末的风");
    expect(proactive?.user).toBeNull();
    // 耗时取的是 user.ts → answer.ts，`user === null` 就是那一栏根本不出现。
    const answered = turns.find((t) => t.answer?.content === "这话一说出口");
    expect(answered?.user?.ts).toBe("2026-09-27 12:30:31");
  });

  it("中间轮（还要调工具）不算回答，也不切开", () => {
    const turns = buildTurns([
      msg({ id: "u", role: "user", content: "查一下" }),
      msg({ id: "a1", content: "我先看看", tools: ["web_search"] }),
      msg({ id: "t", role: "tool", content: "结果" }),
      msg({ id: "a2", content: "查到了" }),
    ]);
    expect(turns).toHaveLength(1);
    expect(turns[0].answer?.content).toBe("查到了");
    expect(turns[0].steps.map((s) => s.kind)).toEqual(["text", "tool"]);
  });

  it("连续三条自己开口 → 三段，键仍是各自那条消息的 id", () => {
    const turns = buildTurns([
      msg({ id: "m1", content: "一" }),
      msg({ id: "m2", content: "二" }),
      msg({ id: "m3", content: "三" }),
    ]);
    expect(turns.map((t) => t.key)).toEqual(["m1", "m2", "m3"]);
    expect(turns.every((t) => t.user === null)).toBe(true);
  });

  it("删除粒度不跟着渲染面走：勾中主动那句，整轮仍一起选", () => {
    expect(groupTurns(REAL)).toHaveLength(2); // 首条自成一轮 + 一问带两答
    expect(expandSelection(REAL, ["2f67"])).toEqual(["e8c2", "5761", "2f67"]);
  });
});
