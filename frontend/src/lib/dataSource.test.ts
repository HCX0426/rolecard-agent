// @vitest-environment jsdom
/**
 * 数据源那一层（M5）。钉的是四件"错了没人报"的事：
 *   1. 地址归一化与写入必须同源 —— 存了原样字符串，下次启动判它不合法就**静默回落本机**；
 *   2. 读不到合法值 = 本机，不猜"上次是云端"（猜错的症状是"我的数据没了"）；
 *   3. 凭据头在非 ASCII 账号上不能抛（抛了表现为"点了切换没反应"）；
 *   4. 三种失败要分得开：连不上 / 账号不对 / 对面不是这个服务。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  LOCAL,
  apiBase,
  authHeaders,
  normalizeBase,
  read,
  save,
  tryConnect,
} from "./dataSource";

beforeEach(() => {
  localStorage.clear();
  vi.unstubAllGlobals();
});

describe("normalizeBase", () => {
  it("缺协议补 https、去尾斜杠，只到 host[:port] 为止", () => {
    expect(normalizeBase("cloud.example.cn:8123/")).toBe("https://cloud.example.cn:8123");
    expect(normalizeBase("  http://127.0.0.1:8123 ")).toBe("http://127.0.0.1:8123");
  });

  it("带路径 / 非 http scheme / 空值一律拒（不合法就不是一个服务地址）", () => {
    expect(normalizeBase("https://a.example/api/v1")).toBeNull();
    expect(normalizeBase("javascript:alert(1)")).toBeNull();
    expect(normalizeBase("")).toBeNull();
  });
});

describe("read / save", () => {
  it("默认与坏值都回落本机，不猜上次是什么", () => {
    expect(read()).toEqual(LOCAL);
    localStorage.setItem("rolecard.dataSource.v1", "{坏 JSON");
    expect(read()).toEqual(LOCAL);
    // 存了个空地址 / 空账号 = 这份值本身不可用：回落本机，而不是"半截云端"
    localStorage.setItem(
      "rolecard.dataSource.v1",
      JSON.stringify({ mode: "cloud", base: "", user: "u", secret: "p" }),
    );
    expect(read()).toEqual(LOCAL);
    localStorage.setItem(
      "rolecard.dataSource.v1",
      JSON.stringify({ mode: "cloud", base: "https://a.example", user: "", secret: "p" }),
    );
    expect(read()).toEqual(LOCAL);
  });

  it("存进去的地址是归一后的，重载之后 base 与凭据头仍然对得上", () => {
    save({ mode: "cloud", base: normalizeBase("a.example:8123/") ?? "", user: "u", secret: "p" });
    expect(apiBase()).toBe("https://a.example:8123");
    expect(authHeaders().Authorization).toBe(`Basic ${btoa("u:p")}`);
  });

  it("本机态不带任何 Authorization（同源那套仍由浏览器/代理决定）", () => {
    expect(authHeaders()).toEqual({});
    expect(apiBase()).toBe("");
  });

  it("中文账号能编进凭据头 —— btoa 只吃 latin1，直接编会抛", () => {
    save({ mode: "cloud", base: "https://a.example", user: "爱莉", secret: "口令" });
    const header = authHeaders().Authorization ?? "";
    expect(header.startsWith("Basic ")).toBe(true);
    const back = new TextDecoder().decode(
      Uint8Array.from(atob(header.slice(6)), (c) => c.charCodeAt(0)),
    );
    expect(back).toBe("爱莉:口令");
  });
});

describe("tryConnect", () => {
  function stubFetch(impl: () => Promise<Response>) {
    vi.stubGlobal("fetch", vi.fn(impl));
  }

  it("地址不合法时连请求都不发", async () => {
    stubFetch(async () => new Response("[]"));
    const probe = await tryConnect("https://a.example/path", "u", "p");
    expect(probe.ok).toBe(false);
    expect(fetch).not.toHaveBeenCalled();
  });

  it("401 说成账号问题，而不是笼统的连不上", async () => {
    stubFetch(async () => new Response("", { status: 401 }));
    const probe = await tryConnect("https://a.example", "u", "p");
    expect(!probe.ok && probe.why).toContain("账号或密码不对");
  });

  it("网络错误要说清可能是跨域没放行（那句提示决定他下一步查什么）", async () => {
    stubFetch(async () => {
      throw new TypeError("Failed to fetch");
    });
    const probe = await tryConnect("https://a.example", "u", "p");
    expect(!probe.ok && probe.why).toContain("API_ALLOW_ORIGINS");
  });

  it("200 但回的不是列表 = 对面不是这个服务，不写状态", async () => {
    stubFetch(async () => new Response(JSON.stringify({ hello: "world" }), {
      headers: { "content-type": "application/json" },
    }));
    const probe = await tryConnect("https://a.example", "u", "p");
    expect(!probe.ok && probe.why).toContain("不是角色卡列表");
  });

  it("成功时回的是**归一后的 origin**（界面存它，不存用户手敲的那串）", async () => {
    stubFetch(async () => new Response(JSON.stringify([{ role_id: "r" }])));
    const probe = await tryConnect("a.example:8123/", "u", "p");
    expect(probe.ok && probe.origin).toBe("https://a.example:8123");
  });
});
