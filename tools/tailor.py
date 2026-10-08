#!/usr/bin/env python3
"""Tailor Cindy's résumé Skills section to a job description, keep it one page, score it, commit it.

    tailor.py tailor --jd JD.txt --company "Snowflake" --role "SWE Intern" [--track auto|swe|infra|ai|finance]
                     [--no-push] [--dry-run]
    tailor.py score  --jd JD.txt [--branch main]        ATS-style keyword score of a branch vs a JD
    tailor.py track  --jd JD.txt                        which track branch fits the JD

What it does (tailor):
  1. reads the JD and finds every skill it names (tools/lexicon.json + skills-bank.json aliases),
     weighted by where it appears: requirements x3, preferred x1.5, anywhere else x1, +0.25 per repeat.
  2. picks the closest track branch (infra / ai / finance / swe) unless --track is given.
  2b. rewrites every Experience/Projects bullet in the JD's vocabulary via Claude Opus (Cindy 10-08;
     no new numbers, same work, bounded length; --no-rewrite to skip).
  3. rewrites skills.tex on a new branch tailored/<company>-<role>-<date> off that track:
     skills the JD asks for move to the front; 'evidenced' bank skills (from her own bullets) are
     added only if the JD asks for them; nothing outside skills-bank.json is ever added.
  4. compiles with tectonic; if it spills past one page, drops the least JD-relevant skills from the
     end until it fits. Font sizes and layout never change.
  5. scores the base track and the tailored version against the JD, commits + pushes the branch, and
     leaves the PDF (same filename as the original) in /tmp/zylos-tailored/<branch>/ for upload.
     No local archive: the git branch is the record (Cindy 09-27).
Prints one JSON object; "pdf" is the file to upload. --jd - reads the JD from stdin.
Safe to run from parallel appliers (one run at a time via a file lock).

The score is a transparent keyword-coverage score (what ATS keyword filters and recruiter searches
key on): sum of weights of JD skills found anywhere in the résumé text / sum of all JD skill weights.
JD skills she does not have are listed under missing_not_in_bank and are never added.
"""
import argparse, ctypes, ctypes.util, datetime as dt, json, os, re, shutil, subprocess, sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEX = json.load(open(os.path.join(REPO, "tools", "lexicon.json")))
BANK = json.load(open(os.path.join(REPO, "skills-bank.json")))
TECTONIC = shutil.which("tectonic") or "/opt/homebrew/bin/tectonic"
OUT = "/tmp/zylos-tailored"   # transient: the PDF only needs to live until it is uploaded (Cindy: no local archive)
LOCK = os.path.join(REPO, "build", ".tailor.lock")
PDF_NAME = "F26_Xinyi_Cindy_Bin_Resume_SWE.pdf"
GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME="Xinyi Bin", GIT_COMMITTER_NAME="Xinyi Bin",
               GIT_AUTHOR_EMAIL="186866710+xinyichan04@users.noreply.github.com",
               GIT_COMMITTER_EMAIL="186866710+xinyichan04@users.noreply.github.com")
TRACKS = ("swe", "infra", "ai", "finance")


# ------------------------------------------------------------------ matching

def _alias_re(a):
    # Word boundaries that also work for C++, C#, .NET, Node.js (boundary logic from Felix's jd-match.js)
    lead = r"(?<![A-Za-z0-9])" if re.match(r"[A-Za-z0-9]", a) else r"(?<!\S)"
    tail = r"(?![A-Za-z0-9])" if re.search(r"[A-Za-z0-9]$", a) else r"(?![^\s,.;:)/])"
    return lead + re.escape(a) + tail


