<!-- logo here -->

> **⚠️ ApplyPilot** is the original open-source project, created by [Pickle-Pixel](https://github.com/Pickle-Pixel) and first published on GitHub on **February 17, 2026**. We are **not affiliated** with applypilot.app, useapplypilot.com, or any other product using the "ApplyPilot" name. These sites are **not associated with this project** and may misrepresent what they offer. If you're looking for the autonomous, open-source job application agent — you're in the right place.

# ApplyPilot

**Applied to 1,000 jobs in 2 days. Fully autonomous. Open source.**

[![PyPI version](https://img.shields.io/pypi/v/applypilot?color=blue)](https://pypi.org/project/applypilot/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-green.svg)](LICENSE)
[![GitHub stars](https://img.shields.io/github/stars/Pickle-Pixel/ApplyPilot?style=social)](https://github.com/Pickle-Pixel/ApplyPilot)
[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/S6S01UL5IO)




https://github.com/user-attachments/assets/7ee3417f-43d4-4245-9952-35df1e77f2df


---

## What It Does

ApplyPilot is a 6-stage autonomous job application pipeline. It discovers jobs across 5+ boards, scores them against your resume with AI, tailors your resume per job, writes cover letters, and **submits applications for you**. It navigates forms, uploads documents, answers screening questions, all hands-free.

Three commands. That's it.

```bash
pip install applypilot
pip install --no-deps python-jobspy && pip install pydantic tls-client requests markdownify regex
applypilot init          # one-time setup: resume, profile, preferences, API keys
applypilot doctor        # verify your setup — shows what's installed and what's missing
applypilot run           # discover > enrich > score > tailor > cover letters
applypilot run -w 4      # same but parallel (4 threads for discovery/enrichment)
applypilot apply         # autonomous browser-driven submission
applypilot apply -w 3    # parallel apply (3 Chrome instances)
applypilot apply --dry-run  # fill forms without submitting
```

> **Why two install commands?** `python-jobspy` pins an exact numpy version in its metadata that conflicts with pip's resolver, but works fine at runtime with any modern numpy. The `--no-deps` flag bypasses the resolver; the second command installs jobspy's actual runtime dependencies. Everything except `python-jobspy` installs normally.

---

## Two Paths

### Full Pipeline (recommended)
**Requires:** Python 3.11+, Node.js (for npx), Gemini API key (free), Claude Code CLI, Chrome

Runs all 6 stages, from job discovery to autonomous application submission. This is the full power of ApplyPilot.

### Discovery + Tailoring Only
**Requires:** Python 3.11+, Gemini API key (free)

Runs stages 1-5: discovers jobs, scores them, tailors your resume, generates cover letters. You submit applications manually with the AI-prepared materials.

---

## The Pipeline

| Stage | What Happens |
|-------|-------------|
| **1. Discover** | Scrapes 5 job boards (Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs) + 48 Workday employer portals + 30 direct career sites |
| **2. Enrich** | Fetches full job descriptions via JSON-LD, CSS selectors, or AI-powered extraction |
| **3. Score** | AI rates every job 1-10 based on your resume and preferences. Only high-fit jobs proceed |
| **4. Tailor** | AI rewrites your resume per job: reorganizes, emphasizes relevant experience, adds keywords. Never fabricates |
| **5. Cover Letter** | AI generates a targeted cover letter per job |
| **6. Auto-Apply** | Claude Code navigates application forms, fills fields, uploads documents, answers questions, and submits |

Each stage is independent. Run them all or pick what you need.

### Tight flow: search + score, then tailor per click

Run **only discovery, enrichment, and scoring** — no resumes, no cover letters:

```bash
applypilot run discover enrich score
```

Then open the dashboard and click **Generate Resume** only on the jobs you care about:

```bash
applypilot dashboard
```

Each click tailors **that one job on demand** (only if it has no tailored text yet; otherwise the existing tailoring is reused) and renders it into your LaTeX master. No batch `tailor`, no `cover`, no `pdf` stage required. Same thing from the terminal:

```bash
applypilot combine <job-url>
```

Optional subsets, only if you want them:

```bash
applypilot run tailor --min-score 8      # batch-tailor every job >= 8 (skips cover)
applypilot run cover  --min-score 8      # cover letters only
```

> `applypilot run` with **no stage names runs `all` six stages** (which includes cover letters) — always name the stages you want.

---

## ApplyPilot vs The Alternatives

| Feature | ApplyPilot | AIHawk | Manual |
|---------|-----------|--------|--------|
| Job discovery | 5 boards + Workday + direct sites | LinkedIn only | One board at a time |
| AI scoring | 1-10 fit score per job | Basic filtering | Your gut feeling |
| Resume tailoring | Per-job AI rewrite | Template-based | Hours per application |
| Auto-apply | Full form navigation + submission | LinkedIn Easy Apply only | Click, type, repeat |
| Supported sites | Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs, 46 Workday portals, 28 direct sites | LinkedIn | Whatever you open |
| License | AGPL-3.0 | MIT | N/A |

---

## Requirements

| Component | Required For | Details |
|-----------|-------------|---------|
| Python 3.11+ | Everything | Core runtime |
| Node.js 18+ | Auto-apply | Needed for `npx` to run Playwright MCP server |
| Gemini API key | Scoring, tailoring, cover letters | Free tier (15 RPM / 1M tokens/day) is enough |
| Chrome/Chromium | Auto-apply | Auto-detected on most systems |
| Claude Code CLI | Auto-apply | Install from [claude.ai/code](https://claude.ai/code) |

**Gemini API key is free.** Get one at [aistudio.google.com](https://aistudio.google.com). OpenAI and local models (Ollama/llama.cpp) are also supported.

### Optional

| Component | What It Does |
|-----------|-------------|
| CapSolver API key | Solves CAPTCHAs during auto-apply (hCaptcha, reCAPTCHA, Turnstile, FunCaptcha). Without it, CAPTCHA-blocked applications just fail gracefully |

> **Note:** python-jobspy is installed separately with `--no-deps` because it pins an exact numpy version in its metadata that conflicts with pip's resolver. It works fine with modern numpy at runtime.

---

## Configuration

All generated by `applypilot init`:

### `profile.json`
Your personal data in one structured file: contact info, work authorization, compensation, experience, skills, resume facts (preserved during tailoring), and EEO defaults. Powers scoring, tailoring, and form auto-fill.

### `searches.yaml`
Job search queries, target titles, locations, boards. Run multiple searches with different parameters.

### `.env`
API keys and runtime config: `GEMINI_API_KEY`, `LLM_MODEL`, `CAPSOLVER_API_KEY` (optional). Also where you register a LaTeX master for Generate Resume (below).

### Resume files (where to put your resume)

Everything lives in `~/.applypilot/`:

| File | Purpose |
|------|---------|
| `resume.txt` | **Master resume as plain text** — the only resume the AI reads. Used by **Score**, **Tailor**, and **Cover Letter**. Created by `applypilot init`; replace it with your own text any time. |
| `resume.pdf` | Optional master PDF, uploaded during auto-apply. |
| your LaTeX master `.tex` | Used by **Generate Resume** to render a job's tailored content in your own template. Register it in `.env` (see [Generate Resume](#generate-resume-latex-master)). |

Setup by path:

1. **Plain text (required):** put your resume at `~/.applypilot/resume.txt` (and optionally `~/.applypilot/resume.pdf`).
2. **LaTeX (optional, for Generate Resume):** add to `~/.applypilot/.env`:

   ```bash
   APPLYPILOT_BASE_RESUME_TEX=/absolute/path/to/resume.tex
   APPLYPILOT_RESUME_SEARCH_DIR=/absolute/path/to/resume-dir   # optional fallback
   ```

   The `.tex` is only ever read; generated PDFs go to `~/.applypilot/tailored_resumes/`.

### Package configs (shipped with ApplyPilot)
- `config/employers.yaml` - Workday employer registry (48 preconfigured)
- `config/sites.yaml` - Direct career sites (30+), blocked sites, base URLs, manual ATS domains
- `config/searches.example.yaml` - Example search configuration

---

## How Stages Work

### Discover
Queries Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs via JobSpy. Scrapes 48 Workday employer portals (configurable in `employers.yaml`). Hits 30 direct career sites with custom extractors. Deduplicates by URL.

### Enrich
Visits each job URL and extracts the full description. 3-tier cascade: JSON-LD structured data, then CSS selector patterns, then AI-powered extraction for unknown layouts.

### Score
AI scores every job 1-10 against your profile. 9-10 = strong match, 7-8 = good, 5-6 = moderate, 1-4 = skip. Only jobs above your threshold proceed to tailoring.

### Tailor
Generates a custom resume per job: reorders experience, emphasizes relevant skills, incorporates keywords from the job description. Your `resume_facts` (companies, projects, metrics) are preserved exactly. The AI reorganizes but never fabricates.

### Cover Letter
Writes a targeted cover letter per job referencing the specific company, role, and how your experience maps to their requirements.

### Auto-Apply
Claude Code launches a Chrome instance, navigates to each application page, detects the form type, fills personal information and work history, uploads the tailored resume and cover letter, answers screening questions with AI, and submits. A live dashboard shows progress in real-time.

The Playwright MCP server is configured automatically at runtime per worker. No manual MCP setup needed.

```bash
# Utility modes (no Chrome/Claude needed)
applypilot apply --mark-applied URL    # manually mark a job as applied
applypilot apply --mark-failed URL     # manually mark a job as failed
applypilot apply --reset-failed        # reset all failed jobs for retry
applypilot apply --gen --url URL       # generate prompt file for manual debugging
```

---

## Generate Resume (LaTeX master)

The **Tailor** stage produces a job-specific resume as text (`~/.applypilot/tailored_resumes/*.txt`). **Generate Resume** takes that already-paid-for tailoring and injects it into *your own* LaTeX resume, then compiles a fresh PDF — so every application keeps your visual style while still being tuned to the job. It never modifies your master file and never re-analyzes the job (no extra tokens).

### 1. Provide a LaTeX master resume

Point ApplyPilot at your master `.tex` in `~/.applypilot/.env`:

```bash
APPLYPILOT_BASE_RESUME_TEX=/absolute/path/to/resume.tex
# Optional: directories to scan if the path above ever moves or vanishes.
APPLYPILOT_RESUME_SEARCH_DIR=/absolute/path/to/resume-dir
```

Ways to make a resume discoverable (first match wins):

1. `APPLYPILOT_BASE_RESUME_TEX` — the exact path to your master `.tex`.
2. A copy inside `~/.applypilot/` named `resume_base.tex`, `resume.tex`, or `resume.latex`.
3. Auto-discovery under `APPLYPILOT_RESUME_SEARCH_DIR` (default `~/ownwork/resume`) — any `resume*.tex` / `*.latex` that contains `\documentclass` … `\end{document}`.

The master is only ever **read**. If it `\documentclass`-es a custom class (e.g. `res.cls`), keep that `.cls` next to it — it is resolved automatically at compile time.

### 2. Use it

- **Dashboard:** run `applypilot dashboard`, then click **Generate Resume** on any job. If that job has no tailored resume yet, ApplyPilot **tailors it on the spot (that one job only)** and renders it into your LaTeX master. Optionally type guidance (e.g. *"emphasize Kubernetes"*) in the prompt. The card then shows **Resume PDF / Resume TEX** links, and a **Delete Resume** button removes the generated `.tex`/`.pdf`.
- **CLI:**

  ```bash
  applypilot combine <job-url>
  applypilot combine <job-url> --instructions "emphasize Kubernetes"
  ```

### 3. Output & fallback

- Writes a new `~/.applypilot/tailored_resumes/Company_Role.tex` plus a compiled `Company_Role.pdf`. PDF compilation needs a TeX engine (`pdflatex` from TeX Live / MacTeX). Your master file is untouched.
- **No LaTeX master found?** Generation falls back to ApplyPilot's default pipeline and renders the tailored text to PDF via Playwright, so the action still succeeds.

> Generate Resume uses the LLM (`LLM_MODEL_TAILOR`) and is strictly per-job — one click affects only that job.

---

## Generate Cover Letter

**Generate Cover Letter** writes a per-job cover letter on demand, same as Generate Resume — one click, one job. It uses the job's tailored resume when present and the `.env` LLM (`LLM_MODEL_COVER`). The prompt always enforces the **exact company name and job title** and forbids fabricated facts.

- **Dashboard:** click **Generate Cover Letter** on any job card. The card then shows **Cover PDF / Cover TXT** links and a **Delete Cover Letter** button.
- **CLI:** `applypilot cover-letter <job-url>`
- **Tune the prompt:** add `COVER_LETTER_PROMPT` to `~/.applypilot/.env` — extra instructions appended to the built-in prompt:

  ```bash
  COVER_LETTER_PROMPT=Lead with one concrete thing I built that matches this role...
  ```

---

## Inbox insights (Gmail, read-only)

For applied jobs, **Inbox Insights** pulls that company's email thread from Gmail and uses the LLM to extract **status + action items** (next step, deadlines, documents, a suggestion). The result is **cached per job** and never regenerated until you hit **Free**.

Setup (one time):

1. Google Cloud Console: enable **Gmail API**, create an **OAuth client ID → Desktop app**, download the JSON.
2. In `~/.applypilot/.env`:

   ```bash
   GMAIL_CREDENTIALS_PATH=~/.applypilot/gmail_credentials.json
   # GMAIL_LABEL=Job Applications   # optional: restrict the scan to one label
   ```

3. Authorize once: `applypilot gmail-auth` (opens the browser; stores a local token).

Use:

- **Dashboard:** on an applied job, click **Inbox Insights** (then **Refresh** / **Free**).
- **CLI:** `applypilot inbox <job-url>`

Security: scope is **`gmail.readonly`** — it can never send, delete, or modify mail. The token is stored locally (`~/.applypilot/gmail_token.json`, chmod 600); secret paths live in `.env`. Revoke anytime from your Google account.

> Requires the optional deps: `pip install "applypilot[gmail]"` (or `google-auth-oauthlib google-api-python-client`).

---

## Focus

Mark the jobs you're actively applying to with the **Focus** button on the card (next to the title). Focused cards are bolded/highlighted, and the **Focused only** filter at the top shows just those — so you don't lose them in the pile.

---

## CLI Reference

```
applypilot init                         # First-time setup wizard
applypilot doctor                       # Verify setup, diagnose missing requirements
applypilot run [stages...]              # Run pipeline stages (or 'all')
applypilot run --workers 4              # Parallel discovery/enrichment
applypilot run --stream                 # Concurrent stages (streaming mode)
applypilot run --min-score 8            # Override score threshold
applypilot run --dry-run                # Preview without executing
applypilot run --validation lenient     # Relax validation (recommended for Gemini free tier)
applypilot run --validation strict      # Strictest validation (retries on any banned word)
applypilot apply                        # Launch auto-apply
applypilot apply --workers 3            # Parallel browser workers
applypilot apply --dry-run              # Fill forms without submitting
applypilot apply --continuous           # Run forever, polling for new jobs
applypilot apply --headless             # Headless browser mode
applypilot apply --url URL              # Apply to a specific job
applypilot status                       # Pipeline statistics
applypilot dashboard                    # Open HTML results dashboard
applypilot combine URL                  # Render a job's tailored resume into your LaTeX master
```

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, coding standards, and PR guidelines.

---

## License

ApplyPilot is licensed under the [GNU Affero General Public License v3.0](LICENSE).

You are free to use, modify, and distribute this software. If you deploy a modified version as a service, you must release your source code under the same license.
