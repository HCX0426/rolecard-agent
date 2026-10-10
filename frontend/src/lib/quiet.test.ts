// 静默状态那一格的措辞（`S-8` + `R26-45`）。钉的是四件容易写歪的事：
//   1. `why === null` 必须写成**肯定句**（"现在随时能开口"），不能留空 —— 空着用户读成「没算出来」；
//   2. 时刻按**本地日历**说"今天/明天"，跨月才给日期 —— 后端给的是带时区的 UTC ISO，
//      直接截字符串会在晚上显示出"昨天 23:5x"；
//   3. 给不出时刻的那两种阻塞（未读封顶、正在对话）**不提"下一次大约"**，不编一个时刻。

import { describe, expect, it } from "vitest";

import type { QuietStatus } from "../api";
import { formatMessageTs, formatNextOk, formatUtcNaive, parseMessageTs, quietParts } from "./quiet";

function status(over: Partial<QuietStatus> = {}): QuietStatus {
  return {
    role_id: "elysia",
    role_name: "爱莉希雅",
    why: "距上次说话不足 66 分钟",
    next_ok_at: null,
    streak: 1,
    unread: 1,
    ...over,
  };
}

describe("formatNextOk", () => {
  const now = new Date(2026, 8, 26, 15, 21); // 本地 2026-09-26 15:21

  it("当天只说时刻，第二天说「明天」，再往后才给日期", () => {
    expect(formatNextOk(new Date(2026, 8, 26, 16, 34).toISOString(), now)).toBe("今天 16:34");
    expect(formatNextOk(new Date(2026, 8, 27, 8, 0).toISOString(), now)).toBe("明天 08:00");
    expect(formatNextOk(new Date(2026, 9, 3, 8, 0).toISOString(), now)).toBe("10月3日 08:00");
  });

  it("跨到昨天/更早的不给（那说明时刻已经过期，界面不该说「下一次大约」）", () => {
    // 这条钉的是"后端算出一个过去的时刻"这种故障：函数照实翻译，别替它圆场。
    expect(formatNextOk(new Date(2026, 8, 25, 22, 0).toISOString(), now)).toBe("9月25日 22:00");
  });

  it("没有时刻、或读不出时刻，一律空串", () => {
    expect(formatNextOk(null, now)).toBe("");
    expect(formatNextOk("不是时间", now)).toBe("");
  });
});

describe("formatUtcNaive（R102-18 的唯一换算出口）", () => {
  // 期望值必须**从同一个时刻现算**（经本地时区渲染），不写死 +8 —— 这样在别的时区
  // （CI 的 UTC runner）跑也成立；写死偏移的用例只在开发机上绿，那是假绿。
  const pad = (n: number) => String(n).padStart(2, "0");
  const local = (d: Date) =>
    `${d.getMonth() + 1}-${d.getDate()} ${pad(d.getHours())}:${pad(d.getMinutes())}`;

  it("库里的 UTC 裸串换算成本地展示（不是把它当本地直接读）", () => {
    const t = new Date(Date.UTC(2026, 9, 2, 10, 32));
    expect(formatUtcNaive("2026-10-02 10:32:00")).toBe(local(t));
    expect(formatUtcNaive("2026-10-02T10:32:00")).toBe(local(t)); // ISO 形态同一口径
    expect(formatUtcNaive("2026-10-02 10:32:00", true)).toBe(
      `${pad(t.getHours())}:${pad(t.getMinutes())}`,
    );
  });

  it("已带时区的串不再补 Z；解析不了的原样透出（不装作换过）", () => {
    const t = new Date("2026-10-02T10:32:00+08:00");
    expect(formatUtcNaive("2026-10-02T10:32:00+08:00")).toBe(local(t));
    expect(formatUtcNaive("不是时间")).toBe("不是时间");
    expect(formatUtcNaive(null)).toBe("");
    expect(formatUtcNaive(undefined)).toBe("");
  });
});

