"use strict";

// Own implementation of metric/domain ranking animation; no external runtime.
(async function initializeLeaderboard() {
  const section = document.getElementById("leaderboard");
  if (!section) return;
  const card = section.querySelector(".leaderboard-card");
  try {
    const [globalData, domainData] = await Promise.all(
      ["results.json", "domain-results.json"].map(async (name) => {
        const response = await fetch(`assets/data/${name}`);
        if (!response.ok) throw new Error(`Leaderboard data unavailable: ${name}`);
        return response.json();
      })
    );
    const systems = new Map(globalData.results.map((system) => [system.system_id, system]));
    if (systems.size !== 16 || domainData.domains.length !== 24) throw new Error("Incomplete leaderboard snapshot");
    const metrics = {
      overall: { title: "Overall Coverage", short: "Overall", field: "overall_percent", abbreviation: "O" },
      breadth: { title: "Breadth", short: "Breadth", field: "breadth_percent", abbreviation: "B" },
      depth: { title: "Depth", short: "Depth", field: "depth_percent", abbreviation: "D" },
    };
    const displayScale = [10, 70];
    const globalScenes = Object.keys(metrics).map((metric) => ({ metric, duration: 4000, domain: null }));
    function buildScenes() {
      const shuffled = [...domainData.domains];
      for (let i = shuffled.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [shuffled[i], shuffled[j]] = [shuffled[j], shuffled[i]];
      }
      return [...globalScenes, ...shuffled.map((domain) => ({ metric: "overall", duration: 2000, domain }))];
    }
    let scenes = buildScenes();
    let index = 0;
    let timer;
    let startedAt = null;
    let remaining = scenes[0].duration;
    let progressAnimation;
    let inView = false;
    const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
    let wantsPlayback = !motion.matches;
    const table = section.querySelector(".leaderboard-table");
    const container = document.getElementById("leaderboard-rows");
    const heading = document.getElementById("leaderboard-heading");
    const scope = document.getElementById("leaderboard-scope");
    const domainSelect = document.getElementById("leaderboard-domain");
    const playButton = document.getElementById("leaderboard-play");
    const progress = section.querySelector(".leaderboard-progress span");
    const position = document.getElementById("leaderboard-position");
    const announcement = document.getElementById("leaderboard-announcement");
    const categories = {
      "Deep Research System": { key: "deep", label: "Deep Research System" },
      "Native Search LLM": { key: "native", label: "Native Search LLM" },
      "Local Research Framework": { key: "local", label: "Local Research Framework" },
    };
    for (const domain of domainData.domains) {
      if (domain.results.length !== systems.size || new Set(domain.results.map((row) => row.system_id)).size !== systems.size) {
        throw new Error(`Incomplete domain: ${domain.name}`);
      }
      domainSelect.add(new Option(domain.name, domain.name));
    }
    const elements = new Map();
    for (const system of systems.values()) {
      const row = document.createElement("div");
      row.className = "leaderboard-row leaderboard-columns";
      row.setAttribute("role", "row");
      row.dataset.systemId = system.system_id;
      // This template is constant; all dataset strings are inserted as text below.
      row.innerHTML = '<div class="leaderboard-rank" role="cell"><span></span></div><div class="leaderboard-agent" role="cell"><span class="leaderboard-agent-name"></span><div class="leaderboard-agent-detail"><span class="leaderboard-category"></span><span class="leaderboard-sub-scores"></span></div></div><div class="leaderboard-bar-cell" role="cell"><span class="leaderboard-bar-track" aria-hidden="true"><span class="leaderboard-bar-fill"></span></span></div><div class="leaderboard-score" role="cell"></div>';
      row.querySelector(".leaderboard-agent-name").textContent = system.name;
      row.querySelector(".leaderboard-agent-name").title = system.name;
      const category = categories[system.category];
      const tag = row.querySelector(".leaderboard-category");
      tag.textContent = category.label;
      tag.dataset.category = category.key;
      elements.set(system.system_id, {
        row, rank: row.querySelector(".leaderboard-rank span"),
        score: row.querySelector(".leaderboard-score"), bar: row.querySelector(".leaderboard-bar-fill"),
        sub: row.querySelector(".leaderboard-sub-scores"),
      });
      container.append(row);
    }
    function stopTimer() {
      if (timer !== undefined) {
        clearTimeout(timer);
        timer = undefined;
        if (startedAt !== null) remaining = Math.max(0, remaining - (performance.now() - startedAt));
      }
      startedAt = null;
      progressAnimation?.pause();
    }
    function syncPlayback() {
      const playing = wantsPlayback && inView && !document.hidden && !motion.matches;
      if (playing && timer === undefined) {
        startedAt = performance.now();
        progressAnimation?.play();
        timer = setTimeout(() => {
          timer = undefined;
          startedAt = null;
          advance(1, false);
        }, remaining);
      } else if (!playing) {
        stopTimer();
      }
      card.dataset.playing = String(playing);
      playButton.textContent = wantsPlayback ? "Pause" : "Play";
      playButton.setAttribute("aria-label", wantsPlayback ? "Pause automatic playback" : "Resume automatic playback");
      playButton.disabled = motion.matches;
      playButton.title = motion.matches ? "Use manual controls with reduced motion enabled" : "";
    }
    function render(manual = false) {
      stopTimer();
      const scene = scenes[index];
      const metric = metrics[scene.metric];
      // Every scene shares a fixed display range; numeric scores stay unchanged.
      const [scaleMin, scaleMax] = displayScale;
      card.dataset.scaleMin = String(scaleMin);
      card.dataset.scaleMax = String(scaleMax);
      document.getElementById("leaderboard-scale-min").textContent = String(scaleMin);
      document.getElementById("leaderboard-scale-max").textContent = String(scaleMax);
      const scaleLabel = document.getElementById("leaderboard-scale");
      scaleLabel.textContent = `Bar scale: ${scaleMin}–${scaleMax}% · Higher is better`;
      scaleLabel.title = `Bars use a ${scaleMin ? "truncated " : ""}${scaleMin}–${scaleMax}% scale. Scores retain their original 0–100% values; ranking uses scores before rounding.`;
      remaining = scene.duration;
      card.dataset.tone = scene.domain ? "domain" : scene.metric;
      card.dataset.scene = scene.domain ? `domain:${scene.domain.name}` : scene.metric;
      card.dataset.sceneIndex = String(index);
      card.dataset.duration = String(scene.duration);
      card.dataset.state = "ready";
      heading.textContent = scene.domain ? `Domain: ${scene.domain.name}` : metric.title;
      scope.textContent = scene.domain ? `${metric.title} (%) · 6 Research Cases` : "All Domains · 144 Research Cases";
      document.getElementById("leaderboard-score-label").textContent = `${metric.short} (%)`;
      position.replaceChildren(document.createTextNode(`${String(index + 1).padStart(2, "0")} `));
      const denominator = document.createElement("span");
      denominator.textContent = `/ ${scenes.length}`;
      position.append(denominator);
      position.setAttribute("aria-label", `Scene ${index + 1} of ${scenes.length}`);
      domainSelect.value = scene.domain?.name || "";
      section.querySelectorAll("[data-leaderboard-metric]").forEach((button) => {
        button.setAttribute("aria-pressed", String(!scene.domain && button.dataset.leaderboardMetric === scene.metric));
      });
      const values = scene.domain ? scene.domain.results : globalData.results;
      const ranked = [...values].sort((a, b) => b[metric.field] - a[metric.field] || a.system_id.localeCompare(b.system_id));
      const previousPositions = new Map();
      for (const [id, ui] of elements) {
        ui.row.getAnimations().forEach((animation) => animation.cancel());
        if (ui.row.isConnected && !ui.row.hidden) previousPositions.set(id, ui.row.getBoundingClientRect().top);
      }
      ranked.forEach((value, rank) => {
        const ui = elements.get(value.system_id);
        ui.row.dataset.rank = String(rank + 1);
        ui.row.dataset.score = String(value[metric.field]);
        ui.row.setAttribute("aria-rowindex", String(rank + 2));
        ui.rank.textContent = String(rank + 1);
        ui.score.textContent = value[metric.field].toFixed(2);
        const barPercent = Math.max(0, Math.min(100, (value[metric.field] - scaleMin) / (scaleMax - scaleMin) * 100));
        ui.bar.style.width = `${barPercent}%`;
        ui.sub.replaceChildren();
        Object.entries(metrics).filter(([key]) => key !== scene.metric).forEach(([, other], i) => {
          if (i) ui.sub.append(document.createTextNode("  "));
          const label = document.createElement("b");
          label.textContent = `${other.abbreviation}:`;
          ui.sub.append(label, document.createTextNode(value[other.field].toFixed(2)));
        });
        ui.sub.setAttribute("aria-label", Object.entries(metrics).filter(([key]) => key !== scene.metric).map(([, other]) => `${other.title}: ${value[other.field].toFixed(2)} percent`).join("; "));
        container.append(ui.row);
      });
      table.setAttribute("aria-rowcount", String(systems.size + 1));
      table.setAttribute("aria-label", `${heading.textContent}: ${metric.title} rankings (%)`);
      if (!motion.matches) {
        for (const [id, ui] of elements) {
          if (ui.row.hidden) continue;
          const oldTop = previousPositions.get(id);
          const newTop = ui.row.getBoundingClientRect().top;
          if (oldTop !== undefined && Math.abs(oldTop - newTop) > 1) {
            ui.row.animate([{ transform: `translateY(${oldTop - newTop}px)` }, { transform: "translateY(0)" }], { duration: 450, easing: "cubic-bezier(.22,1,.36,1)" });
          } else if (oldTop === undefined) {
            ui.row.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 350 });
          }
        }
      }
      progressAnimation?.cancel();
      progressAnimation = progress.animate([{ transform: "scaleX(0)" }, { transform: "scaleX(1)" }], { duration: scene.duration, fill: "forwards" });
      progressAnimation.pause();
      progressAnimation.currentTime = 0;
      if (manual) announcement.textContent = `${heading.textContent}. ${scope.textContent}. Playback paused.`;
      syncPlayback();
    }
    function choose(next) {
      wantsPlayback = false;
      index = next;
      render(true);
    }
    function advance(direction, manual = true) {
      if (index + direction >= scenes.length) {
        scenes = buildScenes();
        index = 0;
      } else {
        index = (index + direction + scenes.length) % scenes.length;
      }
      if (manual) wantsPlayback = false;
      render(manual);
    }
    document.getElementById("leaderboard-previous").addEventListener("click", () => advance(-1));
    document.getElementById("leaderboard-next").addEventListener("click", () => advance(1));
    playButton.addEventListener("click", () => {
      wantsPlayback = !wantsPlayback;
      syncPlayback();
      announcement.textContent = wantsPlayback ? "Automatic playback resumed." : "Automatic playback paused.";
    });
    section.querySelectorAll("[data-leaderboard-metric]").forEach((button) => {
      button.addEventListener("click", () => choose(globalScenes.findIndex((scene) => scene.metric === button.dataset.leaderboardMetric)));
    });
    domainSelect.addEventListener("change", () => choose(domainSelect.value ? scenes.findIndex((scene) => scene.domain?.name === domainSelect.value) : 0));
    table.tabIndex = 0;
    table.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      event.preventDefault();
      advance(event.key === "ArrowRight" ? 1 : -1);
    });
    document.addEventListener("visibilitychange", syncPlayback);
    motion.addEventListener("change", () => {
      if (motion.matches) wantsPlayback = false;
      render();
    });
    section.hidden = false;
    render();
    new IntersectionObserver(([entry]) => {
      inView = entry.isIntersecting;
      syncPlayback();
    }, { threshold: 0 }).observe(section);
  } catch (error) {
    card.dataset.state = "error";
    document.getElementById("leaderboard-heading").textContent = "Leaderboard";
    document.getElementById("leaderboard-scope").textContent = "DRBenchPro benchmark results";
    document.getElementById("leaderboard-fallback").hidden = false;
    section.hidden = false;
    console.warn("Leaderboard could not load.", error);
  }
})();
