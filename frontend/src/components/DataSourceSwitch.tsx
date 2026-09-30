/**
 * 数据源切换器（M5）：侧栏底部那一行 + 登录弹层 + 云端态那条不可关的顶栏。
 *
 * 设计稿是 09-27 拍下来的三态，代码里对应三条硬规矩：
 *
 *   * **切成功就整页重载**。换的是 base_url 与一整份数据集，不是筛选条件；留着本机的
 *     角色卡/会话/模型在屏幕上，就是 M3 前半已经否掉的那种"半换状态"。
 *   * **失败一格都不变**。探测不过就不写状态（`tryConnect` 里挡），所以不存在"界面已经
 *     是云端、数据还是本机"。
 *   * **云端态要一直看得见**，但只留侧栏那一行（`数据源：云端 · <账号>`）。
 *     设计稿里那条不可关的顶栏被用户否了（09-27：「那个横幅我感觉没必要」）——
 *     界面上一格常驻状态就够了，两处说同一件事只会让人两处都不信。
 *     "什么会离开这台机器"这句改到弹层里说（登录前那一刻才是它该被读到的时机）。
 */
import { useEffect, useRef, useState } from "react";

import SyncWizard from "./SyncWizard";
import {
  LOCAL,
  clearJustLoggedIn,
  markJustLoggedIn,
  peekJustLoggedIn,
  read,
  reloadApp,
  save,
  tryConnect,
} from "../lib/dataSource";
import { ApiError } from "../api";
import {
  formatLastSync,
  readLastSync,
  reconcile,
  saveLastSync,
  uploadTarget,
  type LeftConflict,
  type ReconcileResult,
} from "../lib/sync";


