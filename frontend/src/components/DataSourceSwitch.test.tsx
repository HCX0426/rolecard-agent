// @vitest-environment jsdom
//
// 切换器这一格的**接线**（M5）。纯函数那层（`lib/dataSource.test.ts`）已经钉过地址归一、
// 回落本机、凭据编码与三种失败文案；这里只补界面这一段：
//   1. 本机态不渲染云端顶栏（一装错就变成"人人都在云端"的假提示）；
//   2. 点那一行出弹层，三格齐；
//   3. 连不上时**不写状态、不重载** —— 这条是"失败就留在本机"的兑现，
//      它只能在这里测：纯函数层看不出"界面有没有偷偷切过去"。

import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import DataSourceSwitch from "./DataSourceSwitch";

// 只替掉「重载」这一件事：直接改 `window.location` 会让 jsdom 换一个 origin 的
// localStorage，于是"存进去的状态"与"读出来的状态"不是同一份 —— 测出来的是个假的。
const { reloadAppMock } = vi.hoisted(() => ({ reloadAppMock: vi.fn() }));
vi.mock('../lib/dataSource', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../lib/dataSource')>()),
  reloadApp: reloadAppMock,
}));

const KEY = "rolecard.dataSource.v1";

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear(); // 「刚登录」标记不能跨用例泄漏：泄漏的症状是上一个用例的对账在读数卡里炸出来
  reloadAppMock.mockReset();
  vi.unstubAllGlobals();
});

function stubFetch(handler: (url: string) => Response) {
  vi.stubGlobal("fetch", vi.fn(async (input: unknown) => handler(String(input))));
}

