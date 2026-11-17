import { useEffect, useMemo, useRef, useState } from "react";
import { api, timeAgo } from "../api.js";
import { Badge, ClassBadge, ReachChip, RiskBadge, Section, Shimmer, StatusPill, Surface } from "./ui.jsx";

function summarize(s) {
  const r = s.raw || {};
  switch (s.kind) {
    case "mcp_config":
      return `${r.change} MCP server “${r.server_name}” in ${r.client} · ${r.url || [r.command, ...(r.args || [])].join(" ")}`;
    case "mcp_probe":
      return r.status === "ok" ? `tools/list → ${r.tools?.length} tools (${r.server_info?.name || r.server_name})`
        : `probe ${r.status}: ${r.error || r.reason}`;
    case "process":
      return `${r.event} ${r.name} ${(r.cmdline || []).slice(1, 3).join(" ")} · parent ${(r.parent_chain || []).map((p) => p.name).slice(0, 2).join(" ← ") || "–"}`;
    case "net_conn":
      return `→ ${r.hostname || r.ip}:${r.port}${r.system ? ` (${r.system})` : ""}`;
    case "code_import":
      return r.env_file ? `${r.relative_path}: env names ${r.env_names?.join(", ")}`
        : `${r.relative_path}: SDKs ${r.sdks?.join(", ")}${r.systems?.length ? ` · ${r.systems.join(", ")}` : ""}`;
    case "schedule":
      return `${r.scheduler} “${r.cron}” runs ${r.name}`;
    case "oauth_grant":
      return `${r.provider}: ${r.event} “${r.app_name}” by ${r.user_email}${r.scopes?.length ? ` · ${r.scopes.map((x) => x.split("/").pop()).join(" ")}` : ""}`;
    case "api_key":
      return `${r.provider}: ${r.event} in “${r.project_name}”${r.owner_type ? ` (owner: ${r.owner_type})` : ""}`;
    default:
      return s.kind;
  }
}

const KIND_COLOR = {
  mcp_config: "bg-indigo-500", mcp_probe: "bg-violet-500", process: "bg-slate-400", net_conn: "bg-sky-500",
  code_import: "bg-emerald-500", schedule: "bg-amber-500", oauth_grant: "bg-fuchsia-500", api_key: "bg-rose-500",
};

