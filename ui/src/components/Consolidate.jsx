import { marked } from "marked";
import { useEffect, useState } from "react";
import { api } from "../api.js";
import { Badge, Section, Shimmer, Surface } from "./ui.jsx";

const FREQ = { WEEKDAYS: "Mon–Fri", DAILY: "Daily", WEEKLY: "Weekly", MONTHLY: "Monthly", SELECTED_WEEKDAYS: "Selected days", MANUAL: "Manual" };
const DAY = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

export default function Consolidate({ clusterId, cluster, onClose }) {
  const [d, setD] = useState(null);
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const [showReq, setShowReq] = useState(false);

  const load = () => api(`/clusters/${clusterId}/draft`).then(setD).catch((e) => setErr(e.message));
  useEffect(() => { load(); }, [clusterId, cluster?.draft?.written_by, cluster?.draft?.status, cluster?.member_agent_ids?.length]);
  useEffect(() => { // poll while the LLM writes the instructions
    if (!d?.writing && d?.written_by === "llm") return;
    const t = setInterval(load, 1500);
    return () => clearInterval(t);
  }, [d?.writing, d?.written_by]);

  const approve = async () => {
    setBusy(true); setErr(null);
    try { setD(await api(`/clusters/${clusterId}/approve`, { method: "POST", body: { approved_by: "IT admin" } })); }
    catch (e) { setErr(e.message); } finally { setBusy(false); }
  };
  const create = async () => {
    setBusy(true); setErr(null);
    try { setResult(await api(`/clusters/${clusterId}/create`, { method: "POST" })); load(); }
    catch (e) { setErr(e.message); } finally { setBusy(false); }
  };

  const writing = !d || d.writing || d.written_by !== "llm";
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-slate-900/30 p-6" onClick={onClose}>
      <div onClick={(e) => e.stopPropagation()} className="w-full max-w-[1100px] rounded-2xl bg-white shadow-2xl">
        <div className="flex items-start justify-between gap-4 border-b border-slate-100 px-7 py-5">
          <div>
            <div className="text-[12px] font-semibold uppercase tracking-wider text-indigo-600">Consolidated Langdock agent · preview</div>
            {d ? (
              <h2 className="mt-1 text-2xl font-semibold">{d.emoji} {d.name}</h2>
            ) : <span className="shimmer mt-2 inline-block h-7 w-72" />}
            {d && <p className="mt-1 max-w-3xl text-[14px] text-slate-600">{d.description}</p>}
          </div>
          <button onClick={onClose} className="rounded-lg px-2 py-1 text-slate-400 hover:bg-slate-100" aria-label="close">✕</button>
        </div>

        {d && (
          <div className="grid grid-cols-1 gap-x-8 px-7 pb-7 lg:grid-cols-[1.35fr_1fr]">
            <div>
              <div className="mt-5 rounded-xl bg-indigo-600 px-5 py-4 text-white">
                <div className="text-3xl font-semibold">{d.replaces_count} → 1</div>
                <div className="text-[13px] text-indigo-100">replaces {d.replaces_count} agents built by {d.people} people with one shared, governed agent</div>
              </div>

              <Section title="Replaces (base + variants)">
                <ul className="divide-y divide-slate-100 rounded-lg border border-slate-100">
                  {d.variants.map((v) => (
                    <li key={v.agent_id} className="flex items-start gap-3 px-3 py-2 text-[13px]">
                      <div className="w-36 shrink-0"><div className="font-medium">{v.owner}</div><div className="text-[11px] text-slate-500">{v.department || "this laptop"}</div></div>
                      <div className="flex-1"><div className="flex flex-wrap items-center gap-2 text-slate-700">{v.replaces}<Surface value={v.surface} /></div><div className="text-[12px] text-slate-500">{v.diff}</div></div>
                    </li>
                  ))}
                </ul>
              </Section>

              <Section title="Instructions" right={writing && <span className="text-[12px] text-indigo-600">writing…</span>}>
                {writing ? <Shimmer lines={8} /> : (
                  <div className="prose-ld max-h-[460px] overflow-y-auto rounded-lg border border-slate-100 bg-slate-50/50 px-4 py-2 text-[13px] text-slate-700"
                    dangerouslySetInnerHTML={{ __html: marked.parse(d.instruction) }} />
                )}
              </Section>

              {d.conversation_starters?.length > 0 && (
                <Section title="Conversation starters">
                  <div className="flex flex-wrap gap-1.5">{d.conversation_starters.map((c) => <Badge key={c} className="bg-white text-slate-700 ring-slate-200">{c}</Badge>)}</div>
                </Section>
              )}
            </div>

            <div>
              <Section title="Integrations">
                <ul className="space-y-1.5">
                  {d.integrations.map((i) => (
                    <li key={i.langdock_integration} className="flex items-center justify-between rounded-lg border border-slate-100 px-3 py-1.5 text-[13px]">
                      <span className="font-medium">{i.langdock_integration}</span>
                      <span className="text-[12px] text-slate-500">{i.access} · {i.used_by.length} {i.used_by.length === 1 ? "team" : "teams"}</span>
                    </li>
                  ))}
                </ul>
              </Section>

              <Section title="Schedules → Scheduled Tasks">
                {d.schedules.length ? d.schedules.map((s) => (
                  <div key={s.agent_id + s.cron} className="text-[13px] text-slate-700">
                    <Badge className="mr-1 bg-amber-50 text-amber-800 ring-amber-200">{FREQ[s.frequency]}{s.dayOfWeek != null ? ` ${DAY[s.dayOfWeek]}` : ""} {s.timeOfDay}</Badge>
                    {s.department || s.owner} <span className="text-[11px] text-slate-400">(cron {s.cron}, {s.timezone})</span>
                  </div>
                )) : <p className="text-[13px] text-slate-400">No schedules: used interactively.</p>}
              </Section>

              {d.knowledge?.length > 0 && (
                <Section title="Knowledge folders (suggested)">
                  <ul className="space-y-1 text-[13px] text-slate-700">{d.knowledge.map((k) => <li key={k.name}><span className="font-medium">{k.name}</span>: {k.contents}</li>)}</ul>
                </Section>
              )}

              <Section title="Decommission after rollout">
                <ul className="max-h-48 space-y-1 overflow-y-auto text-[12px] text-slate-600">
                  {d.decommission.map((s, i) => <li key={i}>☐ <span className="font-medium text-slate-700">{s.owner}:</span> {s.step}</li>)}
                </ul>
              </Section>

              <Section title="What will be created" right={<button className="text-[12px] text-indigo-600 hover:underline" onClick={() => setShowReq(!showReq)}>{showReq ? "hide" : "show"} API requests</button>}>
                <ul className="space-y-1 text-[13px]">
                  {d.api_requests.map((r) => (
                    <li key={r.step} className="flex items-center gap-2"><span className="rounded bg-slate-900 px-1.5 py-0.5 font-mono text-[10px] text-white">{r.method}</span><span className="font-mono text-[12px] text-slate-700">{r.url.replace(/^https?:\/\/[^/]+/, "")}</span><span className="text-[11px] text-slate-500">{r.step}</span></li>
                  ))}
                </ul>
                {showReq && <pre className="mt-2 max-h-72 overflow-auto rounded-lg bg-slate-900 p-3 text-[11px] text-slate-100">{JSON.stringify(d.api_requests, null, 2)}</pre>}
                <p className="mt-2 text-[12px] text-slate-500">Plus {d.manual_steps.length} admin steps the API can't do (connect integrations via OAuth, share with teams).</p>
              </Section>

              <div className="mt-6 rounded-xl border border-slate-200 p-4">
                <div className="flex items-center justify-between">
                  <div className="text-[13px]">
                    Status: <span className="font-semibold">{d.status}</span>
                    {d.approved_by && <span className="text-slate-500"> · approved by {d.approved_by}</span>}
                  </div>
                  <div className="flex gap-2">
                    <a className="rounded-lg border border-slate-200 px-3 py-1.5 text-[13px] hover:bg-slate-50" href={`/api/clusters/${clusterId}/export.json`}>JSON</a>
                    <a className="rounded-lg border border-slate-200 px-3 py-1.5 text-[13px] hover:bg-slate-50" href={`/api/clusters/${clusterId}/export.md`}>Spec</a>
                  </div>
                </div>
                <div className="mt-3 flex gap-2">
                  <button disabled={busy || writing || d.status === "approved" || d.status === "created"} onClick={approve}
                    className="flex-1 rounded-lg bg-emerald-600 px-4 py-2 text-sm font-medium text-white hover:bg-emerald-700 disabled:bg-slate-300">
                    {d.status === "approved" || d.status === "created" ? "✓ Approved" : "Approve"}
                  </button>
                  <button disabled={busy || !(d.status === "approved" || d.status === "failed")} onClick={create}
                    className="flex-1 rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700 disabled:bg-slate-300">
                    Create in Langdock
                  </button>
                </div>
                {err && <p className="mt-2 text-[12px] text-red-600">{err}</p>}
                {result && (
                  <div className="mt-3 rounded-lg bg-slate-50 p-3 text-[12px] text-slate-700">
                    {result.status === "dry_run" ? (
                      <><span className="font-semibold">Dry run:</span> {result.reason} {result.requests.length} requests listed above. Set LANGDOCK_API_KEY on the backend to send them.</>
                    ) : (
                      <><span className="font-semibold">{result.status}</span>{result.langdock_agent_id && <> · Langdock agent {result.langdock_agent_id}</>}
                        <ul className="mt-1">{result.log.map((e) => <li key={e.step}>{e.step}: HTTP {e.status}</li>)}</ul></>
                    )}
                  </div>
                )}
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
