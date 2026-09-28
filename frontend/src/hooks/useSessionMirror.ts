/** 跟着服务端走的那两路探针，与「她正在说的那半句」镜像气泡（从 ChatPage 抽出）。 */
import { useCallback, useEffect, useRef, useState, type Dispatch, type SetStateAction } from "react";

import { api, type MessagePage, type MessageRow, type TurnProbe } from "../api";

/** 心跳。这一拍问的是 `/api/session/{tid}/turn` —— 它只查后端那份进程内登记
 *  （一次字典查找 + 一次主键 SELECT），所以敢 0.8 秒问一次。它决定的就是
 *  「她正在说的那半句」多久出现在这扇窗上：上限 0.8 秒。 */
const TICK_MS = 800;

/** 全量探针（`?limit=1`，带 `total`）的间隔。它比 `turn` 贵一个量级 —— 每次都要把整份
 *  检查点快照反序列化回来 —— 但**只有它看得见已经落地的东西**：主动开口、别处的编辑与
 *  删除、任何不经过一轮生成的写入，`turn` 一概问不出来。所以两者是分工不是重复。
 *  桌宠那条红点轮询是 3 秒，这里 5 秒：真切的场景是"你在桌宠上回了一句，切回控制台"，
 *  那一下靠 `focus` 立刻补读，节拍只是兜底。 */
const FULL_PROBE_MS = 5_000;

/** 每隔几拍付一次全量探针的代价。整数关系写死，免得两处数字各改各的漂掉。 */
const FULL_PROBE_EVERY_TICKS = Math.ceil(FULL_PROBE_MS / TICK_MS);

