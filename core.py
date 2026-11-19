"""RepoLens core: git history -> import graph -> Gemini module map, per snapshot."""
import hashlib, json, os, posixpath, re, subprocess, sys, tempfile, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")  # reads GEMINI_API_KEY from repolens/.env
CACHE = Path(__file__).parent / "cache"
# No fixed default: all candidates are pinged at startup and the fastest responder is used.
CANDIDATES = os.environ.get("GEMINI_MODELS", "gemini-3.8-flash,gemini-3.7-flash,gemini-3.5-flash,"
                            "gemini-3.5-flash-lite").split(",")
ATTEMPTS = 12  # Gemini 503s ("high demand") are usually short spikes, so keep trying
TIMEOUT_S = 90  # per call; a hung request moves on to the next model
_ranked = None  # candidates ordered fastest-first, filled by pick_models()
EXTS = (".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go")
SKIP = re.compile(r"(^|/)(node_modules|vendor|dist|build|third_party|\.git|__pycache__)/")
MAX_FILES, N_SNAPSHOTS, HEAD_LINES = 300, 4, 30

IMPORT_RE = {
    ".py": re.compile(r"^\s*(?:from\s+(\.*[\w.]*)\s+import|import\s+([\w.]+))", re.M),
    ".js": re.compile(r"""(?:from\s+|require\(\s*|import\(\s*|^import\s+)['"]([^'"]+)['"]""", re.M),
    ".java": re.compile(r"^import\s+(?:static\s+)?([\w.]+)\s*;", re.M),
    ".go": re.compile(r'^\s*(?:import\s+)?(?:[\w.]+\s+)?"([\w./-]+)"', re.M),
}
for e in (".jsx", ".ts", ".tsx"):
    IMPORT_RE[e] = IMPORT_RE[".js"]


def git(repo, *args, data=None):
    return subprocess.run(["git", "-C", repo, *args], input=data, capture_output=True, check=True).stdout


def read_blobs(repo, sha, paths):
    """Read many files at one commit with a single `git cat-file --batch` call."""
    out = git(repo, "cat-file", "--batch", data="".join(f"{sha}:{p}\n" for p in paths).encode())
    blobs, i = {}, 0
    for p in paths:
        nl = out.index(b"\n", i)
        header = out[i:nl].split()
        if header[-1] == b"missing":
            i = nl + 1
            continue
        size = int(header[2])
        blobs[p] = out[nl + 1 : nl + 1 + size].decode("utf-8", "replace")
        i = nl + 1 + size + 1
    return blobs


def import_edges(files):
    """Regex-extract imports and resolve them to repo files -> [(src, dst)]."""
    keys = {}  # import-like key -> file
    for f in files:
        stem = f.rsplit(".", 1)[0]
        keys.setdefault(stem, f)
        base = posixpath.basename(stem)
        if base in ("__init__", "index") or f.endswith(".go"):
            keys.setdefault(posixpath.dirname(f), f)

    def resolve(p):
        p = p.strip("/")
        if p in keys:
            return keys[p]
        return next((v for k, v in keys.items() if p and k.endswith("/" + p)), None)

    edges = set()
    for f, text in files.items():
        ext = "." + f.rsplit(".", 1)[-1]
        d = posixpath.dirname(f)
        for m in IMPORT_RE[ext].finditer(text):
            spec = next(g for g in m.groups() if g is not None)
            if ext == ".py" and spec.startswith("."):
                dots = len(spec) - len(spec.lstrip("."))
                up = d
                for _ in range(dots - 1):
                    up = posixpath.dirname(up)
                target = posixpath.join(up, spec.lstrip(".").replace(".", "/")).strip("/")
                dst = keys.get(target)
            elif ext in (".py", ".java"):
                dst = resolve(spec.replace(".", "/"))
            elif ext == ".go":
                dst = resolve(spec)
            elif spec.startswith("."):  # JS/TS: only relative imports are internal
                dst = keys.get(posixpath.normpath(posixpath.join(d, spec)))
            else:
                dst = None
            if dst and dst != f:
                edges.add((f, dst))
    return sorted(edges)


def client(timeout_s=TIMEOUT_S):
    from google import genai
    return genai.Client(api_key=os.environ["GEMINI_API_KEY"], http_options={"timeout": timeout_s * 1000})


def pick_models(progress=print):
    """Ping every candidate at once; rank them by who answers first (failures go last)."""
    global _ranked
    if _ranked:
        return _ranked
    c, times = client(20), {}

    def ping(m):
        t = time.time()
        try:
            c.models.generate_content(model=m, contents="Reply with: ok")
            times[m] = time.time() - t
        except Exception:
            pass

    with ThreadPoolExecutor() as pool:
        list(pool.map(ping, CANDIDATES))
    _ranked = sorted(times, key=times.get) + [m for m in CANDIDATES if m not in times]
    progress(f"Using {_ranked[0]}" + (f" ({times[_ranked[0]]:.1f}s)" if times else " (no model answered the ping)"))
    return _ranked


