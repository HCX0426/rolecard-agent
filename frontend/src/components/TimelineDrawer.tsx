// 事件簿抽屉（docs/主动消息与记忆设计稿.md §6.2）：角色页每行一个「事件簿」→ 右侧抽屉，
// 把这个角色的主动开口、记下 / 更正过的事实、会话锚点并成一条**只读**时间轴。
//
// 三条不显然的纪律：
//  * **只读**。编辑 / 钉住 / 删除的唯一写点仍在记忆卡（§3 那条"一个设置只有一个写点"）。
//    这里连一个就地修改的入口都不给 —— 同一份事实出现两个入口，先对不上账的总是后来那个。
//  * **动词在这里造**。后端只给 `kind` + `verb`（`core/timeline.py` 明写"这里不写文案"），
//    "记下：/ 更正：/ 开始聊："由界面拼出来。措辞是要改的，注进后端的文案不是。
//  * **措辞只能到"它哪天记下 / 更正了什么"为止**：记忆条目的 `created_at` 是**被记下的时间**，
//    不是事情发生的时间。写成"你搬来杭州"就是把它没有证据的事说成了事实（§6.4）。
//
// 抽屉而不是新页签：它是"关于这个角色"的上下文，离开角色列表就没有归属了。
// 按天分组只是显示层的分组，轴上每一项都还是库里那一行 —— 不在前端造第二份事实。

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, type TimelineEvent, type TimelinePage } from "../api";
import { Button, Modal, Notice } from "./ui";

/** 顶部那个下拉：一个控件而不是四个开关 —— "对话"锚点单独看没有意义（§6.2）。 */
const FILTERS: { key: string; label: string; kinds: string | null }[] = [
  { key: "all", label: "全部", kinds: null },
  { key: "reachout", label: "只看它主动说的", kinds: "reachout" },
  { key: "memory", label: "只看它记下的", kinds: "memory,memory_correct" },
];

/** 徽章是说明不是控件：不做点击筛选（筛选只有上面那个下拉）。 */
const BADGE: Record<TimelineEvent["kind"], { label: string; cls: string }> = {
  reachout: {
    label: "主动",
    cls: "bg-blue-50 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300",
  },
  memory: {
    label: "记忆",
    cls: "bg-green-50 text-green-700 dark:bg-green-900/30 dark:text-green-300",
  },
  memory_correct: {
    label: "更正",
    cls: "bg-amber-50 text-amber-700 dark:bg-amber-900/20 dark:text-amber-300",
  },
  thread: {
    label: "对话",
    cls: "bg-slate-100 text-slate-500 dark:bg-slate-700/50 dark:text-slate-400",
  },
};

function pad(n: number): string {
  return n < 10 ? `0${n}` : String(n);
}

