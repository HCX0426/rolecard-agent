/**
 * 上传结果 → 用户能看懂的一句话。纯函数，便于测试。
 *
 * ## 为什么值得单独抽出来
 *
 * 上传是有**三种结局**的动作（登记但读不了 / 解析了但没文本 / 已入索引并抽取指标），
 * 每种都要给不同的语气与说明。这段判断此前内联在 ChatPage 的 60 行 if/else 里，只能靠
 * 人工点一遍页面来验 —— 而"上传成功但用户以为失败了"正是这类分支最容易出的问题。
 *
 * 抽出来之后，"每种后端状态对应哪句话"可以用表驱动的断言钉死。
 */

export type UploadTone = "info" | "ok" | "warn";

export interface UploadResponse {
  task_id: string;
  reused: boolean;
  file: string;
  status?: string;
  parsed?: boolean;
}

export interface ExtractLike {
  skipped?: string | null;
  written: { index_name: string }[];
  conflicts: { index_name: string }[];
  notes: string[];
}

export interface Outcome {
  tone: UploadTone;
  text: string;
}

/** 上传本身的结局（还没走抽取）。返回 null = 已入索引，需要继续看抽取结果。 */
export function describeUpload(r: UploadResponse): Outcome | null {
  const suffix = r.reused ? "（同一文件此前已登记）" : "";
  if (!r.status || r.status === "pending") {
    return {
      tone: "warn",
      text: `「${r.file}」已登记，但当前无法解析（类型不支持，或图片 OCR 未配置）${suffix}`,
    };
  }
  if (r.status === "parsed") {
    return {
      tone: "warn",
      text: `「${r.file}」已解析，但没有提取到文本（可能是扫描件），暂未入检索${suffix}`,
    };
  }
  return null; // indexed → 交给 describeExtract
}

const SKIP_REASON: Record<string, string> = {
  already_extracted: "此前已识别过，不重复写入",
  no_text: "没有可抽取的文本",
  no_model: "当前没有可用的模型",
};

/** 抽取阶段的结局。`extractError` 非空 = 抽取请求本身失败（上传仍然算成功）。 */
export function describeExtract(
  result: ExtractLike | null,
  extractError: string,
  fileName: string,
): Outcome {
  if (result?.skipped) {
    const why = SKIP_REASON[result.skipped] ?? result.skipped;
    return { tone: "warn", text: `「${fileName}」${why}（原文已入检索，可直接提问）` };
  }
  if (!result) {
    return {
      tone: "warn",
      text: `「${fileName}」AI 识别指标失败：${extractError}（原文已入检索，可直接提问）`,
    };
  }
  let text: string;
  let tone: UploadTone = "warn";
  if (result.written.length > 0) {
    const names = result.written.map((w) => w.index_name).join("、");
    text = `「${fileName}」AI 已提取 ${result.written.length} 项指标：${names}（均标记【未经人工校验】，可在「数据」页核对）`;
    tone = "ok";
  } else if (result.conflicts.length > 0) {
    const names = result.conflicts.map((c) => c.index_name).join("、");
    text = `「${fileName}」识别出 ${result.conflicts.length} 项存疑指标（${names}），按规则未写入 —— 可在「数据」页手动补录`;
  } else {
    text = `「${fileName}」未识别出可入档的指标（原文已入检索，可直接提问）`;
  }
  if (result.notes.length > 0) text += `　备注：${result.notes.join("；")}`;
  return { tone, text };
}

/** 人话的字节数（上传目录回收用）。 */
export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}
