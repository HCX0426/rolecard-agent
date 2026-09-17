// lib/uploadOutcome.ts 的测试：上传的三种结局 + 抽取阶段的四种结局。
//
// 为什么值得表驱动地测：上传是"用户最怕出错"的动作，而这三种结局的文案此前内联在
// ChatPage 的 60 行 if/else 里，只能靠人工点页面验。最坏的情况不是报错，而是
// **上传其实成功了、用户却以为失败了**（或反过来）—— 那会让人不敢用这个功能。

import { describe, expect, it } from "vitest";
import { describeExtract, describeUpload, formatBytes } from "./uploadOutcome";

const base = { task_id: "ing_1", reused: false, file: "报告.pdf" };

describe("describeUpload", () => {
  it("pending（未给 status）：登记成功但读不了 —— 必须说清「没读到」而不是「成功了」", () => {
    const out = describeUpload({ ...base, status: "pending" });
    expect(out?.tone).toBe("warn");
    expect(out?.text).toContain("已登记");
    expect(out?.text).toContain("无法解析");
  });

  it("缺 status 字段与 pending 同样处理（老响应兼容）", () => {
    expect(describeUpload({ ...base })?.tone).toBe("warn");
  });

  it("parsed：解析了但没文本（扫描件）—— 明确「暂未入检索」", () => {
    const out = describeUpload({ ...base, status: "parsed" });
    expect(out?.tone).toBe("warn");
    expect(out?.text).toContain("没有提取到文本");
    expect(out?.text).toContain("暂未入检索");
  });

  it("indexed：返回 null，交给抽取阶段继续判断", () => {
    expect(describeUpload({ ...base, status: "indexed" })).toBeNull();
  });

  it("重复上传带出处说明，但不改变语气", () => {
    const out = describeUpload({ ...base, status: "pending", reused: true });
    expect(out?.text).toContain("此前已登记");
  });
});

describe("describeExtract", () => {
  const empty = { written: [], conflicts: [], notes: [] };

  it("写入成功 → ok 语气 + 条数 + 指标名 + 未校验提示", () => {
    const out = describeExtract(
      { ...empty, written: [{ index_name: "结石直径" }, { index_name: "血糖" }] },
      "",
      "报告.pdf",
    );
    expect(out.tone).toBe("ok");
    expect(out.text).toContain("2 项指标");
    expect(out.text).toContain("结石直径、血糖");
    expect(out.text).toContain("未经人工校验");
  });

  it("只有冲突项：说明「按规则未写入」并指路手动补录", () => {
    const out = describeExtract({ ...empty, conflicts: [{ index_name: "结石" }] }, "", "报告.pdf");
    expect(out.tone).toBe("warn");
    expect(out.text).toContain("存疑指标");
    expect(out.text).toContain("未写入");
  });

  it("既没写入也没冲突：如实说没识别出可入档的指标", () => {
    const out = describeExtract(empty, "", "报告.pdf");
    expect(out.text).toContain("未识别出可入档的指标");
  });

  it("skipped 的三种原因都翻译成人话", () => {
    const cases: [string, string][] = [
      ["already_extracted", "此前已识别过"],
      ["no_text", "没有可抽取的文本"],
      ["no_model", "当前没有可用的模型后端"],
    ];
    for (const [skipped, expected] of cases) {
      const out = describeExtract({ ...empty, skipped }, "", "报告.pdf");
      expect(out.text).toContain(expected);
      expect(out.text).toContain("原文已入检索");
    }
  });

  it("未知的 skipped 原因原样透出（不吞掉后端的信号）", () => {
    expect(describeExtract({ ...empty, skipped: "weird_reason" }, "", "f").text).toContain(
      "weird_reason",
    );
  });

  it("结果为空（抽取请求失败）：带上错误原因，且说明上传本身是成功的", () => {
    const out = describeExtract(null, "502 Bad Gateway", "报告.pdf");
    expect(out.tone).toBe("warn");
    expect(out.text).toContain("502 Bad Gateway");
    expect(out.text).toContain("原文已入检索");
  });

  it("notes 追加到末尾（弱校对等如实标注不能被吞）", () => {
    const out = describeExtract(
      { ...empty, written: [{ index_name: "x" }], notes: ["校对模式为同模型复查"] },
      "",
      "f",
    );
    expect(out.text).toContain("备注：");
    expect(out.text).toContain("同模型复查");
  });
});

describe("formatBytes", () => {
  it("按量级选单位", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(999)).toBe("999 B");
    expect(formatBytes(2048)).toBe("2.0 KB");
    expect(formatBytes(3 * 1024 * 1024)).toBe("3.0 MB");
  });
});