export function useSessionMirror({
  sessionId,
  sendingRef,
  selectSession,
  setMessages,
}: {
  sessionId: string | null;
  /** 这一扇窗自己在不在流里。是 ref 不是 state：它每拍都要读，而不能每拍都重新订阅。 */
  sendingRef: { current: boolean };
  /** 完整的载入路径（历史 / 角色 / 模型 / 上下文预算）。贵探针发现"别处写了字"走的是它，
   *  不是只换消息数组 —— 否则标题、生效模型会留在上一个会话上。 */
  selectSession: (threadId: string) => void | Promise<void>;
  setMessages: Dispatch<SetStateAction<MessageRow[]>>;
}) {
  /** 别处（桌宠）那一轮**正在生成、还没进检查点**的那半句（R26-38 的镜像）。
   *  `null` = 没人在生成；字符串 = 已经投送到哪儿了（空串 = 她在打字、还没出字）。
   *  只在这一扇窗自己没在流的时候才显 —— 那时候屏幕上已经有 `live` 那个气泡了，
   *  再画一格就是同一个人说两遍。 */
  const [mirror, setMirror] = useState<string | null>(null);
  const mirrorRef = useRef<string | null>(null);

  /** 服务端在**上一次我们主动读取时**报的条数。别拿 `messages.length` 当它：那里面混着
   *  乐观发出去的那句和正在流的气泡，一比就误判成"别处写了字"。 */
  const seenTotalRef = useRef<number | null>(null);

  /** 从服务端重读这条会话并记账（要的就是那个 `total`：探针靠它判"别处有没有写字"）。 */
  const reloadMessages = useCallback(
    async (threadId: string): Promise<void> => {
      const page = await api.get<MessagePage>(`/api/session/${threadId}/messages`);
      seenTotalRef.current = page.total;
      setMessages(page.messages);
      // 整句已经落进历史了，镜像那一格的任务就到此为止：不清的话屏幕上会同时有
      // "她正在说的气泡"和"她说完了的那条"，那是重影。
      mirrorRef.current = null;
      setMirror(null);
    },
    [setMessages],
  );

  /**
   * 别处（桌宠面板）往这条会话里写了字，控制台要跟上 —— 用户 09-26："反过来就看不到了"，
   * 当晚又报"在桌宠那发的收到回答，在对话界面同步得有些慢"。实测一轮 419 字的回答：
   * 他那句 0.21 秒就可读、她那句 14.41 秒才进检查点，界面按 5 秒网格收到 15.0 秒 ——
   * **落后的 12.03 秒里只有 0.59 秒是轮询欠的，其余全是"她正在说"对第二个读者不可见**。
   * 所以这里读两路，各治一截：
   *
   *  * `/api/session/{tid}/turn`（每拍一次，便宜）：治"看不见她在说"。回的是后端那份
   *    进程内在飞登记，不做检查点反序列化，所以敢 0.8 秒问一次 —— 发现、跟字、落地三个
   *    时刻的上限都是 0.8 秒。
   *  * `/messages?limit=1`（每 `FULL_PROBE_EVERY_TICKS` 拍一次，贵）：治"看不见已落地的"。
   *    每读一次要把整份检查点快照反序列化回来，不该当高频探针用 —— 但**她那句落地那一拍
   *    例外**：那时立刻补一次贵读，让真消息换掉镜像气泡，而不是再等 4.8 秒。
   *
   * 两道闸：正在流不抢（那轮的屏幕内容还没落库，抢了就是"我发一条她回两条"那个重影），
   * 条数没变一次 set 都不发（否则每几秒把滚动位置与勾选状态清一遍，比"看不到新的"更烦人）。
   *
   * **不看 `document.hidden`**：这是桌面 app，"控制台被别的窗盖住"是常态而不是后台标签页，
   * 而铃铛那侧的 3 秒轮询本来也不看可见性 —— 加一道只有这一半有的闸，只会造出
   * "一处会同步一处不会"这种查不出来的差别。`focus` 那一下直接跳到一次贵读：切回这一扇窗
   * 想立刻看到的正是已经落地的部分。
   *
   * 只有一个 `setTimeout` 自续，不是两个 `setInterval` 来回切：要的是任何时刻只有一拍在飞
   * （两拍并存时探针会以两种间隔之和的节拍打出去，看不出来也测不到）。
   */
  useEffect(() => {
    if (!sessionId) return;
    const tid = sessionId; // 收窄成 string：闭包里 TS 不认 state 的那道判空
    let cancelled = false;
    let timer = 0;
    let ticks = 0; // 距离上一次全量探针过了几拍
    const arm = () => {
      timer = window.setTimeout(() => void tick(), TICK_MS);
    };
    const setMirrorNow = (next: string | null) => {
      if (next === mirrorRef.current) return;
      mirrorRef.current = next;
      setMirror(next);
    };
    /** 贵的那一读：带 `total`，看得见**已经落地**的东西（别处的编辑、主动开口、跑完的那一轮）。 */
    async function heavyProbe(): Promise<void> {
      const page = await api.get<MessagePage>(`/api/session/${tid}/messages?limit=1`);
      if (cancelled) return;
      const seen = seenTotalRef.current;
      seenTotalRef.current = page.total;
      setMirrorNow(page.inflight ? page.inflight.text : null);
      ticks = 0;
      if (seen !== null && seen !== page.total) await selectSession(tid);
    }
    const tick = async () => {
      try {
        if (sendingRef.current) {
          // 这一扇窗自己在流：屏幕上已经有 `live` 那个气泡，镜像必须让位
          setMirrorNow(null);
        } else if (ticks >= FULL_PROBE_EVERY_TICKS) {
          await heavyProbe();
        } else {
          const cheap = await api.get<TurnProbe>(`/api/session/${tid}/turn`);
          if (!cancelled) {
            ticks += 1;
            if (cheap.inflight) {
              setMirrorNow(cheap.inflight.text);
            } else if (mirrorRef.current !== null) {
              // 她那句刚落地（登记清了而这边还画着气泡）：立刻补一次贵读把真消息换进来，
              // 不等下一拍 —— 用户等的那一下就是这个时刻。
              setMirrorNow(null);
              await heavyProbe();
            }
          }
        }
      } catch {
        /* 探针失败就等下一次节拍：为一次网络抖动改界面无意义 */
      }
      if (!cancelled) arm();
    };
    const onVisible = () => {
      clearTimeout(timer);
      ticks = FULL_PROBE_EVERY_TICKS; // 切回来这一次就读贵的
      void tick();
    };
    arm();
    window.addEventListener("focus", onVisible);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      cancelled = true;
      clearTimeout(timer);
      window.removeEventListener("focus", onVisible);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [sessionId]); // eslint-disable-line react-hooks/exhaustive-deps

  return { mirror, seenTotalRef, reloadMessages };
}
