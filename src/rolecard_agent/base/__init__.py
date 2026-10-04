"""base 层：内核与所有上层共享的**零业务知识**件。

收进这一层的判据只有两条，缺一就不许进：

  1. **它只依赖 stdlib / 第三方 / `config` / `storage`** —— 不许 import `core`、`rag`、
     `roles`、`domains`、`api`（方向尺由 import-linter 契约看着，判据在 `pyproject.toml`
     的 `[tool.importlinter]`，另有一条便宜的 AST 尺子在 `tests/unit/test_import_floor.py`）；
  2. **它说的不是某一个域或某一条流程的事** —— 文本取值、来源标记、路径发现、观测出口、
     身份与播种、审计写入咽喉、执行期注入的 ContextVar，这些是"谁都要问一遍"的原语。

为什么要有这一层：这些件从前住在 `core`，于是 `rag`/`roles`/`domains` 想用一个 `text_of`
就得反向 import 内核，`core ↔ rag`、`core ↔ roles` 两条双向边都是这样长出来的
（靠函数里的延迟 import 压着不炸环，但方向已经错了）。把它们放到底层，
上层依赖下层就是唯一方向，双向环从结构上构不出来。

新增一格的时机：当你正准备写"从 core 里借一个原语"的那个 import 时。
"""
