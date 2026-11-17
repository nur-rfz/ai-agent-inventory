import { useMemo, useState } from "react";
import { timeAgo } from "../api.js";
import { ClassBadge, ReachChip, RiskBadge, StatusPill, Surface } from "./ui.jsx";

const FILTERS = [["all", "All"], ["replace", "Move to Langdock"], ["govern_in_place", "Govern in place"], ["ok", "Sanctioned"]];

export default function Inventory({ agents, clusters, firstSeen, onOpen, onOpenCluster }) {
  const [filter, setFilter] = useState("all");
  const [q, setQ] = useState("");
  const clusterById = Object.fromEntries(clusters.map((c) => [c.id, c]));

  const rows = useMemo(() => {
    const list = Object.values(agents).filter((a) => filter === "all" || a.classification === filter);
    const needle = q.trim().toLowerCase();
    const hit = (a) => !needle || [a.name, a.purpose, a.owner?.name, a.owner?.department, a.surface, ...(a.data_reach || []).map((r) => r.system)]
      .join(" ").toLowerCase().includes(needle);
    // newest discoveries on top: the live agent lands in row 1
    return list.filter(hit).sort((a, b) => (b.first_seen || "").localeCompare(a.first_seen || ""));
  }, [agents, filter, q]);

  return (
    <div className="rounded-xl border border-slate-200 bg-white">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 px-4 py-3">
        <div className="flex gap-1">
          {FILTERS.map(([k, l]) => (
            <button key={k} onClick={() => setFilter(k)}
              className={`rounded-full px-3 py-1 text-[12px] font-medium ${filter === k ? "bg-slate-900 text-white" : "text-slate-600 hover:bg-slate-100"}`}>
              {l}
            </button>
          ))}
        </div>
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search agents, people, systems…"
          className="w-64 rounded-lg border border-slate-200 px-3 py-1.5 text-sm outline-none focus:border-indigo-400" />
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead className="text-[11px] uppercase tracking-wider text-slate-500">
            <tr className="border-b border-slate-100">
              <th className="px-4 py-2 font-medium">Agent</th>
              <th className="px-3 py-2 font-medium">Owner</th>
              <th className="px-3 py-2 font-medium">Surface</th>
              <th className="px-3 py-2 font-medium">Reach</th>
              <th className="px-3 py-2 font-medium">Classification</th>
              <th className="px-3 py-2 font-medium">Risk</th>
              <th className="px-3 py-2 font-medium">Last seen</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((a) => {
              const isNew = firstSeen[a.id] && Date.now() - firstSeen[a.id] < 4000;
              const cl = a.cluster_id && clusterById[a.cluster_id];
              return (
                <tr key={a.id} onClick={() => onOpen(a.id)} data-agent={a.id}
                  className={`cursor-pointer border-b border-slate-50 align-top hover:bg-slate-50 ${isNew ? "row-new" : ""}`}>
                  <td className="max-w-[380px] px-4 py-2.5">
                    <div className="flex items-center gap-2">
                      {a.status === "enriching" || a.status === "provisional"
                        ? <span className="shimmer inline-block h-4 w-40" />
                        : <span className="font-medium text-slate-900">{a.name}</span>}
                      <StatusPill status={a.status} />
                    </div>
                    <div className="mt-0.5 line-clamp-2 text-[12px] text-slate-500">
                      {a.status === "enriching" || a.status === "provisional"
                        ? `${(a.tools || []).length} tools · ${a.members?.map((m) => m.name).join(", ")}`
                        : a.purpose}
                    </div>
                    {cl && (
                      <button onClick={(e) => { e.stopPropagation(); onOpenCluster(cl.id); }}
                        className="mt-1 inline-flex items-center gap-1 rounded-md bg-indigo-50 px-1.5 py-0.5 text-[11px] font-medium text-indigo-700 hover:bg-indigo-100">
                        ⧉ {cl.people} people built this: {cl.label}
                      </button>
                    )}
                  </td>
                  <td className="px-3 py-2.5 whitespace-nowrap">
                    <div className="text-slate-800">{a.owner?.name}</div>
                    <div className="text-[12px] text-slate-500">{a.owner?.department || (a.owner?.in_directory === false ? "this laptop" : "")}</div>
                  </td>
                  <td className="px-3 py-2.5"><Surface value={a.surface} /></td>
                  <td className="px-3 py-2.5">
                    <div className="flex max-w-[260px] flex-wrap gap-1">
                      {(a.data_reach || []).map((r) => <ReachChip key={r.system} r={r} />)}
                      {!a.data_reach?.length && <span className="text-[12px] text-slate-400">–</span>}
                    </div>
                  </td>
                  <td className="px-3 py-2.5"><ClassBadge value={a.classification} /></td>
                  <td className="px-3 py-2.5"><RiskBadge level={a.risk?.level} /></td>
                  <td className="px-3 py-2.5 whitespace-nowrap text-[12px] text-slate-500">{timeAgo(a.last_seen)}</td>
                </tr>
              );
            })}
            {!rows.length && (
              <tr><td colSpan={7} className="px-4 py-16 text-center text-slate-400">No agents yet. Waiting for sensors…</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