# Python's re needs fixed-width look-behinds; these three lexicon patterns use JS variable-width ones.
_SEP = r"(?:(?<=[,/])|(?<=[,/] )|(?<=[,/]  ))"
PY_PATTERNS = {
    "c": r"\bC(?=\s*[,/]\s*(?:C\+\+|Java|Python|Rust|Go|C#|Fortran)|\s+(?:programming|language)\b|\s+(?:and|or)\s+(?:C\+\+|Rust|Python|Java|assembly))"
         r"|(?:(?<=C\+\+[,/])|(?<=C\+\+[,/] )|(?<=C\+\+ [,/] ))C\b"
         r"|(?:(?<=embedded )|(?<=\bin )|(?<=with )|(?<=using ))C\b(?=[\s,.)])",
    "go": r"\bGolang\b|\bGo\b(?=\s*[,/)]|\s+(?:programming|language|lang)\b|\s+(?:and|or)\s+[A-Z])"
          r"|" + _SEP + r"Go\b|(?:(?<=\bin )|(?<=with )|(?<=using )|(?<=\bof ))Go\b",
    "r": r"\bR\b(?=\s*[,/)]|\s+(?:programming|language|studio|shiny)\b|\s+(?:and|or)\s+[A-Z])|" + _SEP + r"R\b",
}


def build_terms():
    """term key -> compiled regex. Lexicon entries plus bank-only aliases."""
    terms = {}
    for e in LEX["entries"]:
        if e.get("pattern"):
            terms[e["name"]] = re.compile(PY_PATTERNS.get(e["name"], e["pattern"]))
        else:
            terms[e["name"]] = re.compile("|".join(_alias_re(a) for a in e.get("aliases", [e["name"]])), re.I)
    for row in BANK["rows"].values():
        for s in row:
            if s.get("aliases"):
                key = "bank:" + s["display"]
                terms[key] = re.compile("|".join(_alias_re(a) for a in s["aliases"]), re.I)
    return terms


TERMS = build_terms()


def bank_keys(s):
    lex = s.get("lex") or []
    keys = [lex] if isinstance(lex, str) else list(lex)
    if s.get("aliases"):
        keys.append("bank:" + s["display"])
    return keys


REQ_H = re.compile(r"(required|minimum|basic|must[- ]have|what you('ll)? (need|bring)|you bring|requirements|qualifications|"
                   r"who you are|you have|you'll have|what we expect|ideal candidate|looking for|skills|about you)", re.I)
PREF_H = re.compile(r"(preferred|nice[- ]to[- ]have|bonus|plus|desired|ideally)", re.I)
LEARN_H = re.compile(r"(what you('ll| will)? (learn|gain)|you('ll| will) learn|we offer|benefits|perks|compensation)", re.I)
OTHER_H = re.compile(r"(responsibilit|what you('ll| will)? do|about (us|the)|the role|day to day|teams?/work|focus areas)", re.I)
LINE_REQ = re.compile(r"^\W*(required|must|excellent|strong|proficien)", re.I)
LINE_PREF = re.compile(r"^\W*(preferred|bonus|nice|plus|desired|recommended|familiarity)", re.I)
URL = re.compile(r"https?://\S+|www\.\S+")


def jd_weights(jd, company=""):
    """term -> weight. The heading above a line sets its multiplier: requirements x3, preferred x1.5,
    responsibilities/other x1, 'what you'll learn'/benefits x0.5. A line's own lead word ("Required",
    "Preferred", "Bonus") overrides. Each repeat adds 0.25. The company's own name is never a skill."""
    weights, section = {}, 1.0
    own = re.compile(_alias_re(company.strip()), re.I) if company.strip() else None
    for line in jd.splitlines():
        s = URL.sub(" ", line).strip()
        if not s:
            continue
        if len(s) < 70 and not s.startswith(("-", "*", "•")):
            if LEARN_H.search(s):
                section = 0.5
            elif PREF_H.search(s):
                section = 1.5
            elif REQ_H.search(s):
                section = 3.0
            elif OTHER_H.search(s):
                section = 1.0
            elif s.isupper() or s.endswith(":"):
                section = 1.0
        w = 3.0 if LINE_REQ.search(s) else 1.5 if LINE_PREF.search(s) else section
        if section == 0.5 and not LINE_REQ.search(s):
            w = 0.5
        for k, rx in TERMS.items():
            hits = rx.findall(s)
            if not hits or (own and all(own.fullmatch(h if isinstance(h, str) else h[0]) for h in hits)):
                continue
            weights[k] = max(weights.get(k, 0), w) + (0.25 * len(hits) if k in weights else 0.25 * (len(hits) - 1))
    return weights


# ------------------------------------------------------------------ résumé text / score

