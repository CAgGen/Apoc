"""Node functions for the fusion generation graph.

Each node takes PocState and returns a partial-state dict. LLM access goes
through app.llm (provider chosen per model); artifacts through ArtifactStore.
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from .. import cancel, config, db, llm, models as _models, progress, prompts, research
from ..artifacts import ArtifactStore

logger = logging.getLogger(__name__)

# Candidate ids: index 0 -> "A", 1 -> "B".
_CAND_IDS = ["A", "B", "C", "D"]


def _deepseek_reasoning_kwargs(
    model: str, thinking: str = "enabled", effort: str = "max"
) -> dict[str, str]:
    """Reasoning knobs for a DeepSeek call, tuned per task.

    Deep reasoning earns its latency only where design quality is decided —
    candidates and (Anthropic) judge. Downstream nodes transform an already-settled
    canonical design, so they don't need ``max``: document sections use ``medium``
    and the deck (pure text->slides reformatting) disables thinking entirely.
    Returns ``{}`` for non-DeepSeek models (their thinking/effort live elsewhere).
    """
    if _models.provider_for_model(model) != "deepseek":
        return {}
    return {"deepseek_thinking": thinking, "effort": effort}


def _store(state: dict[str, Any]) -> ArtifactStore:
    return ArtifactStore(config.RUNS_DIR, state["run_id"])


def _audit(state: dict[str, Any], action: str, **detail: Any) -> None:
    """Record one fusion-pipeline step to the audit trail (Trace tab).

    Best-effort: a logging failure here must never abort generation.
    """
    project_id = state.get("project_id", "")
    if not project_id:
        return
    try:
        with db.connect() as conn:
            db.record_audit(conn, action=action, project_id=project_id, detail=detail or {})
    except Exception:  # pragma: no cover - audit is non-critical
        logger.warning("audit record failed for %s", action, exc_info=True)


def research_node(state: dict[str, Any]) -> dict[str, Any]:
    cancel.raise_if_cancelled(state.get("project_id", ""))
    progress.publish(state.get("project_id", ""), "researching",
                     message="Researching current best practices (web grounding)")
    store = _store(state) if state.get("run_id") else None

    def raw_sink(name: str, raw_text: str, meta: dict[str, Any]) -> None:
        if not store:
            return
        store.write_text(f"{name}.raw", raw_text)
        store.write_json(f"{name}.meta", {**meta, "raw_chars": len(raw_text), "parsed": True})

    digest, sources = research.run_research(
        state["brief_text"], state.get("title", ""), model=config.RESEARCH_MODEL_FUSION,
        raw_sink=raw_sink,
    )
    if store:
        store.write_text("research.raw", digest)
        store.write_json("research.meta", {
            "model": config.RESEARCH_MODEL_FUSION,
            "json_mode": False,
            "tool_loop": False,
            "web_search": bool(config.ANTHROPIC_NATIVE_SEARCH),
            "raw_chars": len(digest),
            "source_count": len(sources),
            "parsed": True,
        })
        logger.info(
            "research raw response saved",
            extra={
                "project_id": state.get("project_id", ""),
                "run_id": state.get("run_id", ""),
                "model": config.RESEARCH_MODEL_FUSION,
                "raw_path": str(store.dir / "research.raw.txt"),
                "raw_chars": len(digest),
            },
        )
    # Persist the research grounding so the Trace tab can show the digest and its
    # cited sources. Inserted with poc_id NULL; persist_node links it to the POC
    # once the POC row exists (mirrors the legacy generation path).
    project_id = state.get("project_id", "")
    if project_id and (digest or sources):
        try:
            with db.connect() as conn:
                conn.execute(
                    "INSERT INTO research_notes (id, project_id, poc_id, topic, digest, citations_json, created_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (db.new_id("rn_"), project_id, None, state.get("title", ""),
                     digest, json.dumps(sources), db.now()),
                )
                db.record_audit(conn, action="research.completed", project_id=project_id,
                                detail={"sources": len(sources), "digest_chars": len(digest)})
        except Exception:  # pragma: no cover - persistence is best-effort
            logger.warning("research note persistence failed", exc_info=True)
    return {"digest": digest, "sources": sources}


def make_candidate_node(index: int, model: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
    cand_id = _CAND_IDS[index]

    def candidate_node(state: dict[str, Any]) -> dict[str, Any]:
        cancel.raise_if_cancelled(state.get("project_id", ""))
        progress.publish(state.get("project_id", ""), "designing",
                         message=f"Drafting candidate {cand_id} ({model})")
        user = (
            f"{state['brief_text']}\n\n--- Research digest ---\n{state.get('digest', '')}"
        )
        store = _store(state)
        log_extra = {
            "project_id": state.get("project_id", ""),
            "run_id": state.get("run_id", ""),
            "candidate_id": cand_id,
            "model": model,
        }
        logger.info(
            "candidate generation started",
            extra={**log_extra, "max_tokens": 16000, "json_mode": True},
        )
        raw_text, sources = llm.run_text(
            system=prompts.candidate_system_for_model(model), user=user, model=model,
            max_tokens=16000, json_mode=True, **_deepseek_reasoning_kwargs(model),
        )
        store.write_text(f"candidate_{cand_id}.raw", raw_text)
        raw_path = str(store.dir / f"candidate_{cand_id}.raw.txt")
        logger.info(
            "candidate raw response saved",
            extra={**log_extra, "raw_path": raw_path, "raw_chars": len(raw_text)},
        )
        meta = {
            "id": cand_id,
            "model": model,
            "json_mode": True,
            "tool_loop": False,
            "web_search": False,
            "raw_chars": len(raw_text),
            "source_count": len(sources),
        }
        try:
            design = llm.extract_json(raw_text)
        except Exception as exc:
            store.write_json(f"candidate_{cand_id}.meta", {**meta, "parse_error": str(exc)})
            logger.warning(
                "candidate JSON parse failed",
                extra={**log_extra, "raw_path": raw_path, "parse_error": str(exc)},
                exc_info=True,
            )
            raise
        if not isinstance(design, dict):
            design = {}
        store.write_json(f"candidate_{cand_id}.meta", {**meta, "parsed": True})
        store.write_json(f"candidate_{cand_id}", design)
        logger.info(
            "candidate JSON artifact saved",
            extra={**log_extra, "raw_path": raw_path, "keys": sorted(design.keys())},
        )
        return {"candidates": [{"id": cand_id, "model": model, "design": design}]}

    return candidate_node


def judge_node(state: dict[str, Any]) -> dict[str, Any]:
    cancel.raise_if_cancelled(state.get("project_id", ""))
    progress.publish(state.get("project_id", ""), "judging",
                     message="Comparing candidates and forming the canonical design")
    cands = sorted(state.get("candidates", []), key=lambda c: c["id"])
    # N=2 with compact JSON fits Opus easily — feed both candidates in full.
    blocks = [
        f"### Candidate {c['id']} (model: {c['model']})\n{json.dumps(c['design'], ensure_ascii=False)}"
        for c in cands
    ]
    user = (
        f"{state['brief_text']}\n\n--- Research digest ---\n{state.get('digest', '')}\n\n"
        "--- Candidate designs ---\n" + "\n\n".join(blocks)
    )
    store = _store(state)
    log_extra = {
        "project_id": state.get("project_id", ""),
        "run_id": state.get("run_id", ""),
        "model": config.JUDGE_MODEL,
    }
    logger.info(
        "judge generation started",
        extra={**log_extra, "max_tokens": 16000, "json_mode": True},
    )
    raw_text, sources = llm.run_text(
        system=prompts.judge_system(), user=user, model=config.JUDGE_MODEL,
        max_tokens=16000, json_mode=True,
    )
    store.write_text("judge.raw", raw_text)
    raw_path = str(store.dir / "judge.raw.txt")
    logger.info(
        "judge raw response saved",
        extra={**log_extra, "raw_path": raw_path, "raw_chars": len(raw_text)},
    )
    meta = {
        "model": config.JUDGE_MODEL,
        "json_mode": True,
        "tool_loop": False,
        "web_search": False,
        "raw_chars": len(raw_text),
        "source_count": len(sources),
    }
    try:
        result = llm.extract_json(raw_text)
    except Exception as exc:
        store.write_json("judge.meta", {**meta, "parse_error": str(exc)})
        logger.warning(
            "judge JSON parse failed",
            extra={**log_extra, "raw_path": raw_path, "parse_error": str(exc)},
            exc_info=True,
        )
        raise
    if not isinstance(result, dict):
        result = {}
    canonical = result.get("canonical") if isinstance(result.get("canonical"), dict) else {}
    guidance = result.get("guidance") if isinstance(result.get("guidance"), dict) else {}

    store.write_json("judge.meta", {**meta, "parsed": True})
    store.write_json("judgment", result)
    store.write_json("canonical", canonical)
    logger.info(
        "judge JSON artifact saved",
        extra={**log_extra, "raw_path": raw_path, "keys": sorted(result.keys())},
    )
    # Per-section summaries for the manifest come from the judge's section_notes.
    summaries = guidance.get("section_notes") if isinstance(guidance.get("section_notes"), dict) else {}
    manifest = store.build_manifest(canonical, summaries=summaries)
    _audit(state, "candidates.judged",
           candidates=[c["model"] for c in cands],
           must_fix=len(guidance.get("must_fix", []) or []),
           picked_title=canonical.get("title", ""))
    return {"canonical": canonical, "guidance": guidance, "manifest": manifest,
            "title": canonical.get("title") or state.get("title", "")}


def _clean_section_md(text: str) -> str:
    import re
    t = (text or "").strip()
    m = re.fullmatch(r"```(?:markdown|md)?\s*\n(.+?)\n```", t, re.DOTALL)
    if m:
        t = m.group(1).strip()
    return t


def _write_one_section(
    key: str, heading: str, state: dict[str, Any], store: ArtifactStore,
    *, manifest_text: str, emphasis: str, must_fix: str,
    section_notes: dict[str, Any], digest: str, use_deepseek: bool,
) -> str:
    """Generate one POC document section's Markdown. Thread-safe: only reads shared
    state and writes its own per-section artifact files (distinct filenames)."""
    cancel.raise_if_cancelled(state.get("project_id", ""))
    source_keys = config.DOC_SECTION_SOURCES.get(key, [key])
    # The judge may key section_notes by the doc-section key OR by a canonical
    # field name; gather notes under either so merged sections keep their guidance.
    note = "; ".join(
        section_notes.get(k, "") for k in [key, *source_keys] if section_notes.get(k)
    )

    if use_deepseek:
        # Pre-load canonical sections so DeepSeek gets them directly (no tool loop)
        sections_content = "\n\n".join(
            f"--- Section: {sk} ---\n{store.read_section(sk)}" for sk in source_keys
        )
        user = (
            f"POC title: {state.get('title', '')}\n"
            f"Write the section titled: {heading}\n\n"
            f"Document-wide emphasis: {emphasis or 'n/a'}\n"
            f"Must address: {must_fix or 'n/a'}\n"
            f"Notes for this section: {note or 'n/a'}\n\n"
            f"Canonical design sections:\n{sections_content}\n\n"
            f"Research digest (context, cite sparingly):\n{digest}"
        )
        md, _ = llm.run_text(
            system=prompts.DOCUMENT_SECTION_SYSTEM_DEEPSEEK, user=user,
            model=config.DOCUMENT_MODEL, max_tokens=12000,
            **_deepseek_reasoning_kwargs(config.DOCUMENT_MODEL, effort="medium"),
        )
        tool_loop = False
    else:
        user = (
            f"POC title: {state.get('title', '')}\n"
            f"Write the section titled: {heading}\n"
            f"Read these canonical design section(s) for this content via read_section: {', '.join(source_keys)}\n\n"
            f"Manifest of available canonical sections (read what you need via read_section):\n{manifest_text}\n\n"
            f"Document-wide emphasis: {emphasis or 'n/a'}\n"
            f"Must address: {must_fix or 'n/a'}\n"
            f"Notes for this section: {note or 'n/a'}\n\n"
            f"Research digest (context, cite sparingly):\n{digest}"
        )
        md = llm.run_tool_loop(
            system=prompts.DOCUMENT_SECTION_SYSTEM, user=user,
            model=config.DOCUMENT_MODEL, read_section=store.read_section,
            max_tokens=12000,
        )
        tool_loop = True

    store.write_text(f"document_{key}.raw", md)
    store.write_json(f"document_{key}.meta", {
        "section": key,
        "heading": heading,
        "model": config.DOCUMENT_MODEL,
        "json_mode": False,
        "tool_loop": tool_loop,
        "web_search": False,
        "raw_chars": len(md),
        "parsed": True,
    })
    logger.info(
        "document section raw response saved",
        extra={
            "project_id": state.get("project_id", ""),
            "run_id": state.get("run_id", ""),
            "section": key,
            "model": config.DOCUMENT_MODEL,
            "raw_path": str(store.dir / f"document_{key}.raw.txt"),
            "raw_chars": len(md),
        },
    )
    return md


def document_node(state: dict[str, Any]) -> dict[str, Any]:
    cancel.raise_if_cancelled(state.get("project_id", ""))
    progress.publish(state.get("project_id", ""), "writing_document",
                     message="Writing the POC document (sections in parallel)")
    store = _store(state)
    manifest = state.get("manifest", [])
    guidance = state.get("guidance", {})
    manifest_text = json.dumps(manifest, ensure_ascii=False)
    emphasis = "; ".join(guidance.get("emphasis", []) or [])
    must_fix = "; ".join(guidance.get("must_fix", []) or [])
    section_notes = guidance.get("section_notes", {}) if isinstance(guidance.get("section_notes"), dict) else {}
    digest = (state.get("digest", "") or "")[:1500]

    use_deepseek = _models.provider_for_model(config.DOCUMENT_MODEL) == "deepseek"

    # Sections are independent (each only reads the canonical design + guidance),
    # so fan them out in parallel — same pattern as reviews_node. pool.map keeps
    # input order, so the assembled document preserves DOC_SECTIONS order.
    sections = config.DOC_SECTIONS
    with ThreadPoolExecutor(max_workers=max(1, len(sections))) as pool:
        raw_parts = list(pool.map(
            lambda kh: _write_one_section(
                kh[0], kh[1], state, store,
                manifest_text=manifest_text, emphasis=emphasis, must_fix=must_fix,
                section_notes=section_notes, digest=digest, use_deepseek=use_deepseek,
            ),
            sections,
        ))
    parts = [_clean_section_md(md) for md in raw_parts]
    document_md = "\n\n".join(p for p in parts if p)
    store.write_text("document.raw", "\n".join(p for p in raw_parts if p))
    store.write_json("document.meta", {
        "model": config.DOCUMENT_MODEL,
        "json_mode": False,
        "tool_loop": not use_deepseek,
        "web_search": False,
        "section_count": len(raw_parts),
        "raw_chars": sum(len(p) for p in raw_parts),
        "parsed": True,
    })
    store.write_section("document_md", document_md)
    _audit(state, "document.completed",
           sections=len([p for p in parts if p]), chars=len(document_md))
    return {"document_md": document_md}


def _review_one_lens(
    lens: str, focus: str, document_md: str, store: ArtifactStore | None = None,
    project_id: str = "", run_id: str = "",
) -> dict[str, Any]:
    system = prompts.REVIEW_LENS_SYSTEM.format(lens_label=lens, lens_focus=focus)
    meta = {
        "lens": lens,
        "model": config.REVIEW_MODEL_FUSION,
        "json_mode": True,
        "tool_loop": False,
        "web_search": False,
    }
    raw_name = f"review_{lens}.raw"
    meta_name = f"review_{lens}.meta"
    raw_path = str(store.dir / f"review_{ArtifactStore._safe(lens)}.raw.txt") if store else ""
    try:
        raw_text, sources = llm.run_text(
            system=system, user=document_md, model=config.REVIEW_MODEL_FUSION, max_tokens=4000,
            json_mode=True, **_deepseek_reasoning_kwargs(config.REVIEW_MODEL_FUSION),
        )
        if store:
            store.write_text(raw_name, raw_text)
            logger.info(
                "review raw response saved",
                extra={
                    "project_id": project_id,
                    "run_id": run_id,
                    "lens": lens,
                    "model": config.REVIEW_MODEL_FUSION,
                    "raw_path": raw_path,
                    "raw_chars": len(raw_text),
                },
            )
        meta = {**meta, "raw_chars": len(raw_text), "source_count": len(sources)}
        result = llm.extract_json(raw_text)
    except Exception as exc:  # one lens failing must not drop the others
        if store:
            store.write_json(meta_name, {**meta, "parse_error": str(exc)})
            logger.warning(
                "review JSON parse failed",
                extra={
                    "project_id": project_id,
                    "run_id": run_id,
                    "lens": lens,
                    "model": config.REVIEW_MODEL_FUSION,
                    "raw_path": raw_path,
                    "parse_error": str(exc),
                },
                exc_info=True,
            )
        return {"role": lens, "summary": f"(review failed: {exc})", "verdict": "comment",
                "report_md": "", "annotations": []}
    if not isinstance(result, dict):
        result = {}
    if store:
        store.write_json(meta_name, {**meta, "parsed": True})
    anns = result.get("annotations", []) if isinstance(result.get("annotations"), list) else []
    for a in anns:
        a["domain"] = a.get("domain") or lens
    return {"role": lens, "summary": result.get("summary", ""),
            "verdict": result.get("verdict", "comment"), "report_md": result.get("report_md", ""),
            "annotations": anns}


def reviews_node(state: dict[str, Any]) -> dict[str, Any]:
    cancel.raise_if_cancelled(state.get("project_id", ""))
    progress.publish(state.get("project_id", ""), "reviewing",
                     message="Running the stakeholder review board (parallel)")
    document_md = state.get("document_md", "")
    lenses = list(config.STAKEHOLDER_LENSES.items())
    store = _store(state) if state.get("run_id") else None
    with ThreadPoolExecutor(max_workers=max(1, len(lenses))) as pool:
        results = list(pool.map(
            lambda kv: _review_one_lens(
                kv[0], kv[1], document_md, store,
                state.get("project_id", ""), state.get("run_id", ""),
            ), lenses
        ))
    reviews = [{"role": r["role"], "summary": r["summary"], "verdict": r["verdict"],
                "report_md": r["report_md"]} for r in results]
    annotations: list[dict[str, Any]] = []
    for r in results:
        annotations.extend(r["annotations"])
    _audit(state, "reviews.completed", reviews=len(reviews), annotations=len(annotations))
    return {"reviews": reviews, "annotations": annotations}


_SLIDE_CLASS_RE = re.compile(r"""class\s*=\s*["'][^"']*\bslide\b[^"']*["']""", re.IGNORECASE)
_SLIDE_OPEN_RE = re.compile(
    r"""(<section\b[^>]*\bclass\s*=\s*["'][^"']*\bslide\b[^"']*["'][^>]*>)(.*)(</section>)""",
    re.IGNORECASE | re.DOTALL,
)


