export const CLASS = {
  replace: { label: "Move to Langdock", cls: "bg-indigo-50 text-indigo-700 ring-indigo-200" },
  govern_in_place: { label: "Govern in place", cls: "bg-amber-50 text-amber-800 ring-amber-200" },
  ok: { label: "Sanctioned", cls: "bg-emerald-50 text-emerald-700 ring-emerald-200" },
};
export const RISK = {
  high: "bg-red-50 text-red-700 ring-red-200",
  medium: "bg-orange-50 text-orange-700 ring-orange-200",
  low: "bg-slate-100 text-slate-600 ring-slate-200",
  none: "bg-slate-50 text-slate-400 ring-slate-200",
};
export const SURFACE = {
  cursor: "Cursor", claude_desktop: "Claude Desktop", claude_code: "Claude Code", vscode: "VS Code",
  windsurf: "Windsurf", script: "Script", saas: "SaaS app",
};
const SURFACE_DOT = {
  cursor: "bg-zinc-800", claude_desktop: "bg-orange-500", claude_code: "bg-orange-700", vscode: "bg-sky-600",
  windsurf: "bg-teal-500", script: "bg-slate-500", saas: "bg-fuchsia-500",
};

export function Badge({ className = "", children, title }) {
  return (
    <span title={title} className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset whitespace-nowrap ${className}`}>
      {children}
    </span>
  );
}

export function ClassBadge({ value }) {
  const c = CLASS[value] || { label: value, cls: "bg-slate-100 text-slate-600 ring-slate-200" };
  return <Badge className={c.cls}>{c.label}</Badge>;
}

export function RiskBadge({ level }) {
  return <Badge className={RISK[level] || RISK.none}>{level === "none" ? "no flags" : `${level} risk`}</Badge>;
}

export function Surface({ value }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-[13px] text-slate-700 whitespace-nowrap">
      <span className={`h-2 w-2 rounded-full ${SURFACE_DOT[value] || "bg-slate-400"}`} />
      {SURFACE[value] || value}
    </span>
  );
}

const ACCESS = { r: "read", w: "write", rw: "read/write" };
export function ReachChip({ r, onClick }) {
  const writes = r.access.includes("w");
  return (
    <button type="button" onClick={onClick} title={`${r.system}: ${ACCESS[r.access]}${r.via ? " · " + r.via.join("; ") : ""}`}
      className={`inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[11px] whitespace-nowrap ${writes ? "border-rose-200 bg-rose-50 text-rose-800" : "border-slate-200 bg-white text-slate-700"} ${onClick ? "hover:border-indigo-300" : "cursor-default"}`}>
      {r.system}
      <span className={`font-mono text-[10px] ${writes ? "text-rose-600" : "text-slate-400"}`}>{r.access}</span>
      {r.source === "llm" && <span className="text-[9px] text-indigo-500">inferred</span>}
    </button>
  );
}

export function StatusPill({ status }) {
  if (status === "enriching" || status === "provisional")
    return <span className="inline-flex items-center gap-1 text-[11px] text-indigo-600"><span className="h-1.5 w-1.5 rounded-full bg-indigo-500 live-dot" />analyzing</span>;
  if (status === "needs_review") return <Badge className="bg-yellow-50 text-yellow-800 ring-yellow-200">needs review</Badge>;
  if (status === "rules_only") return <Badge className="bg-slate-50 text-slate-500 ring-slate-200">rules only</Badge>;
  return null;
}

export function Section({ title, right, children }) {
  return (
    <section className="mt-5">
      <div className="mb-2 flex items-center justify-between">
        <h3 className="text-[11px] font-semibold uppercase tracking-wider text-slate-500">{title}</h3>
        {right}
      </div>
      {children}
    </section>
  );
}

export function Shimmer({ lines = 3 }) {
  return <div className="space-y-2">{Array.from({ length: lines }).map((_, i) => <div key={i} className="shimmer h-3.5" style={{ width: `${90 - i * 12}%` }} />)}</div>;
}