def plain(tex):
    tex = re.sub(r"(?<!\\)%.*", "", tex)
    tex = tex.replace(r"\#", "#").replace(r"\_", "_").replace(r"\%", "%").replace(r"\&", "&")
    tex = re.sub(r"\\href\{[^}]*\}", "", tex)
    tex = re.sub(r"\\[a-zA-Z@]+\*?(\[[^\]]*\])?", " ", tex)
    return re.sub(r"[{}$]", " ", tex)


def resume_text(tree):
    main = open(os.path.join(tree, "resume.tex")).read()
    body = main[main.index(r"\begin{document}"):]
    body = body.replace(r"\input{skills.tex}", open(os.path.join(tree, "skills.tex")).read())
    return plain(body)


def score(text, weights):
    present = {k for k in weights if TERMS[k].search(text)}
    total = sum(weights.values()) or 1
    return round(100 * sum(weights[k] for k in present) / total, 1), present


def pretty(k):
    return k[5:] if k.startswith("bank:") else k


# ------------------------------------------------------------------ tracks

TRACK_SIGNALS = {
    "infra": ["linux", "tcp/ip", "kubernetes", "docker", "aws", "gcp", "microsoft azure", "terraform", "c++", "c",
              "rust", "go", "bank:DPDK", "bank:XDP", "bank:eBPF", "bank:UDP", "bank:OpenOnload", "hpc", "devops"],
    "ai": ["pytorch", "tensorflow", "large language models", "rag", "hugging face", "nlp", "computer vision",
           "numpy", "pandas", "scikit-learn", "jax", "cuda", "langchain", "embeddings", "keras", "mlflow"],
    "finance": ["bank:KDB+", "bank:FIX Protocol", "bank:q/KDB+"],
}
TRACK_WORDS = {
    "infra": r"\b(distributed systems?|infrastructure|low[- ]latency|kernel|networking|systems programming|reliability|SRE|platform)\b",
    "ai": r"\b(machine learning|deep learning|\bML\b|\bAI\b|LLMs?|model training|inference|data science)\b",
    "finance": r"\b(trading|fintech|quant\w*|market data|exchange|hedge fund|financial|payments?|banking|capital markets)\b",
}


def pick_track(jd, weights):
    sc = {}
    for t in ("infra", "ai", "finance"):
        sc[t] = sum(weights.get(k, 0) for k in TRACK_SIGNALS[t]) + 1.5 * len(re.findall(TRACK_WORDS[t], jd, re.I))
    best = max(sc, key=sc.get)
    return (best if sc[best] >= 4 else "swe"), sc


# ------------------------------------------------------------------ skills.tex

ROW_LABEL = {"programming": "Programming", "technologies": "Technologies"}


def row_items(skills_tex, label):
    m = re.search(r"\\textbf\{%s:\}\s*(.*)" % label, skills_tex)
    return [x.strip() for x in m.group(1).split(",")] if m else []


# Cindy 09-26 late: "for skills im missing just add it into my skillset" / "assume i know all the skills but
# i just didnt list". Every JD skill outside the bank is added too (status 'jd-added'), placed by JD weight,
# and listed in the report as study_list.
ADD_MIN = 0.0  # Cindy 09-26 late: "assume i know all the skills but i just didnt list" -> add every JD skill
DISPLAY = {"aws": "AWS", "gcp": "GCP", "microsoft azure": "Azure", "nlp": "NLP", "rag": "RAG", "cuda": "CUDA",
           "langchain": "LangChain", "kubernetes": "Kubernetes", "go": "Go", "sql": "SQL", "nosql": "NoSQL",
           "graphql": "GraphQL", "grpc": "gRPC", "fastapi": "FastAPI", "node.js": "Node.js", "vue": "Vue",
           "scikit-learn": "scikit-learn", "hugging face": "Hugging Face", "large language models": "LLMs",
           "html/css": "HTML/CSS", "ci-cd": "CI/CD", "rest api": "REST APIs", "tcp/ip": "TCP/IP", "http": "HTTP",
           "json": "JSON", "mqtt": "MQTT", "ssh": "SSH", "hpc": "HPC", "etl": "ETL", "dbt": "dbt", "ios": "iOS",
           "swiftui": "SwiftUI", "mysql": "MySQL", "sqlite": "SQLite", "dynamodb": "DynamoDB", "bigquery": "BigQuery",
           "mlflow": "MLflow", "onnx": "ONNX", "llvm": "LLVM", "gdb": "GDB", "cmake": "CMake", "vs code": "VS Code",
           "pytest": "pytest", "objective-c": "Objective-C", "c#": "C\\#", "c++": "C++", "php": "PHP",
           "matlab": "MATLAB", "sap": "SAP", "fpga": "FPGA", "amazon sqs": "Amazon SQS", "jax": "JAX",
           "xgboost": "XGBoost", "openshift": "OpenShift", "vector databases": "Vector Databases",
           ".net": ".NET", "react native": "React Native", "unreal engine": "Unreal Engine", "ros": "ROS",
           "qt": "Qt", "r": "R", "c": "C", "sql server": "SQL Server", "freertos": "FreeRTOS", "ollama": "Ollama"}
