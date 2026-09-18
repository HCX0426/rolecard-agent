"""跨会话记忆：kernel_meta 里的一条文本（`memory:facts`）+ AI 可直接调用的写入工具。

## 为什么是"一条文本"而不是"一张表"

本项目的记忆是**用户级**事实（"我住在上海 / 我管三个项目的账"），单用户 demo 下
不分会话、不分角色 —— 一张键值表存一大段文本已经够用，且「设置→通用」面板可以整段
查看 / 编辑 / 清空，对人类最可读。粒度更细（逐条打分、过期、分角色）是后续演进，
不是第一版的形状。

## 谁写记忆

  * `memory_save` 内核工具：AI 在对话中检测到**用户明确说出的、可复用的**事实时调用
    （"记住我每周五要交周报"）。这比"每轮对话后偷偷跑一次抽取"少一次模型调用、且
    动作可见 —— 模型会向用户确认"我已记住"。
  * 面板：查看 / 编辑 / 清空，是人工的最终仲裁面。

## 边界

  * 总开关 `MEMORY_ENABLED`：关掉后注入与工具都停，面板仍可编辑（只存不用）。
  * 文本有字符上限（`MAX_MEMORY_CHARS`），写工具追加新行、超限时丢最旧的 ——
    记忆是 system prompt 的一部分，必须有上界。
  * `memory_save` **不声明幂等**：执行器不会重试它（写入类工具，重试 = 重复副作用）。
  * 记忆区在 prompt 里自带"以用户最新说法为准"的降权声明：一条被提示注入污染的记忆
    不会压过用户当下的明确说法（软层规则，硬门仍是 core/guard.py）。
"""

from __future__ import annotations

from functools import partial

from langchain_core.tools import BaseTool, tool

from rolecard_agent.config import Settings
from rolecard_agent.storage.db import SqlConnection

# kernel_meta 的键与上限。键带 `memory:` 前缀，与 `runtime:` 同一命名约定。
MEMORY_KEY = "memory:facts"
MAX_MEMORY_CHARS = 4000


def load_memory_text(conn: SqlConnection) -> str:
    """读当前记忆文本；没有 / 从未写过 = 空串（调用方按"无记忆"处理）。"""
    row = conn.execute("SELECT value FROM kernel_meta WHERE key = ?", (MEMORY_KEY,)).fetchone()
    return str(row["value"] or "") if row else ""


def save_memory_text(conn: SqlConnection, text: str) -> None:
    """整体覆写记忆文本（面板保存与 memory_save 共用的落点）。超限截断。"""
    capped = (text or "").strip()[:MAX_MEMORY_CHARS]
    conn.execute(
        "INSERT INTO kernel_meta (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP",
        (MEMORY_KEY, capped),
    )
    conn.commit()


def clear_memory_text(conn: SqlConnection) -> None:
    """清空记忆（面板「清空」按钮）。删除整行；没写过则无事发生。"""
    conn.execute("DELETE FROM kernel_meta WHERE key = ?", (MEMORY_KEY,))
    conn.commit()


def make_memory_tool(*, settings: Settings, conn: SqlConnection) -> BaseTool:
    """构建 memory_save 内核工具。闭包持有**构建期**的连接与配置（与 fs 工具同一约定：
    运行环境热切换 = 设置保存后重建 registry，闭包随之重建）。

    `conn` 是 ThreadLocalConnection：工具在独立线程执行，内部按线程分发真实连接
    （见 storage/db.py），闭包持有一个全局共享引用是安全的设计。
    """
    save_one = partial(save_memory_text, conn)
    load_one = partial(load_memory_text, conn)

    @tool("memory_save")
    def memory_save(fact: str) -> str:
        """把用户**明确说出**的、值得长期记住的可靠事实存入跨会话记忆（会永久记忆，直到用户在设置里删除）。

        只在对话里出现明确的长期事实时才用：比如用户的称呼、身份、居住地、固定偏好、
        常做事物的关键背景。一次性信息、当下即可完成的任务不要记。保存后向用户确认。
        """
        if not settings.memory_enabled:
            return "跨会话记忆功能未启用，无法写入。"
        line = (fact or "").strip()
        if not line:
            return "没有可记住的内容：传入的 fact 为空。"
        # 追加新行 + 丢弃与本次完全相同的旧行（去重）；超上限时丢最旧的行。
        current = load_one()
        lines = [ln for ln in current.splitlines() if ln.strip() != line]
        lines.append(line)
        while len("\n".join(lines)) > MAX_MEMORY_CHARS:
            lines.pop(0)
        save_one("\n".join(lines))
        return f"已记住：{line}（当前共 {len(lines)} 条事实）。"

    return memory_save


__all__ = [
    "MEMORY_KEY",
    "MAX_MEMORY_CHARS",
    "clear_memory_text",
    "load_memory_text",
    "make_memory_tool",
    "save_memory_text",
]
