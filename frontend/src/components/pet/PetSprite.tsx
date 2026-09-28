// 桌宠形象层：waifu spritesheet 渲染器（MIT-0「waifu-sprites」的图面协议）。
//
// 协议（见 waifu-sprites 仓库）：一张 1536×1872 的 WebP 精灵图，切成 8 列 × 9 行，
// 每格 192×208；**行 = 一套逐帧动画**，列 = 帧（自左向右循环）。状态→行的映射只取
// 协议里**确认过语义**的几行，没底的映射一律回 idle（宁可形象不动，也不要诡异动作）：
//
//   row 0  idle             row 3  waving（说话/冒泡）
//   row 1  running-right    row 4  jumping（成功）
//   row 2  running-left     （row 5–8 语义未在协议主表确认，不映射）
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

// 状态 → 行 的映射。只收协议主表确认过的行。
const STATUS_ROW: Record<PetStatus, number> = {
  idle: 0,
  typing: 1,
  listening: 2,
  speaking: 3,
  success: 4,
  thinking: 0, // 想想时的动作没底，回 idle 行（表情差异交给 GeometricPet 那层）——宁停不动。
};

export interface PetSpriteProps {
  /** spritesheet 的 URL（`/pets/<包名>/sprite.webp` 之类）。缺省/加载失败 → GeometricPet。 */
  src?: string;
  status?: PetStatus;
  /** 渲染尺寸。默认按单格 192×208 的 5/6 缩放到 160×184（小窗里的常驻尺寸）。 */
  width?: number;
  height?: number;
  className?: string;
  "data-testid"?: string;
}

export function PetSprite({
  src,
  status = "idle",
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

  const row = STATUS_ROW[status];
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
        imageRendering: "pixelated",
      }}
      role="img"
      aria-label="桌宠形象"
    />
  );
}

export default PetSprite;