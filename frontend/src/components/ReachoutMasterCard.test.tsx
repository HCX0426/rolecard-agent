// @vitest-environment jsdom
//
// ReachoutMasterCard（主动开口总闸条）接线测试。用例随组件**整体迁来** MemoryPage.test.tsx
// （2026-10-30：总闸与收件箱折叠窗口从记忆页签搬去角色卡页）—— 断言与桩保持原样，
// 只换 render 目标与住所；mock 的 RuntimePayload 形状与 MemoryPage.test 同一份口径。
//
// 钉的两件事（搬来之前就钉着的，搬家不改语义）：
//  - 窗口档位清单逐项来自 payload 的 `choices`（`R102-19`）—— 不是写死三个 option；
//  - 保存走 runtime 覆盖通道（即时生效），不是"建议改重启"的只读展示。

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    get: vi.fn(),
    put: vi.fn(),
  },
}));

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, api: apiMock };
});

import { ReachoutMasterCard } from "./ReachoutMasterCard";

beforeEach(() => {
  vi.clearAllMocks();
  apiMock.get.mockResolvedValue({
    note: "",
    groups: [
      {
        key: "reachout",
        label: "主动开口",
        items: [
          {
            // 真实后端：`key` 是 **env 名**，`field` 才是 Settings 字段名（保存按它）。
            key: "REACHOUT_ENABLED",
            field: "reachout_enabled",
            label: "全局总闸",
            value: "1",
            default: "1",
            changed: false,
            overridden: false,
            override_value: null,
            kind: "bool",
            choices: null,
          },
          {
            key: "REACHOUT_MERGE_DAYS",
            field: "reachout_merge_days",
            label: "收件箱合并窗口（天）",
            value: "3",
            default: "1",
            changed: false,
            overridden: false,
            override_value: null,
            kind: "int",
            choices: ["1", "3", "7", "14"],
          },
        ],
      },
    ],
  });
  apiMock.put.mockResolvedValue({ note: "", groups: [] });
});

describe("主动开口总闸条（2026-10-30 从记忆页签搬来角色卡页）", () => {
  it("总闸与窗口可写：档位跟着 payload 的 choices 走（R102-19），保存走 runtime 通道", async () => {
    render(<ReachoutMasterCard />);
    // 单写点：这里的可写开关（stub 的 reachout_enabled 生效值 = 1 → 已开启）
    expect(await screen.findByRole("button", { name: "总闸已开启" })).toBeTruthy();
    // 窗口下拉按**生效值**选中（stub 给 3），而不是按出厂默认 1
    const select = (await screen.findByLabelText(/收件箱/)) as HTMLSelectElement;
    expect(select.value).toBe("3");
    // 档位清单逐项来自 payload 的 `choices`（`R102-19`）：14 不在组件兜底的那三个里 ——
    // 看得见它，才说明这一屏没有退回"写死三个 option、后端加档它不知道"的旧形态。
    expect(screen.getByRole("option", { name: "每 14 天一摞" })).toBeTruthy();
    fireEvent.change(select, { target: { value: "7" } });
    await waitFor(() =>
      expect(apiMock.put).toHaveBeenCalledWith(
        "/api/settings/runtime",
        expect.objectContaining({ values: { reachout_merge_days: "7" } }),
      ),
    );
  });

  it("点总闸走 runtime 覆盖保存", async () => {
    render(<ReachoutMasterCard />);
    fireEvent.click(await screen.findByRole("button", { name: "总闸已开启" }));
    await waitFor(() =>
      expect(apiMock.put).toHaveBeenCalledWith(
        "/api/settings/runtime",
        expect.objectContaining({ values: { reachout_enabled: "0" } }),
      ),
    );
  });
});
