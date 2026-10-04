"""产品功能包：装在内核之上、由宿主接线的一族能力（2026-10-04 审查快照的"features 拆分"那一格）。

为什么要有这一层：`core/` 从前装着三个完整产品功能（主动开口 reachout、上行同步 sync、
桌宠形象包 pet_packs）—— 于是"内核"这个词名不副实，而一件功能的改动会波及内核的
所有消费者。快照要的终态是把它们搬到这里，方向保持**单向向下**：

    api → domains → (features) → core → {roles | rag} → base → storage → config

两条纪律：

  * **features 可以 import core，core 绝不可以 import features** —— 这一格在
    `[tool.importlinter]` 的 layers 里排在 core **之上**，方向已用探针双向实测过
    （features→core 放行、core→features 红）。内核要"知道"某个功能，只能像后台任务
    那样走注册表（`Runtime.register_background`：只认 start/stop 两个动作，不认功能名）。
  * **跨功能共用的约定不住在这里**：一件功能被内核与接入层共用的"事实面"（比如主动
    会话的线程 id 算法、把会话行切成 prompt 素材的纯函数）要下沉到 `core/`，否则内核
    就只能反向 import 功能。搬过一次的教训写在 `docs/修复交接（2026-10-04）.md`。

当前居民：`pet_packs`（桌宠形象包的发现与解析）。`reachout` / `sync` 的迁入要先解决
"内核与功能对功能的两条 import"，按序推进，不抢进度。
"""
