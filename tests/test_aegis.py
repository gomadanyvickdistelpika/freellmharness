"""Verification suite.

Not a mock farm: the GGUF parser is tested against a real file this script
builds byte by byte, and the fit calculator is tested against hardware profiles
including the exact machine AEGIS was written for.
"""

from __future__ import annotations

import os
import struct
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("AEGIS_DATA_DIR", tempfile.mkdtemp(prefix="aegis-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, report, section  # noqa: E402

from aegis import catalog, gguf, hardware, oauth, vault  # noqa: E402
from aegis.hardware import GB, MB, GPU, Hardware  # noqa: E402


# ---------------------------------------------------------------------------
# 1. GGUF parser against a file we construct ourselves
# ---------------------------------------------------------------------------

def _kv_string(key: str, value: str) -> bytes:
    kb = key.encode()
    vb = value.encode()
    return (struct.pack("<Q", len(kb)) + kb + struct.pack("<I", gguf.STRING)
            + struct.pack("<Q", len(vb)) + vb)


def _kv_u32(key: str, value: int) -> bytes:
    kb = key.encode()
    return (struct.pack("<Q", len(kb)) + kb + struct.pack("<I", gguf.U32)
            + struct.pack("<I", value))


def _kv_str_array(key: str, values: list[str]) -> bytes:
    """Token lists look like this and must be skipped, not stored."""
    kb = key.encode()
    out = (struct.pack("<Q", len(kb)) + kb + struct.pack("<I", gguf.ARRAY)
           + struct.pack("<I", gguf.STRING) + struct.pack("<Q", len(values)))
    for v in values:
        vb = v.encode()
        out += struct.pack("<Q", len(vb)) + vb
    return out


def build_gguf(path: Path) -> None:
    """A Llama-3.1-8B-shaped header, with the numbers we want to read back."""
    kvs = [
        _kv_string("general.architecture", "llama"),
        _kv_u32("llama.block_count", 32),
        _kv_u32("llama.embedding_length", 4096),
        _kv_u32("llama.attention.head_count", 32),
        _kv_u32("llama.attention.head_count_kv", 8),      # GQA
        _kv_u32("llama.context_length", 131072),
        _kv_u32("general.file_type", 15),                 # Q4_K_M
        _kv_str_array("tokenizer.ggml.tokens", ["a", "b", "c", "hello"]),
        _kv_string("general.name", "Test Llama"),
    ]
    body = b"".join(kvs)
    head = gguf.MAGIC + struct.pack("<IQQ", 3, 291, len(kvs))
    path.write_bytes(head + body)


def test_gguf() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test-8b-Q4_K_M.gguf"
        build_gguf(path)
        info = gguf.read_local(path)

        check("GGUF header parses", info is not None)
        if not info:
            return
        check("architecture read", info.architecture == "llama", info.architecture)
        check("layer count read", info.block_count == 32, str(info.block_count))
        check("GQA kv heads read", info.head_count_kv == 8, str(info.head_count_kv))
        check("context length read", info.context_length == 131072,
              str(info.context_length))
        check("quantisation decoded", info.quantisation == "Q4_K_M", info.quantisation)
        check("head_dim derived", info.head_dim == 128, str(info.head_dim))
        check("string array skipped, not stored",
              "tokenizer.ggml.tokens" not in info.metadata)
        check("key after the array still parsed",
              info.metadata.get("general.name") == "Test Llama")

        # 2 * 32 layers * 4096 ctx * 8 kv heads * 128 head_dim * 2 bytes = 512 MB
        kv = info.kv_cache_bytes(4096)
        check("KV cache maths exact at 4k", kv == 512 * MB, f"{kv / MB:.0f} MB")
        check("KV cache scales linearly with context",
              info.kv_cache_bytes(8192) == 2 * kv)

        # Non-GGUF input must be refused, not guessed at.
        junk = Path(tmp) / "not.gguf"
        junk.write_bytes(b"this is not a gguf file at all, no sir" * 40)
        check("non-GGUF file rejected", gguf.read_local(junk) is None)
        check("missing file rejected", gguf.read_local(Path(tmp) / "nope.gguf") is None)


# ---------------------------------------------------------------------------
# 2. Fit calculator against real machine profiles
# ---------------------------------------------------------------------------

def make_hw(ram_gb: float, avail_gb: float, gpu: GPU | None) -> Hardware:
    return Hardware(
        platform="win32", cpu_name="Test CPU", cpu_cores=4, cpu_threads=8,
        ram_total=int(ram_gb * GB), ram_available=int(avail_gb * GB),
        gpus=[gpu] if gpu else [], disk_free=int(500 * GB),
    )


LATITUDE = make_hw(16, 11, GPU(name="Intel(R) Iris(R) Xe Graphics",
                               integrated=True, vendor="Intel"))
RTX_4090 = make_hw(64, 50, GPU(name="NVIDIA GeForce RTX 4090",
                               vram_total=24 * GB, vram_free=23 * GB,
                               vendor="NVIDIA"))
OLD_ASUS = make_hw(8, 5, None)


def test_fit() -> None:
    info = None
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "t.gguf"
        build_gguf(path)
        info = gguf.read_local(path)

    # A 3B Q4 model, ~2 GB. Should be comfortable on the Latitude.
    small = hardware.assess(2 * GB, None, 4096, LATITUDE)
    check("3B fits the Latitude", small["verdict"] == "green", small["headline"])

    # An 8B Q4 model, ~4.7 GB, with a real 512 MB KV cache at 4k.
    mid = hardware.assess(int(4.7 * GB), info, 4096, LATITUDE)
    check("8B runs on the Latitude at 4k", mid["verdict"] in ("green", "amber"),
          f"{mid['headline']} — needs {mid['breakdown']['required_gb']} GB")
    check("8B uses the real header for KV", mid["kv_source"] == "gguf-header")

    # Same model at 128k context: the KV cache alone becomes 16 GB.
    huge_ctx = hardware.assess(int(4.7 * GB), info, 131072, LATITUDE)
    check("same 8B at 128k context turns red", huge_ctx["verdict"] == "red",
          f"KV alone {huge_ctx['breakdown']['kv_cache_gb']} GB")

    # A 70B Q4 model, ~40 GB. Impossible on a laptop, fine nowhere near 16 GB.
    big = hardware.assess(40 * GB, None, 4096, LATITUDE)
    check("70B refused on the Latitude", big["verdict"] == "red", big["headline"])

    # The 4090 should take the 8B fully on GPU.
    gpu_fit = hardware.assess(int(4.7 * GB), info, 4096, RTX_4090)
    check("8B fully offloads to a 4090", gpu_fit["offload"] == "gpu",
          gpu_fit["headline"])
    check("4090 verdict is green", gpu_fit["verdict"] == "green")

    # 70B Q4 on a 24 GB card: partial offload, not a flat no.
    split = hardware.assess(40 * GB, None, 4096, RTX_4090)
    check("70B on a 4090 reports partial offload",
          split["offload"] == "split" and split["verdict"] == "amber",
          split["headline"])

    # 8 GB machine, 8B model: should not claim it fits.
    tight = hardware.assess(int(4.7 * GB), info, 4096, OLD_ASUS)
    check("8B on an 8 GB machine is not green", tight["verdict"] != "green",
          tight["headline"])

    # Breakdown must add up - the workings have to be honest.
    b = mid["breakdown"]
    total = b["weights_gb"] + b["kv_cache_gb"] + b["overhead_gb"]
    check("breakdown adds up to the total", abs(total - b["required_gb"]) < 0.02,
          f"{total} vs {b['required_gb']}")

    # Bigger context must never produce a smaller requirement.
    ctxs = [2048, 4096, 8192, 32768]
    reqs = [hardware.assess(int(4.7 * GB), info, c, LATITUDE)["required_bytes"]
            for c in ctxs]
    check("requirement is monotonic in context", reqs == sorted(reqs))


# ---------------------------------------------------------------------------
# 3. Catalogue helpers
# ---------------------------------------------------------------------------

def test_catalog() -> None:
    check("parses 8B from a model name", catalog.parse_params("Llama-3.1-8B-Instruct") == 8.0)
    check("parses 1.5B", catalog.parse_params("qwen2.5-coder:1.5b") == 1.5)
    check("parses 137M as 0.137B",
          abs(catalog.parse_params("nomic-embed-text-137M") - 0.137) < 1e-9)
    check("ignores nonsense sizes", catalog.parse_params("model-9000b") == 0.0)
    check("no params found returns 0", catalog.parse_params("mistral-instruct") == 0.0)

    # Real Llama-3.1-8B Q4_K_M ships at roughly 4.92e9 bytes (4.58 GiB).
    # Assert in decimal GB to match how vendors publish sizes.
    size = catalog.params_to_bytes(8.03)
    check("8B Q4_K_M estimate lands near the real 4.92 GB",
          4.6e9 <= size <= 5.2e9, f"{size / 1e9:.2f} GB / {size / GB:.2f} GiB")

    check("quant read from filename",
          gguf.quant_from_filename("Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf") == "Q4_K_M")
    check("IQ quant read from filename",
          gguf.quant_from_filename("model.IQ3_XXS.gguf") == "IQ3_XXS")

    ids = [m["name"] for m in (e.to_dict() for e in catalog.ollama_available(set()))]
    check("shortlist excludes nothing when nothing installed", len(ids) == 15)
    filtered = catalog.ollama_available({"llama3.1:8b"})
    check("shortlist hides already-installed models",
          all(e.name != "llama3.1:8b" for e in filtered))


# ---------------------------------------------------------------------------
# 4. Vault and OAuth
# ---------------------------------------------------------------------------

def test_vault() -> None:
    vault.put("test_key", "sk-secret-value-123456")
    check("secret round-trips", vault.get("test_key") == "sk-secret-value-123456")
    check("mask hides the middle", "secret" not in vault.masked("test_key"),
          vault.masked("test_key"))
    from aegis.config import VAULT_PATH
    raw = VAULT_PATH.read_bytes()
    check("plaintext is not on disk", b"sk-secret-value-123456" not in raw)
    vault.delete("test_key")
    check("delete works", vault.get("test_key") is None)


def test_oauth() -> None:
    check("no ChatGPT/Claude consumer OAuth is shipped",
          "openai" not in oauth.SPECS and "anthropic" not in oauth.SPECS)
    check("HF spec present", "huggingface" in oauth.SPECS)
    check("missing config is reported",
          oauth.missing_config("huggingface") == ["Client ID"])

    try:
        oauth.begin("huggingface")
        check("begin refuses without a client id", False)
    except ValueError:
        check("begin refuses without a client id", True)

    oauth.save_client_config("huggingface", {"client_id": "test-client-id"})
    url = oauth.begin("huggingface", 8817)
    check("auth URL is HF", url.startswith("https://huggingface.co/oauth/authorize"))
    check("PKCE S256 challenge present", "code_challenge_method=S256" in url)
    check("no client_secret leaks into the URL", "client_secret" not in url)
    check("redirect matches the documented URI",
          "redirect_uri=http%3A%2F%2F127.0.0.1%3A8817%2Foauth%2Fcallback" in url)

    # Templated URLs must interpolate the tenant.
    oauth.save_client_config("microsoft", {"client_id": "abc", "tenant": "common",
                                           "endpoint": "https://x.openai.azure.com"})
    auth, token = oauth.SPECS["microsoft"].urls(oauth.client_config("microsoft"))
    check("tenant templated into the auth URL", "/common/oauth2" in auth, auth)
    check("tenant templated into the token URL", "/common/oauth2" in token)

    state = list(oauth._pending.keys())[-1]
    check("PKCE verifier is held server-side",
          len(oauth._pending[state]["verifier"]) >= 43)


# ---------------------------------------------------------------------------
# 5. The app boots and serves
# ---------------------------------------------------------------------------

def test_server() -> None:
    from fastapi.testclient import TestClient
    from aegis.server import app

    with TestClient(app) as client:
        r = client.get("/api/hardware")
        check("GET /api/hardware", r.status_code == 200 and "ram_total_gb" in r.json())

        r = client.get("/api/settings")
        check("GET /api/settings", r.status_code == 200)

        r = client.post("/api/assess", json={"size_bytes": int(4.7 * GB)})
        check("POST /api/assess returns a verdict",
              r.status_code == 200 and r.json()["verdict"] in ("green", "amber", "red"),
              r.json().get("headline", ""))

        r = client.post("/api/assess", json={"size_bytes": 0})
        check("assess with no size does not pretend to know",
              r.json()["headline"] == "Size unknown")

        r = client.get("/api/providers")
        keys = {p["key"] for p in r.json()["providers"]}
        check("all six base providers registered",
              {"ollama", "lmstudio", "openai", "anthropic",
               "codex_cli", "claude_cli"} <= keys, str(sorted(keys)))

        ready = {p["key"]: p["ready"] for p in r.json()["providers"]}
        check("providers with no credentials report not-ready honestly",
              ready.get("openai") is False and ready.get("anthropic") is False)

        r = client.get("/api/oauth")
        check("GET /api/oauth", r.status_code == 200)

        r = client.get("/api/downloads")
        check("GET /api/downloads", r.status_code == 200)

        r = client.get("/api/runner")
        check("GET /api/runner", r.status_code == 200)

        r = client.get("/")
        check("UI is served", r.status_code == 200 and "AEGIS" in r.text)

        r = client.get("/static/app.js")
        check("app.js is served", r.status_code == 200 and len(r.text) > 5000)

        # Unknown provider must fail loudly, not silently produce nothing.
        r = client.post("/api/chat", json={"provider": "does-not-exist",
                                           "model": "x", "messages": []})
        check("unknown provider streams an error", "Unknown provider" in r.text)

        # Path traversal on the delete endpoint must be refused.
        r = client.delete("/api/models/local?path=/etc/passwd")
        check("delete refuses paths outside the models folder",
              r.json()["ok"] is False, r.json()["message"])

        # Unknown key names must not be writable.
        r = client.post("/api/keys", json={"name": "evil", "value": "x"})
        check("unknown key names rejected", r.status_code == 400)


def test_batch_scripts() -> None:
    """Lint the .bat installers for the trap that actually bit.

    %~dp0 ends with a backslash, so "%~dp0" passed as a quoted argument makes
    that backslash escape the closing quote: tar received  C:\\Users\\you\\
    Documents"  and failed with 'could not chdir'. The unpack silently did
    nothing and only a fallback saved it. Nothing in Python catches that, so
    it is checked here as text.
    """
    import re
    from pathlib import Path as P

    root = P(__file__).resolve().parent.parent
    scripts = sorted(root.glob("*.bat"))
    check("the installers ship with the project", len(scripts) >= 2,
          str([s.name for s in scripts]))

    # A quoted %~dp0 handed to a program as an argument is the dangerous form.
    danger = re.compile(r'(-C|-DestinationPath|-LiteralPath|-Path)\s+[\'"]%~dp0[\'"]')
    for script in scripts:
        text = script.read_text(encoding="utf-8", errors="replace")
        hit = danger.search(text)
        check(f"{script.name}: no quoted %~dp0 passed as an argument",
              hit is None, hit.group(0) if hit else "")

    for script in scripts:
        text = script.read_text(encoding="utf-8", errors="replace")
        if "tar -xf" in text or "Expand-Archive" in text:
            check(f"{script.name}: strips the trailing backslash first",
                  "HERE:~-1" in text, "expected the %HERE% guard")
            check(f"{script.name}: has a fallback if tar fails",
                  "Expand-Archive" in text)
            check(f"{script.name}: fails loudly if nothing unpacked",
                  "could not unpack" in text.lower()
                  or "unpack did not" in text.lower()
                  or "no preflight.py" in text.lower(),
                  "an unpack that does nothing must stop the script")
        check(f"{script.name}: forces UTF-8 output",
              "PYTHONUTF8" in text or "PYTHONIOENCODING" in text)


def test_ui_wiring() -> None:
    """Every element app.js reaches for must actually exist.

    This is not pedantry. $('#missing').addEventListener throws a TypeError at
    load time, and because app.js is one script that aborts the rest of it -
    so a single typo in an id does not break one button, it breaks the whole
    window, silently, with the UI simply never wiring itself up. There is no
    Python path that catches that, so it is checked as text.
    """
    import re
    from pathlib import Path as P

    web = P(__file__).resolve().parent.parent / "aegis" / "web"
    html = (web / "index.html").read_text(encoding="utf-8")
    js = (web / "app.js").read_text(encoding="utf-8")

    static_ids = set(re.findall(r'\bid="([A-Za-z][\w-]*)"', html))
    # Ids app.js writes into the DOM itself and then queries back.
    built_ids = set(re.findall(r'\bid="([A-Za-z][\w-]*)"', js))
    known = static_ids | built_ids

    check("index.html defines ids for app.js to find", len(static_ids) > 40,
          str(len(static_ids)))

    wanted = set(re.findall(r"""\$\(\s*['"]#([A-Za-z][\w-]*)['"]\s*\)""", js))
    missing = sorted(wanted - known)
    check("every id app.js queries exists in the page",
          not missing, ", ".join(missing[:8]))

    # The other half of the same mistake: a handler bound to an element that
    # the page has but nothing ever renders into.
    for element in ("budgetList", "autobuildRoute", "cpFreeTier", "budSave",
                    "routeList", "customProviderList",
                    "brUrl", "brGo", "brView", "brElements"):
        check(f"#{element} is on the page", element in static_ids)

    # Newly wired controls must be reachable from the code, not just present.
    for element in ("autobuildRoute", "budSave", "cpFreeTier", "budgetList",
                    "brGo", "brView", "brTarget"):
        check(f"#{element} is wired up in app.js", f"#{element}" in js)

    # A nav button with no matching pane is a tab that opens onto nothing.
    tabs = set(re.findall(r'<button data-tab="([\w-]+)"', html))
    panes = set(re.findall(r'<section class="pane[\w "]*" id="([\w-]+)"', html))
    check("every nav tab has a pane behind it",
          not (tabs - panes), str(sorted(tabs - panes)))
    check("the Browser tab exists", "browser" in tabs and "browser" in panes)


def test_windows_console_encoding() -> None:
    """A Windows console is cp1252. Printing a check mark or an em dash to one
    raises UnicodeEncodeError and kills the run mid-suite - which is exactly
    what happened on the real machine, and looked like a hang.

    This builds a genuine cp1252 stream and pushes the offending characters
    through the reporting path."""
    import io

    import harness

    saved = sys.stdout
    try:
        raw = io.BytesIO()
        # errors='strict' so an unencodable character really does raise, the
        # way a stock Windows console does.
        sys.stdout = io.TextIOWrapper(raw, encoding="cp1252", errors="strict",
                                      newline="")

        exploded = False
        try:
            sys.stdout.write("✓\n")
        except UnicodeEncodeError:
            exploded = True
        ok_raises = exploded

        # The harness must survive the same characters.
        survived = True
        try:
            harness.check("cp1252 probe", True, "tick ✓ dash — dots …")
            harness.section("cp1252 — section")
        except UnicodeEncodeError:
            survived = False
    finally:
        sys.stdout = saved
        harness.PASS[:] = [n for n in harness.PASS if n != "cp1252 probe"]

    check("a raw cp1252 stream really does reject a check mark", ok_raises,
          "if this stops being true the guard is no longer needed")
    check("the test harness survives a cp1252 console", survived,
          "printing test output must never kill the run")

    safe = harness._safe("weights — kv ✓")
    check("unprintable characters are replaced, not dropped silently",
          isinstance(safe, str) and len(safe) > 0, safe)


def test_pythonw_startup() -> None:
    """The desktop shortcut runs pythonw.exe, which gives the process no
    console: sys.stdout and sys.stderr are None. Uvicorn's logging config then
    fails and the app dies with no window and no message. This is what that
    looked like, and what stops it."""
    import logging.config

    import uvicorn

    from aegis.main import bind_streams

    # Results are collected, not printed, while stdout is being tampered with -
    # otherwise the test's own output disappears into the file it rebinds to.
    results: list[tuple[str, bool, str]] = []
    saved_out, saved_err = sys.stdout, sys.stderr
    saved_dunder = (sys.__stdout__, sys.__stderr__)
    try:
        sys.stdout = sys.stderr = None            # type: ignore[assignment]
        sys.__stdout__ = sys.__stderr__ = None    # type: ignore[assignment]

        # Without the guard, building uvicorn's loggers raises.
        raised = False
        try:
            logging.config.dictConfig(dict(uvicorn.config.LOGGING_CONFIG))
        except Exception:
            raised = True
        results.append(("no-console streams break uvicorn's logging config",
                        raised, "if this stops raising, the guard may be moot"))

        bind_streams()
        results.append(("bind_streams replaces a None stdout",
                        sys.stdout is not None, ""))
        results.append(("bind_streams replaces a None stderr",
                        sys.stderr is not None, ""))
        results.append(("bind_streams fixes sys.__stdout__ too",
                        sys.__stdout__ is not None, ""))

        writable = True
        try:
            sys.stdout.write("")
            sys.stderr.write("")
        except Exception as exc:
            writable = False
            results.append(("the replacement streams are writable", False, str(exc)))
        if writable:
            results.append(("the replacement streams are writable", True, ""))

        try:
            logging.config.dictConfig(dict(uvicorn.config.LOGGING_CONFIG))
            results.append(("uvicorn logging configures after the guard", True, ""))
        except Exception as exc:
            results.append(("uvicorn logging configures after the guard",
                            False, str(exc)))

        bound = sys.stdout
        bind_streams()
        results.append(("bind_streams is idempotent", sys.stdout is bound, ""))
    finally:
        sys.stdout, sys.stderr = saved_out, saved_err
        sys.__stdout__, sys.__stderr__ = saved_dunder
        logging.config.dictConfig(dict(uvicorn.config.LOGGING_CONFIG))

    for name, passed, detail in results:
        check(name, passed, detail)


def test_server_tools() -> None:
    """The routes added for MCP, tools, approvals and computer use."""
    from fastapi.testclient import TestClient
    from aegis.server import app

    with TestClient(app) as client:
        r = client.get("/api/tools")
        body = r.json()
        check("GET /api/tools", r.status_code == 200 and "policy" in body)
        check("default approval policy is read-auto/write-ask",
              body["policy"] == "auto_read_ask_write", body["policy"])

        r = client.post("/api/tools/policy", json={"policy": "nonsense"})
        check("an invalid approval policy is rejected", r.status_code == 400)
        r = client.post("/api/tools/policy", json={"policy": "ask_always"})
        check("a valid approval policy is accepted", r.status_code == 200)
        client.post("/api/tools/policy", json={"policy": "auto_read_ask_write"})

        r = client.get("/api/mcp/servers")
        check("GET /api/mcp/servers", r.status_code == 200)

        r = client.get("/api/mcp/discover")
        check("GET /api/mcp/discover", r.status_code == 200
              and "paths_checked" in r.json())

        r = client.post("/api/mcp/servers", json={"name": "x", "spec": {}})
        check("a server with neither command nor url is refused",
              r.status_code == 400)

        r = client.post("/api/mcp/servers",
                        json={"name": "demo", "spec": {"command": "echo"}})
        check("a valid server is stored", r.status_code == 200)
        names = [s["name"] for s in r.json()["servers"]]
        check("stored server appears in the list", "demo" in names, str(names))
        check("a newly added server is not connected",
              all(not s["connected"] for s in r.json()["servers"]))

        r = client.delete("/api/mcp/servers/demo")
        check("a server can be removed",
              "demo" not in [s["name"] for s in r.json()["servers"]])

        r = client.get("/api/approvals")
        check("GET /api/approvals", r.status_code == 200)
        r = client.post("/api/approvals/not-a-real-id", json={"approved": True})
        check("approving an unknown id fails cleanly", r.json()["ok"] is False)

        r = client.get("/api/computer")
        check("GET /api/computer", r.status_code == 200 and "enabled" in r.json())
        check("computer use ships enabled, guarded by the approval gate",
              r.json()["enabled"] is True)


if __name__ == "__main__":
    section("GGUF parser")
    test_gguf()
    section("fit calculator")
    test_fit()
    section("catalogue")
    test_catalog()
    section("vault")
    test_vault()
    section("oauth")
    test_oauth()
    section("server")
    test_server()
    section("installer scripts")
    test_batch_scripts()
    section("UI wiring")
    test_ui_wiring()
    section("Windows console encoding (cp1252)")
    test_windows_console_encoding()
    section("launching without a console (pythonw)")
    test_pythonw_startup()
    section("server: tools, MCP, approvals")
    test_server_tools()

    import test_mcp
    test_mcp.run_all()

    import test_store
    test_store.run_all()

    import test_platform
    test_platform.run_all()

    import test_router
    test_router.run_all()

    import test_files
    test_files.run_all()

    import test_budget
    test_budget.run_all()

    import test_browser
    test_browser.run_all()

    sys.exit(report())
