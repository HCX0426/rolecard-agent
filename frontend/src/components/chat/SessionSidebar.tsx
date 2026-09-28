import { useConfirm } from "../../hooks/useConfirm";
import type { RoleCard, SessionRow } from "../../api";

/**
 * 会话侧栏：桌面常驻、移动端 off-canvas 抽屉。
 *
 * 分两段。固定的那一栏**列的是角色，不是线程**（用户 09-26 的提法）：每个角色一行，
 * 从没被找过也照样在，点一下才把那条线 ensure 出来 —— "她有没有一条对话"不该取决于
 * 用户有没有先收到过主动消息。
 *
 * 两个旗标都来自后端（`is_proactive` / `is_blank`），前端不猜线程 id 的形状，也不拿
 * "有没有标题"猜空不空：前者是 `core/reachout.py` 的事实，后者会被重命名过的空线程与
 * 深链刚建出来的线程一起骗过去。
 */
export default function SessionSidebar({
  roles,
  sessions,
  unreadByRole,
  sessionId,
  sessionsOpen,
  onCloseDrawer,
  tempPick,
  setTempPick,
  onNewSession,
  onOpenLane,
  onSelectSession,
  onClearLane,
  onDeleteOne,
  onDeleteMany,
}: {
  roles: RoleCard[];
  sessions: SessionRow[];
  /** 每个角色还有几条没读的主动开口（与桌宠红点同源的那份读数）。 */
  unreadByRole: Record<string, number>;
  sessionId: string | null;
  /** 移动端抽屉开合。state 在页面级：头部的「对话」按钮也要能开它。 */
  sessionsOpen: boolean;
  onCloseDrawer: () => void;
  /** 批量模式的勾选：`null` = 批量没开（那时不画复选框）。 */
  tempPick: Set<string> | null;
  setTempPick: (next: Set<string> | null) => void;
  onNewSession: () => void;
  onOpenLane: (roleId: string) => void;
  onSelectSession: (threadId: string) => void;
  onClearLane: (threadId: string, name: string) => void;
  onDeleteOne: (threadId: string) => void;
  onDeleteMany: (ids: string[]) => void;
}) {
  const confirm = useConfirm();

  const laneRows = roles.map((r) => ({
    role: r,
    row: sessions.find((s) => s.is_proactive && s.role_id === r.role_id) ?? null,
  }));
  const laneThreadIds = new Set(laneRows.map((l) => l.row?.thread_id));
  /** 角色被删了但那条线还在：不能让它从侧栏消失，否则那段对话就找不回来了。 */
  const orphanLanes = sessions.filter((s) => s.is_proactive && !laneThreadIds.has(s.thread_id));
  const tempSessions = sessions.filter((s) => !s.is_proactive && !s.is_blank);

  /** 批量模式下的勾选。独立成一个函数是因为点击整行有两个意思：没开批量 = 打开这条，
   *  开了批量 = 选中它（用户要的是"少点几次"，不是"多一层菜单"）。 */
  function togglePick(threadId: string) {
    const next = new Set(tempPick ?? []);
    if (next.has(threadId)) next.delete(threadId);
    else next.add(threadId);
    setTempPick(next);
  }

  return (
    <>
      {/* 会话列表面板：桌面常驻，移动端 off-canvas 抽屉 */}
      <aside
        className={`flex w-64 shrink-0 flex-col border-r border-slate-200 bg-white transition-transform dark:border-slate-700 dark:bg-slate-800 max-md:fixed max-md:inset-y-0 max-md:left-0 max-md:z-40 max-md:pt-[41px] ${
          sessionsOpen ? "max-md:translate-x-0" : "max-md:-translate-x-full"
        }`}
      >
        <div className="border-b border-slate-100 dark:border-slate-800 p-3">
          <button
            onClick={onNewSession}
            title="开一条不带角色的临时话题（试个东西、测张图用）。每个角色那条固定对话不受影响。"
            className="w-full rounded-lg bg-blue-600 px-3 py-2 text-sm font-medium text-white hover:bg-blue-700"
          >
            ＋ 开一个临时话题
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto p-2">
          {laneRows.length === 0 && tempSessions.length === 0 && orphanLanes.length === 0 && (
            <p className="px-2 py-4 text-xs text-slate-400 dark:text-slate-500">
              还没有角色，也还没有对话
            </p>
          )}
          {/* 固定的那一栏：一行一个角色。没被找过也照样列着 —— 点一下才 ensure 出那条线。 */}
          {laneRows.length > 0 && (
            <p className="px-2 pb-1 pt-1 text-[11px] font-medium text-slate-400 dark:text-slate-500">
              她们
            </p>
          )}
          {laneRows.map(({ role, row }) => {
            const unread = unreadByRole[role.role_id] ?? 0;
            return (
              <div
                key={role.role_id}
                onClick={() => onOpenLane(role.role_id)}
                title={`与${role.role_name}的那条对话 —— 桌宠显示的就是这一条`}
                className={`group mb-1 flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-2 text-sm ${
                  row?.thread_id === sessionId
                    ? "bg-blue-50 dark:bg-blue-900/30 text-blue-800"
                    : "hover:bg-slate-50 dark:bg-slate-800/50 dark:hover:bg-slate-700/60"
                }`}
              >
                <div className="min-w-0 flex-1">
                  <div className="truncate">{role.role_name}</div>
                  <div className="truncate text-[11px] text-slate-400 dark:text-slate-500">
                    {row ? row.title || "你们的对话" : "还没开始 · 点一下就在这里"}
                  </div>
                </div>
                {unread > 0 && (
                  <span
                    className="shrink-0 rounded-full bg-blue-600 px-1.5 text-[10px] text-white"
                    title={`${unread} 条她主动找你，还没读`}
                  >
                    {unread}
                  </span>
                )}
                {/* 固定栏只给「清空」，不给删除（用户 09-26 拍）：删线程会把收件箱里那些行的
                    跳转目标掏空（§7.2.2 那张表），而清空抹掉的只是这段对话本身。 */}
                {row && (
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      onClearLane(row.thread_id, role.role_name);
                    }}
                    className="shrink-0 rounded px-1 py-0.5 text-[11px] text-slate-300 opacity-0 transition-opacity hover:bg-slate-100 hover:text-red-500 group-hover:opacity-100 dark:text-slate-600 dark:hover:bg-slate-700/50"
                    title={`清空与${role.role_name}的对话（她的角色、记忆与收件箱记录都不动）`}
                  >
                    清空
                  </button>
                )}
              </div>
            );
          })}
          {/* 角色被删了而那条线还在：留在栏里，否则那段对话没有任何入口了。 */}
          {orphanLanes.map((s) => (
            <div
              key={s.thread_id}
              onClick={() => onSelectSession(s.thread_id)}
              className="group mb-1 flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-2 text-sm hover:bg-slate-50 dark:bg-slate-800/50 dark:hover:bg-slate-700/60"
              title="这个角色已经删掉了，而那段对话还在 —— 要清掉就删这一条"
            >
              <div className="min-w-0 flex-1">
                <div className="truncate">{s.title || "旧对话"}</div>
                <div className="truncate text-[11px] text-slate-400 dark:text-slate-500">
                  {s.role_name || s.role_id} · 角色已删
                </div>
              </div>
              <button
                onClick={async (e) => {
                  e.stopPropagation();
                  if (await confirm({ title: "删除这条对话？", body: "对话及其全部消息将被永久删除，不可恢复。", confirmText: "删除", danger: true })) onDeleteOne(s.thread_id);
                }}
                className="shrink-0 rounded px-1 py-0.5 text-xs text-slate-300 opacity-0 transition-opacity hover:bg-slate-100 hover:text-red-500 group-hover:opacity-100 dark:text-slate-600 dark:hover:bg-slate-700/50"
                title="删除对话"
              >
                ✕
              </button>
            </div>
          ))}
          {/* 临时话题那一组：批量清理在这里，一条条删太麻烦（用户 09-26 实测 23 条）。 */}
          {tempSessions.length > 0 && (
            <>
              <div className="flex items-center justify-between gap-2 px-2 pb-1 pt-3">
                <p className="text-[11px] font-medium text-slate-400 dark:text-slate-500">
                  临时话题 · {tempSessions.length}
                </p>
                {tempPick === null ? (
                  <button
                    onClick={() => setTempPick(new Set())}
                    className="text-[11px] text-slate-400 hover:text-blue-600 dark:text-slate-500 dark:hover:text-blue-400"
                    title="勾着删，省得一条条点"
                  >
                    批量清理
                  </button>
                ) : (
                  <span className="flex items-center gap-1.5 text-[11px]">
                    <button
                      onClick={() => setTempPick(new Set(tempSessions.map((s) => s.thread_id)))}
                      className="text-slate-400 hover:text-blue-600 dark:text-slate-500"
                    >
                      全选
                    </button>
                    <button
                      disabled={tempPick.size === 0}
                      onClick={() => onDeleteMany([...tempPick])}
                      className="rounded bg-red-600 px-1.5 py-0.5 text-white disabled:bg-slate-300 dark:disabled:bg-slate-700"
                    >
                      删除 {tempPick.size}
                    </button>
                    <button
                      onClick={() => setTempPick(null)}
                      className="text-slate-400 hover:text-slate-600 dark:text-slate-500"
                    >
                      退出
                    </button>
                  </span>
                )}
              </div>
              {tempSessions.map((s) => (
                <div
                  key={s.thread_id}
                  className={`group mb-1 flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-2 text-sm ${
                    s.thread_id === sessionId ? "bg-blue-50 dark:bg-blue-900/30 text-blue-800" : "hover:bg-slate-50 dark:bg-slate-800/50 dark:hover:bg-slate-700/60"
                  }`}
                  onClick={() => (tempPick === null ? onSelectSession(s.thread_id) : togglePick(s.thread_id))}
                >
                  {tempPick !== null && (
                    <input
                      type="checkbox"
                      checked={tempPick.has(s.thread_id)}
                      onChange={() => togglePick(s.thread_id)}
                      aria-label={`选中「${s.title || "新对话"}」`}
                      className="shrink-0"
                    />
                  )}
                  <div className="min-w-0 flex-1">
                    <div className="truncate">{s.title || "新对话"}</div>
                    <div className="truncate text-[11px] text-slate-400 dark:text-slate-500">
                      {s.role_name || s.role_id}
                    </div>
                  </div>
                  <button
                    onClick={async (e) => {
                      e.stopPropagation();
                      if (await confirm({ title: "删除这个对话？", body: "对话及其全部消息将被永久删除，不可恢复。", confirmText: "删除", danger: true })) onDeleteOne(s.thread_id);
                    }}
                    className="shrink-0 rounded px-1 py-0.5 text-xs text-slate-300 dark:text-slate-600 opacity-0 transition-opacity hover:bg-slate-100 dark:bg-slate-700/50 dark:hover:bg-slate-700 hover:text-red-500 group-hover:opacity-100"
                    title="删除对话"
                  >
                    ✕
                  </button>
                </div>
              ))}
            </>
          )}
        </div>
      </aside>

      {/* 移动端：会话抽屉的遮罩 */}
      {sessionsOpen && (
        <button
          aria-label="关闭对话列表"
          onClick={onCloseDrawer}
          className="absolute inset-0 z-30 bg-slate-900/40 md:hidden"
        />
      )}
    </>
  );
}
