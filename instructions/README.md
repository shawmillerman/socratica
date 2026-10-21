# Socratica instructions library

The human-authored heart of the app: how the tutor teaches, what it will and won't do, and how it speaks.
Edit these files to change the tutor's behavior; no Python needed. The tutor system prompt is assembled from
them by `utils/instructions.py` (`build_tutor_system_prompt`), in the order listed below.

Never put answer keys here. Course-specific material (rubrics, assignment guides) lives under `data/<course>/`.

## Catalog

| Order | File | What it controls | Used by |
|---|---|---|---|
| 1 | `pedagogy/core_rules.md` | Who Socratica is, the Socratic method, never handing over answers, scaffolding. Tone lives in `voice/`, not here. | Tutor chat |
| 2 | `voice/tone_and_encouragement.md` | The shared voice: patient, warm, encouraging; validate effort; keep it accessible; be specific. Edit here and both the tutor and the writing tutor change. | Tutor chat, writing feedback |
| 3 | `pedagogy/reinforcing_understanding.md` | What counts as demonstrated understanding and how to reinforce it (confirm, restate differently, connect, check question). | Tutor chat |
| 4 | `pedagogy/explaining_course_content.md` | The exception: summaries and explanations of course content are given directly, not Socratically. | Tutor chat |
| 5 | (in code) calendar section | Today's date and term info placeholders. Technical. | Tutor chat |
| 6 | `integrity/using_course_materials.md` | How to treat retrieved course excerpts; do not confuse practice exercises with assignments. | Tutor chat |
| 7 | `system/formatting.md` | Rendering constraints: plain Markdown, no LaTeX, Excel formulas in backticks. | Tutor chat |
| 8 | `integrity/honesty.md` | Never invent what the student said, files, or assignments; ask when the question is ambiguous. | Tutor chat |
| 9 | (in code) `{context}` | Retrieved course excerpts are inserted here. Technical. | Tutor chat |

## Not yet moved here (phase 2)

- Writing tutor prompt and department style guide: `utils/writing_tutor_prompt.txt` (it already pulls in `voice/` through a `{voice}` placeholder)
- A shared "never write work the student could submit" rule, currently worded separately in `pedagogy/core_rules.md` and the writing tutor prompt
- `course_guidelines.txt` (private, gitignored, currently read by nothing)

## Editing safely

- Keep `{today}`, `{term_info}`, `{context}` and `{voice}` out of these files; they are filled in by code. Do not use other curly braces, since the prompt template treats them as placeholders.
- Last reviewed: set this per file when you review wording.
