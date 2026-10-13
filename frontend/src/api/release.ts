// release 这一族的后端契约类型（快照 P3-1 第四刀从 `api/index.ts`
// 按路由域搬出，**正文逐字未改**，连注释都跟着它讲的那个名字走）。
// `index.ts` 以 `export *` 再导出本文件：消费者的 `from "../api"` 一字不动，
// 住址只活在实现里（数一份清单就守一份清单，facade 用通配是不给第二份清单留门）。


/** 桌面壳安装包的可下载状态（D②-4）。`available=false` 时**其余字段都不存在**，
 *  界面也就整卡不渲染 —— "能不能下载"由后端看产物文件在不在决定，不是由前端猜。 */
export interface ShellRelease {
  available: boolean;
  configured: boolean;
  file_name?: string;
  size_bytes?: number;
  built_at?: string;
}

/** 「检查更新」的读数（设计稿《更新机制与OCR随包》§3.1，与 `core/update_check.py`
 *  的 `to_api()` 一一对应）。判据全在后端，这里只照实画：
 *  - `source=disabled` = 总闸关着（内网/离线部署），一次网都没出；
 *  - `source=none` + `error` = 问不到（没配源 / 出网失败），界面说"问不到"而不是"已是最新"；
 *  - `has_update` 只在真比出"最新 > 当前"时为 true —— 下载永远走用户自己点的链接。 */
export interface UpdateCheck {
  current: string;
  latest: string | null;
  has_update: boolean;
  source: "github" | "release_dir" | "disabled" | "none";
  url: string | null;
  file_name: string | null;
  size_bytes: number | null;
  error: string | null;
}
