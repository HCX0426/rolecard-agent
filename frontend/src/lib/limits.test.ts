// @vitest-environment jsdom
// limits.ts 的三臂：health 正常下发 / health 异常 / 数值缺失 —— 上限单一出处契约的
// 前端侧（后端那一半在 tests/test_auth.py 的 health 契约用例里）。
// jsdom 环境是最后那条（云端数据源）要的：apiBase() 经 localStorage 读数据源，
// node 环境下 `localStorage` 未定义会走 dataSource 自己的 catch，读到的是"本机"。
import { afterEach, describe, expect, it, vi } from "vitest";

import { fetchLimits } from "./limits";

describe("fetchLimits", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.resetModules();
  });

  it("health 下发的两个上限原样返回", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve({ max_upload_bytes: 123, max_image_bytes: 45 }),
      }),
    );
    await expect(fetchLimits()).resolves.toEqual({ upload: 123, image: 45 });
  });

  it("health 不可达/非 2xx/字段缺失：回落出厂默认而不是撒谎", async () => {
    // 每臂都拿全新模块实例（vi.resetModules 后动态 import）：缓存让静态 import 在
    // 用例之间串味，测的就不是"回落"本身了。
    vi.resetModules();
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("down")));
    const { fetchLimits: fresh } = await import("./limits");
    const first = await fresh();
    expect(first.upload).toBeGreaterThan(0);
    expect(first.image).toBeLessThan(first.upload);

    vi.resetModules();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false }));
    const { fetchLimits: again } = await import("./limits");
    await expect(again()).resolves.toEqual(first);

    vi.resetModules();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }),
    );
    const { fetchLimits: third } = await import("./limits");
    await expect(third()).resolves.toEqual(first);
  });

  it("同一页生命周期内只问一次 health（缓存）", async () => {
    vi.resetModules();
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ max_upload_bytes: 1, max_image_bytes: 1 }),
    });
    vi.stubGlobal("fetch", fetchMock);
    const { fetchLimits: fresh } = await import("./limits");
    await fresh();
    await fresh();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  // 2026-11-03 审查 A5 的另一半：从前 `cached ??=` 会把**回落也缓存**下来，
  // 于是"后端启动晚了/网络抖了一下"这种一次性的失败，会把这对出厂默认钉整页。
  // 现在只有拿到真值才缓存。这条从前没测 —— 三臂各自绿，没人穿过"回落之后还会
  // 不会再问"这一路（一个从不失败的检查的另一半：没被穿过的那条路）。
  it("回落到出厂默认之后，下一次仍然会去问（不把兜底钉死整页）", async () => {
    vi.resetModules();
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: false, json: () => Promise.resolve({}) })
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({ max_upload_bytes: 999, max_image_bytes: 888 }),
      });
    vi.stubGlobal("fetch", fetchMock);
    const { fetchLimits: fresh } = await import("./limits");
    const first = await fresh();
    expect(first.upload).toBeGreaterThan(999); // 出厂默认那一档
    const second = await fresh();
    expect(second).toEqual({ upload: 999, image: 888 }); // 第二问拿到了真值
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("走 api 层而不是裸 fetch：云端数据源形态下问的是**那台后端**", async () => {
    // 上限是给上传做预检的，而上传由 apiBase() 决定打到哪台。从前这里是裸
    // fetch("/api/health")：同源相对路径 = 永远问本机界面自己，云端形态下两台配置
    // 不一样时症状是"前端放行、后端 413"。这条钉住 URL 里带上了数据源的 base。
    vi.resetModules();
    localStorage.setItem(
      "rolecard.dataSource.v1",
      JSON.stringify({ mode: "cloud", base: "https://cloud.test:8123", user: "u", secret: "s" }),
    );
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ max_upload_bytes: 12, max_image_bytes: 34 }),
    });
    vi.stubGlobal("fetch", fetchMock);
    const { fetchLimits: fresh } = await import("./limits");
    await fresh();
    expect(fetchMock.mock.calls[0][0]).toContain("https://cloud.test:8123/api/health");
    localStorage.clear();
  });
});
