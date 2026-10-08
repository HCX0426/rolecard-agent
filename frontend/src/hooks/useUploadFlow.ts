/** 上传 → 解析 → 入索引 → 结构化抽取 这条链路（从 ChatPage 抽出）。 */
import { useState } from "react";

import { api, type ExtractResult } from "../api";
import { describeExtract, describeUpload, pollUploadProgress } from "../lib/uploadOutcome";
import type { Tone } from "../components/Toast";
import { describeError } from '../lib/errors';

interface UploadFlowDeps {
  /** 当前会话；没有时由 `ensureSession` 现开一个。 */
  sessionId: string | null;
  /** 确保有会话可上传，返回 thread_id（失败返回 null）。 */
  ensureSession: () => Promise<string | null>;
  /** 上传后回填消息区（注入的说明消息会进 checkpoint，回放让用户看到）。 */
  reloadMessages: (threadId: string) => Promise<void>;
  onStatus: (text: string, tone: Tone) => void;
}

/**
 * 为什么抽出来：这段有 45 行、5 个分支（登记/读不了/没文本/已入索引/抽取结果），
 * 与对话渲染完全无关，放在组件里只会稀释主线。
 *
 * 抽取失败**不算上传失败**：原文已可提问，指标提取可以重试 —— 这个语义别在搬运时改掉。
 */
export function useUploadFlow({
  sessionId,
  ensureSession,
  reloadMessages,
  onStatus,
}: UploadFlowDeps) {
  const [uploading, setUploading] = useState(false);

  async function handleUpload(file: File) {
    if (uploading) return;
    let tid = sessionId;
    if (!tid) {
      tid = await ensureSession();
      if (!tid) return;
    }
    setUploading(true);
    try {
      const fd = new FormData();
      fd.append("file", file);
      // 走 api.upload（长超时）：落盘与登记在本请求里，重活已交后台（P3-3），
      // 但 400（超限/空文件）仍是当场回答 —— 这个超时仍然按最坏的网络留。
      let r = await api.upload(tid, fd);

      if (r.status === "processing") {
        // 受理即报一句（文案在 describeUpload 里，单测钉着），然后轮到后台停下来。
        const accepted = describeUpload(r);
        if (accepted) onStatus(accepted.text, accepted.tone);
        try {
          const prog = await pollUploadProgress(() => api.uploadTask(r.task_id));
          if (prog.status === "failed") {
            // 解析失败的信息通道从前是"上传请求直接 500"，现在从进度端点读出 ——
            // 错误原文一字不少（台账 last_error），语气与旧 catch 路径同款。
            onStatus(
              `「${r.file}」解析失败：${prog.error ?? "未知原因"}（可修正环境后重传）`,
              "warn",
            );
            return;
          }
          r = { ...r, status: prog.status }; // 终局塞回既有三态措辞，不新造文案
        } catch (e) {
          onStatus(`上传进度查询失败：${describeError(e)}`, "warn");
          return;
        }
      }

      // 三态反馈（登记但读不了 / 解析了没文本 / 已入索引）：判断逻辑在
      // lib/uploadOutcome.ts 并被单测覆盖 —— 这段分支以前只能靠人工点页面验。
      const uploadOutcome = describeUpload(r);
      if (uploadOutcome) {
        onStatus(uploadOutcome.text, uploadOutcome.tone);
        return;
      }

      // v2.3：已入索引 → 自动触发结构化抽取（独立请求 + 进度提示，不拖慢上传本身）。
      onStatus(`「${r.file}」已入检索索引，AI 识别指标中…`, "info");
      let result: ExtractResult | null = null;
      let extractError = "";
      try {
        result = await api.extractRecord(r.task_id);
      } catch (e) {
        extractError = describeError(e);
      }
      const outcome = describeExtract(result, extractError, r.file);
      onStatus(outcome.text, outcome.tone);
    } catch (e) {
      onStatus(`上传失败：${describeError(e)}`, "warn");
    } finally {
      setUploading(false);
      await reloadMessages(tid);
    }
  }

  return { uploading, handleUpload };
}
