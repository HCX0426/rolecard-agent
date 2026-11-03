/**
 * 导航护栏的判据（`R102-56` 落地时写反的那一半，`R102-73`）。
 *
 * 第二十包装机后控制台停在「正在打开控制台…」：着陆页探活成功后要导航去
 * `http://127.0.0.1:<端口>/`（桌宠是 `#/pet` 那一格），而批 13 的护栏写的是
 * 「除 `file://` 一律 deny」—— 它把**应用自己的合法终态**当成了注入攻击。
 * 护栏要挡的是"第三方内容把窗导航到远程站点"，不是本后端那一格 origin。
 *
 * 判据三条：`file://`（本地着陆页）放行、**后端那一个 origin** 放行、其余一律拦。
 * 为什么是 origin 相等而不是前缀包含：`http://127.0.0.1:8000.evil.test/` 以本机 origin
 * 字符串开头，却不是本机的（`consoleUrl()` 归一后由 `backend.ts` 保证形状，这里不放宽）。
 *
 * ⚠ 从前这段注释还写着第四句：「后端 origin 自己解析失败是配置坏了，那种情况下宁可放行
 * origin 里那个字面串，别让界面变成砖」，而**代码没有实现它**（两个 `new URL` 共用一个
 * catch，一律 deny）。这轮先量清再决定，不照抄注释：
 *   * 那条锁死场景的真凶不在这里 —— 在 `ROLECARD_BACKEND_URL` 可以写成 `127.0.0.1:8000`
 *     （不带 scheme）。实测浏览器会把它当相对路径解成 `file:///…/127.0.0.1:8000/`，
 *     于是护栏**放行**、页面 `ERR_FILE_NOT_FOUND`，症状同样是"停在打开中"。所以修在
 *     源头（`backend.ts::normalizeConsoleUrl`），不是在这里放宽判据。
 *   * 而"后端地址坏了就放行一切"这个方向**不能实现**：这一格拦的是 AI 生成内容里的
 *     `<a target="_blank">` 把窗导航去远程钓鱼页（`index.ts` 那段：sandbox 挡代码执行，
 *     挡不住钓鱼页与它对后端的已登录二次调用）。配置一坏就把护栏关掉，等于
 *     "越坏的配置越不安全" —— 方向正好反了。
 *   * 剩下的真问题只是**可诊断性**：`deny` 有两种因（目标不是本机 / 后端地址本身坏了），
 *     从前都写同一行"导航被拒"，用户与排障的人分不出是注入被拦还是自己配错。
 *     所以这里把因分开回给调用方去记日志（`Verdict`），行为一律仍是拦。
 */

/** 拦或放；放只有一种，拦有两种因（分开报，为了日志能说清是谁坏了）。 */
export type NavigationVerdict = "allow" | "deny-remote" | "deny-broken-backend";

export function navigationVerdict(url: string, backendUrl: string): NavigationVerdict {
  if (typeof url !== "string" || url.length === 0) return "deny-remote";
  let target: URL;
  try {
    target = new URL(url);
  } catch {
    return "deny-remote"; // 目标解析不出来 = 未知目标，不该被导航
  }
  // `file:` 只认**解析后的 protocol** 这一条路。从前这里上面还有一句
  // `url.startsWith("file://") return "allow"`：同一件事两套逻辑，而那套字面匹配的更弱
  // （`FILE://` 与 `file:/x` 两种写法它认不出、解析那条认得出；反过来它会把
  // `file://` 开头却解析不动的串也放行）。2026-11-04 归一时删掉字面那句 —— 判据只有一处。
  if (target.protocol === "file:") return "allow";
  let backend: URL;
  try {
    backend = new URL(backendUrl);
  } catch {
    // 后端地址自己解析不了：拦（见文件头：放宽等于让最坏的配置得到最小的防护），
    // 但要报成另一种因 —— 这一行日志读起来应该是"我配错了"，不是"有东西想跑掉"。
    return "deny-broken-backend";
  }
  return target.origin === backend.origin ? "allow" : "deny-remote";
}


