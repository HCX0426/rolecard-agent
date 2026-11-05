// @vitest-environment jsdom
import { describe, expect, it } from "vitest";

import { summarizeFailures } from "./ingestErrors";

/**
 * 导入失败的原因必须出现在界面上（用户 2026-11-06「我也看不到啊」）。
 *
 * 从前那句「详情见进度条目」是指向不存在东西的谎话：error 从进度端点读回来了，
 * 却只被用来数个数，states 一出函数就丢。这一族判据钉的是"原因看得见 + 不重复刷屏
 * + 不撑爆状态条"，都是纯文本层的事实，不需要起 React。
 */
describe("summarizeFailures 把原因带出来", () => {
  it("有原因时原样带出（不许退化成「详情见进度条目」那种空话）", () => {
    const out = summarizeFailures(["嵌入模型与已有索引的维度不一致。"]);
    expect(out).toContain("嵌入模型与已有索引的维度不一致");
    expect(out).not.toContain("详情见进度条目");
  });

  it("同一条原因重复多次只写一次（整批文件常栽在同一因上）", () => {
    const e = "本地 OCR 未配置";
    const out = summarizeFailures([e, e, e]);
    expect(out.split(e).length - 1).toBe(1);
  });

  it("全空/全 null 时如实说没给原因，不拿空串冒充", () => {
    expect(summarizeFailures([null, "", "   "])).toContain("未给出原因");
  });

  it("超长原因截断，避免堆栈把状态条撑爆", () => {
    const long = "x".repeat(300);
    const out = summarizeFailures([long]);
    expect(out.length).toBeLessThan(200);
    expect(out).toContain("…");
  });

  it("超过 max 类原因时报「另有 N 类」（数量可信，不假装全给了）", () => {
    const out = summarizeFailures(["原因A", "原因B", "原因C"], 2);
    expect(out).toContain("原因A");
    expect(out).toContain("原因B");
    expect(out).not.toContain("原因C");
    expect(out).toContain("另有 1 类");
  });
});
