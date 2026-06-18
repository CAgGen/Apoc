import { type ChangeEvent, type MouseEvent, useEffect, useRef, useState } from "react";
import { api, IntakeMessage, IntakeTurn, Project, Stakeholder } from "./api";
import { generations, useGenerations } from "./genStore";

// Re-exported for back-compat (tests import it from here); owned by genStore.
export { GENERATION_STALL_MS } from "./genStore";

const STATUS_STYLE: Record<string, string> = {
  draft: "bg-white/10 text-white/60",
  generating: "bg-amber-500/15 text-amber-300",
  in_review: "bg-blue-500/15 text-blue-300",
  ready_to_align: "bg-emerald-500/15 text-emerald-300",
  failed: "bg-red-500/15 text-red-300",
};

const BRIEF_FIELDS: { key: string; label: string; ph: string }[] = [
  { key: "business_goal", label: "Business goal", ph: "What is the system for? What outcome matters?" },
  { key: "scale", label: "Scale / load", ph: "Users, traffic, data volume, growth" },
  { key: "availability", label: "Availability / performance", ph: "SLA, latency, RTO/RPO" },
  { key: "compliance", label: "Compliance / data residency", ph: "GDPR, HIPAA, region constraints" },
  { key: "cloud", label: "Cloud / platform preference", ph: "AWS / Azure / GCP, existing stack" },
  { key: "budget_sensitivity", label: "Budget sensitivity", ph: "Cost ceiling or sensitivity" },
  { key: "timeline", label: "Timeline", ph: "When is the POC / delivery needed?" },
  { key: "constraints", label: "Other constraints", ph: "Banned tech, mandated standards, etc." },
];

export function formatPdfExtractionMeta(meta: Record<string, unknown> | undefined): string {
  if (!meta || meta.source_type !== "uploaded_pdf") return "";
  const pageCount = Number(meta.page_count ?? 0);
  const charsUsed = Number(meta.chars_used ?? 0);
  const state = meta.truncated ? "truncated" : "complete";
  const pages = pageCount === 1 ? "1 page" : `${pageCount.toLocaleString()} pages`;
  return `PDF: ${pages} | ${charsUsed.toLocaleString()} chars used | ${state}`;
}