describe("DataSourceSwitch", () => {
  it("本机态：侧栏只有那一行", () => {
    render(<DataSourceSwitch />);
    expect(screen.getByText("数据源：本机")).toBeTruthy();
  });

  it("点一下出弹层，地址 / 账号 / 密码三格都在", () => {
    render(<DataSourceSwitch />);
    fireEvent.click(screen.getByText("数据源：本机"));
    expect(screen.getByRole("heading", { name: "切到云端" })).toBeTruthy();
    expect(screen.getByLabelText("服务地址")).toBeTruthy();
    expect(screen.getByLabelText("账号")).toBeTruthy();
    expect(screen.getByLabelText("密码 / 访问令牌")).toBeTruthy();
  });

  it("凭据被拒：弹层里出声，状态没写、页面没重载（失败就留在本机）", async () => {
    stubFetch(() => new Response("", { status: 401 }));
    render(<DataSourceSwitch />);
    fireEvent.click(screen.getByText("数据源：本机"));
    fireEvent.change(screen.getByLabelText("服务地址"), {
      target: { value: "http://cloud.test:8123" },
    });
    fireEvent.change(screen.getByLabelText("账号"), { target: { value: "u1" } });
    fireEvent.change(screen.getByLabelText("密码 / 访问令牌"), { target: { value: "不对" } });
    fireEvent.click(screen.getByRole("button", { name: "连接并切换" }));
    expect(await screen.findByText(/账号或密码不对/)).toBeTruthy();
    expect(localStorage.getItem(KEY)).toBeNull();
    expect(reloadAppMock).not.toHaveBeenCalled();
  });

  it("连上了才写状态并重载整页（半换状态比不换更糟）", async () => {
    stubFetch(() => new Response(JSON.stringify([{ role_id: "r" }])));
    render(<DataSourceSwitch />);
    fireEvent.click(screen.getByText("数据源：本机"));
    fireEvent.change(screen.getByLabelText("服务地址"), {
      target: { value: "cloud.test:8123/" },
    });
    fireEvent.change(screen.getByLabelText("账号"), { target: { value: "u1" } });
    fireEvent.change(screen.getByLabelText("密码 / 访问令牌"), { target: { value: "pw" } });
    fireEvent.click(screen.getByRole("button", { name: "连接并切换" }));
    await vi.waitFor(() => expect(reloadAppMock).toHaveBeenCalledOnce());
    // 存的是**归一后的 origin**：存原样字符串的话下次启动会判它不合法而静默回落本机。
    expect(localStorage.getItem(KEY)).toContain('"base":"https://cloud.test:8123"');
  });

  // 顶栏那一格被用户否了（09-27：「那个横幅我感觉没必要」），云端态的可见指示
  // 就只剩侧栏这一行 —— 所以它必须写清"云端 + 谁"，并且一眼能切回去。
  it("云端态：那一行写着「云端 · 账号」，点它就是切回本机（清状态 + 重载）", () => {
    localStorage.setItem(
      KEY,
      JSON.stringify({ mode: "cloud", base: "https://cloud.test:8123", user: "u1", secret: "pw" }),
    );
    render(<DataSourceSwitch />);
    expect(screen.getByText("数据源：云端 · u1")).toBeTruthy();
    // 云端态那一行的按钮只写「切回」（侧栏 206px，账号一长就折行），完整意思在 title 里
    fireEvent.click(screen.getByText("切回"));
    expect(reloadAppMock).toHaveBeenCalledOnce();
    expect(localStorage.getItem(KEY)).toBeNull();
  });

  // 同步入口（M7/M8）：本机态没有"对端"可言，那一格不该出现（出现了就是死路一条）；
  // 云端态它**常驻**（用户 09-27：「同步入口可以在登录后再常驻吧，随时可同步」）。
  it("本机态不给同步入口，云端态常驻一个", () => {
    render(<DataSourceSwitch />);
    expect(screen.queryByText("数据同步")).toBeNull();
    localStorage.setItem(
      KEY,
      JSON.stringify({ mode: "cloud", base: "https://cloud.test:8123", user: "u1", secret: "pw" }),
    );
    render(<DataSourceSwitch />);
    expect(screen.getByText("数据同步")).toBeTruthy();
  });

  it("点那一行就是向导的第一屏（方向 + 三档）", () => {
    localStorage.setItem(
      KEY,
      JSON.stringify({ mode: "cloud", base: "https://cloud.test:8123", user: "u1", secret: "pw" }),
    );
    render(<DataSourceSwitch />);
    fireEvent.click(screen.getByText("数据同步"));
    expect(screen.getByRole("heading", { name: "数据同步" })).toBeTruthy();
    expect(screen.getByRole("radiogroup", { name: "同步方向" })).toBeTruthy();
  });

  // 登录对账（M8）：登录后自动跑一次双向同步。机器判得了的自己搬完；
  // 判不了的（两端均有修改）端出读数卡让人挑 —— 而不是替人决定。
  it("带着「刚登录」标记挂载 = 自动对账，有未决冲突时出读数卡，立即处理落在裁决屏", async () => {
    localStorage.setItem(
      KEY,
      JSON.stringify({ mode: "cloud", base: "https://cloud.test:8123", user: "u1", secret: "pw" }),
    );
    sessionStorage.setItem("rolecard.dataSource.justLoggedIn", "1");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: unknown) =>
        String(input).includes("/api/sync/reconcile")
          ? new Response(
              JSON.stringify({
                pushed: 2,
                pulled: 1,
                written: {},
                left_for_human: [
                  {
                    kind: "memory",
                    ident: "uid-1",
                    mine: { at: "2026-09-25 10:00:00", preview: "本机版本" },
                    theirs: { at: "2026-09-26 10:00:00", preview: "云端版本" },
                  },
                ],
              }),
              { status: 200 },
            )
          : new Response(JSON.stringify({}), { status: 200 }),
      ),
    );
    render(<DataSourceSwitch />);
    expect(await screen.findByText("同步完成")).toBeTruthy();
    expect(screen.getByText("已上传 2 项，已下载 1 项。")).toBeTruthy();
    expect(screen.getByText("1 项在两端均有修改，需要您确认保留哪个版本。")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "立即处理（1）" }));
    expect(await screen.findByText("冲突 1 / 1 · 记忆")).toBeTruthy();
    expect(screen.getByText("保留两个版本")).toBeTruthy();
  });

  it("对账没有未决冲突时不弹卡（不打扰），入口行照常在", async () => {
    localStorage.setItem(
      KEY,
      JSON.stringify({ mode: "cloud", base: "https://cloud.test:8123", user: "u1", secret: "pw" }),
    );
    sessionStorage.setItem("rolecard.dataSource.justLoggedIn", "1");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: unknown) =>
        String(input).includes("/api/sync/reconcile")
          ? new Response(
              JSON.stringify({ pushed: 1, pulled: 0, written: {}, left_for_human: [] }),
              { status: 200 },
            )
          : new Response(JSON.stringify({}), { status: 200 }),
      ),
    );
    render(<DataSourceSwitch />);
    await vi.waitFor(() => {
      expect(screen.getByText(/上次同步/)).toBeTruthy();
    });
    expect(screen.queryByText("同步完成")).toBeNull();
  });
});
