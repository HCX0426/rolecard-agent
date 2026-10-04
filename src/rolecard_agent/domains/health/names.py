"""health 域的字符串常量：知识作用域名与本域贡献的工具名。

**为什么是单独一个叶模块**：这三个名字被四个地方读（service 的 `knowledge_scope`、
tools 的重试分流、seed 的角色白名单、registry 的装配），而包级 `__init__` 又要在导入期
把 `SPEC` 组装出来 —— 谁 `from rolecard_agent.domains.health import ...` 谁就踩
「包还没初始化完」的半初始化环。把纯常量放进一个不 import 任何东西的叶子，环就没了：
包初始化只依赖叶子与 spec，业务模块也只依赖叶子。
"""

from __future__ import annotations

#: 上传/抽取把文档索引进 chroma 时用的集合名，也是角色 `knowledge_scopes` 里要授权的
#: 那个名字。放在这里是为了让**写索引**（上传）与**删索引**（删除报告）两端不可能各自
#: 写成两个相近但不同的字符串 —— 一旦写错，表现是"删了报告但向量还在"或"索引写进去
#: 检索不到"，且都很难查。
KNOWLEDGE_SCOPE = "health_reports"

#: 本域贡献的工具名（本域的单一事实面：角色白名单、装配点的写/读分流、测试的解析检查
#: 都从这里读，不再有第二份手抄清单）。
DOMAIN_TOOL_NAMES: tuple[str, ...] = (
    "query_health_record",
    "compare_health_index",
    "list_reports",
    "upload_medical_report",
)

#: 有副作用的工具：**不允许执行器重试**。`upload_medical_report` 会写 ingestion 台账，
#: 重试一次就多一条记录。装配点按 `DomainSpec.write_tool_names` 分流注册 —— 声明在域
#: 自身而不是装配点：谁能安全重试是工具的性质，不是宿主的知识。
WRITE_TOOL_NAMES: frozenset[str] = frozenset({"upload_medical_report"})
