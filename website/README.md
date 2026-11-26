# DRBench Pro project website

Static project page: one HTML file, one stylesheet and three small vanilla
JavaScript files. No build step, package installation or analytics. The only
external request is one Google Fonts stylesheet (Outfit + IBM Plex Mono).

## Preview

From the repository root:

```sh
python3 -m http.server 8000 --bind 127.0.0.1 --directory website
```

Open `http://127.0.0.1:8000/`. All resources use relative paths, so the page
also works below the GitHub project path `/DRBenchPro/`.

## Files

| Path | Role |
| --- | --- |
| `index.html` | Page content (all sections) |
| `assets/css/dp.css` | Stylesheet; every class uses the `dp-` prefix |
| `assets/js/dp-figure.js` | Animated method figure (seven phases) |
| `assets/js/dp-results.js` | Leaderboard and domain heatmap, rendered from `assets/data/*.json` |
| `assets/js/dp-site.js` | Copy buttons, top-bar state, active section link |
| `assets/js/overview-video.js` | Overview video start button and error fallback |
| `assets/images/title.svg` | Wordmark (top bar at 171×26, hero at 420×64) |
| `assets/images/favicon.svg` | Tree mark used as the favicon |
| `assets/images/real-logo.svg` | REAL Lab mark (copied from the lab site) |
| `assets/images/video-poster.jpg` | First frame of the overview video, used as poster |

**Cache busting:** CSS/JS links carry `?v=YYYYMMDDx`. Bump it on every page
whenever any CSS or JS file changes. `<img>` `width`/`height` attributes are
display sizes, so a stale stylesheet cannot blow images up.

## Page structure

1. **Top bar** – wordmark (left), section links (centre, hidden below 1060px),
   REAL Lab logo + link to `https://zjureal.com` (right).
2. **Hero** – wordmark title and subtitle, key numbers (24 domains, 144 cases,
   44,102 rubrics, 16 agents), authors and affiliations, and a five-cell button
   strip: Paper, Code, Dataset (HF), Framework Image (HF), Video.
3. **Overview video** – `assets/video/drbenchpro-overview.mp4` (2:07, H.264/AAC,
   `preload="none"`), started from a play pill; native controls appear after start
   and remain available without JavaScript.
4. **Idea** – one sentence: coverage = breadth across directions × depth along chains.
5. **Method figure** (see below), with the paper's Figures 2 and 1 in a collapsed
   "Paper figures" `<details>`.
6. **Benchmark** – statistics, rubrics by hop distance, the 48 root entities
   (paper appendix table); paper statistics table collapsed.
7. **Leaderboard** – metric switch (Overall / Breadth / Depth), domain filter
   (all or one of 24), category filter (Deep Research System / Native Search LLM /
   Local Research Framework). Each row shows the provider logo in a small tile. Bars use a fixed 0–70% scale; ranking uses unrounded
   values. Followed by the category descriptions, the paper's case-study figure
   (Lew Grade / temperance movement) and the paper table (collapsed). Without
   JavaScript, a `<noscript>` image of the paper table is shown.
8. **Domains** – 16 × 24 heatmap from `domain-results.json` with a metric switch,
   a "mean of 16 agents" row and the best agent per domain outlined; the paper
   heatmap is collapsed below.
9. **Validation** – five figures from the paper (judge–human Macro-F1, metric–expert
   correlation, leakage, task quality, ranking stability); paper tables collapsed.
10. **Quick start** – three steps from the repository README, plus the Local
    Research Framework summary and links.
11. **BibTeX** with a copy button, then the footer.

### Method figure

The figure follows one real case from the paper (Appendix E.4, Fig. 10 and
Table 25): root **Roguelike** (Games domain), its research query, the twelve
labelled relations R1–R12 with their rubric texts, and the o3 Deep Research
verdicts (R6 and R12 not satisfied). The eight unlabelled branches, their
verdicts, and the three reports in phase 7 are illustrative; the page says so.
Phases:

