// limits.ts 的三臂：health 正常下发 / health 异常 / 数值缺失 —— 上限单一出处契约的
// 前端侧（后端那一半在 tests/test_auth.py 的 health 契约用例里）。
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
});
