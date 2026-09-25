"""FastAPI app: the whole local API surface behind the desktop window."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import httpx
from fastapi import Body, FastAPI, File, Query, Request, Response, UploadFile
from starlette.requests import ClientDisconnect as ClientDisconnectError
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               PlainTextResponse, StreamingResponse)
from fastapi.staticfiles import StaticFiles

from . import (agent, agents, autoroute, budget, media, projects, superagent, catalog, downloads, files, gguf, hardware,
               memory, oauth, providers, runners, router, runs, scheduler,
               skills, store, tools, vault)
from .providers import custom as custom_providers
from .tools import files as file_tools
from .config import APP_NAME, DEFAULT_PORT, HF_HOST, MODELS_DIR, VERSION, WEB_DIR, settings
from .mcp import discovery
from .mcp.registry import registry
from .tools import browser, computer
from .tools.approval import POLICIES, gate

app = FastAPI(title=APP_NAME, version=VERSION, docs_url="/api/docs")

_http: httpx.AsyncClient | None = None


@app.on_event("startup")
async def _startup() -> None:
    global _http
    _http = httpx.AsyncClient(follow_redirects=True)
    await asyncio.to_thread(store.connect)
    await asyncio.to_thread(memory.connect)
    await asyncio.to_thread(files.connect)
    await asyncio.to_thread(media.connect)
    # Bring up MCP servers that were left enabled, without blocking the UI.
    asyncio.create_task(registry.connect_enabled())
    scheduler.scheduler.start()
    asyncio.create_task(autoroute.refresh_if_stale())
    from . import custom_agents, personal
    custom_agents.ensure_presets()
    personal.ensure()
    # v2.2: get Needle ready in the background so the first command is instant.
    from . import needle_intent
    if needle_intent.enabled() and needle_intent.installed():
        asyncio.create_task(needle_intent.warmup())


@app.on_event("shutdown")
async def _shutdown() -> None:
    if _http:
        await _http.aclose()
    gate.cancel_all()
    await scheduler.scheduler.stop()
    await runs.registry.shutdown()
    await registry.shutdown()
    await (await browser.session()).shutdown()
    runners.server.stop()
    store.close()
    memory.close()
    files.close()
    media.close()


def client() -> httpx.AsyncClient:
    assert _http is not None
    return _http


async def hf_token() -> str | None:
    return await oauth.access_token("huggingface") or vault.get("hf_token")


# ---------------------------------------------------------------------------
# Hardware
# ---------------------------------------------------------------------------

@app.get("/api/hardware")
async def api_hardware(refresh: bool = False) -> dict[str, Any]:
    hw = await asyncio.to_thread(hardware.probe, refresh)
    return hw.to_dict()


@app.post("/api/assess")
async def api_assess(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Can this machine run this model? Reads the real GGUF header when it can."""
    size = int(body.get("size_bytes") or 0)
    context = body.get("context")
    path = body.get("path") or ""
    hf_ref = body.get("hf_ref") or ""

    info: gguf.GGUFInfo | None = None
    if path:
        info = await asyncio.to_thread(gguf.read_local, path)
        if info and not size:
            from pathlib import Path
            try:
                size = Path(path).stat().st_size
            except OSError:
                size = 0
    elif hf_ref:
        parts = hf_ref.split("/")
        if len(parts) >= 3:
            url = f"{HF_HOST}/{parts[0]}/{parts[1]}/resolve/main/{'/'.join(parts[2:])}"
            token = await hf_token()
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            info = await asyncio.to_thread(_read_remote_header, url, headers)

    if not size:
        return {"verdict": "amber", "headline": "Size unknown",
                "detail": "No file size available, so no verdict can be given.",
                "breakdown": {}, "kv_source": "unknown"}

    result = await asyncio.to_thread(hardware.assess, size, info, context)
    if info:
        result["model_info"] = {
            "architecture": info.architecture,
            "layers": info.block_count,
            "context_length": info.context_length,
            "quantisation": info.quantisation,
            "params": info.parameter_count,
        }
    return result


def _read_remote_header(url: str, headers: dict[str, str]) -> gguf.GGUFInfo | None:
    with httpx.Client(follow_redirects=True) as c:
        return gguf.read_remote(c, url, headers)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

@app.get("/api/models")
async def api_models() -> dict[str, Any]:
    entries = await catalog.browse(client())
    return {"models": [e.to_dict() for e in entries]}


@app.get("/api/models/search")
async def api_models_search(q: str = Query(...), limit: int = 20) -> dict[str, Any]:
    entries = await catalog.hf_search(client(), q, limit, await hf_token())
    return {"models": [e.to_dict() for e in entries]}


@app.get("/api/models/files")
async def api_model_files(repo: str = Query(...)) -> dict[str, Any]:
    entries = await catalog.hf_files(client(), repo, await hf_token())
    return {"files": [e.to_dict() for e in entries]}


@app.delete("/api/models/local")
async def api_delete_local(path: str = Query(...)) -> dict[str, Any]:
    okay, msg = downloads.delete_local_model(path)
    return {"ok": okay, "message": msg}


# ---------------------------------------------------------------------------
# Downloads
# ---------------------------------------------------------------------------

@app.get("/api/downloads")
async def api_downloads() -> dict[str, Any]:
    return {"jobs": downloads.manager.list()}


