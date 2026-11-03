import { describe, expect, it } from "vitest";

import { ApiError } from "../api";
import { describeError } from "./errors";

describe("describeError 的分档", () => {
  it("status=0 且 kind=network：底层英文不透传，给一句可行动的话", () => {
    const e = new ApiError(0, "网络错误：TypeError: Failed to fetch", "network");
    expect(describeError(e)).toBe("网络错误：后端连不上，请确认服务已启动");
  });

  // 这一条是 2026-11-03 立的全部理由（审查 A1）：超时与断网从前共用 status=0，
  // 于是这里对超时也说"请确认服务已启动" —— 后端明明还在跑（上传已落盘、索引在建），
  // 这句话催用户去重启服务并照着"假失败"再点一次，正是 api/index.ts:74-78 立论要防的。
  // 两条既有断言各自绿：api.test.ts 测的是 ApiError.message（抛出的那层），
  // 这条文件从前只测 network 形状 —— 拼起来恰好没人穿过"超时 → describeError"。
  it("超时不许被改写成「确认服务已启动」：原样透传那句诚实的话", () => {
    const e = new ApiError(0, "已等待 300s 仍未返回，前端停止等待（那一端可能仍在处理：稍后刷新看看结果）", "timeout");
    const text = describeError(e);
    expect(text).toContain("仍在处理");
    expect(text).not.toContain("确认服务已启动");
  });

  it("status=0 而 kind 缺失 ⇒ 也原样透传（分不清就不赌那一句）", () => {
    // 赌错的代价不对称：漏一句"可行动的提示"只是不够热情，说反话会让人白重做一次。
    const e = new ApiError(0, "本机程序没在限时内回答。");
    expect(describeError(e)).toBe("本机程序没在限时内回答。");
  });

  it("409/429：服务端 detail 已带指引（等 N 秒 / 谁在占用），原样透传", () => {
    const busy = new ApiError(409, "该会话正在生成回复，请等它说完再发");
    const slow = new ApiError(429, "Too Many Requests：3 秒后再试。");
    expect(describeError(busy)).toBe("该会话正在生成回复，请等它说完再发");
    expect(describeError(slow)).toBe("Too Many Requests：3 秒后再试。");
  });

  it("普通 4xx/5xx：服务端中文 detail 原样透传", () => {
    expect(describeError(new ApiError(404, "找不到这份角色卡"))).toBe("找不到这份角色卡");
  });

  it("非 ApiError：有 message 取 message，空则给 fallback", () => {
    expect(describeError(new Error("JSON 解析失败"))).toBe("JSON 解析失败");
    expect(describeError(new Error(""), "炸了")).toBe("炸了");
    expect(describeError(undefined, "炸了")).toBe("炸了");
  });
});
