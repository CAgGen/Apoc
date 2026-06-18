import { generations, GenState, useGenerations } from "./genStore";

const PHASE_LABEL: Record<string, string> = {
  queued: "排队中…",
  reconnecting: "重新连接…",
  intake: "解析需求…",
  research: "联网调研…",
  candidates: "生成候选方案…",
  judge: "比较与融合…",
  document: "撰写文档…",
  reviews: "评审中…",
  deck: "生成幻灯片…",
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
            title="关闭"
            className="ml-auto rounded px-1 text-white/40 hover:text-white"
          >
            ✕
          </button>
        )}
      </div>

      <div className="mt-1 pl-[1.1rem] text-xs text-white/55">
        {running && <span className="text-amber-300">生成中 · {phase}</span>}
        {g.status === "done" && <span className="text-emerald-300">生成完成</span>}
        {g.status === "cancelled" && <span className="text-white/50">已中断，项目回到草稿</span>}
        {g.status === "failed" && <span className="text-red-300">{g.error || "生成失败"}</span>}
      </div>

      <div className="mt-2 flex gap-2 pl-[1.1rem]">
        {running ? (
          <button
            onClick={() => generations.cancel(g.projectId)}
            className="rounded-md border border-red-500/40 bg-red-500/10 px-2.5 py-1 text-xs text-red-300 hover:bg-red-500/20"
          >
            停止
          </button>
        ) : g.status === "done" ? (
          <button
            onClick={() => {
              onOpen(g.projectId);
              generations.dismiss(g.projectId);
            }}
            className="rounded-md bg-blue-500 px-2.5 py-1 text-xs font-medium text-white hover:bg-blue-400"
          >
            查看
          </button>
        ) : null}
        <button
          onClick={() => onOpen(g.projectId)}
          className="rounded-md border border-white/12 px-2.5 py-1 text-xs text-white/60 hover:text-white"
        >
          打开项目
        </button>
      </div>
    </div>
  );
}
