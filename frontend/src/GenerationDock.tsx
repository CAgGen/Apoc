import { generations, GenState, useGenerations } from "./genStore";

const PHASE_LABEL: Record<string, string> = {
  queued: "Queued...",
  reconnecting: "Reconnecting...",
  intake: "Parsing requirements...",
  research: "Researching online...",
  candidates: "Generating candidate designs...",
  judge: "Comparing and merging...",
  document: "Writing document...",
  reviews: "Reviewing...",
  deck: "Generating slide deck...",
};

/**
 * Floating dock that mirrors every tracked generation. It lives at the App root
 * so generations stay visible and controllable no matter which project/view the
 * user navigates to, and it survives refreshes via the genStore.
 */
export function GenerationDock({ onOpen }: { onOpen: (id: string) => void }) {
  const items = useGenerations();
  if (items.length === 0) return null;

  return (
    <div className="pointer-events-none fixed bottom-4 right-4 z-50 flex w-80 max-w-[90vw] flex-col gap-2">
      {items.map((g) => (
        <Card key={g.projectId} g={g} onOpen={onOpen} />
      ))}
    </div>
  );
}

function Card({ g, onOpen }: { g: GenState; onOpen: (id: string) => void }) {
  const running = g.status === "running";
  const phase = PHASE_LABEL[g.phase] ?? g.phase;

  return (
    <div className="pointer-events-auto rounded-xl border border-white/12 bg-[#161a24] p-3 shadow-xl shadow-black/40">
      <div className="flex items-center gap-2">
        {running ? (
          <span className="h-2.5 w-2.5 shrink-0 animate-pulse rounded-full bg-amber-400" />
        ) : g.status === "done" ? (
          <span className="shrink-0 text-emerald-400">✓</span>
        ) : (
          <span className="shrink-0 text-red-400">✕</span>
        )}
        <span className="truncate text-sm font-medium text-white" title={g.title}>
          {g.title || "Untitled POC"}
        </span>
        {!running && (
          <button
            onClick={() => generations.dismiss(g.projectId)}
            title="Close"
            className="ml-auto rounded px-1 text-white/40 hover:text-white"
          >
            ✕
          </button>
        )}
      </div>

      <div className="mt-1 pl-[1.1rem] text-xs text-white/55">
        {running && <span className="text-amber-300">Generating - {phase}</span>}
        {g.status === "done" && <span className="text-emerald-300">Generation complete</span>}
        {g.status === "cancelled" && <span className="text-white/50">Stopped; project returned to draft</span>}
        {g.status === "failed" && <span className="text-red-300">{g.error || "Generation failed"}</span>}
      </div>

      <div className="mt-2 flex gap-2 pl-[1.1rem]">
        {running ? (
          <button
            onClick={() => generations.cancel(g.projectId)}
            className="rounded-md border border-red-500/40 bg-red-500/10 px-2.5 py-1 text-xs text-red-300 hover:bg-red-500/20"
          >
            Stop
          </button>
        ) : g.status === "done" ? (
          <button
            onClick={() => {
              onOpen(g.projectId);
              generations.dismiss(g.projectId);
            }}
            className="rounded-md bg-blue-500 px-2.5 py-1 text-xs font-medium text-white hover:bg-blue-400"
          >
            View
          </button>
        ) : null}
        <button
          onClick={() => onOpen(g.projectId)}
          className="rounded-md border border-white/12 px-2.5 py-1 text-xs text-white/60 hover:text-white"
        >
          Open project
        </button>
      </div>
    </div>
  );
}