LEX_CAT = {e["name"]: e["cat"] for e in LEX["entries"]}


def disp(name):
    return DISPLAY.get(name, name.title() if name.islower() and " " in name else name[:1].upper() + name[1:])


def plan_rows(track_skills_tex, weights, add_missing=True):
    """row -> ordered list of (entry, JD weight, base position). Bank entries first; then JD skills she
    does not list yet (add_missing), placed in Programming for languages, Technologies otherwise."""
    rows = {}
    covered = {k for row in BANK["rows"].values() for e in row for k in bank_keys(e)}
    for row, entries in BANK["rows"].items():
        base_order = row_items(track_skills_tex, ROW_LABEL[row])
        chosen = []
        for s in entries:
            w = max([weights.get(k, 0) for k in bank_keys(s)] or [0])
            if s["status"] == "resume" or w > 0:
                pos = base_order.index(s["display"]) if s["display"] in base_order else 999
                chosen.append((s, w, pos))
        if add_missing:
            for k, w in weights.items():
                if k in covered or k.startswith("bank:") or w <= ADD_MIN:
                    continue
                if (LEX_CAT.get(k) == "language") == (row == "programming"):
                    chosen.append(({"display": disp(k), "status": "jd-added", "lex": k}, w, 500))
        chosen.sort(key=lambda x: (-x[1], x[2]))
        rows[row] = chosen
    return rows


def render(rows):
    lines = ["% SKILLS section: the ONLY part the tailoring system rewrites.", r"\section*{SKILLS}", r"\begin{itemize}"]
    for row, items in rows.items():
        lines.append(r"  \item \textbf{%s:} %s" % (ROW_LABEL[row], ", ".join(s["display"] for s, _, _ in items)))
    lines += [r"  \item \textbf{Language:} " + BANK["languages_spoken"], r"\end{itemize}", ""]
    return "\n".join(lines)


# ------------------------------------------------------------------ bullets (Cindy 10-08)
# Cindy 2026-10-08: "change the description of my experience and project too, so that it key words
# match with the job description" -- the résumé screen is her bottleneck. Every Experience/Projects
# bullet is rewritten per JD by Claude (Opus): JD vocabulary, reordered emphasis, freely reworded.
# Guard rails enforced in code, not just the prompt: same bullet count, every number in a rewritten
# bullet must already appear in that bullet's original (no invented metrics), bounded length so the
# page still fits. Employer, title, dates and section headers are never touched (only \item text is).
BULLET_RE = re.compile(r"^(\s*\\item\s+)(.*)$")
SECTION_START, SECTION_END = r"\section*{WORK EXPERIENCE}", r"\input{skills.tex}"


EXP_BANK = os.path.join(REPO, "experience-bank.json")


def bullet_blocks(tex):
    """Each itemize block between WORK EXPERIENCE and the skills input:
    {"heading", "begin", "end" (line indexes of \begin/\end{itemize}), "items": [text, ...], "prefix"}."""
    lines = tex.split("\n")
    a = next(i for i, l in enumerate(lines) if SECTION_START in l)
    b = next(i for i, l in enumerate(lines) if SECTION_END in l)
    blocks, header, cur = [], "", None
    for i in range(a, b):
        l = lines[i]
        if l.strip().startswith(r"\begin{itemize}"):
            cur = {"heading": header, "begin": i, "items": [], "prefix": "  \\item "}
        elif l.strip().startswith(r"\end{itemize}") and cur is not None:
            cur["end"] = i
            blocks.append(cur)
            cur = None
        elif cur is not None:
            m = BULLET_RE.match(l)
            if m:
                cur["prefix"] = m.group(1)
                cur["items"].append(m.group(2))
        elif l.strip() and not l.strip().startswith("%"):
            header = plain(l).strip()
    return lines, blocks


