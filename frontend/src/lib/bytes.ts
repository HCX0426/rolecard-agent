/** 字节数唯一格式化出处（`R102-60`）：从前四份四口径 —— 同一个字节数可以渲染成
 * "2048.0 MB"、"2.0 GB"、"2 GB"、"2097152.0 KB"，用户无法横向比较。输出档位在此
 * 一次定版：<1KB 用整数 B，其余 KB/MB/GB/TB 一位小数。
 * 消费方：`lib/uploadOutcome.ts`（转发）、ShellReleaseCard、LocalServiceCard、ApprovalPanel。
 */
export function formatBytes(n: number): string {
  if (!Number.isFinite(n) || n < 0) return "—";
  if (n < 1024) return `${Math.round(n)} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = n;
  let unit = "B";
  for (unit of units) {
    value /= 1024;
    if (value < 1024) break;
  }
  return `${value.toFixed(1)} ${unit}`;
}
