"use strict";
// Interactive leaderboard and domain heatmap (original code, MIT). Data: assets/data/*.json.
(async () => {
  const lb = document.querySelector(".dp-lb");
  const hm = document.querySelector(".dp-hm");
  if (!lb && !hm) return;
  const METRICS = {
    overall: { label: "Overall", field: "overall_percent" },
    breadth: { label: "Breadth", field: "breadth_percent" },
    depth: { label: "Depth", field: "depth_percent" },
  };
  const CATS = {
    "Deep Research System": "deep",
    "Native Search LLM": "native",
    "Local Research Framework": "local",
  };
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)");
  let globalData, domainData;
  try {
    [globalData, domainData] = await Promise.all(["results.json", "domain-results.json"].map(async (f) => {
      const r = await fetch(`assets/data/${f}`);
      if (!r.ok) throw new Error(`Cannot load ${f}`);
      return r.json();
    }));
  } catch (err) {
    if (lb) lb.dataset.state = "error";
    if (hm) hm.dataset.state = "error";
    const note = document.createElement("p");
    note.className = "dp-lead";
    note.textContent = "Results could not be loaded here; see the paper table below.";
    lb?.append(note);
    console.warn(err);
    return;
  }
  const systems = new Map(globalData.results.map((s) => [s.system_id, s]));
  // Provider logos (lobe-icons, MIT; trademarks of their owners), identifying each evaluated model.
  const LOGO = {
    o3_deep_research: "openai", gpt_5_6_sol: "openai", claude_opus_5: "claude", gemini_3_1_pro_preview: "gemini",
    gemma4_31b: "gemma", grok_4_5: "grok", sonar_deep_research: "perplexity", sonar_reasoning_pro: "perplexity",
    qwen_deep_research: "qwen", qwen3_8_max: "qwen", qwen3_8_max_native: "qwen", qwen3_7_plus: "qwen",
    deepseek_v4_flash: "deepseek", deepseek_v4_pro: "deepseek", glm5_2: "zhipu", minimax_m3: "minimax",
  };
  const displayName = (s) => (s.system_id === "qwen3_8_max_native" ? "Qwen3.8 Max" : s.name);

  // ---------------- leaderboard ----------------
  if (lb) {
    const rowsEl = lb.querySelector(".dp-lb-rows");
    const select = lb.querySelector("#dp-lb-domain");
    const scope = lb.querySelector(".dp-lb-scope");
    const head = lb.querySelector(".dp-lb-head");
    domainData.domains.forEach((d) => select.add(new Option(d.name, d.name)));
    const state = { metric: "overall", domain: "", cat: "" };
    const rows = new Map();
    for (const s of systems.values()) {
      const row = document.createElement("div");
      row.className = "dp-lb-row";
      row.setAttribute("role", "row");
      row.innerHTML = '<span class="dp-lb-rank" role="cell"></span><span class="dp-lb-logo" aria-hidden="true"><img alt="" width="22" height="22" loading="lazy"></span><span class="dp-lb-agent" role="cell"><span class="dp-lb-name"></span><span class="dp-lb-cat"><i class="dp-dot"></i><span></span></span></span><span class="dp-lb-bar" role="cell"><span class="dp-lb-track"><span class="dp-lb-fill"></span></span></span><span class="dp-lb-main" role="cell"></span><span class="dp-lb-o" role="cell"></span><span class="dp-lb-o" role="cell"></span>';
      const logo = row.querySelector(".dp-lb-logo img");
      if (LOGO[s.system_id]) logo.src = `assets/images/logos/${LOGO[s.system_id]}.svg`;
      else logo.parentElement.classList.add("is-empty");
      row.querySelector(".dp-lb-name").textContent = displayName(s);
      row.querySelector(".dp-lb-cat span").textContent = s.category;
      row.querySelector(".dp-lb-cat .dp-dot").classList.add(`dp-dot-${CATS[s.category]}`);
      rows.set(s.system_id, row);
      rowsEl.append(row);
    }
    function render() {
      const m = METRICS[state.metric];
      const others = Object.keys(METRICS).filter((k) => k !== state.metric);
      lb.dataset.metric = state.metric;
      head.querySelector('[data-col="main"]').textContent = m.label;
      head.querySelector('[data-col="o1"]').textContent = METRICS[others[0]].label;
      head.querySelector('[data-col="o2"]').textContent = METRICS[others[1]].label;
      const source = state.domain ? domainData.domains.find((d) => d.name === state.domain).results : globalData.results;
      const ranked = [...source].sort((a, b) => b[m.field] - a[m.field] || a.system_id.localeCompare(b.system_id));
      const before = new Map();
      rows.forEach((r, id) => { if (!r.hidden) before.set(id, r.getBoundingClientRect().top); });
      let rank = 0;
      ranked.forEach((v) => {
        const s = systems.get(v.system_id);
        const row = rows.get(v.system_id);
        const show = !state.cat || CATS[s.category] === state.cat;
        row.hidden = !show;
        rowsEl.append(row);
        if (!show) return;
        rank += 1;
        row.querySelector(".dp-lb-rank").textContent = rank;
        row.querySelector(".dp-lb-main").textContent = v[m.field].toFixed(2);
        const o = row.querySelectorAll(".dp-lb-o");
        o[0].textContent = v[METRICS[others[0]].field].toFixed(2);
        o[1].textContent = v[METRICS[others[1]].field].toFixed(2);
        row.querySelector(".dp-lb-fill").style.width = `${Math.min(100, (v[m.field] / 70) * 100)}%`;
        row.setAttribute("aria-label", `${rank}. ${displayName(s)} (${s.category}): ${m.label} ${v[m.field].toFixed(2)}%`);
      });
      if (!reduce.matches) {
        rows.forEach((r, id) => {
          if (r.hidden) return;
          const old = before.get(id);
          const now = r.getBoundingClientRect().top;
          if (old === undefined) r.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 300 });
          else if (Math.abs(old - now) > 1) r.animate([{ transform: `translateY(${old - now}px)` }, { transform: "none" }], { duration: 450, easing: "cubic-bezier(.22,1,.36,1)" });
        });
      }
      const n = rank;
      scope.textContent = `${state.domain || "All domains"} · ${state.domain ? "6" : "144"} cases · ${n} agent${n === 1 ? "" : "s"} · ranked by ${m.label}`;
      lb.querySelectorAll("[data-metric]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.metric === state.metric)));
      lb.querySelectorAll("[data-cat]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.cat === state.cat)));
      lb.dataset.state = "ready";
    }
    lb.querySelectorAll("[data-metric]").forEach((b) => b.addEventListener("click", () => { state.metric = b.dataset.metric; render(); }));
    lb.querySelectorAll("[data-cat]").forEach((b) => b.addEventListener("click", () => { state.cat = b.dataset.cat; render(); }));
    select.addEventListener("change", () => { state.domain = select.value; render(); });
    render();
  }

  // ---------------- heatmap ----------------
  if (hm) {
    const table = hm.querySelector(".dp-hm-table");
    const order = [...systems.values()].map((s) => s.system_id);
    // Group rows by category, in the paper's order within each group.
    const paperOrder = ["o3_deep_research", "sonar_deep_research", "qwen_deep_research", "claude_opus_5", "grok_4_5", "gpt_5_6_sol", "gemini_3_1_pro_preview", "sonar_reasoning_pro", "qwen3_8_max_native", "qwen3_8_max", "minimax_m3", "glm5_2", "deepseek_v4_flash", "deepseek_v4_pro", "qwen3_7_plus", "gemma4_31b"];
    const ids = paperOrder.filter((id) => order.includes(id));
    const domains = domainData.domains;
    const cell = new Map();
    domains.forEach((d) => d.results.forEach((r) => cell.set(`${d.name}|${r.system_id}`, r)));
    const ramps = {
      overall: ["#f3efe6", "#2a3140"],
      breadth: ["#eef6f3", "#0f6f67"],
      depth: ["#eef0f9", "#2f3f98"],
    };
    const mix = (a, b, t) => {
      const p = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
      const [x, y] = [p(a), p(b)];
      return `rgb(${x.map((v, i) => Math.round(v + (y[i] - v) * t)).join(",")})`;
    };
    const shade = (t) => 0.95 * Math.pow(Math.max(0, Math.min(1, t)), 2.3);
    function render(metric) {
      const f = METRICS[metric].field;
      let lo = Infinity, hi = -Infinity;
      cell.forEach((r) => { lo = Math.min(lo, r[f]); hi = Math.max(hi, r[f]); });
      const [cLo, cHi] = ramps[metric];
      hm.style.setProperty("--dp-hm-lo", cLo);
      hm.style.setProperty("--dp-hm-hi", cHi);
      hm.querySelector("[data-hm-min]").textContent = lo.toFixed(1);
      hm.querySelector("[data-hm-max]").textContent = hi.toFixed(1);
      const best = new Map(domains.map((d) => [d.name, d.results.reduce((a, b) => (b[f] > a[f] ? b : a)).system_id]));
      const thead = `<thead><tr><th class="dp-hm-agent" scope="col"><span style="writing-mode:horizontal-tb;transform:none">${METRICS[metric].label} (%)</span></th>${domains.map((d) => `<th scope="col"><span>${d.name.replace(/&/g, "&amp;")}</span></th>`).join("")}</tr></thead>`;
      let body = "<tbody>";
      let lastCat = null;
      ids.forEach((id) => {
        const s = systems.get(id);
        if (lastCat && lastCat !== s.category) body += `<tr class="dp-hm-sep"><th colspan="${domains.length + 1}"></th></tr>`;
        lastCat = s.category;
        const label = id === "qwen3_8_max_native" ? "Qwen3.8 Max (native)" : s.name;
        body += `<tr><th class="dp-hm-agent" scope="row" title="${label} · ${s.category}"><i class="dp-dot dp-dot-${CATS[s.category]}"></i> ${label}</th>`;
        domains.forEach((d) => {
          const v = cell.get(`${d.name}|${id}`)[f];
          const t = shade((v - lo) / (hi - lo));
          const isBest = best.get(d.name) === id;
          body += `<td class="${isBest ? "is-best" : ""}" style="background:${mix(cLo, cHi, t)};color:${t > .5 ? "#fff" : "#1e2430"}" title="${label} · ${d.name}: ${v.toFixed(2)}%">${v.toFixed(1)}</td>`;
        });
        body += "</tr>";
      });
      body += `<tr class="dp-hm-mean"><th class="dp-hm-agent" scope="row">Mean of 16 agents</th>`;
      domains.forEach((d) => {
        const v = d.results.reduce((a, r) => a + r[f], 0) / d.results.length;
        const t = shade((v - lo) / (hi - lo));
        body += `<td style="background:${mix(cLo, cHi, t)};color:${t > .5 ? "#fff" : "#1e2430"}" title="Mean · ${d.name}: ${v.toFixed(2)}%">${v.toFixed(1)}</td>`;
      });
      body += "</tr></tbody>";
      table.innerHTML = thead + body;
      hm.querySelectorAll("[data-hm]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.hm === metric)));
      hm.dataset.state = "ready";
    }
    hm.querySelectorAll("[data-hm]").forEach((b) => b.addEventListener("click", () => render(b.dataset.hm)));
    render("overall");
  }
})();
