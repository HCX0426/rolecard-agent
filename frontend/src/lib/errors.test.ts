import { describe, expect, it } from "vitest";

import { ApiError } from "../api";
import { describeError } from "./errors";

describe("describeError 的分档", () => {
  it("ApiError status=0：底层英文不透传，给一句可行动的话", () => {
    const e = new ApiError(0, "网络错误：TypeError: Failed to fetch");
    expect(describeError(e)).toBe("网络错误：后端连不上，请确认服务已启动");
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
