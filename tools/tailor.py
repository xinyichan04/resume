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


OPT_EXP = os.path.join(REPO, "optional-experiences.json")
MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def _end_key(dates):
    """'May 2026 - June 2026' -> (2026, 6); 'Present' sorts last-first."""
    last = re.split(r"\s+[-–]\s+", dates)[-1].strip().lower()
    if last.startswith("present"):
        return (9999, 12)
    m = re.match(r"([a-z]{3})[a-z]*\.?\s+(\d{4})", last)
    return (int(m.group(2)), MONTHS.get(m.group(1), 1)) if m else (0, 0)


# Cindy 10-14: "make sure the cs academy is always included ... u can heavily change the bullet point for cs
# academy because i did mostly everything ... i did machine learning, system, django, claude code stuff. so u can
# just add anything thats best fit for the job description"
ALWAYS_KEEP = ["CS Academy"]
FLEX_SCOPE = {"CS Academy": "She built most of the CS Academy platform herself (Software Engineer Intern, Jul-Aug 2026): "
              "machine learning features, backend/systems work, the Django web app (models, Query APIs, views, data layer), "
              "and AI-assisted engineering with Claude Code (multi-agent workflows, custom skills, worktrees). Any of this "
              "may be emphasised or described in the JD's terms."}


def is_always(name):
    return any(k.lower() in (name or "").lower() for k in ALWAYS_KEEP)


def flex_scope(heading):
    return next((v for k, v in FLEX_SCOPE.items() if k.lower() in (heading or "").lower()), None)


def _same_company(org, company):
    """Cindy 10-26 (PlayStation batch): a prior internship at the company being applied to is never swapped out."""
    norm = lambda x: re.sub(r"[^a-z0-9]+", " ", (x or "").lower()).strip()
    o, c = norm(org), norm(company)
    return bool(o and c) and (c in o or o in c)


def swap_experiences(tex, weights, company=None):
    """Cindy 10-09: keep WORK EXPERIENCE at max_experiences entries; an optional experience
    (optional-experiences.json) replaces the least JD-relevant entry only when it scores strictly higher.
    Relevance = sum of JD weights of skills found in the entry's text. Order stays reverse-chronological.
    Returns (tex, info) where info is None when nothing changed."""
    if not os.path.exists(OPT_EXP):
        return tex, None
    cfg = json.load(open(OPT_EXP))
    opts = [e for e in cfg.get("entries", []) if e.get("dates") and e.get("title") and e.get("location")]
    if not opts:
        return tex, None
    lines = tex.split("\n")
    a = next(i for i, l in enumerate(lines) if SECTION_START in l)
    b = next(i for i, l in enumerate(lines) if l.startswith(r"\textbf{Other Experiences") or l.startswith(r"\section*{PROJECTS}"))
    starts = [i for i in range(a + 1, b) if lines[i].startswith(r"\entry{")]
    units = []                                   # each: entry line .. blank lines before the next entry
    for j, s in enumerate(starts):
        e = starts[j + 1] if j + 1 < len(starts) else b
        chunk = lines[s:e]
        dates = re.findall(r"\{([^{}]*)\}", chunk[0])[3]
        org, _, title = re.findall(r"\{([^{}]*)\}", chunk[0])[:3]
        units.append({"name": org, "title": title, "lines": chunk, "end": _end_key(dates)})
    for o in opts:
        body = [rf"\entry{{{o['org']}}}{{{o['location']}}}{{{o['title']}}}{{{o['dates']}}}", r"\begin{itemize}"]
        body += ["  \\item " + x for x in o["bullets"]] + [r"\end{itemize}", ""]
        units.append({"name": o["org"], "title": o["title"], "lines": body, "end": _end_key(o["dates"]), "optional": True})

    def rel(u):
        txt = plain("\n".join(u["lines"]))
        return round(sum(w for k, w in weights.items() if TERMS[k].search(txt)), 2)
    for u in units:
        u["score"] = rel(u)
    cap = cfg.get("max_experiences", 4)
    keep = list(units)
    while len(keep) > cap:
        # drop the lowest-scoring entry; on a tie drop an optional one first, then the oldest
        # Cindy 10-14: CS Academy is ALWAYS included -- never the one dropped
        keep.remove(min([u for u in keep if not is_always(u["name"]) and not _same_company(u["name"], company)] or keep,
                        key=lambda u: (u["score"], not u.get("optional"), u["end"])))
    if not any(u.get("optional") for u in keep):
        return tex, None
    keep.sort(key=lambda u: u["end"], reverse=True)
    body = []
    for u in keep:                               # one blank line between entries, none after the last
        ls = list(u["lines"])
        while ls and not ls[-1].strip():
            ls.pop()
        body += ls + [""]
    rest = lines[b:]
    removed = [u for u in units if u not in keep]
    # Cindy 10-09: an experience left out is still named on the "Other Experiences" line, newest first
    for i, l in enumerate(rest):
        m = re.match(r"\\textbf\{Other Experiences:\s*(.*)\}\s*$", l)
        if m and removed:
            names = [f'{u["name"]} {u["title"]}' for u in sorted(removed, key=lambda u: u["end"], reverse=True)]
            rest[i] = r"\textbf{Other Experiences: " + ", ".join(names + [m.group(1).strip()]) + "}"
            break
    out = lines[:a + 1] + body[:-1] + rest
    info = {"added": [u["name"] for u in keep if u.get("optional")],
            "removed": [u["name"] for u in units if u not in keep],
            "relevance": {u["name"]: u["score"] for u in units}}
    return "\n".join(out), info


