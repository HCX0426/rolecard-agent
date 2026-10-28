// 把前端手写的契约类型换成**引用生成类型**（P1 自动化：能自动就都自动）。
//
// 为什么：后端契约已由 OpenAPI 导出并生成 `src/api/schema.gen.ts`（`npm run api:types`），
// 但前端 `src/api/*.ts` 里还有几十个手写 interface —— 同一份事实的两份拷贝，
// 后端改字段时前端那份不会自己变（"契约先行"要消灭的正是这类漂移）。
//
// **为什么按"字段集"匹配而不是按名字**：生成物是**后端视角**的名字
// （`ShellReleaseView` / `AuditView` / `RagMetricsViewRoot`），前端手写是**前端视角**
// （`ShellRelease` / `AuditRow` / `RagMetrics`）—— 同名极少（实测 62 个手写类型里
// 只有 2 个与生成物同名）。按名字匹配等于只迁 2 个；按字段集匹配能迁 22 个。
//
// 匹配规则（保守，宁可漏改也不误改）：
//   ⓪ 候选只在**响应侧可达**的组件里找（从 openapi.json 各端点 responses 的 schema
//     跟 $ref 闭包）—— 否则请求体组件（`McpCreateBody` / `IndexCreate` 那类）会因
//     字段集恰好覆盖手写类型被选中，生成的形状语义完全不对（实测假阳性 2 起）。
//   ① 手写类型的**每个字段**都在某个生成组件里存在（生成物可以多字段，手写不能多出）；
//   ② 匹配到的生成组件**唯一**（多个候选同名不同义 → 跳过，让人来判）；
//   ③ 候选按"字段集最接近"排序（多出来的字段越少越像），取第一名。
//
// 替换后**保留前端原名**（`export type AuditRow = components["schemas"]["AuditView"]`）
// —— 消费者的 import 一字不改，迁移只发生在实现里。
//
// 不满足条件的原样留着并在输出里点名：迁移是渐进的，漏掉一个要看得见。

