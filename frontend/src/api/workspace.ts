// workspace 这一族的后端契约类型（快照 P3-1 第四刀从 `api/index.ts`
// 按路由域搬出，**正文逐字未改**，连注释都跟着它讲的那个名字走）。
// `index.ts` 以 `export *` 再导出本文件：消费者的 `from "../api"` 一字不动，
// 住址只活在实现里（数一份清单就守一份清单，facade 用通配是不给第二份清单留门）。


/** 任务目录（角色可读写的授权范围）：path = 生效目录，overridden = 是否 DB 覆盖（否=跟随 env）。 */
export interface WorkspaceDir {
  path: string;
  overridden: boolean;
}

export interface TreeEntry {
  name: string;
  is_dir: boolean;
  size: number;
}

export interface TreeResult {
  path: string;
  parent: string;
  entries: TreeEntry[];
  truncated: boolean;
}

/** 上传目录里没被任何 ingestion 台账引用的文件（`/api/uploads/orphans`）。 */
export interface OrphanUpload {
  name: string;
  size: number;
  companion: boolean;
}

export interface OrphanReport {
  orphans: OrphanUpload[];
  total_bytes: number;
  scanned: number;
  referenced: number;
  /** 反方向（`R28-19`）：台账写着原件、这台机器上没有那些文件。换过数据根时这一列不为空。 */
  dangling: string[];
}

export interface CleanupResult {
  deleted: number;
  freed_bytes: number;
  scanned: number;
  referenced: number;
}