PROJ_BANK = os.path.join(REPO, "project-bank.json")
# Cindy 10-24: "look at all the project i have in my github, and when u tailored my resume, u should pick one thats
# the most matching, and write the descirption to match jd ... make sure to add some numeric number". The pool is
# project-bank.json (facts and numbers read from her repos, each number sourced). The PROJECTS section keeps its
# slot count; a bank project replaces the least JD-relevant current one only when it scores strictly higher.
# Current projects keep her own bullets; swapped-in ones use their sourced draft bullets (rewrite_bullets then
# words them to the JD, and its no-new-numbers guard keeps the numbers to what the bank sourced).
PROJ_TITLES = {"job-pipeline": "LLM Agent Job Application Pipeline",
               "bombhole-computer-vision-approach": "Bombhole: Computer Vision Scoring",
               "django_backend_test": "Network Request Timing Benchmark (vLLM, Django)"}
PROJ_CURRENT = {"bloomberg terminal lite": "BloombergTerminalLite", "kernel bypassing": "AF_XDP"}
PROJ_BULLETS = 2


def swap_projects(tex, weights):
    """Returns (tex, info); info is None when nothing changed."""
    if not os.path.exists(PROJ_BANK):
        return tex, None
    bank = {k: v for k, v in json.load(open(PROJ_BANK)).items() if not k.startswith("_")}
    lines = tex.split("\n")
    try:
        a = next(i for i, l in enumerate(lines) if l.startswith(r"\section*{PROJECTS}"))
        b = next(i for i, l in enumerate(lines) if SECTION_END in l)
    except StopIteration:
        return tex, None
    starts = [i for i in range(a + 1, b) if lines[i].startswith(r"\textbf{")]
    if not starts:
        return tex, None
    rel = lambda txt: round(sum(w for k, w in weights.items() if TERMS[k].search(txt)), 2)
    units = []
    for j, st in enumerate(starts):
        e = starts[j + 1] if j + 1 < len(starts) else b
        chunk = lines[st:e]
        m = re.match(r"\\textbf\{([^{}]*)\}", chunk[0])
        name = plain(m.group(1) if m else chunk[0]).strip()
        key = PROJ_CURRENT.get(name.lower())
        stack = " ".join((bank.get(key) or {}).get("stack", []))
        units.append({"name": name, "key": key, "lines": chunk, "score": rel(plain("\n".join(chunk)) + " " + stack)})
    have = {u["key"] for u in units}
    for key, v in bank.items():
        bul = v.get("draft_bullets") or []
        if key in have or key not in PROJ_TITLES or not bul or v.get("hold"):   # hold: awaiting Cindy's OK
            continue
        stack = " ".join(v.get("stack", []))
        best = sorted(bul, key=lambda x: -rel(x))[:PROJ_BULLETS]
        best = [x for x in bul if x in best]                  # keep the bank's order
        body = [rf"\textbf{{{tex_escape(PROJ_TITLES[key])}}}\par\vspace{{2pt}}", r"\begin{itemize}"]
        body += ["  \\item " + tex_escape(x) for x in best] + [r"\end{itemize}", r"\vspace{2pt}"]
        units.append({"name": PROJ_TITLES[key], "key": key, "lines": body, "score": rel(" ".join(bul) + " " + stack), "new": True})
    # "pick one thats the most matching": at most ONE swap -- the best bank project replaces her least
    # JD-relevant current project, only when it scores strictly higher (her own projects win ties)
    cur = [u for u in units if not u.get("new")]
    cands = [u for u in units if u.get("new")]
    if not cands:
        return tex, None
    best, worst = max(cands, key=lambda u: u["score"]), min(cur, key=lambda u: u["score"])
    if best["score"] <= worst["score"]:
        return tex, None
    keep = [u for u in cur if u is not worst] + [best]
    keep.sort(key=lambda u: (u.get("new", False), -u["score"]))   # her current projects first, then swapped-in
    body = []
    for u in keep:
        ls = [l for l in u["lines"]]
        while ls and (not ls[-1].strip() or ls[-1].strip() == r"\vspace{2pt}"):
            ls.pop()
        body += ls + [r"\vspace{2pt}"]
    body = body[:-1] + [""]
    out = lines[:a + 1] + body + lines[b:]
    info = {"added": [u["name"] for u in keep if u.get("new")],
            "removed": [u["name"] for u in units if u not in keep and not u.get("new")],
            "relevance": {u["name"]: u["score"] for u in units}}
    return "\n".join(out), info


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


