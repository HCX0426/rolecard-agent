/** 角色强调色（accent）的派生与注入（2026-10-25 玻璃重设计）。
 *
 *  规则：**角色卡上的 accent 优先；没配就从 role_id 哈希派生**——确定性（同 id 同色，
 *  跨机一致、不掷骰子）。派生色走"暖→冷"的固定色相环，在可可底上都读得出。
 *
 *  注入方式：写 `<html>` 上的 CSS 变量 `--accent`（材质层用它派生雾底/玻璃底/墨色）。
 *  这样"切角色换整站温度"只是改一个变量，CSS 那边不用动一根手指。
 */

/** 内置角色的默认色（出厂卡各有各的糖衣）。key = role_id。
 *  全部落在**暖色域**——材质是可可底，冷色 accent 与它是撞色（靛蓝配可可棕最明显，
 *  看着像"没改完的旧版蓝"）。玻璃材质的调性要求 accent 与底同族。 */
const BUILTIN_ACCENTS: Record<string, string> = {
  general_assistant: "#a8703c", // 焦糖：通用、暖、稳
  medical_archivist: "#8a5a3c", // 赭石：档案、可信
  elysia: "#a04a6a", // 玫瑰：角色感强
};

/** 派生色相环（HSL 的 H 值）：焦糖 → 玫瑰 → 赭石 → 橄榄 → 赤陶 → 蜜色 → 栗。
 *  刻意**避开 200-280（蓝紫区）**：那一片与可可底是撞色。 */
const DERIVED_HUES = [28, 340, 18, 44, 8, 36, 12];

function hashString(text: string): number {
  let h = 2166136261;
  for (let i = 0; i < text.length; i += 1) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

/** 角色 → 强调色（hex）。显式 accent 优先，否则按 role_id 确定性派生。 */
export function accentFor(roleId: string, explicit?: string | null): string {
  if (explicit && /^#[0-9a-fA-F]{3,6}$/.test(explicit)) {
    return expandHex(explicit);
  }
  const builtin = BUILTIN_ACCENTS[roleId];
  if (builtin) return builtin;
  const h = hashString(roleId);
  const hue = DERIVED_HUES[h % DERIVED_HUES.length];
  // 饱和度/明度固定区间：太灰看不出"糖衣"，太艳在可可底上刺眼。
  return hslToHex(hue, 44, 48);
}

function expandHex(hex: string): string {
  const v = hex.replace("#", "");
  if (v.length === 3) {
    return `#${v[0]}${v[0]}${v[1]}${v[1]}${v[2]}${v[2]}`.toLowerCase();
  }
  return `#${v.toLowerCase()}`;
}

function hslToHex(h: number, s: number, l: number): string {
  const sat = s / 100;
  const lig = l / 100;
  const k = (n: number) => (n + h / 30) % 12;
  const a = sat * Math.min(lig, 1 - lig);
  const f = (n: number) =>
    lig - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)));
  const toHex = (v: number) =>
    Math.round(v * 255)
      .toString(16)
      .padStart(2, "0");
  return `#${toHex(f(0))}${toHex(f(8))}${toHex(f(4))}`;
}

/** 把强调色写进文档根的 CSS 变量（材质层据此派生雾底/玻璃/墨色）。 */
export function applyAccent(accent: string): void {
  const root = document.documentElement;
  root.style.setProperty("--accent", accent);
}
