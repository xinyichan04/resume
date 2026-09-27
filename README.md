# Résumé: Xinyi (Cindy) Bin

LaTeX source of the résumé, with git as its version history.

## Layout
- `resume.tex`: the résumé (layout matches the original Word file: Letter, Times 10pt, 0.3"/0.5" margins).
- `skills.tex`: the **Skills** section. It's the only part tailoring rewrites.
- `skills-bank.json`: the skills she has. Tailoring also adds skills the JD requires that she has not listed (Cindy, 2026-09-26); each report lists those as a study list for interview prep.

## Branches
| branch | use |
|---|---|
| `main` | general SWE (the original) |
| `infra` | infrastructure / systems / networking / low-latency |
| `ai` | AI / ML / LLM roles |
| `finance` | trading / fintech / quant-dev roles |
| `swe` | general software engineering (same as main to start) |
| `tailored/<company>-<role>-<date>` | one per application, branched off the closest track branch |

## Rules
- One page, same font sizes as the original. When space runs short, the least JD-relevant skills are dropped.
- Experience, Education and Projects are never changed by tailoring.

Compile: `tectonic resume.tex` (or upload to Overleaf; pdfLaTeX works).