def _normalize_slides(slides: list[str]) -> str:
    out = []
    for raw in slides:
        s = (raw or "").strip()
        if not s:
            continue
        if not _SLIDE_CLASS_RE.search(s):
            s = f'<section class="slide">{s}</section>'
        if "slide-content" not in s:
            s = _SLIDE_OPEN_RE.sub(r'\1<div class="slide-content">\2</div>\3', s, count=1)
        out.append(s)
    return "\n".join(out)


def _deck_user(state: dict[str, Any], body: str) -> str:
    title = state.get("title", "")
    client = str(state.get("client_name") or "").strip()
    org = str(state.get("consulting_org") or "").strip()
    lines = [f"POC title: {title}"]
    if client:
        lines.append(f"Client company for title slide: {client}")
    if org:
        lines.append(f"Consulting team for title slide: {org}")
    if client and org:
        lines.append(f"Title slide signature: Prepared for {client} · Produced by {org}")
        lines.append("Render this exact signature as a small editable attribution line on the title slide only.")
    elif client:
        lines.append(f"Title slide signature: Prepared for {client}")
        lines.append("Render this exact signature as a small editable attribution line on the title slide only.")
    elif org:
        lines.append(f"Title slide signature: Produced by {org}")
        lines.append("Render this exact signature as a small editable attribution line on the title slide only.")
    lines.extend(["", body])
    return "\n".join(lines)


