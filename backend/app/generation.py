"""The POC generation pipeline.

Redesigned from the parent project's RAG/memory approach to a simpler,
web-grounded flow:

    intake brief
      -> 1. best-practice web research      (grounded, cited -> auditable)
      -> 2. architecture design + markdown   (structured JSON)
      -> 3. Markdown POC document with inline mermaid diagrams
      -> 4. editable HTML slide deck
      -> 5. stakeholder reviews + annotations

Runs synchronously in a background thread; progress is streamed via
:mod:`app.progress`. Every step writes its artifacts and an audit event.
"""

from __future__ import annotations

import json
import re
from typing import Any

from . import cancel, config, db, llm, models, progress, prompts, research
from .artifacts import ArtifactStore
from .cancel import GenerationCancelled

MAX_DETAIL_CHARS = 6_000


def _deepseek_reasoning_kwargs(model: str) -> dict[str, str]:
    if models.provider_for_model(model) != "deepseek":
        return {}
    return {"deepseek_thinking": "enabled", "effort": "max"}


def _clean_html_doc(text: str) -> str:
    """Strip markdown code fences a model may wrap the HTML document in."""
    t = (text or "").strip()
    m = re.search(r"```(?:html)?\s*(.+?)```", t, re.DOTALL)
    if m:
        t = m.group(1).strip()
    return t


def _clean_md_doc(text: str) -> str:
    """Strip a stray wrapping code fence a model may put around the whole doc."""
    t = (text or "").strip()
    m = re.fullmatch(r"```(?:markdown|md)?\s*\n(.+?)\n```", t, re.DOTALL)
    if m:
        t = m.group(1).strip()
    return t