1. **Grow tree** – radial local tree: 50 root children, ≤5 children per node, 3 hops.
2. **Sample scope** – three groups of 10 branches (S1–S3) highlighted; S1 becomes
   the case tree (root + 10 complete branches).
3. **Query & rubrics** – the real query, then one rubric tag per edge (R1–R12 with
   their full text; dots for the other edges).
4. **Agent report** – the tree locks ("hidden from the agent"); the agent receives
   only the query and writes the report (real section headings).
5. **Judge** – Gemini 2.5 Pro verdicts light the tree (covered = ink, uncovered = rust).
6. **Breadth · Depth** – breadth band and per-direction bars (teal), continuous
   chains and depth bars (indigo), then overall = √(B·D).
7. **Report styles** – three hypothetical reports with the same 14 of 47 satisfied
   rubrics: broad–shallow, focused–continuous, distributed–uneven.

Scores in phases 6–7 are computed in the browser with the paper's Algorithm 1 and
default parameters (β 0.60, α 0.25, η 0.75, ρ 0.50) on this simplified tree; they
are not reported results. Animation rules: nothing is visible before its step, no
connector lines between the tree and the panel (a ring pulse on the panel card
marks the hand-off), autoplay only while the figure is on screen, tabs jump to a
phase, and `prefers-reduced-motion` shows a static phase 6 with clickable tabs.

## Data

`assets/data/results.json` holds the verified 2026-10-14 snapshot of the
`1.1-beta0.60` results (16 agents, matching paper Table 2).
`assets/data/domain-results.json` holds the 384 system–domain cells behind the
paper's domain heatmaps (six cases per cell). Refresh it with the matching
analysis CSV:

```sh
python3 website/scripts/export_leaderboard_data.py /path/to/domain_scheme3_3_final_v3_metrics.csv
```

`assets/data/paper-tables.json` and `assets/data/figures.json` record the PDF
checksums, crop boxes and dimensions of the paper tables and figures exported to
`assets/images/`. Update the data, images and page text together when the
manuscript changes. Overall Coverage is averaged per case, so it is not the
geometric mean of the displayed mean breadth and depth.

## arXiv placeholders

The paper is not on arXiv yet. Two places carry the marker `ARXIV_ID`:

- the Paper button in the hero (`href="https://arxiv.org/abs/"`), and
- the BibTeX block (`journal = {arXiv preprint arXiv:}`).

`grep -n ARXIV_ID website/index.html` finds both.

## Deployment

`.github/workflows/pages.yml` uploads `website/` as-is to GitHub Pages; the
public URL is `https://zjureal.com/DRBenchPro/`.

## Third-party assets and licenses

- Fonts: Outfit and IBM Plex Mono, both SIL Open Font License 1.1, loaded from
  Google Fonts (not bundled).
- Model provider logos in `assets/images/logos/` (OpenAI, Claude, Gemini, Gemma, Grok,
  Perplexity, Qwen, DeepSeek, Zhipu, MiniMax) are copied unmodified from
  `@lobehub/icons-static-svg` 1.102.0 – MIT, `licenses/LOBE-ICONS-MIT.txt`. The logos
  are trademarks of their owners and only identify the evaluated models. The
  `system_id` → logo map is `LOGO` in `assets/js/dp-results.js`.
- Lucide icon paths (file-text, code-xml, database, package, play, lock) are
  inlined in `index.html` – ISC, `licenses/LUCIDE-ISC.txt`.

The page was laid out with https://robowild.xlang.ai/ as a width and top-bar
reference; no code or assets were copied from it.

Original website HTML, CSS and JavaScript are MIT-licensed, copyright 2026 REAL Lab,
as stated in the repository's [LICENSE](../LICENSE). The footer links the
benchmark's [CC BY-SA 4.0 data terms](../DATA_LICENSE). The code grant does not
apply to benchmark data, paper figures, paper excerpts or result assets; their
rights remain with the respective rights holders.
