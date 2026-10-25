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

if (!existsSync(SCHEMA)) {
  console.error(`❌ ${SCHEMA} 不存在 —— 先跑 npm run api:types`);
  process.exit(1);
}

const schemaSrc = readFileSync(SCHEMA, "utf-8");

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
        const rest = lines[j].slice(fm[0].length);
        const semi = rest.indexOf(";");
        types.set(fm[1], (semi >= 0 ? rest.slice(0, semi) : rest).trim());
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
 * 松 = 前端拿到的是 unknown ⇒ 迁移会把信息丢掉 ⇒ 162 处 tsc 错误
 * 'x is of type unknown'。判据：类型文本里出现 unknown / Record<string, unknown> /
 * 裸 any。
 */
function isLooseType(typeText) {
  if (!typeText) return true;
  return /unknown/.test(typeText) || /any/.test(typeText);
}

const components = parseSchemaComponents(schemaSrc);
if (components.size === 0) {
  console.error("❌ 没从 schema.gen.ts 里解析出任何组件 —— 解析器该更新了");
  process.exit(1);
}

const FILES = readdirSync(API_DIR).filter(
  (f) => f.endsWith(".ts") && !["schema.gen.ts", "index.ts", "sse.ts"].includes(f),
);

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

    // 候选：每个手写字段都在里的生成组件
    const candidates = [...components.entries()]
      .filter(([, fields]) => [...own].every((f) => fields.has(f)))
      .map(([cname, fields]) => ({ cname, extra: fields.size - own.size }))
      .sort((a, b) => a.extra - b.extra);

    if (candidates.length === 0) {
      skipped.push(`${file}: ${name}（生成物里没有字段集覆盖它的组件）`);
      continue;
    }
    // **生成物不能比手写更松**：手写那一格若是具体类型（X[] / 具名对象），而生成物
    // 那一格是"未知对象"（dict[str, Any] ⇒ 前端 unknown），迁移会**丢掉类型信息** ——
    // 实测 162 处 tsc 错误 'x is of type unknown'。那种字段是"契约推不出的部分由人补"，
    // 保持手写（这才是"自动化的诚实边界"）。
    const chosen = candidates[0];
    const genTypes = fieldTypes.get(chosen.cname) || new Map();
    const loosened = [...own].filter((f) => {
      const genType = genTypes.get(f);
      const ownType = ownTypes.get(f) || "";
      if (!genType) return false;
      if (!isLooseType(genType)) return false;
      // 手写那格也不是具体类型 ⇒ 换过去没损失
      return !isLooseType(ownType);
    });
    if (loosened.length > 0) {
      skipped.push(
        `${file}: ${name}（生成物那几格更松：${loosened.join(", ")} —— 保持手写）`,
      );
      continue;
    }
    // 唯一性：字段数最接近的两个候选若"多出字段数"相同 ⇒ 形状一样但语义可能不同
    if (candidates.length > 1 && candidates[0].extra === candidates[1].extra) {
      skipped.push(
        `${file}: ${name}（多个候选同形：${candidates
          .filter((c) => c.extra === candidates[0].extra)
          .map((c) => c.cname)
          .join(", ")}）`,
      );
      continue;
    }

    src = src.replace(
      whole,
      `export type ${name} = components["schemas"]["${chosen.cname}"];`,
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