def _html_to_text(html: str) -> str:
    """Rough plain-text rendering of the HTML doc to feed the deck step."""
    text = re.sub(r"<figure[^>]*>.*?</figure>", "", html or "", flags=re.DOTALL)
    text = re.sub(r"<(h2|h3)[^>]*>", "\n## ", text)
    text = re.sub(r"<li[^>]*>", "\n- ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def _brief_text(project: dict[str, Any], brief: dict[str, Any]) -> str:
    lines = [
        f"Project title: {project['title']}",
        f"Client: {project.get('client_name') or 'n/a'}",
        f"Consulting org: {project.get('consulting_org') or 'n/a'}",
        "",
        "Requirement intake:",
    ]
    for key, value in brief.items():
        if value:
            label = key.replace("_", " ").capitalize()
            lines.append(f"- {label}: {value}")
    detail = str(project.get("requirements_detail", "") or "").strip()
    if detail:
        lines.extend([
            "",
            "Detailed requirements from uploaded document:",
            detail[:MAX_DETAIL_CHARS],
        ])
    return "\n".join(lines)


def _deck_user(project: dict[str, Any], title: str, body: str) -> str:
    """Build the deck prompt, including optional title-slide attribution."""
    client = str(project.get("client_name") or "").strip()
    org = str(project.get("consulting_org") or "").strip()

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


def _write_raw(store: ArtifactStore, name: str, raw_text: str, meta: dict[str, Any]) -> None:
    store.write_text(f"{name}.raw", raw_text)
    store.write_json(f"{name}.meta", {**meta, "raw_chars": len(raw_text)})


def _run_json_with_raw(
    store: ArtifactStore, name: str, *, system: str, user: str, model: str, max_tokens: int,
    llm_kwargs: dict[str, Any] | None = None,
) -> tuple[Any, list[dict[str, Any]]]:
    raw_text, sources = llm.run_text(
        system=system, user=user, model=model, max_tokens=max_tokens, json_mode=True,
        **(llm_kwargs or {}),
    )
    meta = {
        "model": model,
        "json_mode": True,
        "tool_loop": False,
        "web_search": False,
        "source_count": len(sources),
    }
    _write_raw(store, name, raw_text, meta)
    try:
        data = llm.extract_json(raw_text)
    except Exception as exc:
        store.write_json(f"{name}.meta", {**meta, "raw_chars": len(raw_text), "parse_error": str(exc)})
        raise
    store.write_json(f"{name}.meta", {**meta, "raw_chars": len(raw_text), "parsed": True})
    return data, sources


# Match a `slide` class *token* (any quote style, extra tokens allowed), so a
# model emitting `class="slide title"` is recognised as a slide and not wrapped
# again — a nested `.slide` would be display:none and render a blank frame.
_SLIDE_CLASS_RE = re.compile(r"""class\s*=\s*["'][^"']*\bslide\b[^"']*["']""", re.IGNORECASE)
_SLIDE_OPEN_RE = re.compile(
    r"""(<section\b[^>]*\bclass\s*=\s*["'][^"']*\bslide\b[^"']*["'][^>]*>)(.*)(</section>)""",
    re.IGNORECASE | re.DOTALL,
)


def _normalize_slides(slides: list[str]) -> str:
    """Ensure every slide is a `.slide` wrapping a `.slide-content` (skill shape)."""
    out = []
    for raw in slides:
        s = (raw or "").strip()
        if not s:
            continue
        if not _SLIDE_CLASS_RE.search(s):
            s = f'<section class="slide">{s}</section>'
        if "slide-content" not in s:
            # Wrap the inner markup so the viewport-base centering applies —
            # in place, without adding another <section>.
            s = _SLIDE_OPEN_RE.sub(r'\1<div class="slide-content">\2</div>\3', s, count=1)
        out.append(s)
    return "\n".join(out)


def run_generation(project_id: str) -> None:
    cancel.clear(project_id)
    progress.reset(project_id)
    progress.publish(project_id, "queued", message="Starting POC generation")
    run_id = db.new_id("run_")
    store = ArtifactStore(config.RUNS_DIR, run_id)

    with db.connect() as conn:
        prow = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if prow is None:
            progress.publish(project_id, "failed", message="Project not found")
            return
        project = dict(prow)
        brief = json.loads(project["brief_json"] or "{}")
        conn.execute(
            "UPDATE projects SET status='generating', updated_at=? WHERE id=?",
            (db.now(), project_id),
        )
        db.record_audit(conn, action="generation.started", project_id=project_id,
                        detail={"mode": "legacy", "run_id": run_id})

    brief_text = _brief_text(project, brief)

    try:
        # 1. Research ---------------------------------------------------------
        cancel.raise_if_cancelled(project_id)
        progress.publish(project_id, "researching", message="Researching current best practices (web grounding)")
        digest, sources = research.run_research(
            brief_text, project["title"],
            raw_sink=lambda name, raw_text, meta: _write_raw(store, name, raw_text, {**meta, "parsed": True}),
        )
        _write_raw(store, "research", digest, {
            "model": config.RESEARCH_MODEL,
            "json_mode": False,
            "tool_loop": False,
            "web_search": bool(config.ANTHROPIC_NATIVE_SEARCH),
            "source_count": len(sources),
            "parsed": True,
        })
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO research_notes (id, project_id, poc_id, topic, digest, citations_json, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (db.new_id("rn_"), project_id, None, project["title"], digest, json.dumps(sources), db.now()),
            )
            db.record_audit(conn, action="research.completed", project_id=project_id,
                            detail={"sources": len(sources)})
        progress.publish(project_id, "researched", message=f"Gathered {len(sources)} sources")

        # 2. Design (structured) ---------------------------------------------
        cancel.raise_if_cancelled(project_id)
        progress.publish(project_id, "designing", message="Designing the POC architecture")
        design, _ = _run_json_with_raw(
            store,
            "design",
            system=prompts.DESIGN_SYSTEM,
            user=f"{brief_text}\n\n--- Research digest ---\n{digest}",
            model=config.MODEL,
            max_tokens=16000,
        )
        if not isinstance(design, dict):
            design = {}
        markdown = design.pop("markdown", "")
        title = design.get("title") or project["title"]

        # 3. Detailed Markdown POC document (diagrams are inline mermaid) ------
        cancel.raise_if_cancelled(project_id)
        progress.publish(project_id, "writing_document", message="Writing the detailed POC document")
        doc_user = (
            f"POC title: {title}\n\nStructured design (JSON):\n{json.dumps(design)[:6000]}"
            f"\n\nResearch digest:\n{digest[:1500]}"
        )
        document_raw, document_sources = llm.run_text(
            system=prompts.DOCUMENT_SYSTEM, user=doc_user, model=config.MODEL, max_tokens=16000,
            **_deepseek_reasoning_kwargs(config.MODEL),
        )
        _write_raw(store, "document", document_raw, {
            "model": config.MODEL,
            "json_mode": False,
            "tool_loop": False,
            "web_search": False,
            "source_count": len(document_sources),
            "parsed": True,
        })
        document_md = _clean_md_doc(document_raw)

        poc_id = db.new_id("poc_")
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO pocs (id, project_id, version, title, markdown, document_md,"
                " design_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (poc_id, project_id, 1, title, markdown, document_md,
                 json.dumps(design), db.now(), db.now()),
            )
            conn.execute("UPDATE research_notes SET poc_id=? WHERE project_id=? AND poc_id IS NULL",
                         (poc_id, project_id))
            mermaid_count = document_md.count("```mermaid")
            db.record_audit(conn, action="document.completed", project_id=project_id, poc_id=poc_id,
                            detail={"diagrams": mermaid_count, "doc_chars": len(document_md)})
        progress.publish(project_id, "designed", message="Design & document ready", poc_id=poc_id)

        # 4. Deck -------------------------------------------------------------
        cancel.raise_if_cancelled(project_id)
        progress.publish(project_id, "building_deck", message="Building the editable slide deck")
        deck, _ = _run_json_with_raw(
            store,
            "deck",
            system=prompts.DECK_SYSTEM,
            user=_deck_user(project, title, document_md or markdown),
            model=config.MODEL,
            max_tokens=16000,
            llm_kwargs=_deepseek_reasoning_kwargs(config.MODEL),
        )
        theme_css = deck.get("theme_css", "") if isinstance(deck, dict) else ""
        slides = deck.get("slides", []) if isinstance(deck, dict) else []
        deck_html = _normalize_slides(slides if isinstance(slides, list) else [])
        with db.connect() as conn:
            conn.execute("UPDATE pocs SET deck_html=?, deck_css=?, updated_at=? WHERE id=?",
                         (deck_html, theme_css, db.now(), poc_id))
            db.record_audit(conn, action="deck.completed", project_id=project_id, poc_id=poc_id,
                            detail={"slides": len(_SLIDE_CLASS_RE.findall(deck_html))})
        progress.publish(project_id, "deck_built", message="Slide deck ready")

        # 5. Reviews + annotations -------------------------------------------
        cancel.raise_if_cancelled(project_id)
        progress.publish(project_id, "reviewing", message="Running the stakeholder review board")
        reviews_obj, _ = _run_json_with_raw(
            store,
            "reviews",
            system=prompts.REVIEW_SYSTEM,
            user=document_md or markdown,
            model=config.MODEL,
            max_tokens=16000,
            llm_kwargs=_deepseek_reasoning_kwargs(config.MODEL),
        )
        reviews = reviews_obj.get("reviews", []) if isinstance(reviews_obj, dict) else []
        annotations = reviews_obj.get("annotations", []) if isinstance(reviews_obj, dict) else []
        with db.connect() as conn:
            for r in reviews:
                conn.execute(
                    "INSERT INTO review_reports (id, poc_id, role, summary, verdict, report_md, created_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (db.new_id("rr_"), poc_id, r.get("role", "comment"), r.get("summary", ""),
                     r.get("verdict", "comment"), r.get("report_md", ""), db.now()),
                )
            for a in annotations:
                conn.execute(
                    "INSERT INTO annotations (id, poc_id, anchor, domain, severity, title, body, suggestion, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (db.new_id("an_"), poc_id, a.get("anchor", ""), a.get("domain", ""),
                     a.get("severity", "info"), a.get("title", ""), a.get("body", ""),
                     a.get("suggestion", ""), db.now()),
                )
            conn.execute("UPDATE projects SET status='in_review', updated_at=? WHERE id=?",
                         (db.now(), project_id))
            db.record_audit(conn, action="reviews.completed", project_id=project_id, poc_id=poc_id,
                            detail={"reviews": len(reviews), "annotations": len(annotations)})
        progress.publish(project_id, "done", message="POC ready for review", poc_id=poc_id)

    except GenerationCancelled:
        with db.connect() as conn:
            conn.execute("UPDATE projects SET status='draft', updated_at=? WHERE id=?",
                         (db.now(), project_id))
            db.record_audit(conn, action="generation.cancelled", project_id=project_id,
                            detail={"run_id": run_id})
        progress.publish(project_id, "cancelled", message="Generation cancelled")
        cancel.clear(project_id)
    except Exception as exc:  # noqa: BLE001 — surface any failure to the UI + audit
        with db.connect() as conn:
            conn.execute("UPDATE projects SET status='failed', updated_at=? WHERE id=?",
                         (db.now(), project_id))
            db.record_audit(conn, action="generation.failed", project_id=project_id,
                            detail={"error": str(exc), "run_id": run_id})
        progress.publish(project_id, "failed", message=f"Generation failed: {exc}")
