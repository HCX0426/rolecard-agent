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
