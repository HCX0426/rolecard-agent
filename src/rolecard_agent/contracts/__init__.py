"""contracts 层：**自动生成的 API 响应模型**（P1 契约自动化的产物）——纯 pydantic、
零仓内依赖的形状声明，住在 api 与 domains 之间：api 的路由接线引用它，
域内路由（R102-10 之后路由住在域里）也要引用它 —— 放在 api 里 domains 就够不着
（分层契约禁止 domains → api），放在 base 里又违反"零业务知识"的准入判据。
生成器：`scripts/tools/gen_response_models.py`；接线：`scripts/tools/wire_response_models.py`。
"""
