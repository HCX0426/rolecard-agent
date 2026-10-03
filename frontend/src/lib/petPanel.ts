/**
 * 桌宠消息面板"摊多久自己收回去"的那一份判据。
 *
 * 为什么抽出来：计时器要跑在页面里（只有页面知道指针在不在面板上、你正在不在输入、
 * 她正在不在说话），而这几条判据是**用户能感觉到的行为**，不该靠组件里的一段内联表达式
 * 背下来。抽成纯函数后它能被逐臂测：占住时不倒数、没占住时按拍倒数、0 = 根本不计时。
 *
 * 秒数本身来自托盘第六项（`shell/main/state.ts` 的 `panelAutoHideMs`，0 = 不自动收起），
 * 页面只读不写 —— 与「显示消息内容」「朗读消息」同一条分工。
 */

/** 倒数的一拍多长。500ms 是"到点收起"的观感精度与心跳次数的折中：
 *  15 秒那一档最多晚半秒收，而一分钟也只跳 120 下。 */
export const PANEL_TICK_MS = 500;

/** 面板此刻是不是"被人用着"。三种都算：指针在它上面、焦点在它里面（正在打字）、
 *  她正在说话（流式/思考中）。收走正在读的东西比多摊一会儿贵得多。 */
export type PanelHold = {
  pointerInside: boolean;
  focusInside: boolean;
  speaking: boolean;
};

export function panelHeld(hold: PanelHold): boolean {
  return hold.pointerInside || hold.focusInside || hold.speaking;
}

/** `autoHideMs = 0` ⇒ 这条链整个不启动（托盘那档叫「不自动收起」，就得真的不收）。 */
export function panelTimerArmed(autoHideMs: number): boolean {
  return Number.isFinite(autoHideMs) && autoHideMs > 0;
}

/**
 * 走一拍，返回新的剩余毫秒；`<= 0` 就是"该收了"。
 *
 * 占住时是**原地暂停**，不是清零也不是重新计时：把手放在面板上十分钟再挪开，
 * 面板还是会在剩下的那点时间里收走 —— 但也不会因为你只是路过就立刻收。
 */
export function panelCountdown(
  remaining: number,
  held: boolean,
  tickMs: number = PANEL_TICK_MS,
): number {
  if (held) return remaining;
  return remaining - tickMs;
}
