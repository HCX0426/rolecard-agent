// 「工作区」字段（2026-10-30 从角色卡页底部那张独立卡搬进**每张角色卡内部**）。
//
// 三笔变更一起记：①搬家 —— 用户 2026-10-30「工作区…应该放到角色卡的每个角色里，
// 每个角色可以单独设置工作区」：从前是"全局一张卡、所有角色共用"，那形状下"角色 A 能读的
// 文件"和"角色 B 能读的"是同一片，角色之间根本没有边界可言。现在每个角色一份。
// ②后端契约 —— `role_card.workspace_dir` 那一列 + `/api/workspace/dir?role_id=`
// （角色级优先于全局级；没配就跟全局默认）。③文案 —— 一律说「工作区」，不说「任务目录」。
//
// 为什么它长这样（不是一个独立 Card）：它在角色卡里是**一个字段**，不是一块独立的功能区 ——
// 独立成卡会让人以为"工作区"是另一个层级的对象，而它其实是"这张卡的一个属性"。
import { useCallback, useEffect, useState } from "react";
import { api, type TreeResult, type WorkspaceDir } from "../api";
import { Button } from "./ui";
import { IconFolder } from "./icons";
import { NativeDirPickerButton } from "./NativeDirPickerButton";
import { describeError } from "../lib/errors";

