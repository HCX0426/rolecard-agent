/**
 * 异步动作的通用包装（2026-10-04 审查快照「Toast 与局部状态双出口」那条的解法）。
 *
 * 同一族样板在组件里长了十几遍：busy 旗 + try/catch + `describeError(e)` + 出口。
 * catch 的写法一多，"哪个出口该看到哪个错误"就开始漂 —— 有的人 toast、有的人静默。
 * 这里收成一件事：`run(动作)` 帮你管 busy 与 catch，失败时把 `describeError(e)`
 * 交给构造时给定的出口（Toast 回调或局部状态 setter；不给出参 = 静默吞错留给调用方自取）。
 *
 * 注意：成功路径的反馈（"已保存"这类）仍是调用方的事 —— 这个 hook 只统一"失败长什么样"。
 */
import { useCallback, useState } from "react";

import { describeError } from "../lib/errors";
import type { Tone } from "../components/Toast";

export function useAsyncAction(outlet?: (text: string, tone?: Tone) => void) {
  const [busy, setBusy] = useState(false);

  const run = useCallback(
    async (action: () => Promise<unknown>): Promise<boolean> => {
      setBusy(true);
      try {
        await action();
        return true;
      } catch (e) {
        outlet?.(describeError(e), "warn");
        return false;
      } finally {
        setBusy(false);
      }
    },
    [outlet],
  );

  return { busy, run };
}