describe("parseMessageTs（消息时间戳，格式即纪元 —— 后端 core/clock.py 同一政策）", () => {
  it("纪元后的 UTC ISO-Z 按它自己带的时区解；空串/解析不了是 null", () => {
    expect(parseMessageTs("2026-10-07T02:30:00Z")?.getTime()).toBe(
      new Date("2026-10-07T02:30:00Z").getTime(),
    );
    expect(parseMessageTs("")).toBeNull();
    expect(parseMessageTs("不是时间")).toBeNull();
    expect(parseMessageTs(null)).toBeNull();
  });

  it("纪元前的本地 naive 串按**本地**解（旧写法的真实语义，别把它当 UTC）", () => {
    expect(parseMessageTs("2026-10-07 10:30:00")?.getTime()).toBe(
      new Date(2026, 9, 7, 10, 30, 0).getTime(),
    );
  });

  it("同一时刻两种写法解析出同一瞬间 —— 切版本瞬间的跨族配对（回答耗时）才不是碰运气", () => {
    const pad = (n: number) => String(n).padStart(2, "0");
    const d = new Date();
    const legacy =
      `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
      `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
    const modern = d.toISOString().replace(/\.\d{3}Z$/, "Z");
    const gap = Math.abs(parseMessageTs(modern)!.getTime() - parseMessageTs(legacy)!.getTime());
    expect(gap).toBeLessThan(2000);
  });
});

describe("formatMessageTs（消息时刻的屏幕显示，2026-10-10 修「变成 2026-10-10T06:45:56Z」）", () => {
  // 期望值同样**从同一时刻现算**（经本地时区渲染），不写死 +8 —— CI 的 UTC runner 也要成立。
  const pad = (n: number) => String(n).padStart(2, "0");
  const full = (d: Date) =>
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;

  it("新族 UTC ISO-Z → 本地钟面、无 T 无 Z（用户要的形状）", () => {
    const t = new Date("2026-10-10T06:45:56Z");
    const out = formatMessageTs("2026-10-10T06:45:56Z");
    expect(out).toBe(full(t));
    // 这两个断言才是这次报告的正面：屏幕上不该再出现协议字符，也不该是 UTC 钟面
    expect(out).not.toContain("T");
    expect(out).not.toContain("Z");
  });

  it("旧族本地 naive **原样显示**，绝不当 UTC 移走（与 formatUtcNaive 的分野）", () => {
    // 消息族里空格 = 旧本地（上一条 parseMessageTs 的政策）。若错用 formatUtcNaive，
    // 这一串会被当 UTC 加 8 小时显示，等于把用户升级前的历史时刻整体挪错。
    expect(formatMessageTs("2026-09-27 12:30:31")).toBe("2026-09-27 12:30:31");
  });

  it("解析不了的原样透出，空值给空串（不装作换过）", () => {
    expect(formatMessageTs("不是时间")).toBe("不是时间");
    expect(formatMessageTs(null)).toBe("");
    expect(formatMessageTs(undefined)).toBe("");
    expect(formatMessageTs("")).toBe("");
  });
});

describe("quietParts", () => {
  it("静默中：主句就是闸门那句，时刻另起一段", () => {
    const p = quietParts(
      status({ next_ok_at: new Date(2026, 8, 26, 16, 34).toISOString() }),
      new Date(2026, 8, 26, 15, 21),
    );
    expect(p.head).toBe("爱莉希雅 静默中 · 距上次说话不足 66 分钟");
    expect(p.when).toBe("今天 16:34");
    expect(p.ready).toBe(false);
  });

  // 这条钉的是 `R26-45` 那处改动的另一半：退避从句子搬进徽章，所以**主句里不该再有它**，
  // 而徽章要带着那个数 —— 两处都断言，才不会哪天又拼回一条长串。
  it("退避是徽章不是句子的尾巴", () => {
    expect(quietParts(status()).badge).toBe("连着 1 条没被回 · 已退避");
    expect(quietParts(status()).head).not.toContain("退避");
    expect(quietParts(status({ streak: 0 })).badge).toBe("");
  });

  it("没被挡住时写成肯定句，不留空、也不挂徽章", () => {
    const p = quietParts(status({ why: null, next_ok_at: null, streak: 0, unread: 0 }));
    expect(p.head).toBe("爱莉希雅 现在随时能开口");
    expect(p.badge).toBe("");
    expect(p.when).toBe("");
    expect(p.ready).toBe(true);
  });

  it("要他回话/正在对话这类阻塞给不出时刻 —— 那一段就是空，不编一个时刻", () => {
    const p = quietParts(status({ why: "未读堆积已达上限", next_ok_at: null }));
    expect(p.head).toContain("未读堆积已达上限");
    expect(p.when).toBe("");
  });
});
