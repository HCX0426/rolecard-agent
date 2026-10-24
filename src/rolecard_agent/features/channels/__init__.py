"""外部聊天渠道（`features/channels/`）：QQ 官方机器人等"消息进门"的那一族。

与 `features/reachout/`（主动开口）互补的一族：那边是"她主动说"，这边是"别人从别的
地方跟她说话"。共同纪律：表在内核 schema（`core/schema.sql`），SQL 在功能自己手里，
宿主经 `register_background` 接线，内核不认识任何具体渠道。
"""
