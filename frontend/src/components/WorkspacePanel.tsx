// 「工作区」卡（2026-10-30 从设置页「记忆与任务目录」子页签迁出，由角色卡页托管）。
//
// 两笔变更一起记：①搬家 —— 用户 2026-10-30「任务目录这三个不放在设置界面，跟记忆页签
// 或者角色卡页签走」：它是授权（fs 工具的运行边界）不是记忆，按"角色操作权限跟角色卡走"
// 的口径落在角色卡页；②改名 —— 同一批反馈：「任务目录应该叫工作区，这是给某个 agent
// 随便用的」。界面文案全线换「工作区」；**后端契约键一律未动**（/api/workspace/*、
// `WorkspaceDir`、kernel_meta `workspace:dir`），换的只是人称。
//
// 正文从 components/MemoryPanel.tsx（该文件同日拆除）逐字搬来，除更名与宿主说明外一字未动。
import { useCallback, useEffect, useState } from "react";
import { api, type TreeResult, type WorkspaceDir } from "../api";
import { Button, Card } from "./ui";
import { IconFolder } from "./icons";
import { NativeDirPickerButton } from "./NativeDirPickerButton";
import { describeError } from "../lib/errors";

export function WorkspacePanel() {
  // 工作区：wsDir = 生效值；树抽屉 = 目录选择器。
  const [wsDir, setWsDir] = useState<WorkspaceDir | null>(null);
  const [wsDraft, setWsDraft] = useState("");
  const [wsMsg, setWsMsg] = useState("");
  const [wsErr, setWsErr] = useState("");
  const [treeOpen, setTreeOpen] = useState(false);
  const [tree, setTree] = useState<TreeResult | null>(null);
  const [treeErr, setTreeErr] = useState("");

  const loadWorkspace = useCallback(async () => {
    const d = await api.getWorkspaceDir();
    setWsDir(d);
    // 契约迁移后 path 可空（后端没给 = null）：草稿框以空串起步，与原"没设目录"同形。
    setWsDraft(d.path ?? "");
  }, []);

  useEffect(() => {
    loadWorkspace().catch((e) => setWsErr(`加载工作区失败：${describeError(e)}`));
  }, [loadWorkspace]);

  async function saveWorkspace() {
    setWsErr("");
    setWsMsg("");
    try {
      const d = await api.setWorkspaceDir(wsDraft.trim());
      setWsDir(d);
      setWsDraft(d.path ?? "");
      setWsMsg("已设置（保存即对角色下一轮生效）");
    } catch (e) {
      setWsErr(`保存失败：${describeError(e)}`);
    }
  }

  async function clearWorkspace() {
    setWsErr("");
    setWsMsg("");
    try {
      const d = await api.clearWorkspaceDir();
      setWsDir(d);
      setWsDraft(d.path ?? "");
      setWsMsg("已清除，回落 env 默认目录");
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
    <>
      <Card className="p-5">
        <h3 className="text-sm font-medium text-slate-900 dark:text-slate-100">工作区</h3>
        <p className="mt-1.5 text-xs leading-relaxed text-slate-500 dark:text-slate-400">
          角色读写文件的授权范围（所有角色共用这一处）：只看得到、只碰得到这个目录里的内容
          （目录外一律拒绝）。修改保存后对角色下一轮对话即时生效。
        </p>
        <div className="mt-2.5 flex items-center gap-2">
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
              清除（回落默认）
            </button>
          )}
        </div>
        <div className="mt-2 flex items-center gap-2 text-xs">
          {wsMsg && <span className="text-emerald-600 dark:text-emerald-400">{wsMsg}</span>}
          {wsErr && <span className="text-red-600 dark:text-red-400 dark:text-red-500">{wsErr}</span>}
          <span className="text-slate-400 dark:text-slate-500">
            当前：{wsDir ? (wsDir.overridden ? "自定义" : "env 默认") : "…"}
          </span>
        </div>
        {wsDir?.overridden && (
          <p className="mt-1.5 text-11 text-slate-400 dark:text-slate-500">
            该目录仅存于本机数据库；删除后回到默认的工作区。
          </p>
        )}
      </Card>

      {/* 目录树选择抽屉（角色卡页的工作区卡专用，人用的选择器；后端/树契约见 /api/workspace/tree） */}
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
                  className="flex w-full items-center justify-between rounded-lg px-2.5 py-1.5 text-left text-xs hover:bg-blue-50 disabled:cursor-default disabled:opacity-70 dark:hover:bg-blue-900/30"
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
    </>
  );
}
