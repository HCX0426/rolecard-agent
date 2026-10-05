/**
 * 数据源（M5）：这台界面现在连的是**本机那份**还是**某个云端实例**。
 *
 * 三条口径是设计稿里定的，代码只负责不背叛它们：
 *
 *   1. **切换 = 换 base_url + 换一份完整数据集**，不是在前端加一层过滤。所以真的换成功之后
 *      调用方要整页重载（`reload()`）—— 角色卡、会话、模型列表、记忆全部来自对面，
 *      留着任何一样本机的东西在屏幕上就是"半换状态"，而 M3 前半已经定过：
 *      半换比不换更糟。
 *   2. **默认本机、不登录也能用**：`read()` 拿不到合法值时一律回落本机，绝不"上次是云端
 *      就猜这次也是"——猜错的症状是"我的数据没了"。
 *   3. **连不上就留在本机**：探测失败不写状态，界面上一格都不会变。
 *
 * 凭据存在 localStorage 里，这是**说出来的取舍**而不是疏忽：这台机器上的 `.env` 与
 * 模型库里的 api_key 本来就是明文（见 `core/model_settings.py` 的 "Plaintext at rest"），
 * 换云端登录用的那枚凭据的信任域与它们完全相同（同一个用户账号、同一台机器）。
 * 反过来，如果每次重载都重新要密码，用户会去用一个能记住的弱口令 —— 那是更坏的结果。
 * 真要收紧，正确的做法是服务端发短期令牌，而不是在这里少存一个字段。
 */

import { describeError } from '../lib/errors';
const KEY = "rolecard.dataSource.v1";

export type DataSource =
  | { mode: "local" }
  | { mode: "cloud"; base: string; user: string; secret: string };

export const LOCAL: DataSource = { mode: "local" };

/** 归一化用户填的地址：只认 http/https，去掉尾部斜杠；不合法返回 null。
 *  为什么在这儿挡：留着 `javascript:` 或裸 host 进 `fetch`，症状会以"界面整个白了"
 *  出现，没人会往地址栏上想。 */
export function normalizeBase(raw: string): string | null {
  const text = (raw || "").trim();
  if (!text) return null;
  const withScheme = /^[a-z][a-z0-9+.-]*:\/\//i.test(text) ? text : `https://${text}`;
  try {
    const url = new URL(withScheme);
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
    if (!url.hostname) return null;
    if (url.pathname !== "/" || url.search || url.hash) {
      // 只到 host:port 为止。带路径进去会让 `/api/roles` 拼成 `/foo/api/roles`，
      // 症状是 404，而 404 在界面上长得像"对面没数据"。
      return null;
    }
    return url.origin;
  } catch {
    return null;
  }
}

function basic(user: string, secret: string): string {
  // 非 ASCII 用户名/口令：TextEncoder 给 UTF-8 字节，`btoa` 只吃 latin1，
  // 所以逐字节转 —— 直接 btoa(中文) 会抛，症状是"点了切换没反应"。
  const bytes = new TextEncoder().encode(`${user}:${secret}`);
  let binary = "";
  for (const b of bytes) binary += String.fromCharCode(b);
  return `Basic ${btoa(binary)}`;
}

/** 当前数据源。解析失败 / 版本不对 / 地址已不合法 ⇒ 一律回落本机（见文件头第 2 条）。 */
export function read(): DataSource {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return LOCAL;
    const parsed = JSON.parse(raw) as Partial<DataSource> & Record<string, unknown>;
    if (parsed.mode !== "cloud") return LOCAL;
    const base = normalizeBase(String(parsed.base ?? ""));
    const user = String(parsed.user ?? "");
    if (!base || !user) return LOCAL;
    return { mode: "cloud", base, user, secret: String(parsed.secret ?? "") };
  } catch {
    return LOCAL;
  }
}

export function save(source: DataSource): void {
  try {
    if (source.mode === "local") localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, JSON.stringify(source));
  } catch {
    /* 隐私模式 / 配额满：切换这次不生效，比让界面卡死好。下次启动仍是本机。 */
  }
}

/** 切换之后整页重载 —— 单独给这一件事一个名字，是为了让「真的重载了」可测。
 *  （测试里直接改 `window.location` 会让 jsdom 换一个 origin 的 storage，于是
 *   「存进去的状态」和「读出来的状态」不是同一份，测出来的是个假的。） */
export function reloadApp(): void {
  window.location.reload();
}

export function isCloud(): boolean {
  return read().mode === "cloud";
}

