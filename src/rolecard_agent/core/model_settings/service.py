"""Runtime-editable model configuration - the data layer behind the settings page.

Why a DB table instead of env-only: `MODEL_BACKENDS` (config.py) is a deploy-time contract.
The settings page needs an OPERATOR-time contract: add a SiliconFlow/OpenAI-compatible
endpoint, set the default, and have the next conversation turn use it WITHOUT restarting.
Env stays the bootstrap truth; the first settings save takes over (see `effective_settings`).

## 为什么是两层（2026-09-20 拆层）

一张表混装两层时，同一把 key 抄在每一行上（`siliconflow` + `siliconflow-vl` + …），于是
"改一次 key 要改 N 处"，漏一处就是"部分模型突然 401"（用户："为啥不用供应商和模型名组成
一个键"）。拆完之后：

  * `model_provider` = 凭据层：一组 = 一个 (供应商, base_url) 端点，**key 只有一个家**；
  * `model_backend`  = 模型层：一行 = 一个可调用模型，指向某个组。`name` 仍是主键，
    `session_thread.model_name` / `role_card.model_name` 都指着它 —— 拆层不能断这条链。

  * **用途也不在行上了**：一行服务谁 = `service_endpoint` 里有没有指向它的引用行（含
    category='chat'）。所以 `usage` 是**派生只读**值，唯一的写入口在「服务」页签。

读侧形状不变：`raw_backends` / `list_backends` / `effective_settings` 仍给出带
`provider`/`base_url`/`api_key`/`usage` 的"后端行"（JOIN + 派生补齐），所以内核、模型工厂、
rag 与 services 一行不改。变的是写入与迁移：这些字段不再有第二处副本。

两条规则值得单独说：

  * **API keys are write-only over the wire.** 读接口从不返回 key（只有 `has_key` + 掩码），
    浏览器会话因此永远读不到明文。`save` 把省略/None 的 `api_key` 理解为"保留该组已存的"、
    空串理解为"清除" —— 否则每次没重输 key 的保存都会把 key 抹掉。
  * **Plaintext at rest, stated rather than hidden.** Keys live in the local demo SQLite
    file, which never leaves the machine. Production would move to a secret manager - that
    is a v2 concern, and pretending otherwise in a demo would be worse than the limitation.
  * **一组凭据有主人（M2d）**：`model_provider.user_id` 是这一行的归属，所以本模块每个读写
    都要调用方交出 `user_id`。运行期"花谁的 key"只有一个答案 —— 交出这份 `Settings` 的那个人
    （`effective_settings(..., user_id=)` 是唯一咽喉）。刻意**留着不分身份**的只有两类，各自
    写明原因：分配主键（`_all_group_ids` / `_all_backend_names`，主键是全局的）、启动时的数据
    卫生清扫（`normalize_providers`）。`service_endpoint` 的 chat 引用行**也按人**
    （多租户 B1b，方案 A 收了 §4.1 的尾巴：对话默认/回退链花谁的 key 由谁定），能力端点
    （ocr/embedding/rerank）仍设备级、归 `core/models/services.py` 管。`model_backend` 因此**不另挂
    一列**：模型行的主人从它所属的组继承，按名改一行的那些 UPDATE 靠 JOIN 带上主人条件，
    而不是多存一份冗余归属。

## 为什么这个类被拆成四个 mixin（2026-10-04 审查快照 P1-6）

这个文件从前 1624 行塞着六个职责（URL 校验 / provider 目录 / 掩码 / 搬层 DDL / 30+ 个 CRUD
方法 / 配置合成与行解析）。拆包的规矩是**按职责分文件**，而 30+ 个方法同属一张表的读写，
硬切成两个类会把"同一个服务的读与写"变成两个对象 —— 所以方法按职责进 mixin，类本身只留
装配：读侧（`read`）、写侧 CRUD（`save` / `write`）、配置合成（`effective`）。
"""

from __future__ import annotations

from rolecard_agent.core.model_settings.effective import _EffectiveMixin
from rolecard_agent.core.model_settings.read import _ReadMixin
from rolecard_agent.core.model_settings.save import _SaveMixin
from rolecard_agent.core.model_settings.write import _WriteMixin


class ModelSettingsService(_SaveMixin, _WriteMixin, _EffectiveMixin, _ReadMixin):
    # 基类顺序是 MRO 的要求：后三个 mixin 都继承 `_ReadMixin`（它们要用它定义的
    # `_conn` 与读侧助手），子类必须排在父类之前。功能上与"读在前"完全同形。
    """模型配置的服务对象（设置的读写与"这一轮用哪份配置"的合成）。

    方法按职责住在同包的四个 mixin 里，本类只负责把它们装成一张表的服务 —— 调用方看到的
    仍然只有 `ModelSettingsService` 一个入口（导出面零改）。
    """
