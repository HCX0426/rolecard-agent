import { useCallback, useEffect, useState } from "react";

const KEY = "theme";

function initial(): "light" | "dark" {
  const saved = localStorage.getItem(KEY);
  if (saved === "light" || saved === "dark") return saved;
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

/** 深色模式开关：在 html 上挂/摘 `.dark`（配合 index.css 的 `@custom-variant dark`）。 */
export function useTheme() {
  const [theme, setTheme] = useState<"light" | "dark">(initial);

  useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark");
    localStorage.setItem(KEY, theme);
  }, [theme]);

  const toggle = useCallback(() => {
    setTheme((t) => (t === "dark" ? "light" : "dark"));
  }, []);

  return { theme, toggle };
}