export function WorkspacePanel({ roleId }: { roleId: string }) {
  // 工作区：wsDir = 生效值；树抽屉 = 目录选择器。
  const [wsDir, setWsDir] = useState<WorkspaceDir | null>(null);
  const [wsDraft, setWsDraft] = useState("");
  const [wsMsg, setWsMsg] = useState("");
  const [wsErr, setWsErr] = useState("");
  const [treeOpen, setTreeOpen] = useState(false);
  const [tree, setTree] = useState<TreeResult | null>(null);
  const [treeErr, setTreeErr] = useState("");

  const loadWorkspace = useCallback(async () => {
    const d = await api.getWorkspaceDir(roleId);
    setWsDir(d);
    // 契约迁移后 path 可空（后端没给 = null）：草稿框以空串起步，与原"没设目录"同形。
    setWsDraft(d.path ?? "");
  }, [roleId]);

  useEffect(() => {
    loadWorkspace().catch((e) => setWsErr(`加载工作区失败：${describeError(e)}`));
  }, [loadWorkspace]);

  async function saveWorkspace() {
    setWsErr("");
    setWsMsg("");
    try {
      const d = await api.setWorkspaceDir(wsDraft.trim(), roleId);
      setWsDir(d);
      setWsDraft(d.path ?? "");
      setWsMsg("已保存（对该角色下一轮生效）");
    } catch (e) {
      setWsErr(`保存失败：${describeError(e)}`);
    }
  }

  async function clearWorkspace() {
    setWsErr("");
    setWsMsg("");
    try {
      const d = await api.clearWorkspaceDir(roleId);
      setWsDir(d);
      setWsDraft(d.path ?? "");
      setWsMsg("已清除，跟随全局默认");
    } catch (e) {
      setWsErr(`清除失败：${describeError(e)}`);
    }
  }

  async function browseAt(path?: string) {
    setTreeErr("");
    try {
      setTree(await api.browseTree(path));
    } catch (e) {
      setTreeErr(`浏览失败：${describeError(e)}`);
    }
  }

  return (
    <div>
      {/* 一行标题 + 当前生效值：路径是这字段的主要信息，字号给足 */}
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-xs font-medium text-slate-700 dark:text-slate-200">工作区</span>
        <span className="truncate text-11 text-slate-400 dark:text-slate-500">
          {wsDir?.overridden ? "独享" : "跟随全局默认"}
        </span>
      </div>

      <div className="mt-1.5 flex items-center gap-2">
        <input
          value={wsDraft}
          onChange={(e) => setWsDraft(e.target.value)}
          disabled={!wsDir}
          placeholder={wsDir ? "例如 D:\\my-tasks" : "加载中…"}
          className="min-w-0 flex-1 rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 font-mono text-xs text-slate-700 focus:border-blue-400 focus:outline-none dark:border-slate-600 dark:bg-slate-900 dark:text-slate-200"
        />
        <button
          onClick={() => {
            setTreeOpen(true);
            browseAt(wsDraft || undefined);
          }}
          disabled={!wsDir}
          className="shrink-0 rounded-lg border border-slate-200 px-3 py-1.5 text-xs text-slate-600 hover:border-blue-300 disabled:opacity-50 dark:border-slate-600 dark:text-slate-300"
        >
          浏览…
        </button>
        {/* 桌面壳里多一条系统对话框的路子（B/S 下整个按钮不出现）；挑完同样只填草稿，
            要用户按「保存」才生效 —— 授权范围不该被一次点击顺手改掉。 */}
        <NativeDirPickerButton disabled={!wsDir} onPicked={(p) => setWsDraft(p)} />
        <Button variant="primary" onClick={saveWorkspace} disabled={!wsDir}
          className="shrink-0 rounded-lg px-3 py-1.5 text-xs disabled:opacity-50">
          保存
        </Button>
        {wsDir?.overridden && (
          <button
            onClick={clearWorkspace}
            className="shrink-0 rounded-lg border border-slate-200 px-3 py-1.5 text-xs text-slate-600 hover:border-red-300 hover:text-red-600 dark:border-slate-600 dark:text-slate-300"
          >
            清除
          </button>
        )}
      </div>

      <div className="mt-1 flex items-center gap-2 text-11">
        {wsMsg && <span className="text-emerald-600 dark:text-emerald-400">{wsMsg}</span>}
        {wsErr && <span className="text-red-600 dark:text-red-400">{wsErr}</span>}
      </div>
      {/* 说明压到一行：这个字段在角色卡里已经够密了，长说明会把它撑成一堵墙 */}
      <p className="mt-1 text-11 text-slate-400 dark:text-slate-500">
        这个角色读写文件的范围：目录外一律拒绝。
      </p>

      {/* 目录树选择抽屉（人用的选择器；后端/树契约见 /api/workspace/tree） */}
      {treeOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4">
          <div className="flex h-[70vh] w-full max-w-lg flex-col rounded-xl border border-slate-200 bg-white shadow-xl dark:border-slate-600 dark:bg-slate-800">
            <div className="flex items-center justify-between border-b border-slate-100 px-4 py-2.5 dark:border-slate-700">
              <span className="text-sm font-medium text-slate-900 dark:text-slate-100">选择工作区目录</span>
              <button
                onClick={() => setTreeOpen(false)}
                className="text-xs text-slate-400 hover:text-slate-600 dark:hover:text-slate-200"
              >
                关闭
              </button>
            </div>
            <div className="flex items-center gap-1.5 border-b border-slate-100 px-3 py-2 text-xs dark:border-slate-700">
              <button
                onClick={() => browseAt(tree?.parent ?? undefined)}
                disabled={!tree}
                className="rounded border border-slate-200 px-2 py-0.5 text-slate-600 hover:border-blue-300 disabled:opacity-40 dark:border-slate-600 dark:text-slate-300"
              >
                ← 上一级
              </button>
              <span className="min-w-0 flex-1 truncate font-mono text-slate-500 dark:text-slate-400">
                {tree?.path ?? "…"}
              </span>
            </div>
            <div className="flex-1 overflow-y-auto p-2">
              {treeErr && <p className="px-2 py-1 text-xs text-red-600 dark:text-red-400">{treeErr}</p>}
              {(tree?.entries ?? []).map((e) => (
                <button
                  key={e.name}
                  onClick={() => e.is_dir && browseAt(`${tree!.path}\\${e.name}`)}
                  disabled={!e.is_dir}
                  className="flex w-full items-center justify-between rounded-lg px-2.5 py-1.5 text-left text-xs hover:bg-slate-100 disabled:cursor-default disabled:opacity-70 dark:hover:bg-slate-700/60"
                >
                  <span className={`truncate ${e.is_dir ? "text-slate-800 dark:text-slate-100" : "text-slate-400 dark:text-slate-500"} ${
                      e.is_dir ? "" : "pl-4"
                    }`}>
                    {/* 目录标记用 SVG（2026-10-24 UI 打磨）：emoji 的渲染重量随系统漂。 */}
                    {e.is_dir && <IconFolder />}
                    {e.name}
                  </span>
                  {!e.is_dir && (e.size ?? 0) > 0 && (
                    <span className="ml-2 shrink-0 text-10 text-slate-400">
                      {e.size && e.size > 1024 ? `${(e.size / 1024).toFixed(0)} KB` : `${e.size ?? 0} B`}
                    </span>
                  )}
                </button>
              ))}
              {tree && (tree.entries ?? []).length === 0 && (
                <p className="px-2 py-4 text-center text-xs text-slate-400 dark:text-slate-500">（空目录）</p>
              )}
              {tree?.truncated && (
                <p className="px-2 py-1 text-11 text-slate-400 dark:text-slate-500">（条目过多，仅显示部分）</p>
              )}
            </div>
            <div className="flex items-center justify-end gap-2 border-t border-slate-100 px-4 py-2.5 dark:border-slate-700">
              <button
                onClick={() => setTreeOpen(false)}
                className="rounded-lg border border-slate-200 px-3 py-1.5 text-xs text-slate-600 hover:border-blue-300 dark:border-slate-600 dark:text-slate-300"
              >
                取消
              </button>
              <Button variant="primary" onClick={() => { if (!tree?.path) return; setWsDraft(tree.path); setTreeOpen(false); }}
                className="rounded-lg px-3 py-1.5 text-xs">
                选中当前目录
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