export default function DataSourceSwitch() {
  const source = read();
  const cloud = source.mode === "cloud";
  const [open, setOpen] = useState(false);
  const [base, setBase] = useState(cloud ? source.base : "");
  const [user, setUser] = useState(cloud ? source.user : "");
  const [secret, setSecret] = useState("");
  const [busy, setBusy] = useState(false);
  const [why, setWhy] = useState("");
  const [wizard, setWizard] = useState(false);
  /** 登录对账（M8）：跑一次双向自动同步；有未决冲突时弹读数卡让人挑。 */
  const [syncing, setSyncing] = useState(false);
  const [rec, setRec] = useState<ReconcileResult | null>(null);
  const [recWhy, setRecWhy] = useState("");
  const [wizardConflicts, setWizardConflicts] = useState<LeftConflict[] | undefined>(undefined);
  const [, tick] = useState(0);
  /** connect() 自己会把状态切到云端并触发这里重渲染：那一轮**不能**跑对账 ——
   *  它跑在马上就要被 reload 掉的页面上，真正该跑的是重载后的那次（要弹读数卡的那次）。
   *  ref 而不是 state：同一枚实例的一次翻转就够了，不需要为它再渲染一次。 */
  const suppressReconcile = useRef(false);

  // 登录后那一次自动对账（M8）。标记**不在这儿清**：`save()` 与整页重载之间，
  // 当前这一页也会命中这里一次（`cloud` 由 false 变 true），挂载即取走等于把这件事留给一个
  // 马上就要关掉的页面 —— 真浏览器里实测到的就是"点了连接并切换，什么都没发生"。
  // 之后这条入口常驻在下面那一行（用户 09-27：「同步入口可以在登录后再常驻吧，随时可同步」）。
  useEffect(() => {
    if (!cloud || !peekJustLoggedIn() || syncing || suppressReconcile.current) return;
    const target = uploadTarget();
    if (!target) return;
    setSyncing(true);
    setRecWhy("");
    reconcile(target)
      .then((r) => {
        setRec(r);
        saveLastSync({ pushed: r.pushed, pulled: r.pulled });
        tick((n) => n + 1);
      })
      .catch((e: unknown) =>
        setRecWhy(
          e instanceof ApiError
            ? `登录同步未完成：${e.message}`
            : `登录同步未完成：${(e as Error).message}`,
        ),
      )
      .finally(() => setSyncing(false));
    // uploadTarget/target 只依赖 localStorage 的登录态，cloud 翻转即是它的变化
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cloud]);

  function backToLocal() {
    save(LOCAL);
    reloadApp();
  }

  async function connect() {
    setBusy(true);
    setWhy("");
    const probe = await tryConnect(base, user, secret);
    setBusy(false);
    if (!probe.ok) {
      setWhy(probe.why);
      return;
    }
    // 存**归一之后**的 origin（用户可能少写协议、多敲空格或带尾斜杠）：
    // 存原样的话，下一次启动 `read()` 会判它不合法而静默回落本机 —— 那是最难查的一种"没生效"。
    save({ mode: "cloud", base: probe.origin, user, secret });
    // 状态与"刚登录"这两件事都得活过整页重载，而它们是两种寿命：前者持久，后者一次性的。
    markJustLoggedIn();
    // save() 让 cloud 翻转 ⇒ 本页的 [cloud] effect 马上会命中一次。压掉它：
    // 对账要在重载后的新页面上跑（读数卡才弹得出来），而不是在这个即将卸载的实例上跑两遍。
    suppressReconcile.current = true;
    reloadApp();
  }

  return (
    <>
      <button
        onClick={() => (cloud ? backToLocal() : setOpen(true))}
        title={cloud ? "切回本机那份数据（云端那份不动、也不删）" : "切到云端（另一份完整数据集）"}
        className="relative flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-left text-sm text-slate-600 hover:bg-slate-50 dark:text-slate-300 dark:hover:bg-slate-700/60"
      >
        <span
          className={`h-2 w-2 shrink-0 rounded-full ${cloud ? "bg-sky-500" : "bg-emerald-500"}`}
        />
        {/* 侧栏只有 206px：账号一长就会折成两行（截图里就是这么难看）。
            所以标签不许折、超长就省号，"切回本机"这层意思交给 title 与按钮本身。 */}
        <span className="truncate">
          {cloud ? `数据源：云端 · ${source.user}` : "数据源：本机"}
        </span>
        <span className="ml-auto shrink-0 text-[10px] text-slate-400 dark:text-slate-500">
          {cloud ? "切回" : "切换 ⇄"}
        </span>
      </button>

      {/* 数据同步（M7/M8）：只有连上云端之后才有意义 —— 本机态没有"对端"可言。
          它刻意是**另一行**而不是这一行里的一个菜单：侧栏 206px，藏进二级菜单的入口
          等于没有入口，而"随时可同步"是用户 09-27 明确要的那件事。 */}
      {cloud && (
        <button
          onClick={() => setWizard(true)}
          title="双向同步：角色卡 / 会话 / 记忆 / 主动消息。登录时已自动对账一次，此后手动。"
          className="flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-left text-sm text-slate-600 hover:bg-slate-50 dark:text-slate-300 dark:hover:bg-slate-700/60"
        >
          <span className="shrink-0 opacity-80">⇅</span>
          <span className="truncate">
            {syncing ? "正在同步…" : "数据同步"}
            {!syncing && recWhy ? " · 未完成" : ""}
          </span>
          <span className="ml-auto shrink-0 text-[10px] text-slate-400 dark:text-slate-500">
            {syncing ? "" : (formatLastSync(readLastSync()) ?? "未同步")}
          </span>
        </button>
      )}

      {/* 登录对账后的读数卡：只把"机器判不了的"端上来，其余的已经各自到位。 */}
      {rec && rec.left_for_human.length > 0 && (
        <div className="mx-2 mb-1 rounded-lg border border-slate-200 bg-white p-2.5 text-[11px] shadow-sm dark:border-slate-600 dark:bg-slate-800">
          <b className="text-slate-800 dark:text-slate-100">同步完成</b>
          <p className="mt-0.5 leading-relaxed text-slate-500 dark:text-slate-400">
            已上传 {rec.pushed} 项，已下载 {rec.pulled} 项。
          </p>
          <p className="mt-0.5 leading-relaxed text-amber-700 dark:text-amber-300">
            {rec.left_for_human.length} 项在两端均有修改，需要您确认保留哪个版本。
          </p>
          <div className="mt-1.5 flex justify-end gap-2">
            <button
              onClick={() => {
                clearJustLoggedIn();
                setRec(null);
              }}
              className="px-2 py-1 text-[11px] text-slate-500 hover:text-slate-700 dark:text-slate-400"
            >
              稍后处理
            </button>
            <button
              onClick={() => {
                setWizardConflicts(rec.left_for_human);
                setRec(null);
                setWizard(true);
              }}
              className="rounded-lg bg-blue-600 px-2.5 py-1 text-[11px] text-white"
            >
              立即处理（{rec.left_for_human.length}）
            </button>
          </div>
        </div>
      )}

      {wizard && (
        <SyncWizard
          initialConflicts={wizardConflicts}
          onClose={() => {
            clearJustLoggedIn();
            setWizard(false);
            setWizardConflicts(undefined);
          }}
        />
      )}

      {open && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4"
          role="dialog"
          aria-modal="true"
          aria-label="切到云端"
        >
          <div className="w-full max-w-sm rounded-xl bg-white p-5 shadow-2xl dark:bg-slate-800">
            <h2 className="text-[15px] font-semibold text-slate-900 dark:text-slate-100">
              切到云端
            </h2>
            <p className="mt-1 text-xs leading-relaxed text-slate-500 dark:text-slate-400">
              云端是<b>另一份完整数据集</b>：角色卡、会话、记忆、健康档案都换成对面那一份。
              本机这份不动、也不删。
            </p>
            <label className="mt-3 block text-[11px] text-slate-600 dark:text-slate-300">
              程序地址
              <input
                value={base}
                onChange={(e) => setBase(e.target.value)}
                placeholder="https://cloud.example.cn:8123"
                className="mt-1 w-full rounded-lg border border-slate-200 px-2.5 py-2 text-xs dark:border-slate-600 dark:bg-slate-900 dark:text-slate-100"
              />
            </label>
            <label className="mt-2 block text-[11px] text-slate-600 dark:text-slate-300">
              账号
              <input
                value={user}
                onChange={(e) => setUser(e.target.value)}
                className="mt-1 w-full rounded-lg border border-slate-200 px-2.5 py-2 text-xs dark:border-slate-600 dark:bg-slate-900 dark:text-slate-100"
              />
            </label>
            <label className="mt-2 block text-[11px] text-slate-600 dark:text-slate-300">
              密码 / 访问令牌
              <input
                type="password"
                value={secret}
                onChange={(e) => setSecret(e.target.value)}
                className="mt-1 w-full rounded-lg border border-slate-200 px-2.5 py-2 text-xs dark:border-slate-600 dark:bg-slate-900 dark:text-slate-100"
              />
            </label>
            {/* 失败用红、承诺用黄：两格同色时（第一版截图里就是这样），
                人会分不清哪一句是刚发生的。JSX 注释只能待在元素的孩子位置上。 */}
            {why && (
              <p className="mt-3 rounded-lg border border-rose-200 bg-rose-50 p-2.5 text-[11px] leading-relaxed text-rose-800 dark:border-rose-700 dark:bg-rose-900/30 dark:text-rose-200">
                {why}
              </p>
            )}
            <div className="mt-3 flex justify-end gap-2">
              <button
                onClick={() => setOpen(false)}
                className="rounded-lg border border-slate-200 px-3.5 py-2 text-xs text-slate-600 dark:border-slate-600 dark:text-slate-300"
              >
                取消
              </button>
              <button
                onClick={() => void connect()}
                disabled={busy || !base || !user}
                className="rounded-lg bg-blue-600 px-3.5 py-2 text-xs text-white disabled:opacity-50"
              >
                {busy ? "连接中…" : "连接并切换"}
              </button>
            </div>
            <p className="mt-3 rounded-lg border border-amber-200 bg-amber-50 p-2.5 text-[11px] leading-relaxed text-amber-800 dark:border-amber-700 dark:bg-amber-900/30 dark:text-amber-200">
              连不上或账号不对 = 留在本机这一份，界面上不会出现「半截云端」。
              <b>推理用的 key 由你自己在对面配</b>，这个应用不代付 token。
              <br />
              切过去之后，对话内容与健康数据都会离开这台机器（侧栏那一行会一直写着「云端 ·
              账号」）。
            </p>
          </div>
        </div>
      )}
    </>
  );
}