/** 「刚登录成功」这一件事要活过一次整页重载，所以它得有地方存 —— 但不能存进 `KEY` 那份
 *  数据源状态里：那是"连到哪台"的持久事实，而"要不要问一句要不要上行"是一次性的。
 *  `sessionStorage` 正好是这个语义：换一个标签页就没了，关掉窗口也不会欠用户一个问题。 */
const JUST_LOGGED_IN = "rolecard.dataSource.justLoggedIn";

export function markJustLoggedIn(): void {
  try {
    sessionStorage.setItem(JUST_LOGGED_IN, "1");
  } catch {
    /* 存不下就少问一次：上行入口仍然常驻在侧栏那一行，不会因此丢掉功能。 */
  }
}

/** 只看，不取走。为什么"挂载即取走"是错的（真浏览器实测，两条 `getItem` 日志）：
 *  `connect()` 里 `save(云端)` 与 `reloadApp()` 之间，React 会先把**当前这一页**重渲染一次
 *  —— `cloud` 从 false 变 true，那个 `[cloud]` 的 effect 于是在**跳转前**就跑了一遍，
 *  把标记吃掉；等真正加载完的新页面再去看，已经是 null，弹层永远不出现。
 *  所以标记要活到**人真的答过这一问**为止，而不是活到第一次看见它为止。 */
export function peekJustLoggedIn(): boolean {
  try {
    return sessionStorage.getItem(JUST_LOGGED_IN) === "1";
  } catch {
    return false;
  }
}

/** 答过了（关掉弹层）才清。没答就关标签页 = 标记随 sessionStorage 一起没了，不会欠第二次弹窗。 */
export function clearJustLoggedIn(): void {
  try {
    sessionStorage.removeItem(JUST_LOGGED_IN);
  } catch {
    /* 清不掉只是会多问一次，不是坏消息。 */
  }
}

/** 请求该打到哪儿：本机 = 同源（相对路径原样用），云端 = 那个 origin。 */
export function apiBase(): string {
  const source = read();
  return source.mode === "cloud" ? source.base : "";
}

/** 每次请求都现取（不缓存到模块变量）：切换之后 `reload()` 之前，
 *  已经飞在路上的请求不该被新地址劫走，而缓存会让这件事变成隐式规则。 */
export function authHeaders(): Record<string, string> {
  const source = read();
  return source.mode === "cloud" ? { Authorization: basic(source.user, source.secret) } : {};
}

export type Probe = { ok: true; origin: string; who: string } | { ok: false; why: string };

/**
 * "连接并切换"那一下：拿这组凭据去对面读一次真实数据，成功才写状态。
 *
 * 为什么打 `/api/roles` 而不是 `/api/health`：health 是免鉴权的，拿它当"登录成功"
 * 会把"服务活着"误报成"这个账号能进"——切换之后界面就该是空的。
 * 为什么不带 CORS 白名单式的探测：跨域被拒时浏览器只给一个网络错误，分不出是
 * 连不上还是没放行，所以那句提示**两件事一起说**，让人知道下一步查哪个。
 */
export async function tryConnect(base: string, user: string, secret: string): Promise<Probe> {
  const origin = normalizeBase(base);
  if (!origin) {
    return { ok: false, why: "地址不像一个程序地址（只要 http(s)://主机[:端口]，不要带路径）。" };
  }
  if (!user) return { ok: false, why: "要填账号。" };
  let res: Response;
  try {
    res = await fetch(`${origin}/api/roles`, {
      headers: { Authorization: basic(user, secret) },
      // 云端那台是另一个 origin：不带 credentials 的话浏览器不会送出 Authorization？
      // 不会——Authorization 是显式头，同域跨域都发。但 `same-origin` 是 fetch 的默认，
      // 留着它会让某些实现连预检都省了，行为随浏览器而变，所以显式写 `include`。
      credentials: "include",
      mode: "cors",
    });
  } catch (e) {
    return {
      ok: false,
      why:
        `连不上 ${origin}（网络不通，或对面没放行这个来源的跨域访问 —— ` +
        `云端那台要设 API_ALLOW_ORIGINS）。${describeError(e)}`,
    };
  }
  if (res.status === 401 || res.status === 403) {
    return { ok: false, why: "账号或密码不对（对面拒了这次登录）。" };
  }
  if (!res.ok) {
    return { ok: false, why: `对面回 HTTP ${res.status}，不像一个 rolecard 服务。` };
  }
  const body: unknown = await res.json().catch(() => null);
  if (!Array.isArray(body)) {
    return { ok: false, why: "对面回的内容不是角色卡列表，可能不是 rolecard 服务。" };
  }
  return { ok: true, origin, who: `${origin} · ${user}` };
}
