// 静默状态那一格的措辞（`S-8` + `R26-45`）。钉的是四件容易写歪的事：
//   1. `why === null` 必须写成**肯定句**（"现在随时能开口"），不能留空 —— 空着用户读成「没算出来」；
//   2. 时刻按**本地日历**说"今天/明天"，跨月才给日期 —— 后端给的是带时区的 UTC ISO，
//      直接截字符串会在晚上显示出"昨天 23:5x"；
//   3. 给不出时刻的那两种阻塞（未读封顶、正在对话）**不提"下一次大约"**，不编一个时刻。

import { describe, expect, it } from "vitest";

import type { QuietStatus } from "../api";
import { formatNextOk, quietParts } from "./quiet";

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
