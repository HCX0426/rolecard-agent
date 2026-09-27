// @vitest-environment jsdom
/**
 * 上行同步这一层（M7）的客户端。钉的是四件"错了没人报"的事：
 *
 *   1. **这几发必须打本机**：不带 `apiBase()` 的前缀、也不带当前那枚云端凭据当 Authorization。
 *      接错的症状最难看 —— 云端态下"把本机这份推上去"会变成"让云端把云端自己推给云端"。
 *   2. **对面是谁**从 `dataSource` 现取，不在这里存第二份（存了就会漂：改了密码不重连，
 *      这份里还是旧口令）。
 *   3. 送出去的 body 形状：勾了的类、选中的档、逐条裁决的键。
 *   4. 对面的 `detail` 要成为人能读的那句话（FastAPI 的 detail 可能是字符串也可能是 422 数组）。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

import { save as saveDataSource, type DataSource } from "./dataSource";
import {
  SYNC_ITEMS,
  applyUpload,
  canKeepBoth,
  conflictKey,
  fetchPlan,
  planReads,
  uploadTarget,
} from "./sync";

const CLOUD: DataSource = {
  mode: "cloud",
  base: "https://cloud.test:8123",
  user: "u1",
  secret: "pw",
};

function lastCall(): { url: string; init: RequestInit } {
  const fetchMock = vi.mocked(fetch);
  const call = fetchMock.mock.calls.at(-1) as [string, RequestInit];
  return { url: String(call[0]), init: call[1] };
}

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  vi.unstubAllGlobals();
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify({}), { status: 200 })),
  );
});

describe("lib/sync", () => {
  it("打的是本机同源，且不把云端凭据当调用身份", async () => {
    saveDataSource(CLOUD);
    await fetchPlan(uploadTarget());
    const { url, init } = lastCall();
    expect(url).toBe("/api/sync/plan");
    expect(url).not.toContain("cloud.test");
    expect((init.headers as Record<string, string>).Authorization).toBeUndefined();
  });

  it("对面是哪台、用谁的身份：从数据源现取；本机态压根没有对面", () => {
    expect(uploadTarget()).toBeNull();
    saveDataSource(CLOUD);
    expect(uploadTarget()).toEqual({
      base_url: "https://cloud.test:8123",
      user: "u1",
      secret: "pw",
    });
    // 改了密码但没重新连接 ⇒ 数据源里就是新的那一份。这一层不缓存，所以不可能漂回旧的。
    saveDataSource({ ...CLOUD, secret: "换过了" });
    expect(uploadTarget()?.secret).toBe("换过了");
  });

  it("送出去的是「勾了的类 + 选中的档 + 逐条裁决」", async () => {
    saveDataSource(CLOUD);
    await applyUpload(uploadTarget(), ["memory", "card"], "merge", { "memory:u1": "both" });
    const body = JSON.parse(String(lastCall().init.body));
    expect(body).toMatchObject({
      base_url: "https://cloud.test:8123",
      user: "u1",
      kinds: ["memory", "card"],
      mode: "merge",
      resolutions: { "memory:u1": "both" },
    });
  });

  it("对面的 detail 成为人能读的那句话（字符串与 422 数组都要活下来）", async () => {
    saveDataSource(CLOUD);
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ detail: "对面拒了这组凭据。" }), { status: 401 })),
    );
    await expect(fetchPlan(uploadTarget())).rejects.toThrow(/对面拒了这组凭据/);
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(JSON.stringify({ detail: [{ msg: "字段太长" }] }), { status: 422 }),
      ),
    );
    await expect(fetchPlan(uploadTarget())).rejects.toThrow(/字段太长|HTTP 422/);
  });

  it("「两份都留」只对记忆给，键是 kind:ident", () => {
    expect(canKeepBoth("memory")).toBe(true);
    expect(canKeepBoth("card")).toBe(false);
    expect(canKeepBoth("thread")).toBe(false);
    expect(conflictKey("memory", "uid-1")).toBe("memory:uid-1");
  });

  it("可勾的格子就是后端真的搬的那四类", () => {
    expect(SYNC_ITEMS.map((i) => i.kind).sort()).toEqual(
      ["card", "memory", "reachout", "thread"].sort(),
    );
    // 健康档案与上传原件这一版不给格子（没实现的复选框比没有复选框更坏）
    expect(SYNC_ITEMS.some((i) => /健康|原件|档案/.test(i.label))).toBe(false);
  });

  it("预检那四格：冲突永远排在最后，且只有它带 warn", () => {
    const reads = planReads({
      counts: { only_local: 38, only_remote: 41, same: 17, conflicts: 3 },
      by_kind: {},
      remote_counts: {},
      skipped: [],
      conflicts: [],
      only_local: [],
    });
    expect(reads.map((r) => r.value)).toEqual([38, 41, 17, 3]);
    expect(reads.at(-1)?.tone).toBe("warn");
  });
});
