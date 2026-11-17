function Tile({ label, value, sub, accent }) {
  return (
    <div className="min-w-[132px] flex-1 rounded-xl border border-slate-200 bg-white px-4 py-3">
      <div className="text-[11px] font-medium uppercase tracking-wider text-slate-500">{label}</div>
      <div className={`mt-1 text-2xl font-semibold tabular-nums ${accent || "text-slate-900"}`}>{value}</div>
      {sub && <div className="mt-0.5 text-[12px] text-slate-500">{sub}</div>}
    </div>
  );
}

export default function Header({ stats, connected, tab, setTab, clusters }) {
  const s = stats || {};
  const consolidated = s.agents_in_clusters ? `${s.agents_in_clusters} → ${s.duplicate_clusters}` : "–";
  const llm = s.enrichment;
  return (
    <header className="border-b border-slate-200 bg-white">
      <div className="mx-auto max-w-[1400px] px-6 pt-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <div className="grid h-9 w-9 place-items-center rounded-lg bg-indigo-600 text-white font-bold">S</div>
            <div>
              <h1 className="text-lg font-semibold leading-tight">Shadow Agent Discovery</h1>
              <p className="text-[12px] text-slate-500">AI agents and MCP servers across the company, found without configuring a connector</p>
            </div>
          </div>
          <div className="flex items-center gap-4 text-[12px] text-slate-500">
            {llm && <span title={llm.disabled_reason || ""}>{llm.enabled ? `LLM: ${llm.model}` : "LLM off · rule-based cards"}</span>}
            <span className="inline-flex items-center gap-1.5">
              <span className={`h-2 w-2 rounded-full ${connected ? "bg-emerald-500 live-dot" : "bg-slate-300"}`} />
              {connected ? "live" : "connecting…"}
            </span>
          </div>
        </div>
        <div className="mt-4 flex flex-wrap gap-3">
          <Tile label="Agents found" value={s.agents ?? "–"} sub={s.people ? `built by ${s.people} people` : null} />
          <Tile label="Unmanaged" value={s.agents ? `${s.unmanaged_pct}%` : "–"} sub={s.agents ? `${s.unmanaged} of ${s.agents} outside Langdock` : null} accent="text-rose-600" />
          <Tile label="Duplicate clusters" value={s.duplicate_clusters ?? "–"} sub={s.agents_in_clusters ? `${s.agents_in_clusters} agents doing the same jobs` : "same job, different builders"} accent="text-indigo-600" />
          <Tile label="Consolidate" value={consolidated} sub={s.agents_consolidated ? `${s.agents_consolidated} fewer agents to secure` : "agents → shared Langdock agents"} accent="text-emerald-600" />
          <Tile label="High risk" value={s.risk ? s.risk.high : "–"} sub="write access, tenant-wide grants, …" accent="text-red-600" />
        </div>
        <nav className="mt-4 flex gap-1">
          {[["inventory", "Inventory"], ["clusters", `Duplicates${clusters.length ? ` (${clusters.length})` : ""}`]].map(([k, l]) => (
            <button key={k} onClick={() => setTab(k)}
              className={`rounded-t-lg px-4 py-2 text-sm font-medium ${tab === k ? "border-x border-t border-slate-200 bg-[#f6f7fb] text-slate-900" : "text-slate-500 hover:text-slate-800"}`}>
              {l}
            </button>
          ))}
        </nav>
      </div>
    </header>
  );
}