import { readFileSync, writeFileSync, existsSync, readdirSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const API_DIR = join(HERE, "..", "src", "api");
const SCHEMA = join(API_DIR, "schema.gen.ts");
const OPENAPI = join(HERE, "..", "openapi.json");

if (!existsSync(SCHEMA)) {
  console.error(`❌ ${SCHEMA} 不存在 —— 先跑 npm run api:types`);
  process.exit(1);
}
if (!existsSync(OPENAPI)) {
  console.error(`❌ ${OPENAPI} 不存在 —— 先跑 python scripts/tools/export_openapi.py`);
  process.exit(1);
}

/**
 * 响应侧可达的组件名集合（规则 ⓪）。
 *
 * 从每个端点每个状态码响应的 schema 出发跟 $ref 闭包。请求体组件不在这个集合里
 * —— 它们是给"发出去的数据"建模的，字段集撞上响应类型纯属巧合（实测 DomainInfo
 * 被 McpCreateBody 拐走：三个字段恰好同形，语义八竿子打不着）。
 */
function responseReachableComponents(openapiSrc) {
  const spec = JSON.parse(openapiSrc);
  const comps = spec.components?.schemas || {};
  const reachable = new Set();
  const stack = [];
  for (const ops of Object.values(spec.paths || {})) {
    for (const op of Object.values(ops)) {
      if (!op || typeof op !== "object") continue;
      for (const resp of Object.values(op.responses || {})) {
        for (const media of Object.values(resp.content || {})) {
          if (media.schema) stack.push(media.schema);
        }
      }
    }
  }
  while (stack.length > 0) {
    const node = stack.pop();
    if (Array.isArray(node)) {
      stack.push(...node);
      continue;
    }
    if (!node || typeof node !== "object") continue;
    if (typeof node.$ref === "string") {
      const name = node.$ref.split("/").pop();
      if (!(name in comps) || reachable.has(name)) continue;
      reachable.add(name);
      stack.push(comps[name]);
      continue;
    }
    for (const v of Object.values(node)) {
      if (v && typeof v === "object") stack.push(v);
    }
  }
  return reachable;
}

const schemaSrc = readFileSync(SCHEMA, "utf-8");
const responseReachable = responseReachableComponents(readFileSync(OPENAPI, "utf-8"));

/**
 * 从生成物里抽出「组件名 → 字段名集合」。
 *
 * 结构化提取（按行扫描 openapi-typescript 的输出形状：组件 8 空格缩进、字段 12 空格缩进）。
 * 上一版用一个大正则去配「名字: {」，只匹配到 92/113 个（字段换行写法不匹配）——
 * 正则对生成器的排版太敏感，而行扫描只依赖缩进层级。
 */
const fieldTypes = new Map();

function parseSchemaComponents(src) {
  const start = src.indexOf("export interface components {");
  if (start < 0) return new Map();
  const end = src.indexOf("export type $defs", start);
  const lines = src.slice(start, end > 0 ? end : undefined).split("\n");

  /**
   * 字段类型的**完整**文本：从字段行取到配平的 `;`（联合类型会换行排版，
   * 只取单行会得到 `(` —— 松紧判断因此全被架空，unknown 漏进消费者）。
   */
  function fieldTypeUntilSemi(from) {
    let depth = 0;
    let text = "";
    for (let k = from; k < lines.length; k += 1) {
      const line = lines[k].trim();
      for (const ch of line) {
        if (ch === "(" || ch === "{" || ch === "[") depth += 1;
        else if (ch === ")" || ch === "}" || ch === "]") depth -= 1;
        else if (ch === ";" && depth === 0) {
          text += line.slice(0, line.indexOf(";"));
          return text.trim();
        }
      }
      text += ` ${line}`;
    }
    return text.trim();
  }

  const components = new Map();
  for (let i = 0; i < lines.length; i += 1) {
    const head = /^\s{8}([A-Za-z_]\w*):/.exec(lines[i]);
    if (!head) continue;
    const fields = new Set();
    const types = new Map();
    for (let j = i + 1; j < lines.length; j += 1) {
      if (/^\s{8}\w/.test(lines[j])) break;
      const fm = /^\s{12}([A-Za-z_]\w*)\??:/.exec(lines[j]);
      if (fm) {
        fields.add(fm[1]);
        types.set(fm[1], fieldTypeUntilSemi(j));
      }
    }
    components.set(head[1], fields);
    fieldTypes.set(head[1], types);
  }
  return components;
}

function parseHandwrittenFields(src) {
  const fields = new Set();
  const types = new Map();
  const re = /^\s{2}([A-Za-z_]\w*)\??\s*:\s*(.+?);\s*$/gm;
  let m;
  while ((m = re.exec(src)) !== null) {
    fields.add(m[1]);
    types.set(m[1], m[2]);
  }
  return { fields, types };
}

/**
 * 这一格在生成物里是不是松的（未知对象 / 任意值）。
 *
 * 松 = 前端拿到的是 unknown ⇒ 迁移会把信息丢掉 ⇒ 大量 tsc 错误
 * 'x is of type unknown'。判据：类型文本里出现单词 unknown / any，或
 * dict[str, Any] 落成的索引签名 `[key: string]: unknown`。
 */
function isLooseType(typeText) {
  if (!typeText) return true;
  if (/\[key: string\]: unknown/.test(typeText)) return true;
  return /(^|[^\w$])(unknown|any)([^\w$]|$)/.test(typeText);
}

/**
 * 生成组件的**闭包**里有没有松格（守 ① 的递归版）。
 *
 * 为什么必须递归：松常藏在嵌套组件里 —— 实测 RagMetrics 顶层 p50/p95/p99 全是
 * 具名引用（顶层不松），松在 `RagMetricsViewP50.embed_ms: unknown`（dict 值反推
 * 不出类型）。只查顶层 ⇒ 放过它 ⇒ 迁完 34 处 'x is of type unknown'。
 * 解析不到引用的组件 ⇒ 当松（保守：宁可漏迁不误迁）。
 */
function genFieldIsLoose(typeText, seen = new Set()) {
  if (!typeText) return true;
  const refs = [...typeText.matchAll(/components\["schemas"\]\["(\w+)"\]/g)].map((m) => m[1]);
  const stripped = typeText.replace(/components\["schemas"\]\["\w+"\]/g, "");
  if (isLooseType(stripped)) return true;
  for (const r of refs) {
    if (seen.has(r)) continue;
    seen.add(r);
    const subFields = fieldTypes.get(r);
    if (!subFields) return true;
    for (const t of subFields.values()) if (genFieldIsLoose(t, seen)) return true;
  }
  return false;
}

const components = parseSchemaComponents(schemaSrc);
if (components.size === 0) {
  console.error("❌ 没从 schema.gen.ts 里解析出任何组件 —— 解析器该更新了");
  process.exit(1);
}

const FILES = readdirSync(API_DIR).filter(
  (f) => f.endsWith(".ts") && !["schema.gen.ts", "index.ts", "sse.ts"].includes(f),
);

/**
 * 手写 interface 的「名字 → 字段类型表」全量索引（先扫一遍所有文件建出来）。
 * 松紧比较要能**跨 interface 递归**：`models: ProviderModelRow[]` 的松紧取决于
 * ProviderModelRow 自己 —— 不建全量索引就会把"引用了具体类型"误判成松。
 */
const handIndex = new Map();
for (const file of FILES) {
  const text = readFileSync(join(API_DIR, file), "utf-8");
  for (const m of text.matchAll(/export interface (\w+)\s*\{([\s\S]*?)\n\}/g)) {
    handIndex.set(m[1], parseHandwrittenFields(m[2]).types);
  }
}

/**
 * 手写那一格是不是本来就松（unknown / any / Record<string, unknown>，或递归引用
 * 到松的手写 interface）。引用解析不到的名字（组件、React 类型等）⇒ 当不松。
 */
function ownFieldIsLoose(typeText, seen = new Set()) {
  if (!typeText) return false;
  if (/(^|[^\w$])(unknown|any)([^\w$]|$)/.test(typeText)) return true;
  if (/Record<string, unknown>/.test(typeText)) return true;
  for (const m of typeText.matchAll(/:\s*([A-Z]\w*)|[<\[(]\s*([A-Z]\w*)/g)) {
    const ref = m[1] || m[2];
    if (!ref || seen.has(ref)) continue;
    const fields = handIndex.get(ref);
    if (!fields) continue;
    seen.add(ref);
    for (const t of fields.values()) if (ownFieldIsLoose(t, seen)) return true;
  }
  return false;
}

const CHECK = process.argv.includes("--check");
let migrated = 0;
const skipped = [];

for (const file of FILES) {
  const path = join(API_DIR, file);
  let src = readFileSync(path, "utf-8");
  let changed = false;

  for (const match of [...src.matchAll(/export interface (\w+)\s*\{([\s\S]*?)\n\}/g)]) {
    const [whole, name, body] = match;
    const { fields: own, types: ownTypes } = parseHandwrittenFields(body);
    if (own.size === 0) continue;

    // 候选：响应侧可达（规则 ⓪）且每个手写字段都在里的生成组件
    const candidates = [...components.entries()]
      .filter(([cname]) => responseReachable.has(cname))
      .filter(([, fields]) => [...own].every((f) => fields.has(f)))
      .map(([cname, fields]) => ({ cname, extra: fields.size - own.size }))
      .sort((a, b) => a.extra - b.extra);

    if (candidates.length === 0) {
      skipped.push(`${file}: ${name}（生成物里没有字段集覆盖它的组件）`);
      continue;
    }
    // **生成物不能比手写更松**：手写那一格若是具体类型（X[] / 具名对象），而生成物
    // 那一格（含其嵌套闭包）是 unknown / any / dict，迁移会**丢掉类型信息** ——
    // 实测 34 处 tsc 错误 'x is of type unknown'（旧版只查顶层字段，松藏在
    // RagMetricsViewP50.embed_ms 这类**嵌套第二层**里，顶层检查全放过）。
    // 那种端点是"契约推不出的部分由人补"，保持手写（这才是"自动化的诚实边界"）。
    const chosen = candidates[0];
    let picked = chosen;
    const genTypes = fieldTypes.get(chosen.cname) || new Map();
    const loosened = [...own].filter((f) => {
      const genType = genTypes.get(f);
      const ownType = ownTypes.get(f) || "";
      if (!genType) return false;
      if (!genFieldIsLoose(genType)) return false;
      // 手写那格（含递归引用）也不是具体类型 ⇒ 换过去没损失
      return !ownFieldIsLoose(ownType);
    });
    if (loosened.length > 0) {
      skipped.push(
        `${file}: ${name}（生成物那几格更松：${loosened.join(", ")} —— 保持手写）`,
      );
      continue;
    }
    // 唯一性：字段数最接近的两个候选若"多出字段数"相同 ⇒ 形状一样但语义可能不同。
    // **例外（P1b 第三批）**：并列候选的**字段→类型图完全相同**时，它们在类型层面
    // 不可区分（引用任何一个，前端拿到的字段与松紧一字不差）—— 实测 ToolEntry 被
    // ToolsCatalogDomains_healthItem 与 …KernelItem 并列拦死，而两者本来就是"同一张
    // 工具行"的两处快照。这种平局取字典序第一并在跳过名单外放行；类型图有差别的
    // 平局照旧让人来判（"宁可漏改也不误改"只退到类型图有差这一格）。
    const tied = candidates.filter((c) => c.extra === candidates[0].extra);
    if (tied.length > 1) {
      const maps = tied.map((c) => {
        const types = fieldTypes.get(c.cname) || new Map();
        return [...own]
          .map((f) => `${f}:${types.get(f) ?? ""}`)
          .sort()
          .join("|");
      });
      const allSame = maps.every((m) => m === maps[0]);
      if (!allSame) {
        skipped.push(
          `${file}: ${name}（多个候选同形：${tied.map((c) => c.cname).join(", ")}）`,
        );
        continue;
      }
      tied.sort((a, b) => a.cname.localeCompare(b.cname));
      picked = tied[0];
    }

    src = src.replace(
      whole,
      `export type ${name} = components["schemas"]["${picked.cname}"];`,
    );
    changed = true;
    migrated += 1;
  }

  if (changed) {
    if (!src.includes('from "./schema.gen"')) {
      const importLine = 'import type { components } from "./schema.gen";';
      const anchor = src.indexOf("\nimport ");
      if (anchor >= 0) {
        src = src.slice(0, anchor + 1) + importLine + "\n" + src.slice(anchor + 1);
      } else {
        const firstCode = src.indexOf("\nexport");
        src = src.slice(0, firstCode) + "\n" + importLine + "\n" + src.slice(firstCode);
      }
    }
    if (CHECK) {
      console.log(`❌ ${file} 仍可迁移 —— 跑 npm run api:types:migrate`);
      process.exitCode = 1;
    } else {
      writeFileSync(path, src, "utf-8");
      console.log(`  ${file}: 已迁移`);
    }
  }
}

if (skipped.length > 0) {
  console.log("未迁移（需人工判断）：");
  for (const s of skipped) console.log(`  ${s}`);
}
console.log(CHECK ? `检查完成：${migrated} 个类型待迁移` : `共迁移 ${migrated} 个类型`);
