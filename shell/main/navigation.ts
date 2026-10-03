/**
 * 导航护栏的判据（`R102-56` 落地时写反的那一半，`R102-73`）。
 *
 * 第二十包装机后控制台停在「正在打开控制台…」：着陆页探活成功后要导航去
 * `http://127.0.0.1:<端口>/`（桌宠是 `#/pet` 那一格），而批 13 的护栏写的是
 * 「除 `file://` 一律 deny」—— 它把**应用自己的合法终态**当成了注入攻击。
 * 护栏要挡的是"第三方内容把窗导航到远程站点"，不是本后端那一格 origin。
 *
 * 所以判据只有两条：`file://`（本地着陆页）与**后端那一个 origin**放行，其余一律拦。
 * 解析不出来的地址按拦处理（未知目标不该被导航），但**后端 origin 自己解析失败是配置坏了**，
 * 那时拦等于把应用锁死 —— 那种情况下宁可放行 origin 里那个字面串，也别让界面变成砖。
 */
export function isAllowedNavigation(url: string, backendUrl: string): boolean {
  if (typeof url !== "string" || url.length === 0) return false;
  if (url.startsWith("file://")) return true;
  try {
    const target = new URL(url);
    if (target.protocol === "file:") return true;
    const backend = new URL(backendUrl);
    return target.origin === backend.origin;
  } catch {
    return false;
  }
}
