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
import { useEffect, useState } from "react";

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

  // 登录后那一次自动问（设计稿①）。标记**不在这儿清**：`save()` 与整页重载之间，
  // 当前这一页也会命中这里一次（`cloud` 由 false 变 true），挂载即取走等于把弹层留给一个
  // 马上就要关掉的页面 —— 真浏览器里实测到的就是"点了连接并切换，什么都没弹"。
  // 之后这条入口常驻在下面那一行（用户 09-27：「同步入口可以在登录后再常驻吧，随时可同步」）。
  useEffect(() => {
    if (cloud && peekJustLoggedIn()) setWizard(true);
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

      {/* 上行入口（M7）：只有连上云端之后才有意义 —— 本机态没有"对面"可推。
          它刻意是**另一行**而不是这一行里的一个菜单：侧栏 206px，藏进二级菜单的入口
          等于没有入口，而"随时可同步"是用户 09-27 明确要的那件事。 */}
      {cloud && (
        <button
          onClick={() => setWizard(true)}
          title="把本机这一份（角色卡 / 会话 / 记忆 / 主动消息）带到云端那台"
          className="flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-left text-sm text-slate-600 hover:bg-slate-50 dark:text-slate-300 dark:hover:bg-slate-700/60"
        >
          <span className="shrink-0 opacity-80">⬆</span>
          <span className="truncate">把本机这份带到云端</span>
          <span className="ml-auto shrink-0 text-[10px] text-slate-400 dark:text-slate-500">
            上行
          </span>
        </button>
      )}

      {wizard && (
        <SyncWizard
          onClose={() => {
            clearJustLoggedIn();
            setWizard(false);
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
              服务地址
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
              <b>推理用的 key 由你自己在对面配</b>，本站不代付 token。
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

