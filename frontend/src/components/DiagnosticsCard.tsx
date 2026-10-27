// 「导出诊断包」卡（P2-② 的界面那一半，挂在关于页）。
//
// 存在的理由（docs/开发流程.md P2）：用户报 bug 时要能一键把现场（错误签名历史 +
// 双日志 + 版本指纹 + 环境摘要）打包带走。后端组 zip、这里只管触发与四态：
// 空闲 / 加载中（禁用防双击重复导出）/ 成功（报文件名）/ 失败（中文理由）。
// 下载走 api.downloadDiagnostics（fetch+blob 带鉴权头）而不是裸 href —— 理由在 api 层。

import { useState } from "react";
import { api } from "../api";
import { Button, Card } from "./ui";
import { describeError } from "../lib/errors";

type State = { kind: "idle" } | { kind: "busy" } | { kind: "done"; filename: string } | { kind: "error"; why: string };

export function DiagnosticsCard() {
  const [state, setState] = useState<State>({ kind: "idle" });

  async function exportNow() {
    setState({ kind: "busy" });
    try {
      const { blob, filename } = await api.downloadDiagnostics();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      a.click();
      URL.revokeObjectURL(url);
      setState({ kind: "done", filename });
    } catch (e) {
      setState({ kind: "error", why: describeError(e) });
    }
  }

  return (
    <Card className="p-5">
      <h3 className="text-sm font-medium text-slate-900 dark:text-slate-100">诊断</h3>
      <p className="mt-1.5 text-xs leading-relaxed text-slate-500 dark:text-slate-400">
        遇到问题时导出诊断包发给开发者：内含错误签名、版本指纹与本机日志副本。
        包只在本机组装、不经网络上报；环境摘要不含任何密钥。
      </p>
      <div className="mt-2.5 flex flex-wrap items-center gap-3">
        <Button
          variant="outline"
          size="sm"
          type="button"
          disabled={state.kind === "busy"}
          aria-label="导出诊断包"
          onClick={() => void exportNow()}
        >
          {state.kind === "busy" ? "正在打包…" : "导出诊断包"}
        </Button>
        {state.kind === "done" && (
          <span className="text-11 text-slate-400 dark:text-slate-500">
            已导出 {state.filename}（若浏览器没自动下载，再点一次）
          </span>
        )}
      </div>
      {state.kind === "error" && (
        <p className="mt-2 text-xs text-red-600 dark:text-red-400">{state.why}</p>
      )}
    </Card>
  );
}