export function Dashboard({ me, onOpen }: { me: Stakeholder; onOpen: (id: string) => void }) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState<string | null>(null);

  const load = () => api.projects().then(setProjects);
  useEffect(() => {
    load();
  }, []);

  // Refresh the list whenever a tracked generation settles, so a project's
  // status chip updates even if it finished while the user was elsewhere.
  const gens = useGenerations();
  const settledCount = gens.filter((g) => g.status !== "running").length;
  useEffect(() => {
    if (settledCount > 0) load();
  }, [settledCount]);

  async function handleDelete(e: MouseEvent, id: string, title: string) {
    e.stopPropagation();
    if (!window.confirm(`删除项目「${title}」？此操作不可撤销。`)) return;
    setDeleting(id);
    try {
      await api.deleteProject(id);
      await load();
    } finally {
      setDeleting(null);
    }
  }

  return (
    <div className="mx-auto flex h-full max-w-6xl gap-6 overflow-y-auto p-6">
      <section className="flex-1">
        <div className="mb-4 flex items-center justify-between">
          <h1 className="text-xl font-semibold">POC projects</h1>
          <button
            onClick={() => setCreating((v) => !v)}
            className="rounded-lg bg-blue-500 px-3.5 py-2 text-sm font-medium text-white hover:bg-blue-400"
          >
            {creating ? "Close" : "+ New POC"}
          </button>
        </div>

        {creating && (
          <NewProject
            onCreated={(id) => {
              setCreating(false);
              load();
              onOpen(id);
            }}
          />
        )}

        <div className="grid gap-3">
          {projects.length === 0 && !creating && (
            <p className="text-white/40">No projects yet. Create a POC to get started.</p>
          )}
          {projects.map((p) => (
            <div key={p.id} className="group relative">
              <button
                onClick={() => onOpen(p.id)}
                className="w-full rounded-xl border border-white/10 bg-[#141722] p-4 text-left hover:border-white/25"
              >
                <div className="flex items-center justify-between pr-7">
                  <span className="font-medium text-white">{p.title}</span>
                  <span className={`rounded-full px-2.5 py-0.5 text-xs ${STATUS_STYLE[p.status] ?? "bg-white/10"}`}>
                    {p.status.replace(/_/g, " ")}
                  </span>
                </div>
                <div className="mt-1 text-sm text-white/45">
                  {p.client_name || "—"} {p.consulting_org ? `· ${p.consulting_org}` : ""}
                </div>
              </button>
              <button
                onClick={(e) => handleDelete(e, p.id, p.title)}
                disabled={deleting === p.id}
                title="删除项目"
                className="absolute right-3 top-1/2 -translate-y-1/2 rounded p-1 text-white/0 transition hover:text-red-400 group-hover:text-white/30 disabled:opacity-40"
              >
                {deleting === p.id ? "…" : "✕"}
              </button>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}

const STYLE = `.input{background:#1b1e29;border:1px solid rgba(255,255,255,.12);border-radius:.5rem;padding:.5rem .7rem;color:#fff;}`;

type Brief = Record<string, string>;

interface ConfirmDraft {
  title: string;
  client: string;
  org: string;
  brief: Brief;
  requirementsDetail: string;
  sourceProvenance: Record<string, unknown>;
}

function NewProject({ onCreated }: { onCreated: (id: string) => void }) {
  const [messages, setMessages] = useState<IntakeMessage[]>([]);
  const [turn, setTurn] = useState<IntakeTurn | null>(null);
  const [thinking, setThinking] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState<ConfirmDraft | null>(null);
  const pending = useRef<IntakeMessage[]>([]);
  const started = useRef(false);

  // Kick off the conversation with an empty history — the agent greets + asks Q1.
  useEffect(() => {
    if (started.current) return;
    started.current = true;
    void send([]);
  }, []);

  async function send(history: IntakeMessage[]) {
    pending.current = history;
    setMessages(history); // optimistically show the user's just-added turn
    setThinking(true);
    setError(null);
    setTurn(null);
    try {
      const t = await api.intakeChat(history);
      setMessages([...history, { role: "assistant", content: t.message }]);
      if (t.done) {
        const brief: Brief = {};
        for (const f of BRIEF_FIELDS) brief[f.key] = t.brief?.[f.key] ?? "";
        setDraft({
          title: t.title ?? "",
          client: t.client_name ?? "",
          org: t.consulting_org ?? "",
          brief,
          requirementsDetail: t.requirements_detail ?? "",
          sourceProvenance: { source_type: "guided_chat" },
        });
      } else {
        setTurn(t);
      }
    } catch (e: any) {
      setError(e?.message || "Something went wrong");
    } finally {
      setThinking(false);
    }
  }

  function answer(text: string) {
    const v = text.trim();
    if (!v) return;
    void send([...messages, { role: "user", content: v }]);
  }

  async function onPdfSelected(e: ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    setUploading(true);
    setError(null);
    try {
      const r = await api.extractIntakePdf(file);
      const brief: Brief = {};
      for (const f of BRIEF_FIELDS) brief[f.key] = r.brief?.[f.key] ?? "";
      setDraft({
        title: r.title ?? "",
        client: r.client_name ?? "",
        org: r.consulting_org ?? "",
        brief,
        requirementsDetail: r.requirements_detail ?? "",
        sourceProvenance: r.extraction_meta ?? { source_type: "uploaded_pdf" },
      });
    } catch (err: any) {
      setError(err?.message || "Could not extract this PDF.");
    } finally {
      setUploading(false);
    }
  }

  if (draft) {
    return <ConfirmPanel messages={messages} draft={draft} setDraft={setDraft} onCreated={onCreated} />;
  }

  return (
    <div className="mb-5 rounded-xl border border-white/10 bg-[#141722] p-4">
      <div className="mb-3 flex items-center justify-between gap-3">
        <p className="text-xs uppercase tracking-wide text-white/40">Guided intake</p>
        <label className="cursor-pointer text-xs text-blue-300 hover:text-blue-200">
          <input
            type="file"
            accept="application/pdf,.pdf"
            className="peer sr-only"
            disabled={uploading}
            onChange={onPdfSelected}
          />
          <span className="rounded px-1 py-0.5 peer-focus-visible:outline peer-focus-visible:outline-2 peer-focus-visible:outline-blue-300 peer-disabled:cursor-not-allowed peer-disabled:opacity-50">
            {uploading ? "Extracting..." : "Upload requirements PDF"}
          </span>
        </label>
      </div>
      <ChatLog messages={messages} thinking={thinking} />

      {error && (
        <div className="mt-3 flex items-center gap-3 rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-200">
          <span>{error}</span>
          <button
            onClick={() => send(pending.current)}
            className="rounded bg-red-500/30 px-2 py-1 text-xs hover:bg-red-500/40"
          >
            Retry
          </button>
        </div>
      )}

      {turn && !thinking && (
        <Answerer turn={turn} onAnswer={answer} />
      )}

      <style>{STYLE}</style>
    </div>
  );
}

function ChatLog({ messages, thinking }: { messages: IntakeMessage[]; thinking: boolean }) {
  const endRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages.length, thinking]);

  return (
    <div className="max-h-[46vh] space-y-3 overflow-y-auto pr-1">
      {messages.map((m, i) =>
        m.role === "user" ? (
          <div key={i} className="ml-auto max-w-[75%] rounded-2xl rounded-br-sm bg-blue-500 px-3.5 py-2 text-sm text-white">
            {m.content}
          </div>
        ) : (
          <div
            key={i}
            className="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-bl-sm border border-white/10 bg-[#1b1e29] px-3.5 py-2 text-sm text-white/90"
          >
            {m.content}
          </div>
        )
      )}
      {thinking && (
        <div className="max-w-[85%] rounded-2xl rounded-bl-sm border border-white/10 bg-[#1b1e29] px-3.5 py-2 text-sm text-white/40">
          <span className="inline-flex gap-1">
            <Dot /> <Dot d={0.15} /> <Dot d={0.3} />
          </span>
        </div>
      )}
      <div ref={endRef} />
    </div>
  );
}

function Dot({ d = 0 }: { d?: number }) {
  return (
    <span
      className="inline-block h-1.5 w-1.5 animate-bounce rounded-full bg-white/40"
      style={{ animationDelay: `${d}s` }}
    />
  );
}

function Answerer({ turn, onAnswer }: { turn: IntakeTurn; onAnswer: (t: string) => void }) {
  const [text, setText] = useState("");
  return (
    <div className="mt-3">
      {turn.options.length > 0 && (
        <div className="mb-3 grid gap-2 sm:grid-cols-2">
          {turn.options.map((o, i) => (
            <button
              key={i}
              onClick={() => onAnswer(o.label)}
              className="rounded-lg border border-white/15 bg-[#1b1e29] p-3 text-left transition hover:border-blue-400/60 hover:bg-[#1f2433]"
            >
              <div className="text-sm font-medium text-white">{o.label}</div>
              {o.advantage && <div className="mt-0.5 text-xs text-white/55">{o.advantage}</div>}
            </button>
          ))}
        </div>
      )}
      {turn.allow_free_text && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            onAnswer(text);
            setText("");
          }}
          className="flex gap-2"
        >
          <input
            autoFocus
            className="input flex-1"
            placeholder={turn.options.length ? "…or type your own answer / “recommend one”" : "Type your answer…"}
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
          <button
            type="submit"
            disabled={!text.trim()}
            className="rounded-lg bg-blue-500 px-4 text-sm font-medium text-white hover:bg-blue-400 disabled:opacity-40"
          >
            Send
          </button>
        </form>
      )}
    </div>
  );
}

