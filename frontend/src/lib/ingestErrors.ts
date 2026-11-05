/** 导入/摄取失败原因的展示归一（纯文本层，不起 React、不碰 DOM）。
 *
 *  住在这里而不是组件文件里：组件模块导出非组件会撞上 react-refresh 那条警告
 *  （本仓 eslint --max-warnings 32 是硬闸，多一条就红），更重要的是纯函数该有
 *  能被单测直接钉的位置 —— 与 lib/uploadOutcome.ts 同族。
 */

/** 把失败任务的原因收成一句话。
 *
 *  存在的理由：导入失败的原因早从进度端点读回来了，却被只用来"数个数"，界面回一句
 *  「详情见进度条目」而界面上没有条目 —— 用户看不到原因只能反复重传（2026-11-06 实测）。
 *  去重必要：同一原因（如嵌入维度不符）会让整批文件全栽在同一条上，逐条罗列等于把
 *  同一句话重复 N 遍。截断防长堆栈撑爆状态条。无原因时如实说"没给原因"，不拿空串冒充。
 */
export function summarizeFailures(errors: (string | null | undefined)[], max = 2): string {
  const known = errors.map((e) => (e ?? "").trim()).filter(Boolean);
  if (known.length === 0) return "（后端未给出原因）";
  const uniq = [...new Set(known)];
  const shown = uniq
    .slice(0, max)
    .map((e) => (e.length > 120 ? `${e.slice(0, 120)}…` : e))
    .join("；");
  return uniq.length > max ? `${shown}（另有 ${uniq.length - max} 类原因）` : shown;
}
