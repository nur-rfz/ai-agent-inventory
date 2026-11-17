import { useEffect, useState } from "react";
import { useLive } from "./api.js";
import AgentDrawer from "./components/AgentDrawer.jsx";
import Clusters from "./components/Clusters.jsx";
import Consolidate from "./components/Consolidate.jsx";
import Header from "./components/Header.jsx";
import Inventory from "./components/Inventory.jsx";

export default function App() {
  const { agents, clusters, stats, connected, firstSeen } = useLive();
  const [tab, setTab] = useState("inventory");
  const [agentId, setAgentId] = useState(null);
  const [consolidateId, setConsolidateId] = useState(null);
  const [focusCluster, setFocusCluster] = useState(null);

  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") { setConsolidateId(null); setAgentId(null); } };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const openCluster = (id) => {
    setAgentId(null);
    setTab("clusters");
    setFocusCluster(id);
    setTimeout(() => document.querySelector(`[data-cluster="${id}"]`)?.scrollIntoView({ behavior: "smooth", block: "center" }), 60);
  };

  return (
    <div className="min-h-screen">
      <Header stats={stats} connected={connected} tab={tab} setTab={setTab} clusters={clusters} />
      <main className="mx-auto max-w-[1400px] px-6 py-5">
        {tab === "inventory" ? (
          <Inventory agents={agents} clusters={clusters} firstSeen={firstSeen} onOpen={setAgentId} onOpenCluster={openCluster} />
        ) : (
          <Clusters clusters={clusters} agents={agents} focus={focusCluster} onOpenAgent={setAgentId} onConsolidate={setConsolidateId} />
        )}
      </main>
      {agentId && (
        <AgentDrawer id={agentId} live={agents[agentId]} onClose={() => setAgentId(null)} onOpenCluster={openCluster} />
      )}
      {consolidateId && (
        <Consolidate clusterId={consolidateId} cluster={clusters.find((c) => c.id === consolidateId)} onClose={() => setConsolidateId(null)} />
      )}
    </div>
  );
}