def ask_gemini(prompt):
    c, models = client(), pick_models()
    for attempt in range(ATTEMPTS):
        model = models[attempt % len(models)]
        try:
            r = c.models.generate_content(
                model=model, contents=prompt, config={"response_mime_type": "application/json"})
            return json.loads(r.text)
        except Exception as e:  # busy/rate-limited -> try next model; bad key etc. -> fail fast
            code = getattr(e, "code", None)
            if attempt == ATTEMPTS - 1 or (code is not None and code not in (404, 429) and code < 500):
                raise RuntimeError(f"Gemini error {code}: {getattr(e, 'message', None) or e}") from e
            print(f"  {model} {'error ' + str(code) if code else 'timed out'}, trying next model...", file=sys.stderr)
            # after each full round of models, wait longer before the next round (cap 60s)
            time.sleep(3 if (attempt + 1) % len(models) else min(15 * 2 ** (attempt // len(models)), 60))


PROMPT = """You are a software architect. Below is a snapshot of the repository {repo} at {date}.
Group the source files into 3-8 cohesive architectural modules.

Return JSON exactly in this shape:
{{"modules": [{{"name": "short name", "summary": "one sentence",
   "files": [{{"path": "exact path", "summary": "one short sentence"}}]}}],
 "reading_order": ["5-8 file paths a newcomer should read first, in order"],
 "change_note": "1-2 sentences on how the architecture changed vs the previous snapshot"}}

Rules: every file below must appear in exactly one module. Use the exact paths.
{prev}
## Import graph (file -> file it imports)
{edges}

## Files (first lines of each)
{files}"""


def analyze_snapshot(repo_name, date, files, edges, anchor_names=None, prev_paths=None):
    budget = max(300, min(1500, 250_000 // max(len(files), 1)))
    heads = "\n\n".join(f"### {p}\n" + "\n".join(t.splitlines()[:HEAD_LINES])[:budget] for p, t in files.items())
    prev = ""
    if anchor_names:  # names from the latest snapshot keep the timeline consistent
        prev += "Reuse these module names (from the latest version) where they fit: " + ", ".join(anchor_names) + "\n"
    prev += ("Files in the previous snapshot (compare against these for change_note):\n" + "\n".join(prev_paths)
             if prev_paths else "This is the first snapshot; change_note should describe the starting structure.")
    out = ask_gemini(PROMPT.format(repo=repo_name, date=date, prev=prev,
                                   edges="\n".join(f"{a} -> {b}" for a, b in edges) or "(none)", files=heads))

    # Keep only real files, put anything the model missed into "Other".
    seen, modules = set(), []
    for m in out.get("modules", []):
        fs = [x for x in m.get("files", []) if x.get("path") in files and x["path"] not in seen]
        seen.update(x["path"] for x in fs)
        if fs:
            modules.append({"name": m["name"], "summary": m.get("summary", ""), "files": fs})
    missing = [p for p in files if p not in seen]
    if missing:
        modules.append({"name": "Other", "summary": "Files not assigned to a module.",
                        "files": [{"path": p, "summary": ""} for p in missing]})

    owner = {x["path"]: m["name"] for m in modules for x in m["files"]}
    weights = {}
    for a, b in edges:
        ma, mb = owner[a], owner[b]
        if ma != mb:
            weights[(ma, mb)] = weights.get((ma, mb), 0) + 1
    return {"modules": modules, "edges": [[a, b, w] for (a, b), w in weights.items()],
            "reading_order": [p for p in out.get("reading_order", []) if p in files],
            "change_note": out.get("change_note", "")}


def cache_path(url):
    owner, name = url.rstrip("/").removesuffix(".git").split("/")[-2:]
    return CACHE / f"{owner}__{name}.json", f"{owner}/{name}"


def analyze(url, progress=print):
    path, repo_name = cache_path(url)
    if path.exists():
        return json.loads(path.read_text())
    with tempfile.TemporaryDirectory() as tmp:
        progress(f"Cloning {repo_name}...")
        subprocess.run(["git", "clone", "--bare", "-q", f"https://github.com/{repo_name}.git", tmp], check=True)
        log = git(tmp, "log", "--first-parent", "--reverse", "--format=%H %cs").decode().split("\n")
        commits = [l.split() for l in log if l]
        n = min(N_SNAPSHOTS, len(commits))
        picks = [commits[round(i * (len(commits) - 1) / max(n - 1, 1))] for i in range(n)]

        progress(f"Reading {n} snapshots...")
        snaps = []  # (sha, date, files) — git only, fast
        for sha, date in picks:
            paths = [p for p in git(tmp, "ls-tree", "-r", "--name-only", sha).decode().splitlines()
                     if p.endswith(EXTS) and not SKIP.search(p)][:MAX_FILES]
            if paths:
                snaps.append((sha, date, read_blobs(tmp, sha, paths)))

    def run(i, anchor=None):
        sha, date, files = snaps[i]
        prev_paths = list(snaps[i - 1][2]) if i > 0 else None
        snap = analyze_snapshot(repo_name, date, files, import_edges(files), anchor, prev_paths)
        print(f"  done: {date}", file=sys.stderr)
        return {"sha": sha[:8], "date": date, "n_files": len(files), **snap}

    snapshots = []
    if snaps:
        # Round 1: latest snapshot sets the module names. Round 2: older snapshots in parallel.
        progress("Picking the fastest Gemini model...")
        pick_models(progress)
        progress("Analyzing latest version...")
        latest = run(len(snaps) - 1)
        progress(f"Analyzing {len(snaps) - 1} older snapshots in parallel...")
        names = [m["name"] for m in latest["modules"] if m["name"] != "Other"]
        with ThreadPoolExecutor() as pool:
            snapshots = list(pool.map(lambda i: run(i, names), range(len(snaps) - 1))) + [latest]

    result = {"repo": repo_name, "snapshots": snapshots}
    CACHE.mkdir(exist_ok=True)
    path.write_text(json.dumps(result, indent=1))
    return result


def color(name):
    palette = ["#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f",
               "#edc948", "#b07aa1", "#ff9da7", "#9c755f", "#bab0ac"]
    return palette[int(hashlib.md5(name.encode()).hexdigest(), 16) % len(palette)]


if __name__ == "__main__":
    for u in sys.argv[1:]:
        r = analyze(u)
        print(f"{r['repo']}: {len(r['snapshots'])} snapshots -> {cache_path(u)[0]}")