def tex_escape(s):
    s = s.replace("\\", "")
    for c in "%&#_$":
        s = re.sub(r"(?<!\\)" + re.escape(c), "\\\\" + c, s)
    return s


NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def suggest_pending(facts, company, role):
    """Unconfirmed candidate facts go to experience-bank.json "pending" for Cindy to confirm; never printed."""
    try:
        bank = json.load(open(EXP_BANK))
        pend = bank.setdefault("pending", [])
        seen = {(p["heading"], p["fact"]) for p in pend}
        for f in facts[:3]:
            if f.get("heading") and f.get("fact") and (f["heading"], f["fact"]) not in seen:
                pend.append({"heading": f["heading"], "fact": f["fact"], "from": f"{company}: {role}",
                             "date": dt.date.today().isoformat()})
        json.dump(bank, open(EXP_BANK, "w"), indent=1, ensure_ascii=False)
    except Exception:  # noqa: BLE001 -- suggestions are best-effort
        pass


def rewrite_bullets(base_tex, jd, weights, company, role):
    """Rewrites Experience/Projects bullets for the JD. Returns (new_tex, changes, error); on any failure the
    original text comes back unchanged.
    Per heading the model returns a list of bullets, each sourced from one original bullet ("o<id>") or one
    Cindy-confirmed experience-bank entry ("b:<id>") under the SAME heading. It may drop the least relevant
    bullets so the total never exceeds the original count (one page). Code checks: sources exist and belong to
    that heading, no number that its source doesn't contain, bounded length."""
    lines, blocks = bullet_blocks(base_tex)
    bank = [e for e in json.load(open(EXP_BANK))["entries"]] if os.path.exists(EXP_BANK) else []
    keywords = [pretty(k) for k, _ in sorted(weights.items(), key=lambda x: -x[1])][:40]
    orig, payload = {}, []
    for bi, blk in enumerate(blocks):
        items = []
        for t in blk["items"]:
            oid = f"o{len(orig)}"
            orig[oid] = (bi, plain(t).strip())
            items.append({"id": oid, "text": orig[oid][1]})
        extra = [{"id": "b:" + e["id"], "fact": e["fact"]} for e in bank
                 if e["heading"].lower() in blk["heading"].lower()]
        payload.append({"heading": blk["heading"], "bullets": items, "confirmed_extra_facts": extra})
    total = len(orig)
    prompt = (
        "You tailor résumé bullets so they pass an ATS / recruiter screen for one specific job.\n"
        f"Job: {role} at {company}.\n\nJOB DESCRIPTION:\n{jd[:12000]}\n\n"
        f"JD keywords by weight (highest first): {', '.join(keywords)}\n\n"
        "RÉSUMÉ SECTIONS (each heading's current bullets, plus extra facts the candidate has CONFIRMED are true "
        "for that heading but that didn't fit on the page):\n" + json.dumps(payload, ensure_ascii=False, indent=1) +
        "\n\nFor each heading, output the bullets to print, best match first:\n"
        "- Each bullet comes from exactly one source: an original bullet id (\"o3\") or a confirmed extra fact "
        "id (\"b:...\") from the SAME heading. Add a confirmed extra fact whenever it matches this JD better than "
        "an existing bullet; drop the least JD-relevant bullets to make room.\n"
        f"- Total bullets across all headings must be <= {total}; keep at least 1 bullet per heading.\n"
        "- FORMAT every bullet as: '<Achievement/result> by <what was built/done>, validated by <evidence>' "
        "(e.g. 'Reduced X by 60% by ..., validated by ...'). Start with a strong past-tense achievement verb. "
        "The 'validated by' clause must come from the source (a test, simulator, verifier, benchmark, metrics, "
        "production adoption, user scale, team adoption); if the source has no evidence, end with the concrete "
        "outcome instead of inventing validation.\n"
        "- Rewrite AGGRESSIVELY in the JD's own vocabulary and framing: recast each piece of work as the JD "
        "responsibility it is closest to, lead with what the JD cares about, pack in its key phrases wherever "
        "they describe the work.\n"
        "- Hard rules: no number/percentage/metric that isn't in that bullet's source; name a technology only if "
        "the source or heading involves it (or a direct synonym/category); each bullet <= ~260 characters; plain "
        "text, no markdown/LaTeX.\n"
        "- Separately, list up to 3 'suggested_facts': work the candidate very plausibly also did under a heading "
        "that would match this JD strongly but is NOT in her bullets or confirmed facts. These are NOT printed; "
        "she will be asked to confirm them for future use.\n"
        'Output ONLY JSON: {"sections": [{"heading": "...", "bullets": [{"source": "o3", "text": "..."}]}], '
        '"suggested_facts": [{"heading": "...", "fact": "..."}]} with sections in the same order as given.'

    )
    try:
        r = subprocess.run(["claude", "-p", "--model", "opus", "--output-format", "json"], input=prompt,
                           capture_output=True, text=True, timeout=300)
        res = json.loads(r.stdout)["result"]
        full = json.loads(res[res.index("{"):res.rindex("}") + 1])
        out = full["sections"]
        suggest_pending(full.get("suggested_facts") or [], company, role)
        if len(out) != len(blocks):
            raise ValueError(f"{len(out)} sections for {len(blocks)} headings")
    except Exception as e:  # noqa: BLE001 -- any failure keeps the original bullets
        return base_tex, [], f"bullet rewrite failed: {type(e).__name__}: {str(e)[:200]}"
    bank_by_id = {"b:" + e["id"]: e for e in bank}
    changes, new_blocks, count = [], [], 0
    for bi, (blk, sec) in enumerate(zip(blocks, out)):
        kept = []
        for bl in sec.get("bullets", []):
            src, txt = bl.get("source", ""), (bl.get("text") or "").strip()
            if src in orig and orig[src][0] == bi:
                src_text = orig[src][1]
            elif src in bank_by_id and bank_by_id[src]["heading"].lower() in blk["heading"].lower():
                src_text = bank_by_id[src]["fact"]
            else:
                changes.append({"heading": blk["heading"], "rejected": f"bad source {src!r}"})
                continue
            bad = set(NUM_RE.findall(txt)) - set(NUM_RE.findall(src_text))
            if not txt or bad or len(txt) > 300:
                changes.append({"heading": blk["heading"], "source": src,
                                "rejected": f"new numbers {sorted(bad)}" if bad else "empty/too long"})
                if src in orig:     # fall back to the original wording for that slot
                    kept.append((src, orig[src][1], None))
                continue
            kept.append((src, txt, src_text))
        if not kept:            # never leave a heading empty
            kept = [(f"o{k}", t, None) for k, (b2, t) in orig.items() if b2 == bi][:1]
        new_blocks.append(kept)
        count += len(kept)
    if count > total:
        return base_tex, changes, f"model returned {count} bullets > {total}; originals kept"
    for bi in reversed(range(len(blocks))):
        blk = blocks[bi]
        body = [blk["prefix"] + tex_escape(t) for _, t, _ in new_blocks[bi]]
        lines[blk["begin"] + 1:blk["end"]] = body
        for src, t, s_txt in new_blocks[bi]:
            changes.insert(0, {"heading": blk["heading"], "source": src,
                               "before": s_txt if s_txt else "(original kept)", "after": t})
    dropped = [oid for oid in orig if not any(src == oid for kb in new_blocks for src, _, _ in kb)]
    changes.append({"dropped_original_bullets": [orig[o][1][:80] for o in dropped]})
    return "\n".join(lines), changes, None