function ConfirmPanel({
  messages,
  draft,
  setDraft,
  onCreated,
}: {
  messages: IntakeMessage[];
  draft: ConfirmDraft;
  setDraft: (d: ConfirmDraft | null) => void;
  onCreated: (id: string) => void;
}) {
  const [startError, setStartError] = useState<string | null>(null);
  const [startedId, setStartedId] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);

  // The live generation state lives in the module-level store (genStore), so it
  // survives this panel being unmounted when the user opens another project.
  const gens = useGenerations();
  const gen = startedId ? gens.find((g) => g.projectId === startedId) : undefined;
  const busy = starting || gen?.status === "running";
  const cancelled = gen?.status === "cancelled";
  const error = startError ?? (gen?.status === "failed" ? gen.error ?? "Generation failed." : null);
  const log = gen?.log ?? [];

  // Once the started generation completes, fold this form away and open the POC
  // — but only while the user is still here; if they navigated off, this panel
  // is unmounted and the floating dock carries the result instead.
  const onCreatedRef = useRef(onCreated);
  onCreatedRef.current = onCreated;
  const navigatedRef = useRef(false);
  useEffect(() => {
    if (gen?.status === "done" && !navigatedRef.current) {
      navigatedRef.current = true;
      onCreatedRef.current(gen.projectId);
    }
  }, [gen?.status, gen?.projectId]);

  function handleCancel() {
    if (startedId) generations.cancel(startedId);
  }

  const generate = async () => {
    if (!draft.title.trim()) return;
    setStartError(null);
    setStarting(true);
    navigatedRef.current = false;

    let id: string;
    try {
      ({ id } = await api.createProject({
        title: draft.title,
        client_name: draft.client,
        consulting_org: draft.org,
        brief: draft.brief,
        intake_chat: messages,
        requirements_detail: draft.requirementsDetail,
        source_provenance: draft.sourceProvenance,
      }));
      await api.generate(id);
    } catch (e: any) {
      setStartError(e?.message || "Could not start generation — is the backend running?");
      setStarting(false);
      return;
    }

    generations.start(id, draft.title);
    setStartedId(id);
    setStarting(false);
  };

  const set = (patch: Partial<ConfirmDraft>) => setDraft({ ...draft, ...patch });
  const setBrief = (key: string, value: string) => setDraft({ ...draft, brief: { ...draft.brief, [key]: value } });
  const pdfMeta = formatPdfExtractionMeta(draft.sourceProvenance);

  return (
    <div className="mb-5 rounded-xl border border-white/10 bg-[#141722] p-4">
      <div className="mb-3 flex items-center justify-between">
        <p className="text-xs uppercase tracking-wide text-white/40">Review &amp; confirm</p>
        {!busy && (
          <button onClick={() => setDraft(null)} className="text-xs text-white/45 hover:text-white/80">
            ← Back to chat
          </button>
        )}
      </div>
      <p className="mb-3 text-sm text-white/55">
        Here's what I gathered. Edit anything, then generate the POC.
      </p>

      <div className="grid grid-cols-3 gap-3">
        <input className="input" placeholder="POC title *" value={draft.title} onChange={(e) => set({ title: e.target.value })} />
        <input className="input" placeholder="Client company" value={draft.client} onChange={(e) => set({ client: e.target.value })} />
        <input className="input" placeholder="Consulting org" value={draft.org} onChange={(e) => set({ org: e.target.value })} />
      </div>
      <div className="mt-3 grid grid-cols-2 gap-3">
        {BRIEF_FIELDS.map((f) => (
          <label key={f.key} className="text-xs text-white/50">
            <span className="flex items-center gap-2">
              {f.label}
              {!draft.brief[f.key]?.trim() && (
                <span className="rounded bg-amber-500/15 px-1.5 text-[10px] text-amber-300">待补</span>
              )}
            </span>
            <textarea
              className="input mt-1 h-16 w-full resize-none text-sm"
              placeholder={f.ph}
              value={draft.brief[f.key] ?? ""}
              onChange={(e) => setBrief(f.key, e.target.value)}
            />
          </label>
        ))}
      </div>
      {pdfMeta && (
        <p className="mt-2 text-xs tabular-nums text-white/45">{pdfMeta}</p>
      )}
      {(draft.requirementsDetail || draft.sourceProvenance?.source_type === "uploaded_pdf") && (
        <details className="mt-3 text-xs text-white/50">
          <summary className="cursor-pointer">Detailed requirements</summary>
          <textarea
            className="input mt-2 h-40 w-full resize-none text-sm"
            placeholder="Full requirement detail..."
            value={draft.requirementsDetail}
            onChange={(e) => set({ requirementsDetail: e.target.value })}
          />
        </details>
      )}

      <div className="mt-3 flex items-center gap-3">
        <button
          disabled={busy || !draft.title.trim()}
          onClick={generate}
          className="rounded-lg bg-blue-500 px-4 py-2 text-sm font-medium text-white hover:bg-blue-400 disabled:opacity-40"
        >
          {busy ? "Generating…" : "Generate POC"}
        </button>
        {busy && (
          <>
            <span className="text-sm text-amber-300">{log[log.length - 1]}</span>
            <button
              onClick={handleCancel}
              className="rounded-lg border border-red-500/40 bg-red-500/10 px-3 py-1.5 text-sm text-red-300 hover:bg-red-500/20"
            >
              Stop
            </button>
          </>
        )}
      </div>
      {cancelled && (
        <div className="mt-3 rounded-lg border border-white/15 bg-white/5 p-3 text-sm text-white/60">
          生成已中断，项目已回到草稿状态，可重新生成。
        </div>
      )}
      {error && (
        <div className="mt-3 rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-200">
          {error}
        </div>
      )}
      {log.length > 1 && (
        <pre className="mt-3 max-h-40 overflow-y-auto rounded-lg bg-black/30 p-3 text-xs text-white/55">
          {log.join("\n")}
        </pre>
      )}
      <style>{STYLE}</style>
    </div>
  );
}
