// 明暗开关。判据与落地都在 lib/appearance.ts（readTheme / applyTheme / saveTheme）——
// 2026-11-03 归一化：这个文件从前自己写一份 `getItem("theme") + matchMedia` 的判据，
// 而 index.html 的首帧脚本按另一条路径走（它当时根本不管明暗）；同一职责两处实现，
// 迟早分叉。现在这里只剩转发：键名、判据、DOM 落地各只有一处。
//
// 另一处收口：从前是 `useEffect(apply+写盘, [theme])`，挂载首拍就把 `theme=dark`
// （从系统偏好读来的）写进盘上 —— 于是「跟随系统」这个状态存不住，用户下次改系统
// 偏好应用也不再跟。现在只有**按下切换**才落地与落盘；启动那一路归 initAppearance。
import { useCallback, useState } from "react";

import { applyTheme, readTheme, saveTheme, type Theme } from "../lib/appearance";

export function useTheme() {
  const [theme, setThemeState] = useState<Theme>(readTheme);

  const toggle = useCallback(() => {
    setThemeState((t) => {
      const next: Theme = t === "dark" ? "light" : "dark";
      applyTheme(next);
      saveTheme(next);
      return next;
    });
  }, []);

  return { theme, toggle };
}