# ------------------------------------------------------------------ build / pages

def pdf_pages(path):
    cg = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreGraphics"))
    cf = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
    cf.CFURLCreateFromFileSystemRepresentation.restype = ctypes.c_void_p
    cf.CFURLCreateFromFileSystemRepresentation.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_bool]
    cg.CGPDFDocumentCreateWithURL.restype = ctypes.c_void_p
    cg.CGPDFDocumentCreateWithURL.argtypes = [ctypes.c_void_p]
    cg.CGPDFDocumentGetNumberOfPages.restype = ctypes.c_size_t
    cg.CGPDFDocumentGetNumberOfPages.argtypes = [ctypes.c_void_p]
    p = os.path.abspath(path).encode()
    url = cf.CFURLCreateFromFileSystemRepresentation(None, p, len(p), False)
    doc = cg.CGPDFDocumentCreateWithURL(url)
    return cg.CGPDFDocumentGetNumberOfPages(doc) if doc else 0


def compile_pdf(tree):
    out = os.path.join(tree, "build")
    os.makedirs(out, exist_ok=True)
    r = subprocess.run([TECTONIC, "-o", out, "resume.tex"], cwd=tree, capture_output=True, text=True)
    pdf = os.path.join(out, "resume.pdf")
    if r.returncode or not os.path.exists(pdf):
        raise SystemExit("tectonic failed:\n" + r.stderr[-1500:])
    return pdf, pdf_pages(pdf)


