import { useEffect, useRef, useState } from "react";

export async function api(path, opts = {}) {
  const r = await fetch(`/api${path}`, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(data.detail || r.statusText), { status: r.status, data });
  return data;
}

/** Live inventory: initial fetch + Server-Sent Events. */
export function useLive() {
  const [agents, setAgents] = useState({});
  const [clusters, setClusters] = useState([]);
  const [stats, setStats] = useState(null);
  const [connected, setConnected] = useState(false);
  const [lastEvent, setLastEvent] = useState(null);
  const firstSeen = useRef({}); // id -> ms when the UI first saw it (for row animation)

  useEffect(() => {
    let es;
    const load = async () => {
      const [a, c, s] = await Promise.all([api("/agents"), api("/clusters"), api("/stats")]);
      const map = {};
      a.forEach((x) => { map[x.id] = x; firstSeen.current[x.id] ??= 0; });
      setAgents(map); setClusters(c); setStats(s);
    };
    load().catch(() => {});
    es = new EventSource("/api/stream");
    es.addEventListener("hello", () => { setConnected(true); load().catch(() => {}); });
    es.addEventListener("agent", (e) => {
      const a = JSON.parse(e.data);
      if (!(a.id in firstSeen.current)) firstSeen.current[a.id] = Date.now();
      setAgents((m) => ({ ...m, [a.id]: a }));
      setLastEvent(Date.now());
    });
    es.addEventListener("agent_removed", (e) => {
      const { id } = JSON.parse(e.data);
      setAgents((m) => { const n = { ...m }; delete n[id]; return n; });
    });
    es.addEventListener("clusters", (e) => setClusters(JSON.parse(e.data)));
    es.addEventListener("stats", (e) => setStats(JSON.parse(e.data)));
    es.addEventListener("reset", () => { setAgents({}); setClusters([]); firstSeen.current = {}; });
    es.onerror = () => setConnected(false);
    return () => es.close();
  }, []);

  return { agents, clusters, stats, connected, lastEvent, firstSeen: firstSeen.current };
}

export function timeAgo(iso) {
  if (!iso) return "–";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}
