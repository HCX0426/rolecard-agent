// @vitest-environment jsdom
//
// ApprovalPanel（命令审批抽屉，架构计划 C·§6.2）的接线测试。
//
// 重点：pending 渲染出可点的批准/拒绝、批准调用 decideApproval(id, 'approve')
// 且操作后 pending 计数写回（侧栏红点同步）。这是"批准=后台执行"的人机触点。

import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
    extractRecord: vi.fn(),
    upload: vi.fn(),
    setModelContext: vi.fn(),
    enhancePrompt: vi.fn(),
    listDomainRecords: vi.fn(),
    addDomainRecord: vi.fn(),
    patchDomainRecord: vi.fn(),
    deleteDomainRecord: vi.fn(),
    getWorkspaceDir: vi.fn(),
    setWorkspaceDir: vi.fn(),
    clearWorkspaceDir: vi.fn(),
    browseTree: vi.fn(),
    getReachouts: vi.fn(),
    markReachoutRead: vi.fn(),
    getApprovals: vi.fn(),
    decideApproval: vi.fn(),
  },
}));

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, api: apiMock };
});

import ApprovalPanel from "./ApprovalPanel";
import type { ApprovalsPage } from "../api";

function page(overrides: Partial<ApprovalsPage> = {}): ApprovalsPage {
  return {
    items: [
      {
        id: 1,
        command: "python run.py --mode prod",
        cwd: "/task/prod",
        role_id: "coder",
        role_name: "工程师",
        thread_id: null,
        status: "pending",
        result: null,
        decide_token: "tok-issued-for-1",
        created_at: "2026-09-18T10:00:00",
        updated_at: "2026-09-18T10:00:00",
      },
      {
        id: 2,
        command: "python run.py",
        cwd: "/task",
        role_id: "coder",
        role_name: "工程师",
        thread_id: null,
        status: "done",
        result: {
          exit_code: 0,
          output: "任务完成\nseed 0 rows",
          duration_ms: 42,
          output_bytes: 64,
        },
        decide_token: null,  // 批过的行令牌已清空
        created_at: "2026-09-18T09:00:00",
        updated_at: "2026-09-18T09:00:10",
      },
    ],
    pending: 1,
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  apiMock.getApprovals.mockResolvedValue(page());
  apiMock.decideApproval.mockImplementation(async (id: number, decision: string) => {
    const p = page();
    return { ...p.items.find((r) => r.id === id)!, status: decision === "approve" ? "approved" : "rejected" };
  });
});

describe("ApprovalPanel 审批抽屉", () => {
  it("渲染待批命令与历史结果，红点计数回传", async () => {
    const onPendingChange = vi.fn();
    render(
      <ApprovalPanel open={true} onClose={() => {}} onPendingChange={onPendingChange} />,
    );
    expect(await screen.findByText("python run.py --mode prod")).toBeTruthy();
    expect(screen.getByText("待批准")).toBeTruthy();
    expect(screen.getByText("已完成")).toBeTruthy();
    // done 结果块可见（pre 里的多行输出，用函数匹配）
    expect(
      screen.getByText((text) => typeof text === "string" && text.includes("任务完成")),
    ).toBeTruthy();
    expect(onPendingChange).toHaveBeenCalledWith(1);
  });

  it("批准调用 decideApproval(approve) 并带上这条记录下发的决定令牌", async () => {
    const onPendingChange = vi.fn();
    render(
      <ApprovalPanel open={true} onClose={() => {}} onPendingChange={onPendingChange} />,
    );
    await screen.findByText("python run.py --mode prod");
    fireEvent.click(screen.getAllByText("批准")[0]);
    expect(apiMock.decideApproval).toHaveBeenCalledWith(1, "approve", "tok-issued-for-1");
  });

  it("拒绝调用 decideApproval(reject)", async () => {
    render(<ApprovalPanel open={true} onClose={() => {}} onPendingChange={() => {}} />);
    await screen.findByText("python run.py --mode prod");
    fireEvent.click(screen.getAllByText("拒绝")[0]);
    expect(apiMock.decideApproval).toHaveBeenCalledWith(1, "reject", "tok-issued-for-1");
  });

  it("令牌被后端拒了（403）就照实报错，不悄悄改状态", async () => {
    const onPendingChange = vi.fn();
    apiMock.decideApproval.mockRejectedValueOnce(new Error("决定令牌已过期，请重新查看待批列表后再批"));
    render(
      <ApprovalPanel open={true} onClose={() => {}} onPendingChange={onPendingChange} />,
    );
    await screen.findByText("python run.py --mode prod");
    fireEvent.click(screen.getAllByText("批准")[0]);
    expect(await screen.findByText(/决定令牌已过期/)).toBeTruthy();
    // 待批计数没被前端自行减掉 —— 那条命令还在等批，界面不能假装它没了。
    expect(onPendingChange).not.toHaveBeenCalledWith(0);
    expect(screen.getByText("待批准")).toBeTruthy();
  });

  it("空列表给出空态文案", async () => {
    apiMock.getApprovals.mockResolvedValue({ items: [], pending: 0 });
    render(<ApprovalPanel open={true} onClose={() => {}} onPendingChange={() => {}} />);
    expect(await screen.findByText("还没有待审批的命令")).toBeTruthy();
  });

  it("关着时不发请求", () => {
    render(<ApprovalPanel open={false} onClose={() => {}} onPendingChange={() => {}} />);
    expect(apiMock.getApprovals).not.toHaveBeenCalled();
  });
});
  it("令牌为空（用过或列表是旧的）：批准/拒绝禁用并说明怎么恢复", async () => {
    apiMock.getApprovals.mockResolvedValue(
      page({ items: [{ ...page().items[0], decide_token: null }] }),
    );
    render(<ApprovalPanel open={true} onClose={() => {}} onPendingChange={() => {}} />);
    await screen.findByText("python run.py --mode prod");
    for (const name of ["批准", "拒绝"]) {
      const btn = screen.getByRole("button", { name }) as HTMLButtonElement;
      expect(btn.disabled).toBe(true);
    }
    expect(screen.getByText(/已经批过了，或者这份列表是旧的/)).toBeTruthy();
    expect(apiMock.decideApproval).not.toHaveBeenCalled();
  });
