// @vitest-environment jsdom
//
// AuditPanel 2026-10-30 重设计的接线测试：分组 pill、计数同源、文本筛选叠加。

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    get: vi.fn(),
  },
}));

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, api: apiMock };
});

import { AuditPanel } from "./AuditPanel";

const ROWS = [
  { id: 1, ts: "2026-10-30 10:00:00", actor: "operator", action: "add_memory_item", target: "memory:", detail_json: '{"chars":5}' },
  { id: 2, ts: "2026-10-30 10:05:00", actor: "operator", action: "create_role", target: "r1", detail_json: null },
  { id: 3, ts: "2026-10-30 10:10:00", actor: "agent", action: "set_role_task_dir", target: "r1", detail_json: '{"path":"D:/x"}' },
  { id: 4, ts: "2026-10-30 10:15:00", actor: "agent", action: "run_command", target: "1", detail_json: null },
];

beforeEach(() => {
  vi.clearAllMocks();
  apiMock.get.mockResolvedValue(ROWS);
});

describe("AuditPanel 分组筛选（2026-10-30 重设计）", () => {
  it("pill 带条数且与筛选同源：点『记忆』只剩记忆行，再点取消回全部", async () => {
    render(<AuditPanel />);
    expect(await screen.findByText(/记忆 1/)).toBeTruthy();
    expect(screen.getByText(/角色 1/)).toBeTruthy();
    expect(screen.getByText(/工作区 1/)).toBeTruthy();
    // run_command 落"审批"族（含 command）—— 空族不摆出来是另一条纪律，这里 4 行各归各族。
    expect(screen.getByText(/审批 1/)).toBeTruthy();
    expect(screen.getByText("4 / 4 条")).toBeTruthy();

    fireEvent.click(screen.getByText(/记忆 1/));
    expect(await screen.findByText("1 / 4 条")).toBeTruthy();
    expect(screen.getByText("add_memory_item")).toBeTruthy();
    expect(screen.queryByText("create_role")).toBeNull();

    // 再点同一个 pill = 取消筛选，回全部
    fireEvent.click(screen.getByText(/记忆 1/));
    expect(await screen.findByText("4 / 4 条")).toBeTruthy();
  });

  it("文本筛选与分组叠加；详情展开时 JSON 是 pretty-print 的", async () => {
    render(<AuditPanel />);
    await screen.findByText(/记忆 1/);
    fireEvent.change(screen.getByPlaceholderText(/筛选/), { target: { value: "r1" } });
    expect(await screen.findByText("2 / 4 条")).toBeTruthy(); // create_role + set_role_task_dir

    // 清空文本筛选，展开带详情的那条 → JSON 换行格式化（压缩串是没人读的）
    fireEvent.change(screen.getByPlaceholderText(/筛选/), { target: { value: "" } });
    fireEvent.click(await screen.findByText('{"chars":5}'));
    const pre = await screen.findByText((_, el) => el?.tagName === "PRE");
    expect(pre.textContent).toContain('\n');
    // 解析不了的详情原样给出（不吞）—— 用另一条带非 JSON 详情的行验证太绕，
    // prettyDetail 的 fallback 由这同一个 try/catch 覆盖，这里只钉 happy path。
    void within;
    void waitFor;
  });
});
