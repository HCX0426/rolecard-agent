"""工具层的「用户安全错误」基类。

约定：工具执行中**预料内**的失败（网络不可达、路径越界、目标网页拒绝……）抛本基类的
子类，message 面向用户说人话（无栈、无内部路径）。执行器（core/agent/nodes.py）对这类错误
**透传原文**进 ToolMessage —— 模型能据此向用户解释，对话页的工具卡也能显示真实原因；
预料外的异常仍回通用句（TOOL_FAILED），避免内部细节外泄。
（"每一层都要失败时说人话"，但说的必须是设计过的话 —— 这就是两者的分界。）
"""

from __future__ import annotations


class ToolExecutionError(Exception):
    """工具预料内失败的可读错误。子类：WebToolError / FsToolError（按需增加）。"""
