// mcp 这一族的后端契约类型（快照 P3-1 第四刀从 `api/index.ts`
// 按路由域搬出，**正文逐字未改**，连注释都跟着它讲的那个名字走）。
// `index.ts` 以 `export *` 再导出本文件：消费者的 `from "../api"` 一字不动，
// 住址只活在实现里（数一份清单就守一份清单，facade 用通配是不给第二份清单留门）。


/** MCP server（架构计划 C·§6.1 operator 接入；仅 http）。headers 值永不出明文（后端掩码）。 */
export interface McpServer {
  id: string;
  display_name: string;
  transport: string;
  url: string;
  headers: Record<string, string>;
  enabled: boolean;
}

export interface McpServersView {
  servers: McpServer[];
  effective_count: number;
}

/** POST /api/mcp/servers/{id}/test 的结果（不落库，真连一次列工具）。 */
export interface McpTestResult {
  id: string;
  ok: boolean;
  tool_count: number;
  tools: string[];
  error?: string;
}