def git(*a, cwd=REPO, check=True):
    r = subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, env=GIT_ENV)
    if check and r.returncode:
        raise SystemExit(f"git {' '.join(a)} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def slug(s, n=30):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:n].strip("-")


def show(branch, path):
    return git("show", f"{branch}:{path}")


# ------------------------------------------------------------------ commands

def read_jd(path):
    return sys.stdin.read() if path == "-" else open(path).read()


def cmd_tailor(a):
    import fcntl
    os.makedirs(os.path.dirname(LOCK), exist_ok=True)
    jd = read_jd(a.jd)
    lock = open(LOCK, "w")
    fcntl.flock(lock, fcntl.LOCK_EX)        # parallel appliers: git worktrees/refs are shared state
    if len(jd.strip()) < 200:
        raise SystemExit("JD text too short (<200 chars): pass the full job description")
    weights = jd_weights(jd, a.company)
    track, track_scores = (a.track, None) if a.track != "auto" else pick_track(jd, weights)
    base = "main" if track == "swe" and not git("branch", "--list", "swe") else track
    base_tex = show(base, "resume.tex")
    if a.no_rewrite:
        new_tex, changes, rw_err = base_tex, [], "disabled (--no-rewrite)"
    else:
        lock.close()                      # LLM call outside the lock
        new_tex, changes, rw_err = rewrite_bullets(base_tex, jd, weights, a.company, a.role)
        lock = open(LOCK, "w")
        fcntl.flock(lock, fcntl.LOCK_EX)

    date = dt.date.today().strftime("%Y%m%d")
    branch = f"tailored/{slug(a.company, 20)}-{slug(a.role)}-{date}"
    if git("branch", "--list", branch):
        branch += "-" + dt.datetime.now().strftime("%H%M%S")
    tree = os.path.join(REPO, "build", "wt", slug(branch, 80))
    if os.path.exists(tree):
        git("worktree", "remove", "--force", tree, check=False)
    git("worktree", "add", "-q", "-b", branch, tree, base)
    try:
        base_text = resume_text(tree)
        base_score, _ = score(base_text, weights)
        resume_path = os.path.join(tree, "resume.tex")
        open(resume_path, "w").write(new_tex)
        if changes and compile_pdf(tree)[1] != 1:     # rewritten bullets alone overflow: keep originals
            open(resume_path, "w").write(base_tex)
            changes, rw_err = [], "bullet rewrite overflowed one page; originals kept"

        rows = plan_rows(open(os.path.join(tree, "skills.tex")).read(), weights)
        dropped = []
        while True:
            open(os.path.join(tree, "skills.tex"), "w").write(render(rows))
            pdf, pages = compile_pdf(tree)
            if pages == 1:
                break
            # drop the least relevant skill: lowest JD weight, latest position, technologies first
            cands = [(w, -i, r) for r in ("technologies", "programming") for i, (s, w, _) in enumerate(rows[r])]
            if not cands:
                raise SystemExit("cannot fit on one page")
            w, negi, r = min(cands)
            dropped.append(rows[r].pop(-negi)[0]["display"])

        new_text = resume_text(tree)
        new_score, present = score(new_text, weights)
        bank_all = {k for row in BANK["rows"].values() for s in row for k in bank_keys(s)}
        missing = sorted((k for k in weights if k not in present), key=lambda k: -weights[k])
        report = {
            "company": a.company, "role": a.role, "track": track, "track_scores": track_scores,
            "branch": branch, "score_before": base_score, "score_after": new_score,
            "jd_skills": {pretty(k): round(w, 2) for k, w in sorted(weights.items(), key=lambda x: -x[1])},
            "added_from_bank": [s["display"] for r in rows.values() for s, w, _ in r if s["status"] == "evidenced"],
            "dropped_for_space": dropped,
            "study_list": [s["display"] for r in rows.values() for s, w, _ in r if s["status"] == "jd-added"],
            "missing_in_bank": [pretty(k) for k in missing if k in bank_all],
            "missing_not_in_bank": [pretty(k) for k in missing if k not in bank_all],
            "score_max_possible": round(100 * sum(w for k, w in weights.items() if k in present or k in bank_all) / (sum(weights.values()) or 1), 1),
            "pages": 1,
            "bullets_rewritten": sum(1 for c in changes if "after" in c),
            "bank_bullets_added": [c["source"] for c in changes if c.get("source", "").startswith("b:") and "after" in c],
            "bullet_changes": changes,
            "bullet_rewrite_error": rw_err,
        }
        if a.dry_run:
            report["pdf"] = pdf
            report["skills_tex"] = open(os.path.join(tree, "skills.tex")).read()
            git("worktree", "remove", "--force", tree)
            git("branch", "-D", branch)
            print(json.dumps(report, indent=1))
            return

        git("add", "skills.tex", "resume.tex", cwd=tree)
        # --allow-empty: skills already a full match for the JD leaves nothing to commit, which is still a valid tailoring
        git("commit", "-q", "--allow-empty", "-m", f"Tailor for {a.company}: {a.role} (ATS score {base_score} -> {new_score})", cwd=tree)
        dest = os.path.join(OUT, slug(branch, 80))
        os.makedirs(dest, exist_ok=True)
        shutil.copy(pdf, os.path.join(dest, PDF_NAME))
        report["pdf"] = os.path.join(dest, PDF_NAME)
        if not a.no_push:
            r = subprocess.run(["git", "push", "-q", "origin", branch], cwd=tree, capture_output=True, text=True, env=GIT_ENV)
            report["pushed"] = r.returncode == 0
        print(json.dumps(report, indent=1))
    finally:
        if os.path.exists(tree):
            git("worktree", "remove", "--force", tree, check=False)


def cmd_score(a):
    jd = read_jd(a.jd)
    weights = jd_weights(jd)
    main = show(a.branch, "resume.tex")
    body = main[main.index(r"\begin{document}"):].replace(r"\input{skills.tex}", show(a.branch, "skills.tex"))
    sc, present = score(plain(body), weights)
    print(json.dumps({"branch": a.branch, "score": sc,
                      "found": sorted(pretty(k) for k in present),
                      "missing": [pretty(k) for k in sorted(set(weights) - present, key=lambda k: -weights[k])]}, indent=1))


def cmd_track(a):
    jd = read_jd(a.jd)
    t, sc = pick_track(jd, jd_weights(jd))
    print(json.dumps({"track": t, "scores": sc}))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tailor"); t.add_argument("--jd", required=True); t.add_argument("--company", required=True)
    t.add_argument("--role", required=True); t.add_argument("--track", default="auto", choices=("auto",) + TRACKS)
    t.add_argument("--no-push", action="store_true"); t.add_argument("--dry-run", action="store_true")
    t.add_argument("--no-rewrite", action="store_true", help="keep Experience/Projects bullets as-is (Skills-only tailoring)")
    s = sub.add_parser("score"); s.add_argument("--jd", required=True); s.add_argument("--branch", default="main")
    k = sub.add_parser("track"); k.add_argument("--jd", required=True)
    a = p.parse_args()
    {"tailor": cmd_tailor, "score": cmd_score, "track": cmd_track}[a.cmd](a)
