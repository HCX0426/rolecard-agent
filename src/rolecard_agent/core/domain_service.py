"""域查询服务抽象（core 层，向下无依赖）。

M9：解耦 `AppContext` 对具体域查询服务类的硬编码。`deps.py` / routers 只依赖本协议，
不直接 import 任何 `domains/<x>` 具体域模块（否则 api 层重新耦合到具体域，与
`registry.build_registry` 的"API 层绝不 import 具体域"约定相悖）。内核源码亦不出现具体域的
专有名词（L1：分层 token 卫生，一致性脚本机器校验）。

**2026-10-04 收口（审查快照 P1-5 的协议瘦身那一格）**：本协议原先带八个**首个富域的专名**
方法（`create_report` / `list_records` / `get_record` / `update_index` / `delete_index` /
`delete_report` / `report_task_id` / `report_id_for_task`）——那不是抽象，是**把一个域的
形状抄进了内核**：第二个域要么照第一个域的报告/指标模型长，要么根本没法实现，于是数据型域
至今没有富服务，而 api 层只能继续点名具体域（本模块连提那个域名都会被 `core/ no domain
token` 判红，正是这条纪律存在的理由）。

收口方式是把富 CRUD **搬回域内**（`api/routers/records.py` 那族端点迁进它所属的域包），
由 `DomainSpec.router_contrib` 把路由交回宿主挂载（宿主只交请求期依赖，不 import 具体域）。
搬完之后，内核与接入层对"域查询服务"真正只剩一个问题要问 —— **删报告时该顺手清掉哪个向量
集合**（写索引与删索引必须是同一个字符串，否则表现是"删了报告但向量还在"）。那个问题的答案
就是 `knowledge_scope`，协议因此瘦成这一件。

域自己的富方法（及其异常族）住在域里，谁需要谁 import 那个域 —— api 层不在此列。
"""

from __future__ import annotations

from typing import Protocol


class DomainQueryService(Protocol):
    """一个域查询服务对**内核/接入层**兑现的唯一契约（结构化匹配即可，无需显式继承）。"""

    #: 该域在知识库里的作用域名（上传把文档索引进 chroma 用的集合名）。
    #:
    #: 为什么要经协议暴露：写索引（上传）与删索引（删除报告）必须用**同一个**字符串，
    #: 而 api 层按约定不 import 具体域模块 —— 于是由域自己声明，api 只读抽象上的这个属性。
    knowledge_scope: str