function todayKey(offsetDays = 0): string {
  const d = new Date(Date.now() - offsetDays * 86_400_000);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/** "YYYY-MM-DD HH:MM:SS" → "今天 / 昨天 / 4月9日"。
 *  按**本机日子**比，不做时区换算：库里那串就是本机写下去的本地时间，换算一次反而
 *  会把"今天"挪走一天（那是这类时间轴最容易被当成 bug 的地方）。 */
function dayLabel(date: string): string {
  if (date === todayKey()) return "今天";
  if (date === todayKey(1)) return "昨天";
  const [, m, d] = date.split("-");
  return `${Number(m)}月${Number(d)}日`;
}

/** 会话锚点的中文动词。`recent` 是"这条会话最近又聊到了"，不是新的一次对话。 */
function phraseOf(ev: TimelineEvent): string {
  if (ev.kind === "memory") return `记下：${ev.text}`;
  if (ev.kind === "memory_correct") return ev.text ? `更正为：${ev.text}` : "作废了一条事实";
  if (ev.kind === "thread") return ev.verb === "recent" ? `接着聊：${ev.text}` : `开始聊：${ev.text}`;
  return ev.text;
}

interface DayGroup {
  date: string;
  label: string;
  items: TimelineEvent[];
}

export default function TimelineDrawer({
  open,
  role,
  onClose,
  onOpenThread,
}: {
  open: boolean;
  role: { role_id: string; role_name: string } | null;
  onClose: () => void;
  /** 复用收件箱那条跳转（App 里它会顺手切到对话页签）。 */
  onOpenThread?: (threadId: string) => void;
}) {
  const [filterKey, setFilterKey] = useState("all");
  const [items, setItems] = useState<TimelineEvent[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [truncated, setTruncated] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  // 换角色 / 换筛选 / 关抽屉时，上一页还没回来的响应不许再写进状态。
  const reqRef = useRef(0);

  const kinds = FILTERS.find((f) => f.key === filterKey)?.kinds ?? null;
  const baseUrl = role
    ? `/api/roles/${role.role_id}/timeline?limit=30${kinds ? `&kinds=${kinds}` : ""}`
    : "";

  const fetchPage = useCallback(
    async (url: string, append: boolean) => {
      const req = ++reqRef.current;
      setLoading(true);
      setError("");
      try {
        const page = await api.get<TimelinePage>(url);
        if (req !== reqRef.current) return;
        setItems((prev) => (append ? [...prev, ...page.items] : page.items));
        setCursor(page.next_cursor);
        setTruncated(page.truncated);
      } catch (e) {
        if (req !== reqRef.current) return;
        setError((e as Error).message);
        if (!append) {
          setItems([]);
          setCursor(null);
        }
      } finally {
        if (req === reqRef.current) setLoading(false);
      }
    },
    [],
  );

  useEffect(() => {
    if (!open || !role) {
      reqRef.current += 1; // 让在途的响应作废，别在下次打开时把上一个角色的行拼进来
      return;
    }
    void fetchPage(baseUrl, false);
  }, [open, role, baseUrl, fetchPage]);

  const groups = useMemo<DayGroup[]>(() => {
    const out: DayGroup[] = [];
    for (const ev of items) {
      const date = ev.at.slice(0, 10);
      const last = out[out.length - 1];
      if (last && last.date === date) last.items.push(ev);
      else out.push({ date, label: dayLabel(date), items: [ev] });
    }
    return out;
  }, [items]);

  return (
    <Modal
      open={open && Boolean(role)}
      onClose={onClose}
      variant="drawer"
      title={
        <span className="flex items-center justify-between gap-3">
          <span>
            {role?.role_name || role?.role_id} · 事件簿
            <span className="ml-2 text-[11px] font-normal text-slate-400 dark:text-slate-500">
              只读；要改事实去记忆卡
            </span>
          </span>
          <select
            value={filterKey}
            onChange={(e) => setFilterKey(e.target.value)}
            className="rounded-lg border border-slate-200 bg-white px-2 py-1 text-xs font-normal text-slate-600 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-300"
            title="只看某一类"
          >
            {FILTERS.map((f) => (
              <option key={f.key} value={f.key}>
                只看 ▾ {f.label}
              </option>
            ))}
          </select>
        </span>
      }
      footer={
        cursor ? (
          <Button variant="outline" size="sm" disabled={loading} onClick={() => void fetchPage(`${baseUrl}&before=${encodeURIComponent(cursor)}`, true)}>
            {loading ? "读取中…" : "加载更多 ↓"}
          </Button>
        ) : null
      }
    >
      {error && <Notice className="mb-3">事件簿没读到：{error}</Notice>}
      {!error && loading && items.length === 0 && (
        <p className="text-xs text-slate-400 dark:text-slate-500">读取中…</p>
      )}
      {!error && !loading && items.length === 0 && (
        // 空态要说清"怎么才会有东西"，不然它读起来像坏了。
        <p className="text-xs text-slate-400 dark:text-slate-500">
          还没有记录 —— 聊几句，或在角色卡上开启「主动找你」。
        </p>
      )}
      <div className="space-y-4">
        {groups.map((group) => (
          <section key={group.date}>
            <h4 className="mb-1.5 text-[11px] font-medium text-slate-400 dark:text-slate-500">
              {group.label}
            </h4>
            <ul className="ml-1 space-y-2 border-l border-slate-200 pl-3.5 dark:border-slate-700">
              {group.items.map((ev, i) => {
                const badge = BADGE[ev.kind];
                const jumpable = Boolean(ev.thread_id) && Boolean(onOpenThread);
                return (
                  <li key={`${ev.kind}-${ev.ref_id}-${i}`} className="relative text-xs leading-relaxed">
                    <span
                      className={`absolute -left-[19px] top-1 h-2 w-2 rounded-full ${badge.cls} ring-2 ring-white dark:ring-slate-800`}
                    />
                    <span className="mr-1.5 text-slate-400 dark:text-slate-500">
                      {ev.at.slice(11, 16)}
                    </span>
                    <span className={`mr-1.5 rounded px-1.5 py-0.5 text-[10px] ${badge.cls}`}>
                      {badge.label}
                    </span>
                    {jumpable ? (
                      <button
                        onClick={() => onOpenThread?.(ev.thread_id as string)}
                        className="text-left text-blue-600 hover:underline dark:text-blue-400"
                        title="打开这条会话（能翻完整历史、能回话）"
                      >
                        {phraseOf(ev)} →
                      </button>
                    ) : (
                      <span className="break-words">{phraseOf(ev)}</span>
                    )}
                    {ev.kind === "memory_correct" && ev.from_text && (
                      // 「失效不删」第一次被用户看见的地方：旧事实划掉摆在那儿，而不是不见了。
                      <p className="mt-0.5 break-words text-[11px] text-slate-400 line-through dark:text-slate-500">
                        {ev.from_text}
                      </p>
                    )}
                  </li>
                );
              })}
            </ul>
          </section>
        ))}
      </div>
      {truncated && (
        <p className="mt-3 text-[11px] text-slate-400 dark:text-slate-500">
          某一类事太多，这里只扫到最近的一段（不是全部）。
        </p>
      )}
    </Modal>
  );
}
