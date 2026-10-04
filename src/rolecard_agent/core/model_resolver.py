"""角色级模型解析（`ModelResolver`）—— Runtime 八个职责里"模型"那一格。

为什么单拎出来（2026-10-04 审查快照里"Runtime 单对象多职责"那一格）：`resolve_role_model`
与 `effective_for` 带着两份缓存和一个**代数**（`R28-05`：热重建换装挡不住"清完之后才落笔
的旧值"），这三件是一套自洽的东西，过去和图装配、后台调度、主动开口投递挤在同一个对象里。
搬出来之后 `Runtime` 只留一个 `models` 引用，`resolve_role_model` 的调用方**一行不改**
（Runtime 上仍有两个转调方法 —— 它们是稳定的对外形状，代数测试也因此不必动）。

两条纪律随代码一起搬，改这两处之前请先读：

  * **凭据按"这一轮的主人"取**（M2d 尾巴）：`user_id` 参数是给跨线程调用方的（
    `bound_user` 是 ContextVar，不跨线程传播），缺省回落 `active_user_id(实例主人)`；
  * **代数先加再清**（`R28-05`）：`invalidate()` 由 `Runtime.rebuild` 在锁外调用，
    在飞写者手上拿着旧代数，回填时被自己否掉；刻意不再加一把锁 —— "构建在锁外"是
    这里要保住的性能。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from rolecard_agent.base.identity import active_user_id
from rolecard_agent.base.observability import TraceEvent, Tracer
from rolecard_agent.config import Settings
from rolecard_agent.core import runtime_settings
from rolecard_agent.core.model_settings import ModelSettingsService
from rolecard_agent.core.nodes import ChatLike
from rolecard_agent.storage.db import ThreadLocalConnection


@dataclass(slots=True)
class ModelResolver:
    """按 (本轮主人, 后端名, 温度) 解析模型实例，并按身份缓存有效配置。

    `state` 是 Runtime 那份**共享**的可变槽位：当前生效的配置与编译期默认模型都住在里面
    （`effective` / `default_model`），所以热重建换装之后这里读到的就是新的一份 —— 缓存与
    配置只有一个家，不存在"resolver 还记着旧的那份"这种中间态。
    """

    model_settings: ModelSettingsService
    model_factory: Callable[..., ChatLike]
    env_settings: Settings
    conn: ThreadLocalConnection
    state: dict[str, Any]
    tracer: Tracer
    identity: Callable[[], str]
    #: 测试注入的模型实例（None = 按配置构建）。注入了它就不再有"角色级后端"这回事。
    injected_model: ChatLike | None = None
    #: 角色级模型实例缓存。键是 `(本轮主人, 后端名, 温度)` —— 主人这一维是 M2d 尾巴的
    #: 收口：同一台实例上两个身份各配同名后端、各带自己的 key 时，**实例**必须是两个。
    role_models: dict[tuple[str, str | None, float | None], ChatLike] = field(
        default_factory=dict, repr=False
    )
    #: 按身份解析出来的有效配置（`effective_for`）。实例主人那一份不在这里 —— 它在
    #: `state["effective"]`（编译期就位）。改配置走 `rebuild`，两处缓存一起清。
    effective_by_user: dict[str, Settings] = field(default_factory=dict, repr=False)
    #: 上面那两份缓存的**代数**（`R28-05`）。`clear()` 只挡得住"已经进缓存的旧值"，
    #: 挡不住"清完之后才回填的旧值"：并发那一轮在锁外拿着**旧配置快照**构建模型，
    #: 构建要花几百毫秒到几秒，落笔时 `clear()` 已经过去了 —— 于是换装完成之后的轮次
    #: 继续花旧 key。写者进场前记下代数、落笔时比对：对不上就丢弃（下一次调用自然重建）。
    #: 单靠 GIL 保证的是 int 读写不会撕裂，这就够了 —— 不用再加一把锁，那会把每次
    #: 模型解析都串到重建锁上，而"构建在锁外"是这里刻意保住的性能。
    generation: int = field(default=0, repr=False)

    def invalidate(self) -> None:
        """热重建换装时调用：代数**先加**，再清两份缓存（`R28-05` 的顺序是承重的）。"""
        self.generation += 1
        self.clear_caches()

    def clear_caches(self) -> None:
        """只清缓存、不加代数 —— 供 `rebuild` 换装临界区里那第二次清（见 `Runtime.rebuild`）。"""
        self.role_models.clear()
        self.effective_by_user.clear()

    def effective_for(self, user_id: str | None = None) -> Settings:
        """**这一次模型调用花谁的 key** 的那份有效配置。

        `M2d` 那句"`effective_settings` 是唯一咽喉"原先只兑现了半边：配置**按人存**了，
        但运行期那份快照是**实例级**的（谁登录都花实例主人的 key）。这里补的是另半边 ——
        按这次调用的人拼一份出来。

        它与实例主人那份（`state["effective"]`）不是同一件事，也不该合并：那份喂知识库、
        工具闭包与历史预算，问的是"这台机器能干什么"，换个人不会变；而凭据问的是"这次谁
        付钱"，跟着人走。单机形态（一台实例一个主人）下两者恒等，走同一条短路，零额外开销。

        刻意**不写成"整份配置按请求"**：`effective_settings` 只覆盖 `model_backends` /
        `model_default` / `model_fallbacks` 三项（其余字段来自 env 与运行环境覆盖，是设备
        级的），所以按身份解析出来的两份，差别只在凭据那三项。

        缓存按主人分格。`rebuild` 整体清空 —— 与角色级模型缓存同一条纪律：配置改了必须重建。
        """
        owner = user_id or self.identity()
        if owner == self.identity():
            return self.state["effective"]
        cached = self.effective_by_user.get(owner)
        if cached is None:
            gen = self.generation
            cached = runtime_settings.apply_overrides(
                self.model_settings.effective_settings(self.env_settings, user_id=owner),
                runtime_settings.load_overrides(self.conn),
            )
            # 代数没变才写回去（`R28-05`）：变了说明这期间换过一次装，手上这份是旧配置。
            if self.generation == gen:
                self.effective_by_user[owner] = cached
        return cached

    def resolve_role_model(
        self,
        backend_name: str | None,
        temperature: float | None = None,
        *,
        user_id: str | None = None,
    ) -> ChatLike:
        """US-8：角色声明了后端名 → 按名解析；未声明 → 默认模型。

        **凭据按"这一轮的主人"取**（M2d 尾巴的收口，§4.1）：图节点入口已把本轮主人绑进
        上下文（`base/identity.bound_user`），所以 `active_user_id` 在这里答的就是"这次该花
        谁的 key"。不在任何一轮里（后台调度器替她冒话）则回落到**这台实例的主人** ——
        那正是她替谁开口。

        `user_id` 是给**跨线程**调用方的（`bound_user` 是 `ContextVar`，不跨线程传播）：
        自动提取跑在"响应流完之后"的后台线程里，那边 `ctx.current_user()` 还对（那是请求
        视图上的备忘属性），但 `active_user_id` 一定是空的 —— 知道这一轮属于谁的调用方
        必须显式传进来，否则它就会拿实例主人的 key 去替别人抽记忆。

        `temperature` 参与缓存键：同一后端在不同温度下是**不同的模型实例**
        （采样参数只能在构造期设置，见 `core.graph._init_model`）。主人现在是缓存键的
        第一维：两个身份各配同名后端时，"按后端名复用"会把 key 张冠李戴 —— 这正是这一步
        要买的那个性质。
        未知后端名（设置页删掉了一个仍被角色引用的后端）→ 降级到默认并留痕，而不是
        让整轮对话 500：权限 fail-closed，可用性 fail-soft。降级落在**实例主人那台**编译期
        默认模型上 —— 它是"这台机器上一定跑得起来"的那一份。
        """
        if self.injected_model is not None:
            return self.injected_model
        user = user_id or active_user_id(self.identity())
        if not backend_name and temperature is None and user == self.identity():
            return self.state["default_model"]
        cache_key = (user, backend_name, temperature)
        cached = self.role_models.get(cache_key)
        if cached is not None:
            return cached
        gen = self.generation
        try:
            built = self.model_factory(self.effective_for(user), backend_name, temperature)
        except KeyError:
            self.tracer.emit(
                TraceEvent(
                    event="role_backend_missing",
                    detail={"backend": backend_name, "user_id": user},
                )
            )
            return self.state["default_model"]
        # 与 `effective_for` 同一条纪律：代数变了就把手上这份丢掉，别让它活过这次换装
        # （`R28-05`）。下一次解析自然按新配置重建 —— 代价是多构造一次，不是花错 key。
        if self.generation == gen:
            self.role_models[cache_key] = built
        return built


__all__ = ["ModelResolver"]
