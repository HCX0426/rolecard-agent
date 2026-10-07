// models 这一族的后端契约类型（快照 P3-1 第四刀从 `api/index.ts`
// 按路由域搬出，**正文逐字未改**，连注释都跟着它讲的那个名字走）。
// `index.ts` 以 `export *` 再导出本文件：消费者的 `from "../api"` 一字不动，
// 住址只活在实现里（数一份清单就守一份清单，facade 用通配是不给第二份清单留门）。


export interface BackendRow {
  name: string;
  provider: string;
  /** 客户端风格（native = Ollama 原生口 / openai = 兼容口），来自凭据组。界面判「这一档能不能
   *  设采样惩罚」只认它：后端 `core/graph.client_style()` 已是同一份判定的唯一出处，前端再拿
   *  provider 名字猜一遍就是第三处事实面（09-26 轮 R26-12：改供应商 id 时漏一处，症状是
   *  「明明支持却不给设」）。 */
  style?: string;
  base_url: string | null;
  model: string;
  sort_order: number;
  /** 本地 Ollama 的实际上下文窗口（tokens）；null = 引擎默认。 */
  num_ctx: number | null;
  /** 后端能力位：能否收图。对话页据此渲染「视觉」徽标；后端还把它当作**调用前拦截的一半
   *  证据**（与 Ollama `/api/show` 的实测同时为否才拒，见 core/nodes P1-2）。发图按钮故意
   *  不据此 disabled —— 判定只留在后端一处。 */
  supports_vision: boolean;
  /** 后端能力位：工具调用是否可用（false 时该轮不绑工具，兼容带 tools 会返回空的云端 VLM）。 */
  supports_tools: boolean;
  /** 采样惩罚三栏（null = 没设 = 不传 = 听引擎的）。`repeat_penalty` 只对本地 Ollama 有意义。 */
  repeat_penalty?: number | null;
  frequency_penalty?: number | null;
  presence_penalty?: number | null;
  has_key: boolean;
  key_masked: string | null;
  /** 派生只读：这行被哪些服务引用（模型页不再有用途下拉，见 ProviderModelRow.used_by）。 */
  used_by?: string[];
}

export interface ModelProvider {
  id: string;
  label: string;
  // 布尔（`R102-16`）：目录行从前发 "0"/"1" 字符串、分组行发 boolean —— JS 里 "0" 是
  // truthy，谁把目录行当分组行用就永远"要 key"。同一契约键一种类型。
  needs_key: boolean;
  base_url_hint: string;
  style: string; // native | openai
}

/** 模型设置：**只有分组这一个视图**（旧平铺 `backends` 投影随旧界面一起删了）。
 * 行上的凭据（provider/base_url/has_key）来自它所属的组 —— 需要平铺时用 helper 摊平。 */
export interface ModelSettings {
  default: string | null;
  fallbacks: string[];
  providers: ProviderGroup[];
}

/** 服务类别的内部键 → 给人看的名字。模型页与服务页都在回显 `used_by`，译名只留一份
 *  （两处各写一遍就是"同一个事实两个答案"，两页会各自漂移）。 */
export const USAGE_LABEL: Record<string, string> = {
  chat: "对话",
  embedding: "嵌入",
  rerank: "重排",
  ocr: "OCR",
};

/** 一行模型（属于某个凭据组）。能力位是**三态**：null = 没测过 → 界面渲染 `?`。 */
export interface ProviderModelRow {
  name: string;
  model: string;
  num_ctx: number | null;
  supports_vision: boolean | null;
  supports_tools: boolean | null;
  /** 采样惩罚三栏（null = 没设）。`repeat_penalty` 只有本地 Ollama 行会非空 —— 云端那一栏
   *  在界面上根本不出现，后端也会 400 挡下写入（OpenAI 兼容体没有这个标准字段）。 */
  repeat_penalty: number | null;
  frequency_penalty: number | null;
  presence_penalty: number | null;
  /** 这行被哪些服务引用（派生自服务页，模型页只读）：chat / embedding / rerank / ocr。 */
  used_by: string[];
  is_default: boolean;
}

/** 凭据组：一组 = 一个 (供应商, 端点)，key 只在这里出现一次。 */
export interface ProviderGroup {
  id: string;
  provider: string;
  label: string;
  base_url: string | null;
  style: string; // native | openai
  needs_key: boolean;
  has_key: boolean;
  key_masked: string | null;
  models: ProviderModelRow[];
}

/** 探测结论（`POST /api/settings/models/probe`）。三态字段 null = 不知道，不是"不行"。 */
export interface ProbeOutcome {
  reachable: boolean;
  detail: string;
  model_listed: boolean | null;
  tools: boolean | null;
  vision: boolean | null;
  vision_source: "free-metadata" | "uploaded-image" | "not-tested" | string;
  calls_used: number;
  models: string[];
}

/** POST /api/services/check 的单探测量（ollama 或 openai_compatible 之一）。 */
export interface ConnectivityProbe {
  reachable: boolean;
  detail: string;
  models?: string[];
}

export type ConnectivityResult = Record<string, ConnectivityProbe>;

/** 本地推理服务（Ollama）的一张状态快照：模型页「本地推理服务」卡的数据面。
 *  `pinned` = 常驻（`keep_alive=-1`，自己永远不会让出显存）；`is_local` 决定给不给"常驻"按钮。 */
export interface ResidentModel {
  name: string | null;
  size_bytes: number;
  expires_at: string | null;
  pinned: boolean;
}

export interface LocalServiceStatus {
  base_url: string;
  running: boolean;
  resident: ResidentModel[];
  resident_bytes: number;
  pinned: boolean;
  is_local: boolean;
  model: string | null;
}
