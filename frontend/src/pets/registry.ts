// 桌宠形象包的**解析**（不是清单的搬运 —— 读数据走 `api`，这里只做纯函数）。
//
// 一条角色卡上的 `pet_pack` 到一个可渲染的包，中间只有两种"没配上"，它们必须分开：
//  * 「这个角色从没配过形象」（`pet_pack` 是空串）→ 落**默认包**。这不叫失败。
//  * 「配了一个清单里没有的包」（素材被删了 / 换机器了 / 手写过）→ 也落默认包，
//    但要把原因留给界面说一句话 —— 否则症状是"我明明选了爱莉，怎么还是那只团子"，
//    而没有任何地方承认它听见了那个选择。
//  * 第三种才落到 `GeometricPet`（SVG 兜底本体）：包在清单里、图却加载失败 —— 那才是"素材坏了"。
//    这条不用在这里做：`PetSprite` 自己管，本模块只保证"给它的 src 是清单里真有的那一个"。

import {
  DEFAULT_PACK_ROWS,
  DEFAULT_PET_SHEET,
  type PetStatus,
} from "../components/pet/PetSprite";

/** 后端 `/api/pets` 回的那一条（字段与 `api/routers/pets.py` 一一对应）。 */
export interface PetPack {
  id: string;
  label: string;
  /** 渲染器种类：`"sheet"`（序列帧）或 `"live2d"`（模型）。没有渲染器的包不会出现在清单里。 */
  kind: string;
  /** 这个包自己声明的状态→行（序列帧包：协议主表没确认的 5–8 行归包说）。 */
  rows: Partial<Record<PetStatus, number>>;
  /** Live2D 包自己声明的状态→motion 组名（`{"speaking": "TapBody"}`）；序列帧包用不上。 */
  motions: Record<string, string>;
  /** 入口文件在包目录里的相对路径（`sprite.png` 或 `<角色>.model3.json`）。 */
  entry: string;
  /** "user" = 数据根下外挂的那份；"bundled" = 随包那份。 */
  source: string;
  /** 入口文件的地址（名字留着是因为序列帧那条路已经按它写了；live2d 给的是 model3.json）。 */
  sheet_url: string;
}

export interface PetPackListing {
  packs: PetPack[];
  /** 放了、但没进取选项的那些目录，各带一句为什么。 */
  skipped: { id: string; reason: string }[];
  /** 外挂素材该放的那个目录（印在界面上，不让人去猜路径）。 */
  user_dir: string;
  /** Cubism Core 在不在位 —— 不在 ⇒ live2d 包一个都不会出现在 packs 里，原因在 skipped。 */
  cubism_core: boolean;
  cubism_core_path: string;
}

/** 仓库自绘的默认包 id（`scripts/make_pet_sheet.py` 生成的那份）。 */
export const DEFAULT_PACK_ID = "default";

/**
 * **清单没读到**时用的那一份（对着 `PetSprite` 里那两个常量组装，不抄第二份数字）。
 *
 * 为什么要留这条而不是直接退化成 SVG：对面可能是一个还没有 `/api/pets` 的旧后端
 * （升级顺序不由我们控制），而随包那张 `dist/pets/default/sprite.png` 明明就在
 * 静态目录里 —— 这时候画几何体是**把能用的东西弄坏**，与"素材坏了才兜底"那条分工不符。
 */
const UNLISTED_DEFAULT: PetPack = {
  id: DEFAULT_PACK_ID,
  label: "默认",
  kind: "sheet",
  rows: DEFAULT_PACK_ROWS,
  motions: {},
  entry: "sprite.png",
  source: "bundled",
  sheet_url: DEFAULT_PET_SHEET,
};

/**
 * 角色卡上的 `petPack` → 这一只该画什么。
 *
 * `listing === null` 表示"清单没读到"（旧后端 / 网络断了）：保持今天的行为，画随包那张默认图。
 * 读到清单而里面**一个包都没有**才返回 `pack: null` —— 那才是真没素材，落 `GeometricPet`。
 * "配了个清单里没有的包"落默认包并把 `misassigned` 立起来（话要说，否则症状是"选了没反应"）。
 */
export function resolvePack(
  listing: PetPackListing | null,
  petPack: string | null | undefined,
): { pack: PetPack | null; misassigned: boolean } {
  if (listing === null) return { pack: UNLISTED_DEFAULT, misassigned: false };
  const packs = listing.packs;
  const fallback = packs.find((p) => p.id === DEFAULT_PACK_ID) ?? null;
  if (!petPack) return { pack: fallback, misassigned: false };
  const hit = packs.find((p) => p.id === petPack);
  if (hit) return { pack: hit, misassigned: false };
  return { pack: fallback, misassigned: true };
}
