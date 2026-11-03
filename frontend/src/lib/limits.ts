/**
 * 大小上限的唯一事实面在后端（config.py 的 max_upload_bytes / max_image_bytes），
 * 经 /api/health 下发 —— 前端不再手抄数字（改后端一处即可）。
 *
 * 模块级缓存：同一页生命周期内只问一次。后端不可达时回落出厂默认 —— 那两个数
 * 与 config.py 的出厂值一致，只是"连不上后端"时的兜底；连得上时永远现读。
 *
 * ⚠ 2026-11-03 审查 A5 的收口：从前这里是 `await fetch("/api/health")` —— **绕过 api 层
 * 的第二套请求路径**，三件事同时不对：
 *   1. **没有超时**。`fetch` 默认永远不等（`api/index.ts:63-68` 立论就为治这个），后端挂起
 *      （进程在但不响应）时这里连一句错误都不出，而它坐在 `Composer.pickImage` 的
 *      **判尺寸之前** —— 症状是"选了图片永远没有任何反应"。
 *   2. **缓存的是那个挂死的 promise**。`cached ??=` 之后整个页面生命周期每次 await 都拿到
 *      同一个永不 settle 的对象，一次挂起 = 永久挂起。
 *   3. 更隐蔽的一条：`/api/health` 在后端是**免鉴权**的（默认在 AUTH_EXEMPT_PATHS 里），
 *      所以这句裸 fetch 也顺带绕开了数据源 —— 云端形态下上限该问**接这趟上传的那台后端**
 *      （`api.upload` 走 apiBase()），而裸相对路径问的是本机界面自己那台。两个答案在
 *      "两台机器配置不同"时不一样，症状是"前端放行、后端 413"。
 * 现在走 `api.get`：超时、apiBase()、authHeaders() 全部复用同一套（不在这里再造第二套
 * 判据），并且**只有拿到真值才缓存** —— 回落那一路不缓存，让下一次尝试还有机会问到真值。
 * 残留如实记：后端真挂起时首趟仍要等满一个请求超时（30s）才回落 —— 这与全站任何一条
 * 请求的等待一致，是归一的结果而不是新缺陷；根治要么给预检单独一档更短的超时，
 * 要么让上传直接由后端拦（后端本来就在拦），两者都不是本轮该顺手改的形状。
 */
import { api } from "../api";

/** 后端 health 里我们关心的那两个键（其余字段不声明，免得看起来像完整契约 ——
 *  完整形状在 `api/schema.gen.ts` 的 `/api/health` 那一格，由 openapi 同步尺子把着）。 */
type HealthLimits = { max_upload_bytes?: unknown; max_image_bytes?: unknown };

const FALLBACK = { upload: 20 * 1024 * 1024, image: 15 * 1024 * 1024 };

export interface Limits {
  /** 上传文件单文件字节上限（config.max_upload_bytes）。 */
  upload: number;
  /** 单张图片原始字节上限（config.max_image_bytes）。 */
  image: number;
}

let cached: Promise<Limits> | null = null;

export function fetchLimits(): Promise<Limits> {
  cached ??= (async () => {
    // 拿不到真值就回落、并且**不缓存这个回落**（下一次还有可能问到）：
    // 三种"拿不到"合在同一个 null 里 —— 请求失败/超时、非 2xx（api.get 会抛）、
    // 形状不对（旧后端或反代回了别的东西）。
    const body = await api
      .get<HealthLimits>("/api/health")
      .catch(() => null);
    const { max_upload_bytes: upload, max_image_bytes: image } = body ?? {};
    if (typeof upload !== "number" || typeof image !== "number") {
      cached = null;
      return FALLBACK;
    }
    return { upload, image };
  })();
  return cached;
}