def deck_node(state: dict[str, Any]) -> dict[str, Any]:
    cancel.raise_if_cancelled(state.get("project_id", ""))
    progress.publish(state.get("project_id", ""), "building_deck",
                     message="Building the editable slide deck")
    store = _store(state)
    document_md = state.get("document_md", "")
    user = _deck_user(state, document_md)
    log_extra = {
        "project_id": state.get("project_id", ""),
        "run_id": state.get("run_id", ""),
        "model": config.DECK_MODEL,
    }
    logger.info("deck generation started", extra={**log_extra, "max_tokens": 16000, "json_mode": True})
    raw_text, _ = llm.run_text(
        system=prompts.DECK_SYSTEM, user=user, model=config.DECK_MODEL,
        max_tokens=16384, json_mode=True,
        # Deck is text->slides reformatting on a settled design — no thinking needed.
        **_deepseek_reasoning_kwargs(config.DECK_MODEL, thinking="disabled"),
    )
    store.write_text("deck.raw", raw_text)
    logger.info(
        "deck raw response saved",
        extra={**log_extra, "raw_path": str(store.dir / "deck.raw.txt"), "raw_chars": len(raw_text)},
    )
    meta = {
        "model": config.DECK_MODEL,
        "json_mode": True,
        "tool_loop": False,
        "web_search": False,
        "raw_chars": len(raw_text),
    }
    try:
        deck = llm.extract_json(raw_text)
    except Exception as exc:
        store.write_json("deck.meta", {**meta, "parse_error": str(exc)})
        logger.warning("deck JSON parse failed", extra={**log_extra, "parse_error": str(exc)}, exc_info=True)
        raise
    if not isinstance(deck, dict):
        deck = {}
    theme_css = deck.get("theme_css", "") if isinstance(deck.get("theme_css"), str) else ""
    slides = deck.get("slides", []) if isinstance(deck.get("slides"), list) else []
    deck_html = _normalize_slides(slides)
    store.write_json("deck.meta", {**meta, "slides": len(slides), "parsed": True})
    _audit(state, "deck.completed", slides=len(slides))
    return {"deck_html": deck_html, "deck_css": theme_css}