@app.post("/api/downloads/hf")
async def api_download_hf(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    ref = (body.get("ref") or "").strip()
    if not ref:
        return JSONResponse({"error": "ref is required"}, status_code=400)
    job = downloads.manager.start_hf(ref, await hf_token())
    return job.to_dict()


@app.post("/api/downloads/ollama")
async def api_download_ollama(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    model = (body.get("model") or "").strip()
    if not model:
        return JSONResponse({"error": "model is required"}, status_code=400)
    return downloads.manager.start_ollama(model).to_dict()


@app.post("/api/downloads/{job_id}/cancel")
async def api_download_cancel(job_id: str) -> dict[str, Any]:
    return {"ok": downloads.manager.cancel(job_id)}


@app.post("/api/downloads/clear")
async def api_downloads_clear() -> dict[str, Any]:
    return {"cleared": downloads.manager.clear_finished()}


# ---------------------------------------------------------------------------
# Providers and chat
# ---------------------------------------------------------------------------

@app.get("/api/providers")
async def api_providers() -> dict[str, Any]:
    return {"providers": await providers.status_all()}


# ---------------------------------------------------------------------------
# Routes and model health
# ---------------------------------------------------------------------------

@app.get("/api/routes")
async def api_routes() -> dict[str, Any]:
    out = []
    for route in router.routes().values():
        attempt = router.plan(route)
        out.append({
            **route.to_dict(),
            "usable": [c.to_dict() for c in attempt.usable],
            "resting": [{**c.to_dict(), "health": h.to_dict()}
                        for c, h in attempt.resting],
            "excluded": [{**c.to_dict(), "why": why}
                         for c, why in attempt.excluded],
            "over_budget": [{**c.to_dict(), "verdict": v.to_dict()}
                            for c, v in attempt.over_budget],
            "ready": attempt.ok,
            "next": attempt.usable[0].to_dict() if attempt.usable else None,
        })
    return {"routes": out, "health": router.book.all(),
            "budget": budget.ledger.snapshot(),
            "default_route": settings.get("default_route", "")}


@app.post("/api/routes")
async def api_route_save(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    key = "".join(ch for ch in str(body.get("key", "")).strip().lower()
                  if ch.isalnum() or ch in "-_")[:32]
    if not key:
        return JSONResponse({"error": "A short key is required."}, status_code=400)

    candidates = []
    for entry in body.get("candidates") or []:
        if isinstance(entry, dict) and entry.get("provider") and entry.get("model"):
            candidates.append(router.Candidate(provider=str(entry["provider"]),
                                               model=str(entry["model"]),
                                               note=str(entry.get("note", ""))))

    allow_paid = bool(body.get("allow_paid", True))
    if key == autoroute.AUTO_KEY:
        settings.set("auto_route_managed", False)   # you edited it; hands off
    if not allow_paid:
        # Enforced here, not just in the UI: a free route must not be able to
        # contain a paid model at all.
        candidates = [c for c in candidates
                      if not router.is_paid(c.provider, c.model)]

    router.save_route(router.Route(
        key=key, label=str(body.get("label") or key),
        description=str(body.get("description", "")),
        allow_paid=allow_paid, candidates=candidates,
        smart=bool(body.get("smart", True)),
        then_route=str(body.get("then_route") or "")))
    return {"ok": True, "key": key, "routes": [r.to_dict()
                                               for r in router.routes().values()]}


@app.delete("/api/routes/{key}")
async def api_route_delete(key: str) -> dict[str, Any]:
    removed = router.delete_route(key)
    if settings.get("default_route") == key:
        settings.set("default_route", "")
    return {"ok": removed}


@app.post("/api/routes/default")
async def api_route_default(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    key = str(body.get("key", ""))
    if key and key not in router.routes():
        return JSONResponse({"error": "No such route"}, status_code=404)
    settings.set("default_route", key)
    return {"default_route": key}


@app.post("/api/routes/autobuild")
async def api_route_autobuild(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """Build a free route across every free provider you have, in one click.

    Ordering is deliberate: free cloud tiers first, because they are far faster
    than a CPU-only local model, and a local engine last because it is the one
    thing that can never run out. When xKiro's daily allowance is gone the route
    falls to OpenRouter, then 9Router, then whatever else, and only lands on
    Ollama when every hosted option is spent.
    """
    key = str(body.get("key") or "test")
    result = await autoroute.build(key, int(body.get("per_provider") or 3),
                                   smart=bool(body.get("smart", True)))
    if not result.get("ok"):
        return JSONResponse({"error": result.get("error"),
                             "notes": result.get("notes", [])}, status_code=400)
    return result


@app.post("/api/setup/quick")
async def api_quick_setup(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Paste keys for the free presets; get a working Auto route as default."""
    keys = body.get("keys") or {}
    if not isinstance(keys, dict) or not any(str(v).strip() for v in keys.values()):
        return JSONResponse({"error": "Paste at least one key."}, status_code=400)
    return await autoroute.quick_setup(
        {str(k): str(v) for k, v in keys.items()},
        make_default=bool(body.get("make_default", True)))


@app.get("/api/setup/status")
async def api_setup_status() -> dict[str, Any]:
    from .providers import custom as cp
    stored = cp.stored()
    auto = router.get_route(autoroute.AUTO_KEY)
    return {"providers": len(stored),
            "with_keys": sum(1 for k in stored if vault.has(cp.key_name(k))),
            "auto_models": len(auto.candidates) if auto else 0,
            "default_route": settings.get("default_route", ""),
            "quick_presets": [p for p in cp.PRESETS if p.get("free_tier") == "yes"
                              or p["key"] in ("openrouter", "xkiro")],
            "needs_setup": not stored}


@app.post("/api/routes/free-tag")
async def api_free_tag(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Mark a model free or paid by hand.

    Needed for providers that publish no pricing - Atria, a work gateway, a
    self-hosted proxy. AEGIS assumes paid when it cannot tell, so a free route
    will refuse the model until you say otherwise.
    """
    provider = str(body.get("provider", "")).strip()
    model = str(body.get("model", "")).strip()
    if not provider or not model:
        return JSONResponse({"error": "provider and model are required"},
                            status_code=400)
    tag = f"{provider}::{model}"
    tags = set(settings.get("free_models") or [])
    if bool(body.get("free")):
        tags.add(tag)
    else:
        tags.discard(tag)
    settings.set("free_models", sorted(tags))
    return {"ok": True, "free": tag in tags,
            "is_paid": router.is_paid(provider, model)}


@app.get("/api/routes/budget")
async def api_budget() -> dict[str, Any]:
    """What each model has spent, and against what limit."""
    return {"usage": budget.ledger.snapshot(),
            "limits": settings.get("model_budgets") or {},
            "learned": settings.get("learned_budgets") or {},
            "fields": {name: budget.FIELD_LABEL[name] for name in budget.FIELDS}}


@app.post("/api/routes/budget")
async def api_budget_set(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Set your own allowance for a model, or reset what has been counted.

    model='*' sets a default for the whole provider, which is usually what you
    want: a free plan's cap is on the account, not on each model.
    """
    action = str(body.get("action") or "set")
    provider = str(body.get("provider", "")).strip()
    model = str(body.get("model", "")).strip()

    if action == "reset":
        return {"ok": True, "cleared": budget.ledger.clear(provider, model),
                "usage": budget.ledger.snapshot()}

    if not provider or not model:
        return JSONResponse({"error": "provider and model are required "
                                      "(model may be '*')"}, status_code=400)
    limits = budget.set_limits(
        provider, model,
        **{name: body[name] for name in budget.FIELDS if name in body})
    return {"ok": True, "limits": limits.to_dict(),
            "usage": budget.ledger.snapshot()}


@app.post("/api/routes/health")
async def api_health_action(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    action = body.get("action")
    if action == "revive":
        return {"revived": router.book.revive_all(), "health": router.book.all()}
    if action == "clear":
        return {"cleared": router.book.clear(str(body.get("provider", "")),
                                             str(body.get("model", ""))),
                "health": router.book.all()}
    return JSONResponse({"error": "action must be 'revive' or 'clear'"},
                        status_code=400)


# ---------------------------------------------------------------------------
# Custom OpenAI-compatible providers
# ---------------------------------------------------------------------------

@app.get("/api/custom-providers")
async def api_custom_providers() -> dict[str, Any]:
    return custom_providers.summary()


@app.post("/api/custom-providers")
async def api_custom_save(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    raw_models = body.get("models")
    if isinstance(raw_models, str):
        raw_models = [m for m in raw_models.replace("\n", ",").split(",")]
    result = custom_providers.save(
        str(body.get("key", "")), str(body.get("label", "")),
        str(body.get("base_url", "")), note=str(body.get("note", "")),
        extra_headers=body.get("headers") or {},
        models=raw_models or [],
        has_models_endpoint=bool(body.get("has_models_endpoint", True)),
        free_tier=bool(body.get("free_tier", False)))
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    if "api_key" in body:
        custom_providers.set_api_key(result["key"], str(body["api_key"]))
    if "extra_keys" in body:
        custom_providers.set_extra_keys(result["key"], str(body["extra_keys"]))
    return {**result, **custom_providers.summary()}


@app.delete("/api/custom-providers/{key}")
async def api_custom_delete(key: str) -> dict[str, Any]:
    return {"ok": custom_providers.remove(key), **custom_providers.summary()}


@app.get("/api/custom-providers/{key}/models")
async def api_custom_models(key: str) -> dict[str, Any]:
    return await custom_providers.list_models(key)


@app.get("/api/providers/{key}/models")
async def api_provider_models(key: str) -> dict[str, Any]:
    provider = providers.get(key)
    if not provider:
        return JSONResponse({"error": "unknown provider"}, status_code=404)
    return {"models": await provider.models()}


def _event_stream(source) -> StreamingResponse:
    async def wrapped():
        try:
            async for event in source:
                if not str(event.get("type", "")).startswith("_"):
                    yield _sse(event)
        except asyncio.CancelledError:
            raise                       # the run keeps going; only this view ends
        except Exception as exc:
            yield _sse({"type": "error", "text": f"{type(exc).__name__}: {exc}"})

    return StreamingResponse(wrapped(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.post("/api/chat")
async def api_chat(body: dict[str, Any] = Body(...)) -> Any:
    """Send a message. The transcript lives server-side, so the body is small.

    The run is started as a background task and this response merely subscribes
    to it - closing the connection does not stop the agent.
    """
    key = body.get("provider") or ""
    if not key and body.get("project"):
        proj = projects.get(str(body["project"])) or {}
        if proj.get("route"):
            key = f"route:{proj['route']}"
    if not key and body.get("agent"):
        from . import custom_agents
        agent_def = custom_agents.get(str(body["agent"])) or {}
        if agent_def.get("route"):
            key = f"route:{agent_def['route']}"
    if not key and settings.get("default_route"):
        key = f"route:{settings.get('default_route')}"
    key = key or settings.get("default_provider")
    model = body.get("model") or ""
    text = (body.get("text") or "").strip()
    chat_id = body.get("chat_id") or ""

    provider = providers.get(key)
    if not provider:
        return JSONResponse({"error": f"Unknown provider '{key}'"}, status_code=400)
    if not text:
        return JSONResponse({"error": "text is required"}, status_code=400)

    project = str(body.get("project") or "")
    if project and not projects.get(project):
        project = ""
    if not chat_id or not store.get_chat(chat_id):
        chat_id = store.create_chat(provider=key, model=model)
        if project:
            projects.assign(chat_id, project)
    if runs.registry.is_running(chat_id):
        return JSONResponse({"error": "This chat already has a run in flight."},
                            status_code=409)

    # Attachments: small text rides inline, large text is announced and left
    # for the agent to search. See files.message_parts.
    attachment_ids = [str(a) for a in (body.get("attachment_ids") or [])]
    blocks, images = ("", [])
    if attachment_ids:
        dest = (projects.workspace(project) if project
                else projects.general_workspace()) / "uploads"
        blocks, images = await asyncio.to_thread(files.message_parts,
                                                 attachment_ids, dest)
    content = f"{text}\n\n{blocks}".strip() if blocks else text

    store.add_message(chat_id, "user", content, images=images)
    store.touch(chat_id, key, model)

    opts = {k: v for k, v in body.items()
            if k in ("temperature", "max_tokens", "context", "workdir", "max_steps",
                     "project", "boost", "agent")}
    run = runs.registry.start(chat_id, provider, model,
                              use_tools=bool(body.get("use_tools", True)), **opts)

    response = _event_stream(run.subscribe(0))
    response.headers["X-Aegis-Chat-Id"] = chat_id
    response.headers["X-Aegis-Run-Id"] = run.id
    return response


@app.get("/api/chats/{chat_id}/stream")
async def api_chat_stream(chat_id: str, from_index: int = 0) -> Any:
    """Reattach to a run already in flight, replaying what was missed."""
    run = runs.registry.get(chat_id)
    if not run:
        return JSONResponse({"error": "No run for this chat."}, status_code=404)
    return _event_stream(run.subscribe(from_index))


@app.post("/api/chats/{chat_id}/stop")
async def api_chat_stop(chat_id: str) -> dict[str, Any]:
    return {"stopped": await runs.registry.stop(chat_id)}


# ---------------------------------------------------------------------------
# Chats
# ---------------------------------------------------------------------------

@app.get("/api/chats")
async def api_chats(q: str = "", archived: bool = False) -> dict[str, Any]:
    rows = await asyncio.to_thread(store.list_chats, q, archived)
    live = set(runs.registry.running_chat_ids())
    owners = await asyncio.to_thread(projects.chat_map)
    return {"chats": [{**c, "running": c["id"] in live,
                       "project": owners.get(c["id"], "")} for c in rows],
            "search": store.search_backend()}


@app.post("/api/chats")
async def api_chat_new(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    chat_id = store.create_chat(title=body.get("title", ""),
                                provider=body.get("provider", ""),
                                model=body.get("model", ""))
    return {"chat": store.get_chat(chat_id)}


@app.get("/api/chats/{chat_id}")
async def api_chat_get(chat_id: str) -> dict[str, Any]:
    chat = await asyncio.to_thread(store.get_chat, chat_id)
    if not chat:
        return JSONResponse({"error": "No such chat"}, status_code=404)
    messages = await asyncio.to_thread(store.transcript_for_display, chat_id)
    run = runs.registry.get(chat_id)
    return {"chat": chat, "messages": messages,
            "run": run.status() if run else None}


@app.patch("/api/chats/{chat_id}")
async def api_chat_patch(chat_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if "title" in body:
        fields["title"] = str(body["title"])[:200]
    if "pinned" in body:
        fields["pinned"] = int(bool(body["pinned"]))
    if "archived" in body:
        fields["archived"] = int(bool(body["archived"]))
    store.update_chat(chat_id, **fields)
    return {"chat": store.get_chat(chat_id)}


@app.delete("/api/chats/{chat_id}")
async def api_chat_delete(chat_id: str) -> dict[str, Any]:
    await runs.registry.stop(chat_id)
    return {"ok": store.delete_chat(chat_id)}


@app.get("/api/chats/{chat_id}/export")
async def api_chat_export(chat_id: str, format: str = "md") -> Any:
    if not store.get_chat(chat_id):
        return JSONResponse({"error": "No such chat"}, status_code=404)
    if format == "json":
        return JSONResponse(store.export_json(chat_id))
    text = store.export_markdown(chat_id)
    return PlainTextResponse(text, headers={
        "Content-Disposition": f'attachment; filename="chat-{chat_id}.md"'})


@app.get("/api/blobs/{blob_id}")
async def api_blob(blob_id: str) -> Any:
    found = await asyncio.to_thread(store.get_image, blob_id)
    if not found:
        return JSONResponse({"error": "Gone — evicted by the image budget."},
                            status_code=404)
    media_type, data = found
    return Response(content=data, media_type=media_type,
                    headers={"Cache-Control": "private, max-age=86400"})


@app.get("/api/store")
async def api_store_stats() -> dict[str, Any]:
    return await asyncio.to_thread(store.stats)


# ---------------------------------------------------------------------------
# Attachments and folders
# ---------------------------------------------------------------------------

@app.post("/api/chats/{chat_id}/attachments")
async def api_attach(chat_id: str,
                     uploads: list[UploadFile] = File(...)) -> dict[str, Any]:
    results = []
    for upload in uploads:
        data = await upload.read()
        results.append(await asyncio.to_thread(
            files.add, chat_id, upload.filename or "file", data))
    return {"attachments": results}


@app.put("/api/chats/{chat_id}/upload")
async def api_upload_stream(chat_id: str, request: Request, name: str = Query(...),
                            size: int = Query(0)) -> Any:
    """v2.1: stream one file of any size (default limit 30 GB) straight to disk.

    The browser sends the raw file as the request body, so nothing is held in
    memory and there is no temporary copy - a 30 GB video is written once.
    """
    declared = size or int(request.headers.get("content-length") or 0)
    if why := files.check_space(declared):
        return JSONResponse({"ok": False, "error": why}, status_code=413)
    target = files.new_path(chat_id, name)
    limit = files.max_upload()
    written = 0
    try:
        with open(target, "wb") as fh:
            async for chunk in request.stream():
                written += len(chunk)
                if written > limit:
                    raise ValueError(f"over the {files.human(limit)} limit")
                fh.write(chunk)
    except (ValueError, OSError, ClientDisconnectError) as exc:
        target.unlink(missing_ok=True)
        return JSONResponse({"ok": False, "error": f"Upload stopped: {exc}"},
                            status_code=413 if isinstance(exc, ValueError) else 400)
    if written == 0:
        target.unlink(missing_ok=True)
        return JSONResponse({"ok": False, "error": "The file was empty."},
                            status_code=400)
    result = await asyncio.to_thread(files.add_path, chat_id, name, target)
    return result


@app.get("/api/upload-limit")
async def api_upload_limit() -> dict[str, Any]:
    import shutil
    files.ATTACH_DIR.mkdir(parents=True, exist_ok=True)
    return {"max_bytes": files.max_upload(),
            "free_bytes": shutil.disk_usage(files.ATTACH_DIR).free}


@app.get("/api/chats/{chat_id}/attachments")
async def api_attachments(chat_id: str) -> dict[str, Any]:
    return {"attachments": await asyncio.to_thread(files.for_chat, chat_id)}


@app.get("/api/attachments/{attach_id}")
async def api_attachment(attach_id: str) -> Any:
    found = await asyncio.to_thread(files.bytes_of, attach_id)
    if not found:
        return JSONResponse({"error": "No such attachment"}, status_code=404)
    media, data = found
    return Response(content=data, media_type=media)


@app.get("/api/attachments/{attach_id}/text")
async def api_attachment_text(attach_id: str, offset: int = 0,
                              limit: int = 8000) -> dict[str, Any]:
    return await asyncio.to_thread(files.text_of, attach_id, offset, limit)


@app.delete("/api/attachments/{attach_id}")
async def api_attachment_delete(attach_id: str) -> dict[str, Any]:
    return {"ok": await asyncio.to_thread(files.delete, attach_id)}


@app.get("/api/files")
async def api_files() -> dict[str, Any]:
    return await asyncio.to_thread(file_tools.status)


@app.post("/api/files/folders")
async def api_folder_add(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    result = await asyncio.to_thread(file_tools.add_folder,
                                     str(body.get("path", "")))
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    return result


@app.delete("/api/files/folders")
async def api_folder_remove(path: str = Query(...)) -> dict[str, Any]:
    return await asyncio.to_thread(file_tools.remove_folder, path)


# ---------------------------------------------------------------------------
# Scheduled tasks
# ---------------------------------------------------------------------------

@app.get("/api/tasks")
async def api_tasks() -> dict[str, Any]:
    return await asyncio.to_thread(scheduler.summary)


@app.post("/api/tasks")
async def api_task_save(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    key = str(body.get("key", "")).strip()
    fields = {k: v for k, v in body.items()
              if k in scheduler.Task.__dataclass_fields__ and k != "key"}

    if key and (existing := scheduler.get(key)):
        for name, value in fields.items():
            setattr(existing, name, value)
        try:
            scheduler.parse_cron(existing.cron)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        scheduler.save(existing)
        return {"ok": True, "key": key, "task": existing.to_dict()}

    result = await asyncio.to_thread(
        scheduler.create, str(body.get("name", "")), str(body.get("prompt", "")),
        str(body.get("cron", "0 8 * * 1-5")),
        **{k: v for k, v in fields.items() if k not in ("name", "prompt", "cron")})
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    return result


@app.delete("/api/tasks/{key}")
async def api_task_delete(key: str) -> dict[str, Any]:
    return {"ok": await asyncio.to_thread(scheduler.delete, key)}


@app.post("/api/tasks/{key}/run")
async def api_task_run(key: str) -> dict[str, Any]:
    result = await scheduler.run_once(key, reason="run now")
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    return result


@app.post("/api/tasks/{key}/windows")
async def api_task_windows(key: str, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    if body.get("remove"):
        return await asyncio.to_thread(scheduler.unregister_windows, key)
    result = await asyncio.to_thread(scheduler.register_windows, key)
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    return result


@app.get("/api/tasks/{key}/preview")
async def api_task_preview(cron: str = Query(...), count: int = 5) -> dict[str, Any]:
    """Show the next few firing times, so a cron can be checked before saving."""
    try:
        scheduler.parse_cron(cron)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    from datetime import datetime
    upcoming, moment = [], datetime.now()
    for _ in range(max(1, min(count, 10))):
        moment = scheduler.next_run(cron, moment)
        if not moment:
            break
        upcoming.append(moment.strftime("%a %d %b %Y, %H:%M"))
    return {"ok": True, "text": scheduler.describe(cron), "next": upcoming}


# ---------------------------------------------------------------------------
# MCP servers
# ---------------------------------------------------------------------------

@app.get("/api/mcp/discover")
async def api_mcp_discover() -> dict[str, Any]:
    """What this machine already has configured, from every host we can read."""
    return await asyncio.to_thread(discovery.scan_report)


@app.get("/api/mcp/servers")
async def api_mcp_servers() -> dict[str, Any]:
    return {"servers": registry.status()}


@app.post("/api/mcp/servers")
async def api_mcp_add(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    name = (body.get("name") or "").strip()
    spec = body.get("spec") or {}
    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)
    if not spec.get("command") and not spec.get("url"):
        return JSONResponse({"error": "spec needs a command or a url"},
                            status_code=400)
    registry.add(name, spec, origin=body.get("origin", "manual"), enabled=False)
    return {"ok": True, "servers": registry.status()}


@app.post("/api/mcp/import")
async def api_mcp_import(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Copy discovered servers into AEGIS's own config. Still disabled."""
    wanted = set(body.get("keys") or [])
    found = await asyncio.to_thread(discovery.scan)
    imported: list[str] = []
    for entry in found:
        if entry.key in wanted and entry.spec:
            registry.add(entry.name, entry.spec, origin=entry.origin, enabled=False)
            imported.append(entry.name)
    return {"ok": True, "imported": imported, "servers": registry.status()}


@app.post("/api/mcp/servers/{name}/enable")
async def api_mcp_enable(name: str) -> dict[str, Any]:
    result = await registry.enable(name)
    return {**result, "servers": registry.status()}


@app.post("/api/mcp/servers/{name}/disable")
async def api_mcp_disable(name: str) -> dict[str, Any]:
    result = await registry.disable(name)
    return {**result, "servers": registry.status()}


@app.delete("/api/mcp/servers/{name}")
async def api_mcp_remove(name: str) -> dict[str, Any]:
    await registry.disable(name)
    registry.remove(name)
    return {"ok": True, "servers": registry.status()}


# ---------------------------------------------------------------------------
# Tools, approvals, computer use
# ---------------------------------------------------------------------------

@app.get("/api/tools")
async def api_tools() -> dict[str, Any]:
    return {**tools.summary(),
            "policy": gate.policy(),
            "policies": list(POLICIES),
            "decisions": settings.get("tool_decisions") or {}}


@app.post("/api/tools/policy")
async def api_tools_policy(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    policy = body.get("policy")
    if policy not in POLICIES:
        return JSONResponse({"error": f"policy must be one of {POLICIES}"},
                            status_code=400)
    settings.set("tool_policy", policy)
    return {"ok": True, "policy": policy}


@app.post("/api/tools/decisions")
async def api_tools_decisions(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    if body.get("clear"):
        gate.forget_all()
    elif (name := body.get("tool")):
        gate.remember(name, body.get("decision", ""))
    return {"decisions": settings.get("tool_decisions") or {}}


@app.get("/api/approvals")
async def api_approvals() -> dict[str, Any]:
    return {"pending": gate.list()}


@app.post("/api/approvals/{approval_id}")
async def api_approve(approval_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    resolved = gate.resolve(approval_id, bool(body.get("approved")),
                            bool(body.get("remember")))
    return {"ok": resolved}


@app.get("/api/computer")
async def api_computer() -> dict[str, Any]:
    return computer.status()


@app.post("/api/computer")
async def api_computer_toggle(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    settings.set("computer_use_enabled", bool(body.get("enabled")))
    return computer.status()


# ---------------------------------------------------------------------------
# Browser use
# ---------------------------------------------------------------------------

@app.get("/api/browser")
async def api_browser() -> dict[str, Any]:
    return await browser.status()


@app.post("/api/browser")
async def api_browser_settings(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    for key in ("browser_use_enabled", "browser_headless"):
        short = key.replace("browser_", "").replace("_enabled", "")
        if key in body:
            settings.set(key, bool(body[key]))
        elif short in body:
            settings.set(key, bool(body[short]))
    if "cdp_url" in body:
        settings.set("browser_cdp_url", str(body["cdp_url"]).strip())
    if "target" in body:
        want = str(body["target"]).strip().lower()
        if want not in browser.TARGETS:
            return JSONResponse(
                {"error": f"target must be one of {', '.join(browser.TARGETS)}"},
                status_code=400)
        settings.set("browser_target", want)
    return await browser.status()


@app.post("/api/browser/close")
async def api_browser_close() -> dict[str, Any]:
    sess = await browser.session()
    await sess.close()
    return await browser.status()


@app.post("/api/browser/open")
async def api_browser_open(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Drive the same browser the agent uses, from the Browser tab.

    Deliberately one session rather than two: you and the agent look at the
    same page, so handing a task over mid-flight - you sign in, it carries on -
    works without either of you losing the thread.
    """
    url = str(body.get("url", "")).strip()
    if not url:
        return JSONResponse({"error": "A url is required."}, status_code=400)
    if not url.startswith(("http://", "https://", "about:")):
        url = "https://" + url
    sess, err = await browser._ready(str(body.get("target", "")))
    if err:
        return JSONResponse({"error": err["text"]}, status_code=400)
    try:
        await sess.page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await sess.page.wait_for_timeout(500)
    except Exception as exc:
        return JSONResponse({"error": f"Could not open {url}: "
                                      f"{type(exc).__name__}: {exc}"},
                            status_code=400)
    return await browser.page_state()


@app.get("/api/browser/control")
async def api_browser_control_get() -> dict[str, Any]:
    return dict(browser.USER_CONTROL)


@app.post("/api/browser/control")
async def api_browser_control(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Take the browser over from the agent, or hand it back."""
    return browser.set_user_control(bool(body.get("user")))


@app.get("/api/browser/page")
async def api_browser_page(max_chars: int = Query(4000)) -> dict[str, Any]:
    return await browser.page_state(max_chars=max_chars)


@app.get("/api/browser/view")
async def api_browser_view() -> Response:
    """A frame of the live page. Polled by the Browser tab."""
    raw = await browser.view_jpeg()
    if raw is None:
        return Response(status_code=204)
    return Response(content=raw, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@app.post("/api/browser/act")
async def api_browser_act(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Click, type, or move through history — the same actions the agent has."""
    action = str(body.get("action", "")).strip()
    sess, err = await browser._ready()
    if err:
        return JSONResponse({"error": err["text"]}, status_code=400)
    try:
        if action == "click":
            ref = int(body.get("ref", 0))
            await sess.page.click(f'[data-aegis-ref="{ref}"]', timeout=15000)
            await sess.page.wait_for_timeout(800)
        elif action == "type":
            ref = int(body.get("ref", 0))
            selector = f'[data-aegis-ref="{ref}"]'
            await sess.page.fill(selector, str(body.get("text", "")), timeout=15000)
            if body.get("submit"):
                await sess.page.press(selector, "Enter")
                await sess.page.wait_for_timeout(1000)
        elif action == "back":
            await sess.page.go_back(wait_until="domcontentloaded", timeout=30000)
        elif action == "forward":
            await sess.page.go_forward(wait_until="domcontentloaded", timeout=30000)
        elif action == "reload":
            await sess.page.reload(wait_until="domcontentloaded", timeout=45000)
        elif action == "scroll":
            await sess.page.mouse.wheel(0, int(body.get("amount", 600)))
            await sess.page.wait_for_timeout(300)
        elif action == "click_xy":
            # A click on the live view: x/y arrive as fractions of the frame.
            size = sess.page.viewport_size or await sess.page.evaluate(
                "({width: innerWidth, height: innerHeight})")
            x = float(body.get("x", 0)) * size["width"]
            y = float(body.get("y", 0)) * size["height"]
            await sess.page.mouse.click(x, y)
            await sess.page.wait_for_timeout(600)
        elif action == "key":
            await sess.page.keyboard.press(str(body.get("key", "Enter")))
            await sess.page.wait_for_timeout(300)
        elif action == "text":
            await sess.page.keyboard.type(str(body.get("text", "")), delay=15)
            await sess.page.wait_for_timeout(200)
        else:
            return JSONResponse({"error": f"Unknown action {action!r}"},
                                status_code=400)
    except Exception as exc:
        return JSONResponse({"error": f"{type(exc).__name__}: {exc}"},
                            status_code=400)
    return await browser.page_state()


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------

@app.get("/api/skills")
async def api_skills() -> dict[str, Any]:
    return await asyncio.to_thread(skills.summary)


@app.get("/api/skills/{name}")
async def api_skill_body(name: str) -> dict[str, Any]:
    skill = skills.registry().get(name) or skills.registry().get(f"pending:{name}")
    if not skill:
        return JSONResponse({"error": "No such skill"}, status_code=404)
    return {"skill": skill.to_dict(include_body=True)}


@app.post("/api/skills/{name}/approve")
async def api_skill_approve(name: str) -> dict[str, Any]:
    result = await asyncio.to_thread(skills.approve, name)
    return {**result, "skills": skills.summary()}


@app.post("/api/skills/{name}/reject")
async def api_skill_reject(name: str) -> dict[str, Any]:
    result = await asyncio.to_thread(skills.reject, name)
    return {**result, "skills": skills.summary()}


@app.delete("/api/skills/{name}")
async def api_skill_delete(name: str) -> dict[str, Any]:
    result = await asyncio.to_thread(skills.delete_user_skill, name)
    return {**result, "skills": skills.summary()}


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------

@app.get("/api/agents")
async def api_agents() -> dict[str, Any]:
    return await asyncio.to_thread(agents.summary)


@app.post("/api/agents")
async def api_agents_toggle(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    if "enabled" in body:
        settings.set("subagents_enabled", bool(body["enabled"]))
    return agents.summary()


# ---------------------------------------------------------------------------
# Memory and the vault
# ---------------------------------------------------------------------------

@app.get("/api/memory")
async def api_memory(q: str = "", limit: int = 50) -> dict[str, Any]:
    facts = await asyncio.to_thread(memory.search_facts, q, "", limit)
    return {"facts": facts, "stats": await asyncio.to_thread(memory.vault_stats)}


@app.post("/api/memory")
async def api_memory_save(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    fact_id = memory.save_fact(str(body.get("text", "")),
                               str(body.get("tags", "")), source="user")
    return {"ok": bool(fact_id), "id": fact_id}


@app.delete("/api/memory/{fact_id}")
async def api_memory_forget(fact_id: int) -> dict[str, Any]:
    return {"ok": memory.forget_fact(fact_id)}


@app.get("/api/vault")
async def api_vault() -> dict[str, Any]:
    return await asyncio.to_thread(memory.vault_stats)


@app.post("/api/vault")
async def api_vault_set(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    if "path" in body:
        settings.set("vault_path", str(body["path"]).strip())
    return await asyncio.to_thread(memory.vault_stats)


@app.post("/api/vault/reindex")
async def api_vault_reindex(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    result = await asyncio.to_thread(memory.reindex_vault, bool(body.get("force")))
    return {**result, "stats": memory.vault_stats()}


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload)}\n\n"


# ---------------------------------------------------------------------------
# llama.cpp runner
# ---------------------------------------------------------------------------

@app.get("/api/runner")
async def api_runner() -> dict[str, Any]:
    return runners.server.status()


@app.post("/api/runner/start")
async def api_runner_start(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    return await asyncio.to_thread(
        runners.server.start, body.get("path", ""),
        context=body.get("context"), gpu_layers=body.get("gpu_layers"),
        threads=body.get("threads"))


@app.post("/api/runner/stop")
async def api_runner_stop() -> dict[str, Any]:
    await asyncio.to_thread(runners.server.stop)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Secrets, OAuth, settings
# ---------------------------------------------------------------------------

KEY_NAMES = ("openai_api_key", "anthropic_api_key", "hf_token",
             "brave_search_key", "tavily_key")


@app.get("/api/keys")
async def api_keys() -> dict[str, Any]:
    names = list(KEY_NAMES)
    return {"backend": vault.backend_name(),
            "keys": [{"name": n, "set": vault.has(n), "masked": vault.masked(n)}
                     for n in names]}


@app.post("/api/keys")
async def api_set_key(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    name = (body.get("name") or "").strip()
    value = (body.get("value") or "").strip()
    if name not in KEY_NAMES:
        return JSONResponse({"error": "unknown key name"}, status_code=400)
    if value:
        vault.put(name, value)
    else:
        vault.delete(name)
    return {"ok": True, "set": bool(value)}


@app.get("/api/oauth")
async def api_oauth() -> dict[str, Any]:
    return {"providers": oauth.status_all()}


@app.post("/api/oauth/{key}/config")
async def api_oauth_config(key: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    if key not in oauth.SPECS:
        return JSONResponse({"error": "unknown provider"}, status_code=404)
    oauth.save_client_config(key, body)
    return {"ok": True, "missing": oauth.missing_config(key)}


@app.post("/api/oauth/{key}/start")
async def api_oauth_start(key: str) -> dict[str, Any]:
    try:
        url = oauth.begin(key, settings.get("port", DEFAULT_PORT))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    import webbrowser
    webbrowser.open(url)
    return {"ok": True, "url": url}


@app.post("/api/oauth/{key}/signout")
async def api_oauth_signout(key: str) -> dict[str, Any]:
    oauth.sign_out(key)
    return {"ok": True}


@app.get(oauth.REDIRECT_PATH, response_class=HTMLResponse)
async def oauth_callback(request: Request) -> HTMLResponse:
    params = request.query_params
    if error := params.get("error"):
        return _callback_page(False, f"{error}: {params.get('error_description', '')}")
    code, state = params.get("code"), params.get("state")
    if not code or not state:
        return _callback_page(False, "The provider did not return an authorisation code.")
    okay, message = await oauth.complete(code, state)
    return _callback_page(okay, message)


def _callback_page(okay: bool, message: str) -> HTMLResponse:
    colour = "#16a34a" if okay else "#dc2626"
    title = "Signed in" if okay else "Sign-in failed"
    return HTMLResponse(f"""<!doctype html><meta charset="utf-8">
<title>{title} · AEGIS</title>
<style>
 body{{font:15px/1.6 system-ui,sans-serif;background:#0f1115;color:#e6e8ee;
      display:grid;place-items:center;height:100vh;margin:0;text-align:center}}
 .card{{max-width:30rem;padding:2rem;border:1px solid #262b36;border-radius:14px;
       background:#161a22}}
 h1{{margin:0 0 .5rem;font-size:1.2rem;color:{colour}}}
 p{{margin:0;color:#9aa3b2}}
</style>
<div class="card"><h1>{title}</h1><p>{message}</p>
<p style="margin-top:1rem">You can close this tab and go back to AEGIS.</p></div>""")


@app.get("/api/settings")
async def api_settings() -> dict[str, Any]:
    return {"settings": settings.all(), "version": VERSION,
            "models_dir": str(MODELS_DIR), "vault_backend": vault.backend_name()}


@app.post("/api/settings")
async def api_settings_update(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    allowed = {"fit_context", "ram_headroom_mb", "igpu_counts_as_ram",
               "default_provider", "default_model", "theme", "mem_bandwidth_gbs",
               "max_steps", "server_policies", "chat_image_budget_mb",
               "vault_path", "subagents_enabled", "browser_prefer_chrome",
               "browser_cdp_url", "browser_headless",
               # v2
               "media_allow_paid", "profile_sharing", "profile_lite",
               "tool_diet", "code_tools_enabled", "web_tools_enabled",
               "workspace_enabled", "searxng_url", "default_route",
               "auto_route_managed",
               # v2.1
               "boost_mode", "text_tools", "max_upload_gb",
               # v2.2
               "needle_enabled", "needle_min_confidence", "needle_max_chars",
               "needle_weights"}
    settings.update({k: v for k, v in body.items() if k in allowed})
    return {"settings": settings.all()}


# ---------------------------------------------------------------------------
# v2: projects
# ---------------------------------------------------------------------------

@app.get("/api/projects")
async def api_projects() -> dict[str, Any]:
    return await asyncio.to_thread(projects.summary)


@app.post("/api/projects")
async def api_project_save(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    result = await asyncio.to_thread(
        projects.save, str(body.get("name") or ""),
        key=str(body.get("key") or ""),
        instructions=str(body.get("instructions") or ""),
        description=str(body.get("description") or ""),
        folder=str(body.get("folder") or ""),
        route=str(body.get("route") or ""),
        skills=[str(x) for x in (body.get("skills") or [])])
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    return result


@app.delete("/api/projects/{key}")
async def api_project_delete(key: str) -> dict[str, Any]:
    return {"ok": await asyncio.to_thread(projects.delete, key)}


@app.post("/api/chats/{chat_id}/project")
async def api_chat_project(chat_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    project = str(body.get("project") or "")
    if project and not projects.get(project):
        return JSONResponse({"error": "No such project"}, status_code=404)
    await asyncio.to_thread(projects.assign, chat_id, project)
    return {"ok": True, "project": project}


@app.get("/api/projects/{key}/notes")
async def api_project_notes(key: str) -> dict[str, Any]:
    if not projects.get(key):
        return JSONResponse({"error": "No such project"}, status_code=404)
    path = projects.workspace(key) / projects.NOTES_FILE
    return {"text": path.read_text("utf-8", errors="replace") if path.exists() else ""}


@app.post("/api/projects/{key}/notes")
async def api_project_notes_save(key: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    if not projects.get(key):
        return JSONResponse({"error": "No such project"}, status_code=404)
    path = projects.workspace(key) / projects.NOTES_FILE
    path.write_text(str(body.get("text") or ""), encoding="utf-8")
    return {"ok": True}


# ---------------------------------------------------------------------------
# v2: Studio (media)
# ---------------------------------------------------------------------------

@app.get("/api/media")
async def api_media_list(kind: str = "", project: str = "",
                         limit: int = 120) -> dict[str, Any]:
    items = await asyncio.to_thread(media.list_media, kind, limit, project)
    return {"items": items}


@app.get("/api/media/{media_id}")
async def api_media_file(media_id: str) -> Any:
    item = await asyncio.to_thread(media.get, media_id)
    if not item or not os.path.isfile(item["path"]):
        return JSONResponse({"error": "No such file"}, status_code=404)
    return FileResponse(item["path"], media_type=item["mime"],
                        filename=os.path.basename(item["path"]),
                        content_disposition_type="inline")


@app.delete("/api/media/{media_id}")
async def api_media_delete(media_id: str) -> dict[str, Any]:
    return {"ok": await asyncio.to_thread(media.delete, media_id)}


@app.post("/api/media/generate")
async def api_media_generate(body: dict[str, Any] = Body(...)) -> Any:
    kind = str(body.get("kind") or "image")
    prompt = str(body.get("prompt") or "")
    extra = {k: body[k] for k in ("width", "height", "seed", "voice", "lyrics",
                                  "duration", "narration") if body.get(k)}
    project = str(body.get("project") or "")
    if project and projects.get(project):
        projects.set_current(project)
    try:
        item = await media.generate(kind, prompt,
                                    backend=str(body.get("backend") or ""),
                                    **extra)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except media.Failed as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    return {"item": item}


@app.get("/api/media-backends")
async def api_media_backends() -> dict[str, Any]:
    return await asyncio.to_thread(media.status)


@app.post("/api/media-backends")
async def api_media_backends_save(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    if body.get("reset"):
        media.reset_backends()
    elif isinstance(body.get("backends"), list):
        media.save_backends(body["backends"])
    if "allow_paid" in body:
        settings.set("media_allow_paid", bool(body["allow_paid"]))
    if body.get("revive"):
        for b in media.backends():
            router.book.clear(f"media:{b['id']}", b["kind"])
    return await asyncio.to_thread(media.status)


# ---------------------------------------------------------------------------
# v2: Super Agent - see how a message would be routed
# ---------------------------------------------------------------------------

@app.get("/api/superagent")
async def api_superagent(text: str = "") -> dict[str, Any]:
    from . import taskroute
    messages = [{"role": "user", "content": text}]
    routing = superagent.route(messages)
    return {**superagent.summary(), "routing": routing.to_dict(),
            "profile": taskroute.profile(messages).to_dict()}


# ---------------------------------------------------------------------------
# v2.1: your agents, the MCP catalogue, checkpoints, what AEGIS knows about you
# ---------------------------------------------------------------------------

@app.get("/api/my-agents")
async def api_my_agents() -> dict[str, Any]:
    from . import custom_agents
    return custom_agents.summary()


@app.post("/api/my-agents")
async def api_my_agents_save(body: dict[str, Any] = Body(...)) -> Any:
    from . import custom_agents
    if body.get("preset"):
        result = custom_agents.add_preset(str(body["preset"]))
    else:
        result = custom_agents.save(
            str(body.get("name") or ""), key=str(body.get("key") or ""),
            instructions=str(body.get("instructions") or ""),
            description=str(body.get("description") or ""),
            skills=[str(x) for x in body.get("skills") or []],
            tools=[str(x) for x in body.get("tools") or []],
            route=str(body.get("route") or ""), boost=str(body.get("boost") or "auto"),
            icon=str(body.get("icon") or ""))
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    return result


@app.delete("/api/my-agents/{key}")
async def api_my_agents_delete(key: str) -> dict[str, Any]:
    from . import custom_agents
    return {"ok": custom_agents.delete(key)}


@app.get("/api/mcp/catalog")
async def api_mcp_catalog() -> dict[str, Any]:
    from .mcp import catalog
    return {"items": await asyncio.to_thread(catalog.listing),
            "prerequisites": catalog.prerequisites()}


@app.post("/api/mcp/catalog/install")
async def api_mcp_catalog_install(body: dict[str, Any] = Body(...)) -> Any:
    from .mcp import catalog
    result = await catalog.install(str(body.get("key") or ""), body.get("values") or {})
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    return {**result, "servers": registry.status()}


@app.get("/api/checkpoints")
async def api_checkpoints() -> dict[str, Any]:
    return {"items": await asyncio.to_thread(file_tools.checkpoints, 200)}


@app.post("/api/checkpoints/restore")
async def api_checkpoint_restore(body: dict[str, Any] = Body(...)) -> Any:
    result = await asyncio.to_thread(file_tools.restore, str(body.get("id") or ""))
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    return result


@app.get("/api/me")
async def api_me() -> dict[str, Any]:
    from . import personal
    return await asyncio.to_thread(personal.summary)


@app.get("/api/me/{name}")
async def api_me_get(name: str) -> dict[str, Any]:
    from . import personal
    return {"name": name, "text": await asyncio.to_thread(personal.raw, name)}


@app.post("/api/me")
async def api_me_save(body: dict[str, Any] = Body(...)) -> Any:
    from . import personal
    if body.get("restore_defaults"):
        return {"ok": True, "restored": personal.restore_defaults()}
    result = await asyncio.to_thread(personal.save, str(body.get("name") or ""),
                                     str(body.get("text") or ""))
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    return result


@app.delete("/api/me/{name}")
async def api_me_delete(name: str) -> dict[str, Any]:
    from . import personal
    return {"ok": await asyncio.to_thread(personal.delete, name)}


# ---------------------------------------------------------------------------
# v2.1: voice and file downloads
# ---------------------------------------------------------------------------

@app.post("/api/voice/transcribe")
async def api_voice_transcribe(file: UploadFile = File(...),
                               language: str = Query("")) -> Any:
    from . import voice
    data = await file.read()
    if not data:
        return JSONResponse({"error": "empty recording"}, status_code=400)
    result = await voice.transcribe(data, file.filename or "speech.webm", language)
    if not result.get("ok"):
        return JSONResponse(result, status_code=502)
    return result


@app.get("/api/files/download")
async def api_file_download(path: str = Query(...)) -> Any:
    """Download a file the agent made - only from inside the fence."""
    try:
        target = file_tools.resolve(path)
    except file_tools.OutsideFence as exc:
        return JSONResponse({"error": str(exc)}, status_code=403)
    if not target.is_file():
        return JSONResponse({"error": "No such file"}, status_code=404)
    return FileResponse(str(target), filename=target.name)


@app.get("/api/workspace")
async def api_workspace(project: str = "") -> dict[str, Any]:
    """Recent files in the workspace, newest first, for the download list."""
    root = projects.workspace(project) if project and projects.get(project) \
        else projects.general_workspace()
    items = []
    for p in root.rglob("*"):
        if p.is_file() and ".aegis-run" not in p.parts and p.stat().st_size < 2_000_000_000:
            items.append({"path": str(p), "name": str(p.relative_to(root)),
                          "bytes": p.stat().st_size, "mtime": p.stat().st_mtime})
    items.sort(key=lambda x: -x["mtime"])
    return {"root": str(root), "items": items[:200]}


# ---------------------------------------------------------------------------
# v2.2: Needle fast intent
# ---------------------------------------------------------------------------

@app.get("/api/intent/status")
async def api_intent_status() -> dict[str, Any]:
    from . import needle_intent
    return needle_intent.status()


@app.post("/api/intent")
async def api_intent(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Is this short message one of AEGIS's own commands? Never an error:
    anything unexpected is simply 'not matched' and the message goes on to
    the model as usual."""
    from . import needle_intent
    return await needle_intent.detect(str(body.get("text") or ""))


@app.post("/api/intent/warmup")
async def api_intent_warmup() -> Any:
    from . import needle_intent
    result = await needle_intent.warmup()
    return result if result.get("ok") else JSONResponse(result, status_code=503)


# ---------------------------------------------------------------------------
# Static UI
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
