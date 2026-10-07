// runtime 这一族的后端契约类型（快照 P3-1 第四刀从 `api/index.ts`
// 按路由域搬出，**正文逐字未改**，连注释都跟着它讲的那个名字走）。
// `index.ts` 以 `export *` 再导出本文件：消费者的 `from "../api"` 一字不动，
// 住址只活在实现里（数一份清单就守一份清单，facade 用通配是不给第二份清单留门）。


/** 运行环境（env + DB 覆盖）的展示项。kind=ro 表示不可在线修改；secret 永不回明文。 */
export interface RuntimeItem {
  key: string;
  field: string;
  label: string;
  value: string;
  default: string;
  changed: boolean;
  /** DB 覆盖在位（≠ changed：env 也可能与出厂默认不同）。 */
  overridden: boolean;
  /** 覆盖的原始值（可编辑初值）；secret / 未覆盖 = null。 */
  override_value: string | null;
  kind: "bool" | "str" | "secret" | "float" | "int" | "ro";
  choices: string[] | null;
  note: string;
}

export interface RuntimeGroup {
  key: string;
  label: string;
  items: RuntimeItem[];
}

export interface RuntimePayload {
  note: string;
  groups: RuntimeGroup[];
}