def persist_node(state: dict[str, Any]) -> dict[str, Any]:
    cancel.raise_if_cancelled(state.get("project_id", ""))
    project_id = state["project_id"]
    canonical = state.get("canonical", {})
    title = state.get("title") or canonical.get("title") or ""
    poc_id = db.new_id("poc_")
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO pocs (id, project_id, version, title, markdown, document_md,"
            " design_json, deck_html, deck_css, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (poc_id, project_id, 1, title, "", state.get("document_md", ""),
             json.dumps(canonical),
             state.get("deck_html", ""), state.get("deck_css", ""),
             db.now(), db.now()),
        )
        conn.execute("UPDATE research_notes SET poc_id=? WHERE project_id=? AND poc_id IS NULL",
                     (poc_id, project_id))
        for r in state.get("reviews", []):
            conn.execute(
                "INSERT INTO review_reports (id, poc_id, role, summary, verdict, report_md, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (db.new_id("rr_"), poc_id, r.get("role", "comment"), r.get("summary", ""),
                 r.get("verdict", "comment"), r.get("report_md", ""), db.now()),
            )
        for a in state.get("annotations", []):
            conn.execute(
                "INSERT INTO annotations (id, poc_id, anchor, domain, severity, title, body, suggestion, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (db.new_id("an_"), poc_id, a.get("anchor", ""), a.get("domain", ""),
                 a.get("severity", "info"), a.get("title", ""), a.get("body", ""),
                 a.get("suggestion", ""), db.now()),
            )
        conn.execute("UPDATE projects SET status='in_review', updated_at=? WHERE id=?",
                     (db.now(), project_id))
        db.record_audit(conn, action="fusion.completed", project_id=project_id, poc_id=poc_id,
                        detail={"run_id": state.get("run_id"),
                                "candidates": [c["model"] for c in state.get("candidates", [])],
                                "reviews": len(state.get("reviews", [])),
                                "annotations": len(state.get("annotations", []))})
    progress.publish(project_id, "done", message="POC ready for review", poc_id=poc_id)
    return {"poc_id": poc_id}
