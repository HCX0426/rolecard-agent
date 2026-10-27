# 自审 PR（P12：风险改动先让 CI 看一眼，再进 main）

<!-- 自己批自己合：模板只问三件事——改了什么、怎么验的、动了哪些风险族。 -->
<!-- 日常小改（文案、读数、单个用例）仍直推 main，不走这张表（触发清单见 docs/开发流程.md P12）。 -->

## 改了什么

<!-- 一句话 + 涉及的文件族。给三个月后的自己看的那句。 -->

## 怎么验的

<!-- 本地跑过什么命令、结果如何；红测先行的话给用例名（tests/...::test_...）。
     CI 臂见 PR checks；分支上的红不许合（这是本流程存在的全部理由）。 -->

## 风险族（勾选触发这次 PR 的；没勾的 = 这次改动不涉及）

- [ ] 装配根（bootstrap / create_app / Runtime 生命周期 / ctx 读穿面）
- [ ] 公开契约（HTTP 端点 / response_models / openapi.json / schema.gen.ts）
- [ ] 进程生命周期（壳 / 随包后端 / 看门狗 / 更新器 / dev.ps1）
- [ ] 依赖面（requirements* / pyproject / 两把锁 / package.json）
- [ ] 门禁本体（gate.py / consistency 尺子 / workflows）

## 回写清单（本仓唯一不可跳步）

- [ ] docs（开发流程 / 架构总览 / 前端设计 或 CONTRIBUTING）已同步
- [ ] 涉及读数的提交里带上了 gate-readings