def log_inferred(heading, clause, company, role):
    """Inferred (unconfirmed, inherent-to-the-work) clauses that went onto a résumé: logged for Cindy's weekly
    veto review (Cindy 10-08: automate, don't ask per tailoring). A vetoed clause moves to "vetoed" and the
    prompt is told never to claim it again."""
    try:
        bank = json.load(open(EXP_BANK))
        bank.setdefault("inferred_used", []).append({"heading": heading, "fact": clause, "for": f"{company}: {role}",
                                                     "date": dt.date.today().isoformat()})
        json.dump(bank, open(EXP_BANK, "w"), indent=1, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass


def rewrite_bullets(base_tex, jd, weights, company, role):
    """Rewrites Experience/Projects bullets for the JD (Cindy 10-08 v3 spec).
    Returns (render, changes, error): render(k) -> tex with the k least-relevant bullets dropped (for page fit).
    - every original bullet is kept, same order; the first bullet of each heading is her summary bullet and is
      never dropped or moved;
    - wording stays close to the original (JD vocabulary woven in, the core of the work preserved);
    - Cindy-confirmed experience-bank facts are appended as a short trailing clause to the most related bullet
      of the same heading -- they never replace the main point;
    - code checks: every number must appear in that bullet's sources; bounded length.
    On any failure render(0) returns the original text."""
    lines, blocks = bullet_blocks(base_tex)
    bank_all = json.load(open(EXP_BANK)) if os.path.exists(EXP_BANK) else {}
    bank, vetoed = bank_all.get("entries", []), bank_all.get("vetoed", [])
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
        payload.append({"heading": blk["heading"], "bullets": items, "confirmed_extra_facts": extra,
                        "never_claim": [v["fact"] for v in vetoed if v["heading"].lower() in blk["heading"].lower()],
                        **({"flexible_scope": flex_scope(blk["heading"])} if flex_scope(blk["heading"]) else {})})
    unchanged = lambda k=0: base_tex  # noqa: E731
    prompt = (
        "You tailor résumé bullets for one specific job so they pass an ATS / recruiter screen.\n"
        f"Job: {role} at {company}.\n\nJOB DESCRIPTION:\n{jd[:12000]}\n\n"
        f"JD keywords by weight (highest first): {', '.join(keywords)}\n\n"
        "RÉSUMÉ SECTIONS (each heading's bullets in order, plus extra facts the candidate CONFIRMED are true for "
        "that heading):\n" + json.dumps(payload, ensure_ascii=False, indent=1) +
        "\n\nRules:\n"
        "1. Keep EVERY bullet, in the SAME order and with the same id. The first bullet under each heading is the "
        "summary of the role (what she mainly did) and must stay first and stay a summary.\n"
        "2. Rewrite each bullet in the JD's vocabulary, but stay CLOSE to the original: keep its core subject, "
        "main system and main result recognisable; weave in the JD's key phrases where they truthfully describe "
        "the work; don't recast it as a different kind of work.\n"
        "3. Format: start with a strong past-tense achievement verb, 'achieved X by doing Y'; add 'validated by Z' "
        "only when the bullet or a fact gives real evidence (simulator, verifier, benchmark, load test, adoption, "
        "user scale). Never invent validation.\n"
        "4. Confirmed extra facts: if one matches the JD, APPEND it as a short trailing clause to the most related "
        "bullet under the same heading (e.g. '..., and added latency/health alerts for feed drops'); the bullet's "
        "main point must still lead. List the fact ids you used in that bullet's 'extras'. Don't create new bullets.\n"
        "4b. If no confirmed fact fits a heading but the JD stresses something that is a NATURAL, INHERENT part of "
        "doing that bullet's own work (e.g. testing, debugging, monitoring, profiling, documenting or code-reviewing "
        "the SAME system), you may append such short clauses wherever they help -- no fixed limit (Cindy 10-08), but at most one per bullet and only when genuinely natural for that work. It must not add a new system, tool, "
        "metric, team or responsibility, and never anything in that heading's never_claim list. Put "
        "'inferred:<the clause>' in that bullet's 'extras'.\n"
        "5. No number/percentage/metric that isn't in that bullet or its extras; name a technology only if the "
        "bullet, its heading or its extras involve it; each bullet <= ~270 characters; plain text.\n"
        "5b. A heading that has 'flexible_scope' (Cindy 10-14) is EXEMPT from rule 2 and rule 4b's limits: rewrite its "
        "bullets HEAVILY so they best fit THIS job -- choose whichever parts of the work described in flexible_scope "
        "(ML, systems/backend, Django, Claude Code/AI tooling) the JD cares about most and describe them in the JD's "
        "terms; you may name any technology the flexible_scope names. Still: same number of bullets, same ids, "
        "no number/metric that isn't already in that heading's bullets.\n"
        "6. drop_priority: ALL non-first bullet ids, least JD-relevant first (used only if the page overflows).\n"
        'Output ONLY JSON: {"bullets": {"o0": {"text": "...", "extras": []}, ...}, "drop_priority": ["o7", ...]}'
    )
    try:
        r = subprocess.run(["claude", "-p", "--model", "opus", "--output-format", "json"], input=prompt,
                           capture_output=True, text=True, timeout=300)
        res = json.loads(r.stdout)["result"]
        full = json.loads(res[res.index("{"):res.rindex("}") + 1])
        new, drop_prio = full["bullets"], full.get("drop_priority") or []
    except Exception as e:  # noqa: BLE001 -- any failure keeps the original bullets
        return unchanged, [], f"bullet rewrite failed: {type(e).__name__}: {str(e)[:200]}"
    bank_by_id = {"b:" + e["id"]: e for e in bank}
    firsts = {f"o{sum(len(b['items']) for b in blocks[:bi])}" for bi in range(len(blocks))}
    final, changes = {}, []
    for oid, (bi, old) in orig.items():
        nb = new.get(oid) or {}
        txt = (nb.get("text") or "").strip()
        ex = [x for x in nb.get("extras") or [] if x in bank_by_id
              and bank_by_id[x]["heading"].lower() in blocks[bi]["heading"].lower()]
        inferred = [x for x in nb.get("extras") or [] if isinstance(x, str) and x.startswith("inferred:")]
        ex += inferred
        if flex_scope(blocks[bi]["heading"]):
            old_nums = set().union(*[set(NUM_RE.findall(t)) for (b2, t) in orig.values() if b2 == bi])
        else:
            old_nums = set(NUM_RE.findall(old))
        allowed = old_nums.union(*[set(NUM_RE.findall(bank_by_id[x]["fact"])) for x in ex if x in bank_by_id])
        bad = set(NUM_RE.findall(txt)) - allowed
        if not txt or bad or len(txt) > 320:
            final[oid] = old
            changes.append({"heading": blocks[bi]["heading"], "source": oid,
                            "rejected": f"new numbers {sorted(bad)}" if bad else "empty/too long"})
            continue
        final[oid] = txt
        for x in inferred:
            log_inferred(blocks[bi]["heading"], x[9:].strip(), company, role)
        changes.append({"heading": blocks[bi]["heading"], "source": oid, "extras": ex, "before": old, "after": txt})
    drop_prio = [d for d in drop_prio if d in orig and d not in firsts]
    drop_prio += [o for o in orig if o not in firsts and o not in drop_prio]   # anything the model omitted
    # Cindy 10-14: CS Academy bullets are dropped last
    drop_prio.sort(key=lambda o: is_always(blocks[orig[o][0]]["heading"]))

    def render(k=0):
        gone = set(drop_prio[:k])
        out = list(lines)
        for bi in reversed(range(len(blocks))):
            blk = blocks[bi]
            ids = [o for o, (b2, _) in orig.items() if b2 == bi and o not in gone]
            out[blk["begin"] + 1:blk["end"]] = [blk["prefix"] + tex_escape(final[o]) for o in ids]
        return "\n".join(out)
    render.drop_prio = drop_prio
    render.orig = orig
    return render, changes, None

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
    base_tex, exp_swap = swap_experiences(base_tex, weights, a.company)
    base_tex, proj_swap = swap_projects(base_tex, weights)
    if a.no_rewrite:
        new_tex, changes, rw_err = (lambda k=0: base_tex), [], "disabled (--no-rewrite)"
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
        dropped_bullets = []
        if changes:
            # page fit: drop least JD-relevant non-summary bullets until the rewritten bullets fit on one page
            for k in range(len(getattr(new_tex, "drop_prio", [])) + 1):
                open(resume_path, "w").write(new_tex(k))
                if compile_pdf(tree)[1] == 1:
                    dropped_bullets = [new_tex.orig[o][1][:80] for o in new_tex.drop_prio[:k]]
                    break
            else:
                open(resume_path, "w").write(base_tex)
                changes, rw_err = [], "bullet rewrite could not fit one page; originals kept"
        else:
            open(resume_path, "w").write(new_tex(0))

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
            "bank_extras_used": sorted({x for c in changes for x in c.get("extras", []) if x.startswith("b:")}),
            "inferred_extras_used": [x[9:] for c in changes for x in c.get("extras", []) if x.startswith("inferred:")],
            "dropped_bullets_for_space": dropped_bullets,
            "experience_swap": exp_swap,
            "project_swap": proj_swap,
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
