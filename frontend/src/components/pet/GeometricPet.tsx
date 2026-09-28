// 桌宠形象层的**兜底本体**：一只圆润的 SVG 角色，靠 CSS keyframes 活起来。
//
// 为什么需要它：形象素材（waifu spritesheet）是"内容"不是"代码" —— 没有它桌宠不该
// 消失（那是把差异内核的贴皮当成了本体）。这一只负责三件事，全部由 `status` 一个 prop
// 驱动，不引任何图片/动画库：
//
//   · 呼吸（idle）：身体与耳朵的轻缓 scale 往复，让"没人说话"也有活气；
//   · 眨眼（idle）：每 ~3.4s 一次 150ms 的合眼。挑 3.4 是因为它不能被 60fps 的帧整除，
//     眨眼的时刻不会跟呼吸的节拍锁死成"每次都在同一姿态眨眼"的机械感；
//   · 说话（speaking/typing）：嘴巴张开开合、身体轻微上下浮 —— 她正冒话/正在生成；
//   · 思考（thinking）：眼睛半闭 + 头顶一颗"…"气泡。
//
// 只切 data-status（CSS 选择器），动画时长/曲线全在样式表里 —— 换手感不用改逻辑。
// 形状与颜色刻意中性（米色系 + 深灰描边）：它是"默认出厂"，用户放进 spritesheet 之后
// 由 PetSprite 换人，这儿就是个体面的替身。

import type { CSSProperties } from "react";

import type { PetStatus } from "./PetSprite";

export interface GeometricPetProps {
  status?: PetStatus;
  width?: number;
  height?: number;
  className?: string;
  "data-testid"?: string;
}

// 台词框（speaking 时头顶的气泡）与思考泡泡共用这颗小圆底的定位。
const bubbleBase: CSSProperties = {
  position: "absolute",
  top: "6%",
  left: "50%",
  transform: "translateX(-50%)",
};

export function GeometricPet({
  status = "idle",
  width = 160,
  height = 184,
  className,
  "data-testid": testId = "pet-figure",
}: GeometricPetProps) {
  return (
    <div
      data-testid={testId}
      data-status={status}
      className={`geometric-pet relative select-none overflow-visible ${className ?? ""}`}
      style={{ width, height }}
      role="img"
      aria-label="桌宠形象"
    >
      {/* 思考泡泡：thinking 才有（"…"由 CSS content 给） */}
      <div className="pet-think" style={bubbleBase} aria-hidden="true" />

      <svg viewBox="0 0 192 208" className="pet-body" width="100%" height="100%">
        {/* 耳朵 */}
        <ellipse cx="62" cy="52" rx="16" ry="26" fill="#f5c8a8" stroke="#8a7268" strokeWidth="4" className="pet-ear pet-ear-l" />
        <ellipse cx="130" cy="52" rx="16" ry="26" fill="#f5c8a8" stroke="#8a7268" strokeWidth="4" className="pet-ear pet-ear-r" />
        {/* 头身一体的圆滚身体 */}
        <ellipse cx="96" cy="112" rx="62" ry="58" fill="#fbe8d2" stroke="#8a7268" strokeWidth="5" className="pet-torso" />
        {/* 肚皮高光 */}
        <ellipse cx="96" cy="130" rx="38" ry="30" fill="#fff7ec" opacity="0.9" />
        {/* 眼睛 */}
        <g className="pet-eyes">
          <ellipse cx="72" cy="100" rx="9" ry="12" fill="#4a403a" />
          <ellipse cx="120" cy="100" rx="9" ry="12" fill="#4a403a" />
          <circle cx="75" cy="95" r="3.5" fill="#fff" />
          <circle cx="123" cy="95" r="3.5" fill="#fff" />
        </g>
        {/* 腮红 */}
        <ellipse cx="52" cy="120" rx="8" ry="5" fill="#f2a489" opacity="0.55" />
        <ellipse cx="140" cy="120" rx="8" ry="5" fill="#f2a489" opacity="0.55" />
        {/* 嘴：speaking 时张开（CSS 里放大 / 眼睛半闭见 thinking） */}
        <path
          className="pet-mouth"
          d="M 84 132 Q 96 142 108 132 Q 96 138 84 132 Z"
          fill="#b5655e"
        />
      </svg>
    </div>
  );
}

export default GeometricPet;