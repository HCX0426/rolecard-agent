import { useEffect, useState } from "react";
import { api, type ModelSettings } from "../api";

// 可编辑的一行后端。api_key 永远不回显（后端只给 has_key）：
// 留空 = 保留已存 key；输入新值 = 覆盖；输入空格后清空 = 清除 key。
interface EditableBackend {
  name: string;
  provider: string;
  base_url: string;
  model: string;
  api_key: string;
  has_key: boolean;
}

const PROVIDERS = ["openai", "ollama"];

export default function SettingsPage() {
  const [def, setDef] = useState<string>("");
  const [rows, setRows] = useState<EditableBackend[]>([]);
  const [status, setStatus] = useState<{ ok: boolean; msg: string } | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    api
      .get<ModelSettings>("/api/settings/models")
      .then((s) => {
        setDef(s.default || s.backends[0]?.name || "");
        setRows(
          s.backends.map((b) => ({
            name: b.name,
            provider: b.provider,
            base_url: b.base_url || "",
            model: b.model,
            api_key: "",
            has_key: b.has_key,
          })),
        );
        setLoaded(true);
      })
      .catch((e) => setStatus({ ok: false, msg: `加载失败：${e.message}` }));
  }, []);

  function update(i: number, patch: Partial<EditableBackend>) {
    setRows((rs) => rs.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  }

  function addRow() {
    setRows((rs) => [
      ...rs,
      { name: "", provider: "openai", base_url: "", model: "", api_key: "", has_key: false },
    ]);
  }

  function removeRow(i: number) {
    setRows((rs) => rs.filter((_, j) => j !== i));
  }

  async function save() {
    try {
      const body = {
        default: def,
        backends: rows.map((r) => ({
          name: r.name.trim(),
          provider: r.provider.trim(),
          base_url: r.base_url.trim() || null,
          model: r.model.trim(),
          // 空串会被后端理解为"清除"；这里区分"没碰过"（保持 None=保留）与"清空"
          api_key: r.api_key === "" && r.has_key ? null : r.api_key,
        })),
      };
      const saved = await api.put<ModelSettings>("/api/settings/models", body);
      setDef(saved.default || "");
      setRows(
        saved.backends.map((b) => ({
          name: b.name,
          provider: b.provider,
          base_url: b.base_url || "",
          model: b.model,
          api_key: "",
          has_key: b.has_key,
        })),
      );
      setStatus({
        ok: true,
        msg: "已保存并热生效：下一轮对话即使用新模型后端（无需重启）。",
      });
    } catch (e) {
      setStatus({ ok: false, msg: `保存失败：${(e as Error).message}` });
    }
  }

  return (
    <div className="h-full overflow-y-auto p-6">
      <div className="mx-auto max-w-3xl">
        <h2 className="text-base font-semibold text-slate-900">设置 · 模型后端</h2>
        <p className="mt-0.5 text-xs text-slate-400">
          后端 = 一个可调用的模型端点（本地 Ollama 或任意 OpenAI 兼容 API）。保存即热生效。
          api_key 只写入不回显，存储在本机演示库中。
        </p>

        {status && (
          <p
            className={`mt-3 rounded-lg px-3 py-2 text-xs ${
              status.ok ? "bg-green-50 text-green-700" : "bg-red-50 text-red-600"
            }`}
          >
            {status.msg}
          </p>
        )}

        {loaded && (
          <>
            <div className="mt-4 space-y-3">
              {rows.map((r, i) => (
                <div key={i} className="rounded-xl border border-slate-200 bg-white p-4">
                  <div className="flex items-center gap-3">
                    <label className="flex items-center gap-1.5 text-xs text-slate-500">
                      <input
                        type="radio"
                        name="default-backend"
                        checked={def === r.name}
                        onChange={() => setDef(r.name)}
                      />
                      默认
                    </label>
                    <input
                      value={r.name}
                      onChange={(e) => update(i, { name: e.target.value })}
                      placeholder="后端名（如 siliconflow）"
                      className="w-40 rounded-lg border border-slate-200 px-2.5 py-1.5 font-mono text-sm"
                    />
                    <select
                      value={r.provider}
                      onChange={(e) => update(i, { provider: e.target.value })}
                      className="rounded-lg border border-slate-200 px-2 py-1.5 text-sm"
                    >
                      {PROVIDERS.map((p) => (
                        <option key={p} value={p}>
                          {p}
                        </option>
                      ))}
                    </select>
                    <button
                      onClick={() => removeRow(i)}
                      className="ml-auto rounded px-2 py-1 text-xs text-red-400 hover:bg-red-50 hover:text-red-600"
                    >
                      移除
                    </button>
                  </div>
                  <div className="mt-2.5 grid grid-cols-2 gap-3">
                    <input
                      value={r.model}
                      onChange={(e) => update(i, { model: e.target.value })}
                      placeholder="模型名（如 deepseek-ai/DeepSeek-V4-Flash）"
                      className="w-full rounded-lg border border-slate-200 px-2.5 py-1.5 text-sm"
                    />
                    <input
                      value={r.base_url}
                      onChange={(e) => update(i, { base_url: e.target.value })}
                      placeholder="base_url（Ollama 可留空，如 https://api.siliconflow.cn/v1）"
                      className="w-full rounded-lg border border-slate-200 px-2.5 py-1.5 text-sm"
                    />
                    <input
                      type="password"
                      value={r.api_key}
                      onChange={(e) => update(i, { api_key: e.target.value })}
                      placeholder={
                        r.has_key ? "已保存密钥（留空 = 保持不变）" : "api_key（可选）"
                      }
                      className="w-full rounded-lg border border-slate-200 px-2.5 py-1.5 text-sm"
                    />
                    {r.has_key && (
                      <label className="flex items-center gap-1.5 text-xs text-slate-400">
                        <input
                          type="checkbox"
                          checked={r.api_key.trim() === "" && r.api_key.length > 0}
                          onChange={(e) =>
                            update(i, { api_key: e.target.checked ? " " : "" })
                          }
                        />
                        清除已存密钥
                      </label>
                    )}
                  </div>
                </div>
              ))}
            </div>

            <div className="mt-4 flex gap-2">
              <button
                onClick={addRow}
                className="rounded-lg border border-slate-200 bg-white px-4 py-2 text-sm text-slate-600 hover:bg-slate-50"
              >
                ＋ 添加后端
              </button>
              <button
                onClick={save}
                className="rounded-lg bg-blue-600 px-5 py-2 text-sm font-medium text-white hover:bg-blue-700"
              >
                保存并生效
              </button>
            </div>

            <p className="mt-4 rounded-lg bg-slate-50 px-3 py-2.5 text-xs leading-relaxed text-slate-400">
              说明：env（<code>MODEL_BACKENDS</code>）仍是首次启动的种子配置；在设置页保存过一次后，
              数据库里的配置接管，同名后端覆盖。删除所有后端会保存失败 —— 至少保留一个。
            </p>
          </>
        )}
      </div>
    </div>
  );
}
