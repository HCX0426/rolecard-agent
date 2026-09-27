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

  // 上行入口（M7）：本机态压根没有"对面"可推，所以那一格不该出现（出现了就是死路一条）；
  // 云端态它**常驻**（用户 09-27：「同步入口可以在登录后再常驻吧，随时可同步」）。
  it("本机态不给上行入口，云端态常驻一个", () => {
    render(<DataSourceSwitch />);
    expect(screen.queryByText("把本机这份带到云端")).toBeNull();
    localStorage.setItem(
      KEY,
      JSON.stringify({ mode: "cloud", base: "https://cloud.test:8123", user: "u1", secret: "pw" }),
    );
    render(<DataSourceSwitch />);
    expect(screen.getByText("把本机这份带到云端")).toBeTruthy();
  });

  it("点那一行就是四屏的第一屏", () => {
    localStorage.setItem(
      KEY,
      JSON.stringify({ mode: "cloud", base: "https://cloud.test:8123", user: "u1", secret: "pw" }),
    );
    render(<DataSourceSwitch />);
    fireEvent.click(screen.getByText("把本机这份带到云端"));
    expect(screen.getByRole("heading", { name: "要把本机这份带过去吗？" })).toBeTruthy();
  });

  // 「刚登录」是一次性的：它得活过那次整页重载，但答过之后不该再弹。
  // "答过"= 关掉弹层，**不是"挂载看过一眼"** —— 首屏这棵子树在真浏览器里会被重挂一次，
  // 挂载即清的实现会让第二次弹层凭空消失（M7 的两实例探针实测到的就是这个）。
  it("带着「刚登录」标记挂载 = 自动弹第一屏，关掉之后才不再弹", () => {
    localStorage.setItem(
      KEY,
      JSON.stringify({ mode: "cloud", base: "https://cloud.test:8123", user: "u1", secret: "pw" }),
    );
    sessionStorage.setItem("rolecard.dataSource.justLoggedIn", "1");
    const first = render(<DataSourceSwitch />);
    expect(screen.getByRole("heading", { name: "要把本机这份带过去吗？" })).toBeTruthy();
    // 重挂一次（模拟首屏那棵子树被重挂）：仍然弹，而不是"第一眼看走眼就没了"
    first.unmount();
    render(<DataSourceSwitch />);
    expect(screen.getByRole("heading", { name: "要把本机这份带过去吗？" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "这次先不带" }));
    expect(sessionStorage.getItem("rolecard.dataSource.justLoggedIn")).toBeNull();
    render(<DataSourceSwitch />);
    expect(screen.queryByRole("heading", { name: "要把本机这份带过去吗？" })).toBeNull();
  });
});
