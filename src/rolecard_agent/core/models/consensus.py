"""多模型结果比对（consensus）—— 把同一问题并行发给 N 个对话后端，再聚合比对。

形态选择（审查报告后续功能建议 ③，M6 异步化已就绪）：做成**内核工具**
`compare_model_answers` 而不是对话模式开关 —— 权限模型（白名单/审计/超时）全部复用
现有工具链路，前端零改动；任何角色在角色卡勾选即可用。

三条边界，写清楚再写代码：

  * **并行**：N 个后端同时发问（ThreadPoolExecutor），墙钟时间 ≈ 最慢的那个，
    而不是 N 倍。每个后端沿用 build_model 的超时与回退链。
  * **成本**：一次比对 = N 次 LLM 调用 + 1 次聚合调用。因此 `idempotent=False`
    —— 失败重试等于成倍烧 token，执行器对它只执行一次。
  * **隐私**：同一问题会发给多个供应商（含云端 siliconflow）。与 OCR 云端兜底同一
    告知义务，.env.example 已注明；云端不可用时自动跳过该后端并如实标注。

聚合器用**默认后端**：它是对话主路径，可用性已经被日常使用验证。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Protocol

from rolecard_agent.base.markers import AI_TEXT_MARKER
from rolecard_agent.base.text import text_of

MAX_CONSENSUS_BACKENDS = 3


class _Chat(Protocol):
    """比对需要的最小模型面：invoke(question) -> 有 .content 的消息。"""

    def invoke(self, prompt: str) -> Any: ...


def build_consensus_tool(*, settings: Any, build: Any = None) -> Any:
    """构建比对工具。`build(backend_name) -> _Chat` 可注入（测试用假模型）。"""
    from langchain_core.tools import tool

    from rolecard_agent.core.agent.graph import build_model

    model_builder = build or (lambda name: build_model(settings, name))

    def _targets() -> list[str]:
        """参与比对的后端：默认后端 + 回退链，去重后最多 MAX_CONSENSUS_BACKENDS 个。

        `respect_policy=False`（ENGI-36 C）：比对工具是**模型显式调用**、把同一问题主动发给
        多家做对照 —— 它本就要出网，不存在「从本地静默滑到云端」那件事，所以不受
        `model_fallback_policy=local_only` 约束（那条策略管的是回退链的静默降级方向）。
        它的闸门是 `CONSENSUS_ENABLED` 总闸与角色白名单，不在这里。
        """
        names = [
            settings.model_default,
            *settings.resolve_fallbacks(settings.model_default, respect_policy=False),
        ]
        seen: list[str] = []
        for n in names:
            if n and n not in seen:
                seen.append(n)
        return seen[:MAX_CONSENSUS_BACKENDS]

    # 一次比对里每个后端只构建一次：模型持有 httpx 连接池，每次 _ask 都新建等于
    # 每次比对泄漏 N 个客户端（审查报告 P2）。工具实例本身是长生命周期的，
    # 所以缓存挂在闭包上即可 —— 请求之间共享同一个客户端，这正是初衷。
    built: dict[str, Any] = {}

    def _model(name: str) -> Any:
        if name not in built:
            built[name] = model_builder(name)
        return built[name]

    def _ask(name: str, question: str) -> tuple[str, str]:
        """单后端问答。失败不抛：比对要的是"每个后端各自的状态"，缺一个就缺一个。"""
        try:
            model = _model(name)
            answer = text_of(model.invoke(question)).strip()
            return name, (answer or "（这个模型返回了空回答）")
        except Exception as exc:  # noqa: BLE001 - 只透出类型名，内部细节不进对话
            return name, f"（这个模型调用失败：{type(exc).__name__}，结论仅基于其余回答）"

    @tool("compare_model_answers")
    def compare_model_answers(question: str) -> str:
        """把同一问题并行发给多个已配置的对话后端，并聚合比对它们的回答：一致结论、
        实质分歧点、有无后端缺席。适合事实核查与重要判断；一次调用约等于 N 次普通对话。"""
        # 全局总闸（与 web 工具、run_command 同一模式：工具自己把关，返回可读的关闭说明，
        # 而不是让模型对着一句"工具不存在"瞎猜）。
        if not settings.consensus_enabled:
            return (
                "多模型比对已被总闸关闭（CONSENSUS_ENABLED=0）。"
                "要启用请在「设置 → 运行环境 → 超时与预算」里打开总闸。"
            )
        targets = _targets()
        if len(targets) < 2:
            return (
                f"当前只有 {len(targets)} 个可对话的模型，无从比对。"
                "去「模型」页签再加一个（并在「服务」页排序）就能比对。"
            )
        with ThreadPoolExecutor(max_workers=len(targets)) as pool:
            answers = list(pool.map(lambda n: _ask(n, question), targets))

        joined = "\n\n".join(f"【{name}】\n{answer}" for name, answer in answers)
        aggregator = _model(settings.model_default)
        summary = text_of(
            aggregator.invoke(
                "你是事实核查员。同一个问题发给了多个模型，下面是它们各自的回答。\n"
                "请输出：1) 它们一致同意的结论；2) 任何实质分歧点；3) 若某个模型调用失败，"
                "说明结论仅基于其余回答。用简洁中文，不要复述各回答全文。\n\n"
                f"问题：{question}\n\n{joined}"
            )
        ).strip()
        # 缺席后端显式标注（带原因类型）：不依赖聚合器转述（聚合器自己也可能漏说）。
        failed = [(n, a) for n, a in answers if a.startswith("（这个模型调用失败")]
        # 标记放在**第一行**：这段文本整体是模型产物，而比对结果常被拿去当"事实核查依据"。
        # 架构总览 §5-5 要的是"标记跟着数据走"，不是指望模型转述时记得补一句（P1 补漏）。
        lines = [AI_TEXT_MARKER, f"参与比对的模型：{'、'.join(n for n, _ in answers)}"]
        if failed:
            lines.append("缺席的模型：" + "；".join(f"{n} {a}" for n, a in failed))
        lines.append("")
        lines.append(summary or "（聚合比对没有返回内容）")
        return "\n".join(lines)

    return compare_model_answers
