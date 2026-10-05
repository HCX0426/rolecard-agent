/**
 * 大小上限的唯一事实面在后端（config.py 的 max_upload_bytes / max_image_bytes），
 * 经 /api/health 下发 —— 前端不再手抄数字（改后端一处即可）。
 *
 * 模块级缓存：同一页生命周期内只问一次。后端不可达时回落出厂默认 —— 那两个数
 * 与 config.py 的出厂值一致，只是"连不上后端"时的兜底；连得上时永远现读。
 */

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
    try {
      const res = await fetch("/api/health");
      if (!res.ok) return FALLBACK;
      const body = (await res.json()) as { max_upload_bytes?: number; max_image_bytes?: number };
      if (typeof body.max_upload_bytes !== "number" || typeof body.max_image_bytes !== "number") {
        return FALLBACK;
      }
      return { upload: body.max_upload_bytes, image: body.max_image_bytes };
    } catch {
      return FALLBACK;
    }
  })();
  return cached;
}
