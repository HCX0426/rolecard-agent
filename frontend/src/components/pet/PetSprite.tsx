// 桌宠形象层：waifu spritesheet 渲染器（MIT-0「waifu-sprites」的图面协议）。
//
// 协议（见 waifu-sprites 仓库）：一张 1536×1872 的精灵图，切成 8 列 × 9 行，
// 每格 192×208；**行 = 一套逐帧动画**，列 = 帧（自左向右循环）。状态→行的映射只取
// **有底**的几行：0–4 是协议主表确认过的；5–8 主表未确认，所以它们不由这张通用表说了算，
// 而是**由动物包的作者声明**（见 `rows` prop 与 `DEFAULT_PACK_ROWS`）——
//
//   row 0  idle             row 3  waving（说话/冒泡）
//   row 1  running-right    row 4  jumping（成功）
//   row 2  running-left     （5–8：包自定，默认包是 晕/等待/琢磨/睡）
//
// 为什么这样切开：换一张集市上的包时，那几行是什么语义只有那张包知道；通用表若替它猜，
// 猜错就是"想事情时她在打滚"。所以通用表保守（只放协议确认过的），包自己多说的放包里。
//
// 为什么"素材缺失回退到 GeometricPet"：这一层是**差异内核的贴皮**，不该因为一张图没放进
// 资源目录就让桌宠消失 —— 形象本体由 `GeometricPet` 兜底（呼吸/眨眼/说话开合），用户哪天
// 放进一张合法 spritesheet（`frontend/public/pets/`），这里立刻换上。
// 自研只留差异内核：帧推进的循环、缩放、回退都在这一个组件里，换素材不换代码。
//
// 图片尺寸与剪裁全由协议常量推导（不写死一处渲染样式抄一份数字）：加一行状态只需补
// 一行映射。

import { useEffect, useRef, useState } from "react";

import { GeometricPet } from "./GeometricPet";

export type PetStatus =
  | "idle"
  | "typing"
  | "listening"
  | "speaking"
  | "thinking"
  | "success";

// waifu-sprites 图面协议常量（唯一的事实源，渲染不抄第二遍）。
export const SHEET_COLS = 8;
export const SHEET_ROWS = 9;
export const CELL_W = 192;
export const CELL_H = 208;
export const FRAME_MS = 150; // 每帧时长：一行 8 帧 ≈ 1.2s 一个循环

// 状态 → 行 的映射。只收协议主表确认过的行（保守的那半边，见文件头）。
const STATUS_ROW: Record<PetStatus, number> = {
  idle: 0,
  typing: 1,
  listening: 2,
  speaking: 3,
  success: 4,
  thinking: 0, // 通用表不猜 5–8：想想时的动作由包声明（默认包在 DEFAULT_PACK_ROWS 里改到 7）
};

/** 默认形象包（`scripts/make_pet_sheet.py` 生成的那份）自带的额外声明。
 *  rows 5–8 的语义是**这份包定的**：5=晕、6=等待、7=琢磨、8=睡 —— 协议主表没确认，
 *  所以它不进通用映射表，只随这张包走。 */
export const DEFAULT_PACK_ROWS: Partial<Record<PetStatus, number>> = {
  listening: 6, // 等待：头歪着等你把话说完（比"向左跑"贴切得多）
  thinking: 7,  // 琢磨：目光扫读，正好配"她在想"
};

/** 默认包的地址（`frontend/public/pets/default/sprite.png`，构建时随 public 进 dist）。 */
export const DEFAULT_PET_SHEET = "/pets/default/sprite.png";

export interface PetSpriteProps {
  /** spritesheet 的 URL（`/pets/<包名>/sprite.png` 之类）。缺省 → GeometricPet。 */
  src?: string;
  status?: PetStatus;
  /** 覆盖/追加状态→行（包作者声明 5–8 行语义的地方）。缺省 = 只有通用表。 */
  rows?: Partial<Record<PetStatus, number>>;
  /** 渲染尺寸。默认按单格 192×208 缩到 160×184（小窗里的常驻尺寸）。 */
  width?: number;
  height?: number;
  className?: string;
  "data-testid"?: string;
}

export function PetSprite({
  src,
  status = "idle",
  rows,
  width = 160,
  height = 184,
  className,
  ...rest
}: PetSpriteProps) {
  const [frame, setFrame] = useState(0);
  // 状态变了从头动起；同状态不重置节拍（换一次行不该把那 1.2s 拨乱）。
  const prevStatus = useRef<PetStatus>(status);
  useEffect(() => {
    if (prevStatus.current !== status) {
      prevStatus.current = status;
      setFrame(0);
    }
  }, [status]);

  // 逐帧推进：挂机（idle）也走，让素材活泼起来；渲染用的是固定 8 帧循环。
  useEffect(() => {
    const timer = setInterval(() => setFrame((f) => (f + 1) % SHEET_COLS), FRAME_MS);
    return () => clearInterval(timer);
  }, []);

  if (!src) {
    return <GeometricPet status={status} width={width} height={height} className={className} {...rest} />;
  }

  const row = rows?.[status] ?? STATUS_ROW[status];
  const scaleX = width / CELL_W;
  const scaleY = height / CELL_H;
  return (
    <div
      data-testid={rest["data-testid"] ?? "pet-sprite"}
      className={className}
      style={{
        width,
        height,
        // 整张 sheet 按渲染尺寸缩放背景；position 按 (列, 行)×格 同尺度。
        backgroundImage: `url(${src})`,
        backgroundSize: `${CELL_W * SHEET_COLS * scaleX}px ${CELL_H * SHEET_ROWS * scaleY}px`,
        backgroundPosition: `-${frame * CELL_W * scaleX}px -${row * CELL_H * scaleY}px`,
      }}
      role="img"
      aria-label="桌宠形象"
    />
  );
}

export default PetSprite;