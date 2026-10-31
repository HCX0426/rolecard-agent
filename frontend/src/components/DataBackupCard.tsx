// 「全量备份」卡（R1031-15，挂在资料页）。
//
// 存在的理由（审计快照 §七）：换机迁移从前**只有开发者口径** —— 得会命令行、还得有
// python 环境才能把"她"整体搬走。这里补上应用内那一条：点一下，整根数据根打成 zip
// 下载下来（库用 sqlite 快照拷，开着程序也不坏；库先文件后，写完回头查每个库都在包里）。
//
// 三个必须说清的事实（文案里都带着，因为知道有入口的人得同时知道包是敏感的）：
//   1. **包里含模型 API key**（存在 sqlite 里，明文是设计拍板）—— 别丢进网盘/聊天工具；
//   2. **这是"整台机器的她"**，不是"某张角色卡"（后者走角色卡导出，给朋友分享用）；
//   3. 备份**不含**别台机器上才有的一切 —— 它就是这台机器这一份。
//
// 四态与《诊断》那张卡同一条：空闲 / 打包中（禁用防双击）/ 成功（报文件名）/ 失败（中文理由）。
// 下载走 api.downloadDataBackup（fetch+blob 带鉴权头）而不是裸 href —— 理由在 api 层。

import { useState } from "react";
import { api } from "../api";
import { Button, Card } from "./ui";
import { describeError } from "../lib/errors";

type State = { kind: "idle" } | { kind: "busy" } | { kind: "done"; filename: string } | { kind: "error"; why: string };

export function DataBackupCard() {
  const [state, setState] = useState<State>({ kind: "idle" });

  async function backupNow() {
    setState({ kind: "busy" });
    try {
      const { blob, filename } = await api.downloadDataBackup();
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
      <h3 className="text-sm font-medium text-slate-900 dark:text-slate-100">全量备份</h3>
      <p className="mt-1.5 text-xs leading-relaxed text-slate-500 dark:text-slate-400">
        把这台机器上的全部数据（角色卡、会话、记忆、设置、知识库、上传原件）打成 zip 下载，换机/搬家时带走。
        <b className="text-amber-700 dark:text-amber-400">包里含模型 API key</b>
        ，别丢进网盘或聊天工具；只在本机生成、不经网络。
      </p>
      <div className="mt-2.5 flex flex-wrap items-center gap-3">
        <Button
          variant="outline"
          size="sm"
          type="button"
          disabled={state.kind === "busy"}
          aria-label="导出全量备份"
          onClick={() => void backupNow()}
        >
          {state.kind === "busy" ? "正在打包…" : "导出全量备份"}
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
