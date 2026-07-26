# AI Morning Brief — zero-cost edition

A daily AI intelligence brief that emails itself to you at **07:00 IST** and publishes a
searchable archive — running entirely on free infrastructure. **Total cost: ₹0/month.**

No servers, no database service, no email service, no domain purchase.

---

## What replaced what

| Paid design | Free replacement | Trade-off |
|---|---|---|
| Railway / Fly.io container | **GitHub Actions** (unlimited minutes on public repos) | Cron drifts 5–30 min; the send job waits for 07:00 exactly to compensate |
| Neon Postgres + pgvector | **SQLite committed to the repo** | No vector search; dedupe uses URL hashing + token overlap instead of embeddings |
| Claude API | **Gemini Flash free tier** → Groq → GitHub Models → extractive fallback | Lower quality summaries; free-tier prompts may be used for model training |
| Resend / SES | **Gmail SMTP + app password** | 500 recipients/day cap (you need 2); no bounce webhooks |
| Vercel + Next.js dashboard | **GitHub Pages + one static HTML file** | Client-side search only; no server-side API |
| Cloudflare R2 archive | **The git repo is the archive** | Full version history for free |
| Sentry / Better Stack | **Actions failure notifications + a self-alert email** | Less detail; adequate for a one-person system |
| Custom domain | `yourname.github.io/ai-brief` | Free HTTPS included |

The pipeline logic is unchanged: collect → dedupe → rank → summarise → render → send → archive.

---

## Setup (about 30 minutes)

### 1. Create the repo

Make it **public** — that gives unlimited Actions minutes and free GitHub Pages.
Nothing sensitive lives in the repo; all credentials go in GitHub Secrets, which stay
encrypted even on a public repo.

```bash
git init && git add . && git commit -m "initial" && git push
```

### 2. Get a free LLM key (2 minutes)

Go to **aistudio.google.com** → *Get API key*. No credit card. The free tier allows far
more requests per day than this pipeline uses (~8 calls per edition).

Optional second key from **console.groq.com** as a fallback. If both are missing, the
brief still ships — it falls back to extractive summaries built from the source excerpts.

### 3. Create a Gmail app password (5 minutes)

1. Enable 2-Step Verification on the Gmail account you'll send from
2. Google Account → Security → **App passwords** → generate one for "Mail"
3. Copy the 16-character password (ignore the spaces)

Sending from a real Gmail account is *more* deliverable than a brand-new domain — it has
sender history and normal reputation.

### 4. Add repository secrets

Settings → Secrets and variables → **Actions** → New repository secret:

| Secret | Value |
|---|---|
| `GMAIL_USER` | `youraddress@gmail.com` |
| `GMAIL_APP_PASSWORD` | the 16-character app password |
| `RECIPIENTS` | `Shashankbhargava@dhampur.com,Shashankbhargavalive@gmail.com` |
| `GEMINI_API_KEY` | from AI Studio |
| `GROQ_API_KEY` | optional fallback |

Under **Variables** (not secrets), add `ARCHIVE_URL` = `https://YOURNAME.github.io/ai-brief/`

### 5. Turn on Pages

Settings → Pages → Source: *Deploy from a branch* → `main` / `/docs`.

### 6. Test it before trusting it

```bash
pip install -r requirements.txt
python brief.py generate --demo          # sample data, no network, no keys needed
open docs/briefs/*.html                   # check the email design

export GMAIL_USER=... GMAIL_APP_PASSWORD=... RECIPIENTS=your@gmail.com
python brief.py send                      # send yourself one for real
```

Then in the Actions tab, run **generate brief** manually, check the committed output, and
run **send brief** manually. Only after both work by hand should you rely on the schedule.

---

## How it runs

```
00:00 UTC (05:30 IST)   generate brief   collect → dedupe → rank → summarise → render
                                         → commit archive + dedupe memory to the repo
                                         → GitHub Pages redeploys itself

01:20 UTC (06:50 IST)   send brief       checkout the committed edition
                                         → sleep until exactly 07:00:00 IST
                                         → send via Gmail SMTP
                                         → on failure, email yourself an alert
```

The 80-minute gap is deliberate slack. If generation fails you have time to notice and
run it by hand before the send job fires.

---

## Things that will bite you (and what's already done about it)

**GitHub disables scheduled workflows after 60 days of repo inactivity.** The generate job
commits the archive every day, which counts as activity, so this never triggers. If you
ever pause the brief for two months, re-enable the workflow manually.

**Actions cron is not punctual** — it queues behind everyone else's jobs and can start
5–30 minutes late. That's why the send job is scheduled at 06:50 and then sleeps to the
exact minute rather than being scheduled at 07:00.

**Free LLM tiers change without notice.** The provider chain tries Gemini, then Groq, then
GitHub Models, then gives up gracefully and ships extractive summaries. The brief never
fails to arrive because a model endpoint changed. If output quality suddenly drops, check
the "summariser" line in the email footer — it tells you which provider actually answered.

**Gemini's free tier may use your prompts for training.** Everything sent is public news
text, so this is acceptable here. Never extend this pipeline to internal Dhampur documents
on a free tier.

**Corporate mail filters.** `@dhampur.com` is likely Microsoft 365. Ask IT to allow-list
your Gmail address, and add it to your own contacts. Send only to the Gmail address for the
first week, then add the work address.

**Model names drift.** `GEMINI_MODEL` is an environment variable for exactly this reason.
If calls start failing with 404, check the current model list at aistudio.google.com and
update the variable — no code change.

---

## Files

```
brief.py                     the whole pipeline, one file, ~600 lines
sources.yaml                 the feed list — add sources here, no code change
docs/index.html              the static dashboard (GitHub Pages serves this)
docs/data/*.json             one file per edition + latest.json + index.json
docs/briefs/*.html           the rendered email, permanently linkable
data/brief.db                SQLite: dedupe memory (14 days) + edition store
.github/workflows/           generate.yml (05:30 IST) · send.yml (07:00 IST)
```

## Commands

```bash
python brief.py generate            # build today's edition
python brief.py generate --demo     # build from sample data, no network
python brief.py send                # email it
python brief.py send --dry-run      # validate without sending
python brief.py send --to me@x.com  # send to one address only
python brief.py send --wait-until 07:00
python brief.py preview             # path to the last rendered edition
```

## When it doesn't arrive

1. Actions tab → check which job failed and read the log
2. Re-run the failed job (button, top right)
3. Or locally: `python brief.py generate && python brief.py send`
4. Worst case, the extractive fallback means step 3 works even with every LLM key removed

Ship something. A headlines-only brief at 07:05 beats a perfect brief at 11:00.

---

## What to add later, still free

- **Telegram delivery** — a bot token is free and has no sending limits; ~20 lines
- **More sources** — `sources.yaml` only; consider vendor blogs, Papers with Code, GitHub trending
- **Better dedupe** — cache a small sentence-transformer model with `actions/cache` and swap
  the token-overlap check for cosine similarity
- **Weekly deep dive** — a Sunday workflow reading the last 7 JSON files with a longer prompt

## When to start paying

Only two things are genuinely worth money later, and neither is urgent:

1. **A paid LLM tier (~$5–18/month)** if summary quality matters more than cost. Everything
   else stays free — swap the provider in `LLM.complete()`.
2. **A domain + email service (~$2/month)** if you ever send to more than a handful of
   people, at which point Gmail SMTP stops being appropriate.
