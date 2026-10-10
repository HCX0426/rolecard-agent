// services 这一族的后端契约类型（快照 P3-1 第四刀从 `api/index.ts`
// 按路由域搬出，**正文逐字未改**，连注释都跟着它讲的那个名字走）。
// `index.ts` 以 `export *` 再导出本文件：消费者的 `from "../api"` 一字不动，
// 住址只活在实现里（数一份清单就守一份清单，facade 用通配是不给第二份清单留门）。


/** 「服务」页的载荷契约（`R102-22`：从前这 23 个键全部声明在 ServicesPanel.tsx 里，
 * `api/index.ts` 头部自称"与后端契约一一对应"却看不见它们 —— 改一个字段名，契约测试与这里两处
 * 都不红，界面静默显示"—"。键面 = `GET /api/services` 的实测响应。 */
export interface ServiceEndpoint {
  id: string;
  label: string;
  kind: "local" | "cloud";
  available: boolean;
  reason: string;
  enabled: boolean;
  builtin: boolean;
  key_masked: string | null;
  base_url: string | null;
  model: string | null;
  ref_backend: string | null;
  stale: boolean;
  order: number | null;
}

export interface ServiceCategoryView {
  key: string;
  title: string;
  hint: string;
  effective: string | null;
  effective_kind: string | null;
  degraded_from: string | null;
  /** 配置里的第 1 位（「默认」徽标钉它）。与 `effective`（此刻实际在服务那台）分开：
   *  本地默认挂了、运行时由云端兜底时二者不同 —— 徽标要让用户看出"该用哪个 vs 正在凑合用哪个"。 */
  default_backend?: string | null;
  readonly: boolean;
  /** order_only = 只能调顺序（第 1 位 = 默认，其余依次回退）；增删与 key 在「模型」页签。 */
  order_only?: boolean;
  candidates: ServiceEndpoint[];
}

export interface ServicesView {
  services: ServiceCategoryView[];
}
