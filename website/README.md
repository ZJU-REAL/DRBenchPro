# DRBenchPro project website

Static academic project page: HTML, Bulma 0.9.4, local CSS, and vanilla JavaScript.
No build, Node package installation, analytics, or external runtime CDN is required.

## Preview

From the repository root:

```sh
python3 -m http.server 8000 --bind 127.0.0.1 --directory website
```

Open `http://127.0.0.1:8000/`. All runtime resources use relative paths and also
work below the GitHub project path `/DRBenchPro/`.

## Content and interactions

The page has a publication header, overview video, animated leaderboard, teaser and six sections: Abstract, Benchmark
Construction and Evaluation, Main Results, Analysis, Local Research Framework,
and BibTeX. Metric definitions appear in the method section, after Abstract.
Abstract reproduces the complete current paper abstract. Analysis contains a
three-slide carousel (case studies, domain coverage, and research activity) and
the always-visible Benchmark Statistics and Validation panel. Both carousels
support independent arrows, dots, keyboard navigation, and touch scrolling.
Domain heatmap metric buttons remain available within the domain slide.
The case-study slide includes the paper’s depth-length and breadth-width/opacity
legends exported directly from `case_study_equal_overall_en.pdf`, arranged side
by side on desktop and stacked on mobile. Original labels, ticks, widths, and
opacity are preserved; `assets/data/figures.json` records PDF checksums and crop
coordinates. The desktop legend is centered at 72% of the case figure’s
visible width, capped at 600px, with a 10px gap below the plot. The stylesheet
URL includes its content hash to invalidate stale browser copies; update that
query when editing `site.css`.
Its prose explains the visual encoding and both cases using the appendix scores.
The case figure uses its natural height so the legend and explanation stay
close together. Navigation arrows align to the active visual when switching
slides or resizing.
The research-activity slide displays the original paper’s Table 13, exported
directly from the PDF with every column retained; provenance is recorded in
`assets/data/paper-tables.json`.
Local Research Framework is a separate section
with a directly displayed workflow figure and two paper-derived paragraphs.
The dated Wikipedia dump link is embedded in the prose, which explains local
offline Search/Open retrieval, disabled provider-native web search, and the
time-invariant evidence space. There is no workflow card or configuration table.
Main Results displays the original paper's Table 2, followed by short descriptions
of the three evaluation categories. Benchmark Statistics and Validation presents complete
original Tables 7, 3, 4, 5, and 6, in that order. Each figure is followed by a concise analysis paragraph,
without a visible table-number prefix. The main results analysis likewise follows
the original table image. Scientific images can be clicked to open a zoom view;
separate figure and PDF controls are intentionally omitted.

The Code button links to `https://github.com/ZJU-REAL/DRBenchPro`. Dataset links
to `https://huggingface.co/datasets/zhuiluozhiji/DRBenchPro`; Framework Image links
to `https://huggingface.co/datasets/zhuiluozhiji/DRBenchPro-runtime`, displayed as
**DRBenchPro Local Research Framework — Docker Image**. The framework section
links the image download/import guide and explains that independent report
evaluation only needs the dataset and Python evaluator. The arXiv button remains
disabled until its verified URL is available. BibTeX is intentionally empty, with only its section
heading retained; no provisional entry or copy action is displayed. The full
Query/report/rubric case explorer remains deferred.

### Overview video

The header's **Video** link jumps to the 16:9 player between the benchmark scale
and leaderboard. `assets/video/drbenchpro-overview.mp4` is the 2:07 English
overview, served locally as H.264/AAC with the MP4 metadata moved to the front
for progressive playback. It uses `preload="none"`, starts only on request, and
does not loop. Native playback, volume, seeking and fullscreen controls appear
after clicking the central play button. Without JavaScript, native controls
remain available. Playback failures offer a direct MP4 link.

The 960px-wide player scales to mobile and keeps a white background, subtle
border, 12px corners and a blue play button. `assets/images/video-placeholder.svg`
is an intentionally blank white poster. **TODO: replace it with the dedicated
video cover once available.** Keep the poster 16:9 and update its URL when
replacing it. Reduced-motion preferences disable the play-button animation.

While the video plays, leaderboard autoplay is suspended without changing the
visitor's playback preference or the current scene's remaining time. Pausing or
finishing the video restores autoplay only if it was enabled, the leaderboard
is visible, and reduced motion is off. Manual leaderboard browsing stays paused.

The fixed corpus label links to the original dated source dump at
`https://dumps.wikimedia.org/enwiki/20260701/`, matching the downloader's
`DUMP_BASE_URL`. This is the raw Wikimedia dump, not the processed project index.

`assets/data/results.json` contains the verified 2026-10-14 snapshot of the
`1.1-beta0.60` results. `assets/images/main-results.webp` is exported directly from
the current compiled manuscript's Table 2, preserving all rows and formatting.
It works without JavaScript. Update the image and data snapshot together when
the manuscript results change. `assets/data/paper-tables.json` records the PDF
checksum, original table numbers, page/crop coordinates, dimensions, and extracted
text for Tables 2–7 and 13. Tables are not reconstructed as website HTML tables.
Overall Coverage is computed per case as the geometric mean of breadth and
depth, then averaged over cases; it is not the geometric mean of the displayed
macro-averaged breadth/depth scores.