function Graph({ graph }) {
  const cols = { person: 0, agent: 1, mcp_server: 2, script: 2, oauth_app: 2, llm_project: 2, system: 3 };
  const byCol = [[], [], [], []];
  const seen = new Set();
  graph.nodes.forEach((n) => { if (!seen.has(n.id)) { seen.add(n.id); byCol[cols[n.type] ?? 2].push(n); } });
  const W = 640, colX = [70, 230, 400, 565], rowH = 44;
  const H = Math.max(...byCol.map((c) => c.length), 1) * rowH + 20;
  const pos = {};
  byCol.forEach((col, ci) => col.forEach((n, i) => { pos[n.id] = { x: colX[ci], y: 10 + (H - 20) / 2 - ((col.length - 1) * rowH) / 2 + i * rowH }; }));
  const fill = { person: "#0f172a", agent: "#4f46e5", system: "#ffffff" };
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label="person to agent to tools to systems">
      {graph.edges.map((e, i) => {
        const a = pos[e.from], b = pos[e.to];
        if (!a || !b) return null;
        const writes = e.label?.includes("w");
        return (
          <g key={i}>
            <path d={`M${a.x + 58},${a.y} C${(a.x + b.x) / 2},${a.y} ${(a.x + b.x) / 2},${b.y} ${b.x - 58},${b.y}`}
              fill="none" stroke={writes ? "#e11d48" : "#cbd5e1"} strokeWidth={writes ? 1.6 : 1.2} />
            {["r", "w", "rw"].includes(e.label) && (
              <text x={b.x - 64} y={b.y - 5} fontSize="9" textAnchor="end" fill={writes ? "#e11d48" : "#64748b"}>{e.label}</text>
            )}
          </g>
        );
      })}
      {Object.entries(pos).map(([id, p]) => {
        const n = graph.nodes.find((x) => x.id === id);
        const dark = n.type === "person" || n.type === "agent";
        return (
          <g key={id}>
            <rect x={p.x - 58} y={p.y - 14} width={116} height={28} rx={8}
              fill={fill[n.type] || "#f1f5f9"} stroke={n.type === "system" ? "#cbd5e1" : "none"} />
            <text x={p.x} y={p.y + 4} fontSize="10.5" textAnchor="middle" fill={dark ? "#fff" : "#0f172a"}>
              {n.label.length > 19 ? n.label.slice(0, 18) + "…" : n.label}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

export default function AgentDrawer({ id, live, onClose, onOpenCluster }) {
  const [d, setD] = useState(null);
  const [hl, setHl] = useState(new Set());
  const [open, setOpen] = useState(null);
  const timeline = useRef(null);

  useEffect(() => { // refetch whenever the live agent changes (card arrives, cluster joins)
    api(`/agents/${id}`).then(setD).catch(() => setD(null));
  }, [id, live?.status, live?.evidence_hash, live?.cluster_id, live?.name]);

  const groups = useMemo(() => {
    if (!d) return [];
    const m = new Map();
    for (const s of d.evidence) {
      const key = summarize(s).replace(/\d+ runs seen/, "");
      const g = m.get(key) || { key, items: [], kind: s.kind };
      g.items.push(s);
      m.set(key, g);
    }
    return [...m.values()];
  }, [d]);

  const show = (ids) => {
    const set = new Set(ids);
    setHl(set);
    setTimeout(() => timeline.current?.querySelector("[data-hl='1']")?.scrollIntoView({ behavior: "smooth", block: "center" }), 50);
  };

  if (!d) return null;
  const a = { ...d, ...(live || {}) };
  const analyzing = a.status === "enriching" || a.status === "provisional";
  return (
    <div className="fixed inset-0 z-40 flex justify-end bg-slate-900/20" onClick={onClose}>
      <aside onClick={(e) => e.stopPropagation()} className="h-full w-full max-w-[760px] overflow-y-auto bg-white shadow-2xl">
        <div className="sticky top-0 z-10 border-b border-slate-100 bg-white/95 px-6 py-4 backdrop-blur">
          <div className="flex items-start justify-between gap-4">
            <div>
              <div className="flex flex-wrap items-center gap-2">
                {analyzing ? <span className="shimmer inline-block h-6 w-56" /> : <h2 className="text-xl font-semibold">{a.name}</h2>}
                <StatusPill status={a.status} />
              </div>
              <div className="mt-1 flex flex-wrap items-center gap-3 text-[13px] text-slate-600">
                <span>{a.owner.name}{a.owner.department ? ` · ${a.owner.department}` : ""}{a.owner.title ? ` · ${a.owner.title}` : ""}</span>
                <Surface value={a.surface} />
                <ClassBadge value={a.classification} />
                <RiskBadge level={a.risk.level} />
                {a.confidence != null && <span className="text-[12px] text-slate-500">confidence {Math.round(a.confidence * 100)}%</span>}
              </div>
            </div>
            <button onClick={onClose} className="rounded-lg px-2 py-1 text-slate-400 hover:bg-slate-100 hover:text-slate-700" aria-label="close">✕</button>
          </div>
        </div>

        <div className="px-6 pb-10">
          <Section title="Purpose">
            {analyzing ? <Shimmer lines={2} /> : (
              <>
                <p className="text-[15px] text-slate-800">{a.purpose}</p>
                {a.rationale?.purpose && (
                  <p className="mt-1 text-[12px] text-slate-500">
                    Why: {a.rationale.purpose}{" "}
                    {a.rationale.purpose_evidence_ids?.length > 0 && (
                      <button className="text-indigo-600 hover:underline" onClick={() => show(a.rationale.purpose_evidence_ids)}>
                        evidence ({a.rationale.purpose_evidence_ids.length})
                      </button>
                    )}
                  </p>
                )}
                {a.capabilities?.length > 0 && (
                  <div className="mt-2 flex flex-wrap gap-1">{a.capabilities.map((c) => <Badge key={c} className="bg-slate-50 text-slate-700 ring-slate-200">{c}</Badge>)}</div>
                )}
                {a.cluster_id && (
                  <button onClick={() => onOpenCluster(a.cluster_id)} className="mt-3 rounded-lg bg-indigo-50 px-3 py-1.5 text-[13px] font-medium text-indigo-700 hover:bg-indigo-100">
                    ⧉ Part of a duplicate cluster: open consolidation →
                  </button>
                )}
              </>
            )}
          </Section>

          <Section title="Who → agent → tools → systems"><Graph graph={d.graph} /></Section>

          <div className="grid grid-cols-1 gap-x-6 md:grid-cols-2">
            <Section title="Data reach">
              <div className="space-y-1.5">
                {a.data_reach.map((r) => (
                  <div key={r.system} className="flex items-center justify-between gap-2 text-[13px]">
                    <ReachChip r={r} onClick={() => show(r.evidence_ids)} />
                    <span className="truncate text-[11px] text-slate-500" title={(r.via || []).join("; ")}>{r.rationale || (r.via || [])[0]}</span>
                  </div>
                ))}
                {!a.data_reach.length && <p className="text-[13px] text-slate-400">No business systems seen.</p>}
                {a.llm_providers?.length > 0 && <p className="text-[12px] text-slate-500">LLM providers: {a.llm_providers.join(", ")}</p>}
              </div>
            </Section>
            <Section title="Triggers">
              {a.triggers.map((t, i) => (
                <div key={i} className="text-[13px] text-slate-700">
                  <Badge className="mr-1 bg-amber-50 text-amber-800 ring-amber-200">{t.type}</Badge>{t.description}
                  {t.evidence_ids?.length > 0 && <button className="ml-1 text-[12px] text-indigo-600 hover:underline" onClick={() => show(t.evidence_ids)}>evidence</button>}
                </div>
              ))}
              {!a.triggers.length && <p className="text-[13px] text-slate-400">No runs or schedules seen yet.</p>}
            </Section>
          </div>

          <Section title={`Risk (${a.risk.flags.length})`}>
            <ul className="space-y-1.5">
              {a.risk.flags.map((f, i) => (
                <li key={i} className="flex items-start gap-2 text-[13px]">
                  <RiskBadge level={f.severity} />
                  <span className="flex-1 text-slate-700">{f.text}</span>
                  <button className="text-[12px] text-indigo-600 hover:underline" onClick={() => show(f.evidence_ids)}>evidence</button>
                </li>
              ))}
              {!a.risk.flags.length && <li className="text-[13px] text-slate-400">No risk flags.</li>}
            </ul>
          </Section>

          {a.tools.length > 0 && (
            <Section title={`Tools (${a.tools.length})`}>
              <div className="grid grid-cols-1 gap-1 sm:grid-cols-2">
                {a.tools.map((t) => (
                  <div key={t.server + t.name} className="rounded-lg border border-slate-100 px-2.5 py-1.5" title={t.description}>
                    <div className="flex items-center justify-between font-mono text-[12px] text-slate-800">
                      {t.name}<span className={t.access.includes("w") ? "text-rose-600" : "text-slate-400"}>{t.access}</span>
                    </div>
                    <div className="truncate text-[11px] text-slate-500">{t.system ? `${t.system} · ` : ""}{t.description}</div>
                  </div>
                ))}
              </div>
            </Section>
          )}

          {a.instructions_summary && (
            <Section title="Instructions (visible or inferred)"><p className="text-[13px] text-slate-700">{a.instructions_summary}</p></Section>
          )}

          <Section title={`Evidence timeline (${d.evidence_total} signals)`} right={hl.size > 0 && <button className="text-[12px] text-slate-500 hover:underline" onClick={() => setHl(new Set())}>clear highlight</button>}>
            <ol ref={timeline} className="relative border-l border-slate-200 pl-4">
              {groups.map((g) => {
                const s = g.items[0];
                const isHl = g.items.some((x) => hl.has(x.id));
                return (
                  <li key={g.key} data-hl={isHl ? "1" : "0"} className={`relative mb-1.5 rounded-lg px-2 py-1 ${isHl ? "flash bg-indigo-50 ring-1 ring-indigo-300" : ""}`}>
                    <span className={`absolute -left-[22px] top-2.5 h-2.5 w-2.5 rounded-full ring-2 ring-white ${KIND_COLOR[s.kind] || "bg-slate-400"}`} />
                    <button className="w-full text-left" onClick={() => setOpen(open === g.key ? null : g.key)}>
                      <div className="flex items-baseline justify-between gap-3">
                        <span className="text-[13px] text-slate-800">{summarize(s)}{g.items.length > 1 && <span className="ml-1 text-[11px] text-slate-500">×{g.items.length}</span>}</span>
                        <span className="shrink-0 text-[11px] text-slate-400" title={s.ts}>{timeAgo(s.ts)}</span>
                      </div>
                      <div className="text-[11px] text-slate-400">{s.kind} · {s.source} · {s.host}</div>
                    </button>
                    {open === g.key && <pre className="mt-1 max-h-72 overflow-auto rounded-md bg-slate-900 p-2 text-[11px] text-slate-100">{JSON.stringify(s, null, 2)}</pre>}
                  </li>
                );
              })}
            </ol>
          </Section>
        </div>
      </aside>
    </div>
  );
}
