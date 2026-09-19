// 桌宠（D②-2 极简驻留形态）：一张色片 + 一句气泡，200×240，透明无边框置顶。
//
// 这一页**只在桌面壳里出现**：浏览器直接开 #/pet 也能看（就是窗口不透明而已），但真正
// 的透明/置顶/无边框由壳的第二扇窗给（shell/src-tauri/src/pet.rs）。界面仍在这份前端里，
// 不在壳里另写一份 —— 两壳看到同一个 dist 是里程碑 D 的立身之本。
//
// 只做三件事：角色主动说话时抬眼能看到；一眼看出攒了几条；点一下能把未读清掉。
// 「点开跳回那条主动会话」需要原生桥（壳替我们打开主窗并定位会话），排在 D②-3。

import { useCallback, useEffect, useMemo, useState } from "react";

import { api, type ReachoutRow } from "../api";

const POLL_MS = 10_000; // 与铃铛红点同一节奏：后端没有推送，如实降级为轮询
const BUBBLE_MS = 30_000; // 气泡自己淡出：驻留件不该把一句话长期戳在桌面上
const MAX_BUBBLE_CHARS = 64;

/** role_id → 稳定色相。同一个角色的色片跨设备/跨主题都一样，认脸靠它。 */
function hueOf(roleId: string): number {
  let hash = 0;
  for (const ch of roleId) hash = (hash * 31 + ch.codePointAt(0)!) % 360;
  return hash;
}

function shorten(text: string): string {
  return text.length > MAX_BUBBLE_CHARS ? `${text.slice(0, MAX_BUBBLE_CHARS)}…` : text;
}

export default function PetPage() {
  const [items, setItems] = useState<ReachoutRow[]>([]);
  const [offline, setOffline] = useState(false);
  const [bubbleShownAt, setBubbleShownAt] = useState(0);
  const [faded, setFaded] = useState(false);

  const load = useCallback(async () => {
    try {
      const page = await api.getReachouts();
      setItems(page.items);
      setOffline(false);
    } catch {
      setOffline(true); // 后端没起来 / 在换：桌面件必须说清"我现在是哑的"，不能装作没有消息
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = setInterval(() => void load(), POLL_MS);
    return () => clearInterval(timer);
  }, [load]);

  const latest = items[0] ?? null;
  const unread = useMemo(() => items.filter((row) => row.state === "unread"), [items]);

  // 新的一条（id 变大）出现时重新计时；同一条不再弹第二次。
  useEffect(() => {
    if (!latest) return;
    setBubbleShownAt(Date.now());
    setFaded(false);
  }, [latest?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!bubbleShownAt || faded) return;
    const timer = setTimeout(() => setFaded(true), BUBBLE_MS);
    return () => clearTimeout(timer);
  }, [bubbleShownAt, faded]);

  const unreadOfLatest = latest ? unread.filter((row) => row.role_id === latest.role_id).length : 0;
  const hue = latest ? hueOf(latest.role_id) : 210;
  const name = latest?.role_name || latest?.role_id || "助手";

  async function acknowledge(row: ReachoutRow) {
    // 先标已读再收气泡：标失败就留着，让用户知道"这条还没真被读过"。
    try {
      await api.markRoleReachoutsRead(row.role_id);
    } catch {
      setOffline(true);
      return;
    }
    setFaded(true);
    await load();
  }

  return (
    // data-tauri-drag-region 现在**不生效**（实测：拖拽窗口不动）。原因不是写错属性，
    // 而是 Tauri 的拖拽区靠注入脚本实现，而后端源的页面默认拿不到注入 —— 要等原生桥
    // 那一步把 backend origin 加进 capability 的 remote.urls（D②-3）。留着这两个标记，
    // 那时不用改界面代码；在那之前桌宠位置由壳按屏幕右下角摆放。
    <div
      className="flex h-full select-none flex-col items-center justify-end gap-2 pb-1"
      data-tauri-drag-region
    >
      {latest && !faded && (
        <button
          onClick={() => void acknowledge(latest)}
          title={unreadOfLatest > 1 ? `还有 ${unreadOfLatest - 1} 条未读，点击全部标记已读` : "点击标记已读"}
          className="w-full rounded-2xl border border-slate-200/70 bg-white/90 px-3 py-2 text-left text-[11px] leading-relaxed text-slate-700 shadow-sm backdrop-blur-sm transition-opacity duration-500 dark:border-slate-600/70 dark:bg-slate-800/90 dark:text-slate-100"
        >
          {shorten(latest.text)}
          {unreadOfLatest > 1 && (
            <span className="ml-1 rounded-full bg-blue-600 px-1.5 text-[10px] text-white">
              +{unreadOfLatest - 1}
            </span>
          )}
        </button>
      )}

      <div
        className="grid h-[88px] w-[88px] shrink-0 place-items-center rounded-full text-2xl font-medium text-white shadow-md"
        style={{ background: `hsl(${hue} 62% 48%)` }}
        title={`${name}${offline ? " · 连不上本地服务" : ""}`}
        data-tauri-drag-region
      >
        {name.slice(0, 1)}
      </div>

      {offline && (
        <span className="text-[10px] text-amber-600 dark:text-amber-400">连不上本地服务</span>
      )}
    </div>
  );
}