### Homepage leaderboard

The original `assets/js/leaderboard.js` and `assets/css/leaderboard.css` implement
a compact ranking table inspired by Super Research's homepage animation. Global
Overall Coverage, Breadth and Depth each appear for four seconds, followed by
all 24 domain rankings, each for two seconds. Each round shuffles the domains
without replacement and lasts 60 seconds. Domain scenes rank by domain-level
Overall Coverage and show Breadth/Depth alongside the category tag. Global bars
use three distinct gradient palettes; domain bars are solid blue. Every global
and domain scene uses the same fixed 10–70% display scale. Width is
`(score - 10) / 60`, clamped to 0–100%. All 432 ranked scores across the 27
scenes are within the display range (14.3162–67.9350%), so every agent has a
visible, untruncated bar.
Axis endpoints and the footer label identify the active display range; numeric
scores retain their original percentage values. Ranking uses scores before display rounding, and `system_id`
distinguishes identically named agents evaluated under different configurations.

All 16 agents appear in every scene. Desktop rows are 40px high, with compact
typography and spacing; tablet/mobile layouts preserve readable wrapped labels.
Pause, previous/next, global metric buttons and domain selection support manual browsing;
manual actions pause playback. The table also supports left/right arrow keys.
Playback preserves the remaining scene time when the section leaves the viewport
or the document becomes hidden, and while the overview video plays. Reduced-motion users get static manual browsing.
If JavaScript is disabled, the original Main Results table remains available.

`assets/data/domain-results.json` contains 384 validated system–domain cells
from the CSV underlying the paper's current three domain heatmaps. The export
verifies six cases per cell, the `1.1-beta0.60` baseline, completeness and agreement
with `results.json`. Only aggregate scores, public identifiers and provenance
are exported. To refresh with the matching manuscript analysis CSV:

```sh
python3 website/scripts/export_leaderboard_data.py /path/to/domain_scheme3_3_final_v3_metrics.csv
```

Update global scores, domain scores, and the original paper figures together
when changing the benchmark snapshot.

`assets/data/figures.json` records original manuscript filenames, PDF checksums,
and web-image dimensions. Website copies are exported from the selected arXiv figure PDFs:
`main_method/main-method-v20-cropped.pdf`,
`local_research_framework/edit-v3.pdf`, and `teaser/teaser-v5-cropped.pdf`
under `arxiv/figures/`. Use each PDF's CropBox without an additional teaser trim.
The teaser's left and right panels are exported separately and stacked on mobile;
their PDF crop coordinates are recorded in `figures.json`. Original figure PDFs
are retained as source assets. README figures reuse the website WebP files, and
the HF dataset card keeps a separate copy of `construction.webp`. Image URLs use
content-hash version queries; refresh them whenever a figure changes. Website
development does not edit manuscript sources.

## Deployment

The repository workflow `.github/workflows/pages.yml` uploads only `website/`.
In GitHub, select **Settings → Pages → Source → GitHub Actions** before the first
deployment. The workflow follows the official static Pages actions:
https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages

For this repository, development on `codex/webpage` is merged into the local
maintenance `main` only after review. Public files are then synchronized to the
separate `codex/public-release` history before publication. Do not push the
maintenance branch directly to the public remote. No remote settings are
changed by adding the workflow locally.

## Design and third-party assets

The layout follows the open academic project-page style used by:

- OmniEAR: https://zjureal.com/OmniEmbodied/
- VerifyBench: https://zjureal.com/VerifyBench/
- ViewSpatial-Bench: https://zjureal.com/ViewSpatial-Page/
- Super Research leaderboard: https://github.com/cnsdqd-dyb/Super-Research-Benchmark/blob/main/leaderboard.html
- Academic Project Page Template: https://github.com/eliahuhorwitz/Academic-project-page-template
- Nerfies: https://nerfies.github.io/

The custom HTML/CSS/JavaScript implements DRBenchPro's content and interactions;
it does not bundle the reference sites' research assets or scripts. References
are recorded here; the displayed footer contains only project contact information,
and no related-work navigation menu is shown. Third-party local dependencies and
their licenses:

- Bulma 0.9.4 — MIT, `licenses/BULMA-MIT.txt`.
- Noto Sans Latin variable font — SIL Open Font License 1.1,
  `licenses/NOTO-SANS-OFL.txt`; served locally to avoid external font requests.
- Lucide SVG icons — ISC, `licenses/LUCIDE-ISC.txt`.

Original website HTML, CSS and JavaScript are MIT-licensed, copyright 2026 REAL Lab,
as stated in the repository's [LICENSE](../LICENSE). The footer identifies this
scope and links the benchmark's [CC BY-SA 4.0 data terms](../DATA_LICENSE).
The code grant does not apply
to benchmark data, paper figures, paper excerpts or result assets; their rights
remain with the respective rights holders. Bundled dependencies retain the
licenses listed above.
