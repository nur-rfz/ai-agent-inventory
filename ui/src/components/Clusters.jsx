import { Badge, RiskBadge, Surface } from "./ui.jsx";

export default function Clusters({ clusters, agents, onOpenAgent, onConsolidate, focus }) {
  if (!clusters.length)
    return <div className="rounded-xl border border-dashed border-slate-300 bg-white p-12 text-center text-slate-500">No duplicate agents found yet. Clusters appear once agents are analyzed.</div>;
  return (
    <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
      {clusters.map((c) => {
        const confirming = c.status !== "confirmed";
        return (
          <article key={c.id} data-cluster={c.id}
            className={`rounded-xl border bg-white p-5 ${focus === c.id ? "border-indigo-400 ring-2 ring-indigo-200" : "border-slate-200"}`}>
            <div className="flex items-start justify-between gap-3">
              <div>
                <div className="text-[12px] font-semibold uppercase tracking-wider text-indigo-600">
                  {c.people} people built the same agent
                </div>
                <h3 className="mt-0.5 text-lg font-semibold">{c.label}</h3>
                {c.base_purpose ? <p className="mt-1 text-[13px] text-slate-600">{c.base_purpose}</p>
                  : <p className="mt-1 text-[13px] text-slate-400">{c.rationale}</p>}
              </div>
              <div className="flex flex-col items-end gap-1">
                {confirming
                  ? <span className="inline-flex items-center gap-1 text-[12px] text-indigo-600"><span className="h-1.5 w-1.5 rounded-full bg-indigo-500 live-dot" />{c.status === "confirming" ? "confirming…" : "candidate"}</span>
                  : <Badge className="bg-emerald-50 text-emerald-700 ring-emerald-200">confirmed{c.confidence != null ? ` · ${Math.round(c.confidence * 100)}%` : ""}</Badge>}
                <RiskBadge level={c.risk_level} />
              </div>
            </div>

            <ul className="mt-4 divide-y divide-slate-100 rounded-lg border border-slate-100">
              {c.variants.map((v) => {
                const a = agents[v.agent_id];
                const m = c.members.find((x) => x.agent_id === v.agent_id) || {};
                const fresh = a && a.first_seen && Date.now() - new Date(a.first_seen).getTime() < 10 * 60 * 1000;
                return (
                  <li key={v.agent_id} onClick={() => onOpenAgent(v.agent_id)} className={`flex cursor-pointer items-start gap-3 px-3 py-2 hover:bg-slate-50 ${fresh ? "bg-indigo-50/60" : ""}`}>
                    <div className="w-36 shrink-0">
                      <div className="text-[13px] font-medium text-slate-800">{m.owner}</div>
                      <div className="text-[11px] text-slate-500">{m.department || (fresh ? "just discovered" : "")}</div>
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2 text-[13px] text-slate-700">
                        <span className="truncate">{m.name}</span><Surface value={m.surface} />
                      </div>
                      <div className="mt-0.5 text-[12px] text-slate-500">
                        {confirming && !v.diff ? <span className="shimmer inline-block h-3 w-48 align-middle" /> : (v.diff || "–")}
                        {" · "}{(m.systems || []).join(", ")}
                      </div>
                    </div>
                  </li>
                );
              })}
            </ul>

            {c.shared_capabilities?.length > 0 && (
              <div className="mt-3 flex flex-wrap gap-1">
                {c.shared_capabilities.map((s) => <Badge key={s} className="bg-slate-50 text-slate-700 ring-slate-200">{s}</Badge>)}
              </div>
            )}

            <div className="mt-4 flex items-center justify-between">
              <span className="text-[12px] text-slate-500">Systems: {c.systems.join(", ")}</span>
              <button disabled={confirming} onClick={() => onConsolidate(c.id)}
                className="rounded-lg bg-indigo-600 px-3.5 py-2 text-sm font-medium text-white shadow-sm hover:bg-indigo-700 disabled:cursor-not-allowed disabled:bg-slate-300">
                Preview one Langdock agent →
              </button>
            </div>
          </article>
        );
      })}
    </div>
  );
}
