import { useEffect, useState } from "react";
import { api, Stakeholder } from "./api";
import { Dashboard } from "./Dashboard";
import { ProjectView } from "./ProjectView";
import { GenerationDock } from "./GenerationDock";
import { generations } from "./genStore";

const ROLE_LABEL: Record<string, string> = {
  architect: "Architect",
  compliance: "Compliance",
  security: "Security",
  finops: "FinOps",
  legal: "Legal",
  cto: "CTO",
  client_sponsor: "Sponsor",
  consultant: "Consultant",
};

const ROLE_KEY = "apoc_role_id";

function getHashProject(): string | null {
  const m = window.location.hash.match(/^#\/project\/(.+)$/);
  return m ? m[1] : null;
}

function setHashProject(id: string | null) {
  window.location.hash = id ? `#/project/${id}` : "";
}

export function App() {
  const [stakeholders, setStakeholders] = useState<Stakeholder[]>([]);
  const [me, setMe] = useState<Stakeholder | null>(null);
  const [projectId, setProjectId] = useState<string | null>(getHashProject);

  useEffect(() => {
    api.stakeholders().then((s) => {
      setStakeholders(s);
      const savedId = localStorage.getItem(ROLE_KEY);
      const saved = s.find((x) => x.id === savedId);
      setMe(saved ?? s[0] ?? null);
    });
  }, []);

  // After a refresh, reconnect to any generation still running on the server.
  useEffect(() => {
    void generations.init();
  }, []);

  // Sync hash → state (handles browser back/forward)
  useEffect(() => {
    const onHash = () => setProjectId(getHashProject());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  function openProject(id: string) {
    setHashProject(id);
    setProjectId(id);
  }

  function closeProject() {
    setHashProject(null);
    setProjectId(null);
  }

  function changeRole(id: string) {
    const s = stakeholders.find((x) => x.id === id) ?? null;
    setMe(s);
    if (s) localStorage.setItem(ROLE_KEY, s.id);
  }

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center gap-4 border-b border-white/10 bg-[#11131a] px-5 py-3">
        <button
          onClick={closeProject}
          className="text-lg font-semibold tracking-tight text-white"
        >
          APoc <span className="text-white/40 text-sm font-normal">· architecture POC workspace</span>
        </button>
        <div className="ml-auto flex items-center gap-2 text-sm">
          <span className="text-white/40">acting as</span>
          <select
            value={me?.id ?? ""}
            onChange={(e) => changeRole(e.target.value)}
            className="rounded-lg border border-white/15 bg-[#1b1e29] px-3 py-1.5 text-white"
          >
            {stakeholders.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name} — {ROLE_LABEL[s.role] ?? s.role}
              </option>
            ))}
          </select>
          {me?.role === "architect" && (
            <span className="rounded-full bg-blue-500/15 px-2.5 py-1 text-xs text-blue-300">
              can edit POC
            </span>
          )}
        </div>
      </header>

      <main className="min-h-0 flex-1 overflow-hidden">
        {me && !projectId && <Dashboard me={me} onOpen={openProject} />}
        {me && projectId && (
          <ProjectView projectId={projectId} me={me} onBack={closeProject} />
        )}
      </main>

      <GenerationDock onOpen={openProject} />
    </div>
  );
}
