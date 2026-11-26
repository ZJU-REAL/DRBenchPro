"use strict";
// Animated method figure for DRBench Pro (original code, MIT).
// Real content: root "Roguelike", its query, labelled nodes, rubrics R1–R12 and the o3 Deep Research
// verdicts (paper Appendix E.4, Fig. 10 / Table 25). Unlabelled branches and reports A–C are illustrative.
(() => {
  const fig = document.getElementById("dp-fig");
  if (!fig) return;
  const svg = fig.querySelector(".dp-tree");
  const NS = "http://www.w3.org/2000/svg";
  const tabs = [...fig.querySelectorAll("[data-phase]")];
  const views = [...fig.querySelectorAll(".dp-pv")];
  const caption = fig.querySelector(".dp-fig-caption");
  const playBtn = fig.querySelector(".dp-fig-play");
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)");

  // ---------- metric (paper Algorithm 1, default parameters) ----------
  const P = { beta: 0.6, alpha: 0.25, eta: 0.75, rho: 0.5 };
  const rankAgg = (ys) => {
    if (!ys.length) return 0;
    const s = [...ys].sort((a, b) => b - a);
    let n = 0, d = 0;
    s.forEach((y, i) => { const w = P.rho ** i; n += w * y; d += w; });
    return n / d;
  };
  function evalNode(v, cov) {
    const x = cov.has(v.id) ? 1 : 0;
    if (!v.c) return { B: x, D: x };
    const r = v.c.map((w) => evalNode(w, cov));
    const mB = r.reduce((a, q) => a + q.B, 0) / r.length;
    const d = rankAgg(r.map((q) => q.D));
    return { B: P.beta * x + (1 - P.beta) * mB, D: P.alpha * x + (1 - P.alpha) * d * (P.eta + (1 - P.eta) * x) };
  }
  function score(cov) {
    const per = DIRS.map((b) => evalNode(b, cov));
    const B = per.reduce((a, q) => a + q.B, 0) / per.length;
    const D = rankAgg(per.map((q) => q.D));
    return { B, D, S: Math.sqrt(B * D), per };
  }

  // ---------- tree data ----------
  const N = (id, label, r, c) => ({ id, label, r, c });
  const DIRS = [
    N("rogue", "Rogue", "R1", [
      N("perma", "Permadeath", "R2", [N("save", "Save scumming", "R3"), N("table", "Tabletop games", "R4")]),
      N("hack", "Hack", "R5", [N("port", "Porting", "R6")]),
      N("bsd", "BSD", "R7"),
      N("ken", "Ken Arnold", "R8", [N("curses", "Curses", "R9")]),
    ]),
    N("bam", "Beneath Apple Manor", "R10", [N("proc", "Procedural generation", "R11", [N("seed", "Random seed", "R12")])]),
  ];
  ["a", "b", "c", "b", "a", "c", "a", "b"].forEach((t, i) => {
    const p = "u" + i;
    if (t === "a") DIRS.push(N(p, "", "", [N(p + "x", "", "", [N(p + "x1"), N(p + "x2")])]));
    if (t === "b") DIRS.push(N(p, "", "", [N(p + "x", "", "", [N(p + "x1")]), N(p + "y", "", "", [N(p + "y1")])]));
    if (t === "c") DIRS.push(N(p, "", "", [N(p + "x", "", "", [N(p + "x1")]), N(p + "y")]));
  });
  const RUBRICS = {
    R1: ["Roguelike", "Rogue", "The 1980 game Rogue is considered the forerunner and namesake of the roguelike genre.", true],
    R2: ["Rogue", "Permadeath", "Rogue implements permadeath, meaning a character that loses all health is dead.", true],
    R3: ["Permadeath", "Save scumming", "Players may circumvent permadeath by backing up and restoring savefiles (“save scumming”), considered cheating.", true],
    R4: ["Permadeath", "Tabletop games", "Permadeath is a mechanic used in tabletop games as well as video games.", true],
    R5: ["Rogue", "Hack", "Rogue inspired the development of Hack.", true],
    R6: ["Hack", "Porting", "Hack was ported to other platforms, some later ports using graphical tiles instead of ASCII.", false],
    R7: ["Rogue", "Berkeley Software Distribution", "Rogue became popular in part due to its inclusion in the Berkeley Software Distribution (BSD).", true],
    R8: ["Rogue", "Ken Arnold", "Ken Arnold made later contributions to Rogue.", true],
    R9: ["Ken Arnold", "Curses", "Ken Arnold wrote the first curses library, released with BSD UNIX and used for games such as Rogue.", true],
    R10: ["Roguelike", "Beneath Apple Manor", "Beneath Apple Manor predates Rogue within the history of the roguelike genre.", true],
    R11: ["Beneath Apple Manor", "Procedural generation", "Beneath Apple Manor was one of the first video games to use procedural generation.", true],
    R12: ["Procedural generation", "Random seed", "Procedural generation used predefined seed values (map seeds) to generate very large game worlds.", false],
  };
  const RIDS = Object.keys(RUBRICS);
  // o3 Deep Research: real verdicts for R1–R12, illustrative ones for the unlabelled branches.
  const O3 = new Set(["rogue", "perma", "save", "table", "hack", "bsd", "ken", "curses", "bam", "proc", "u0", "u2", "u2y", "u4", "u6", "u6x"]);
  const H1 = DIRS.map((d) => d.id);
  const STYLES = [
    new Set([...H1, "perma", "proc", "u0x", "u3x"]),
    new Set(["rogue", "perma", "save", "table", "hack", "port", "bsd", "ken", "curses", "bam", "proc", "seed", "u0", "u0x"]),
    new Set(["rogue", "perma", "save", "hack", "bam", "u0", "u1", "u2y", "u3", "u3x", "u4x", "u5", "u6y", "u7"]),
  ];

  // ---------- layout ----------
  const X = [34, 160, 305, 445];
  const ROOT = { id: "root", label: "Roguelike", x: X[0], y: 247 };
  const nodes = [];
  let cursor = 44;
  DIRS.forEach((d, i) => {
    const labelled = Boolean(d.label);
    const rowH = labelled ? 30 : 11;
    (function place(v, hop, parent) {
      v.hop = hop; v.parent = parent; v.x = X[hop];
      nodes.push(v);
      if (!v.c) { v.y = cursor; cursor += rowH; return; }
      v.c.forEach((w) => place(w, hop + 1, v));
      v.y = v.c[0].y;
    })(d, 1, ROOT);
    d.dir = i;
    cursor += labelled ? 3 : 6;
  });
  const byId = Object.fromEntries(nodes.map((n) => [n.id, n]));

  const el = (tag, attrs, parent = svg) => {
    const e = document.createElementNS(NS, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    parent.append(e);
    return e;
  };
  const curve = (x1, y1, x2, y2) => {
    if (Math.abs(y1 - y2) < .5) return `M${x1},${y1} L${x2},${y2}`;
    const mx = (x1 + x2) / 2;
    return `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`;
  };

  const gBand = el("g", {});
  const gTex = el("g", {});
  const gEdge = el("g", {});
  const gTag = el("g", {});
  const gNode = el("g", {});
  const gLab = el("g", {});
  const gBar = el("g", {});

  // column heads
  const heads = ["root", "hop 1", "hop 2", "hop 3"].map((t, i) => {
    const e = el("text", { x: X[i], y: 16, class: "dp-colh", "text-anchor": i ? "middle" : "start" }, gLab);
    e.textContent = t;
    return e;
  });
  heads[0].setAttribute("x", 14);
  const barHeads = [["B", 571, "var(--dp-teal)"], ["D", 627, "var(--dp-indigo)"]].map(([t, x, c]) => {
    const e = el("text", { x, y: 16, class: "dp-colh", "text-anchor": "middle", style: `fill:${c}` }, gLab);
    e.textContent = t;
    return e;
  });

  // full local tree (phase 1): radial view, 50 root children, up to 5 children per node, 3 hops
  const cand = [], tex = [[], [], []], hopRings = [];
  const C = { x: 330, y: 236 }, RR = [0, 66, 138, 206];
  let seed = 7;
  const rnd = () => ((seed = (seed * 9301 + 49297) % 233280) / 233280);
  const polar = (r, deg) => [C.x + r * Math.cos(deg * Math.PI / 180), C.y + r * Math.sin(deg * Math.PI / 180)];
  [1, 2, 3].forEach((h) => {
    const g = el("g", { class: "dp-hopring" }, gTex);
    el("circle", { cx: C.x, cy: C.y, r: RR[h] }, g);
    const t = el("text", { x: C.x + 4, y: C.y - RR[h] - 4 }, g);
    t.textContent = `hop ${h}`;
    hopRings.push(g);
  });
  for (let i = 0; i < 50; i++) {
    const a = -90 + i * 7.2 + 3.6;
    const [x1, y1] = polar(RR[1], a);
    tex[0].push(el("path", { d: `M${C.x},${C.y} L${x1.toFixed(1)},${y1.toFixed(1)}`, class: "dp-tex", pathLength: 1 }, gTex));
    const k2 = 2 + Math.floor(rnd() * 4);
    let d2 = "", d3 = "";
    for (let j = 0; j < k2; j++) {
      const a2 = a + (j - (k2 - 1) / 2) * 1.35;
      const [x2, y2] = polar(RR[2], a2);
      d2 += `M${x1.toFixed(1)},${y1.toFixed(1)} L${x2.toFixed(1)},${y2.toFixed(1)} `;
      const k3 = 1 + Math.floor(rnd() * 5);
      for (let m = 0; m < k3; m++) {
        const [x3, y3] = polar(RR[3], a2 + (m - (k3 - 1) / 2) * .42);
        d3 += `M${x2.toFixed(1)},${y2.toFixed(1)} L${x3.toFixed(1)},${y3.toFixed(1)} `;
      }
    }
    tex[1].push(el("path", { d: d2, class: "dp-tex", pathLength: 1 }, gTex));
    tex[2].push(el("path", { d: d3, class: "dp-tex dp-tex-3", pathLength: 1 }, gTex));
    cand.push(el("circle", { cx: x1.toFixed(1), cy: y1.toFixed(1), r: 2.6, class: "dp-cand" }, gTex));
  }
  const GROUPS = [[2, 7, 11, 16, 20, 25, 31, 36, 41, 47], [0, 5, 9, 14, 22, 27, 33, 38, 43, 48], [3, 12, 18, 23, 29, 34, 39, 44, 45, 49]];
  const rootTex = el("circle", { cx: C.x, cy: C.y, r: 6.5, class: "dp-n dp-root" }, gTex);
  const rootTexLab = el("text", { x: C.x, y: C.y + 20, class: "dp-l dp-l-root", "text-anchor": "middle" }, gTex);
  rootTexLab.textContent = "Roguelike";

  // breadth band (phase 6)
  const band = el("rect", { x: X[1] - 11, y: 30, width: 22, height: 430, rx: 11, class: "dp-band dp-band-b" }, gBand);

  // scoped tree
  const edges = {}, nodeEls = {}, labels = {}, tags = {}, ticks = {}, rings = {}, bars = {};
  const rootEl = el("circle", { cx: ROOT.x, cy: ROOT.y, r: 6, class: "dp-n dp-root" }, gNode);
  const rootLab = el("text", { x: ROOT.x, y: ROOT.y + 21, class: "dp-l dp-l-root", "text-anchor": "middle" }, gLab);
  rootLab.textContent = ROOT.label;
  nodes.forEach((v) => {
    const p = v.parent;
    const path = el("path", { d: curve(p.x, p.y, v.x, v.y), class: "dp-e", pathLength: 1 }, gEdge);
    edges[v.id] = path;
    const r = v.label ? 4.6 : 3.2;
    nodeEls[v.id] = el("circle", { cx: v.x, cy: v.y, r, class: "dp-n" }, gNode);
    if (v.hop === 1) rings[v.id] = el("circle", { cx: v.x, cy: v.y, r: r + 3.2, class: "dp-ring" }, gNode);
    if (v.label) {
      const leaf = !v.c;
      const t = el("text", leaf ? { x: v.x + 9, y: v.y + 4, class: "dp-l" } : { x: v.x + 7, y: v.y - 7, class: "dp-l" }, gLab);
      t.textContent = v.label;
      if (v.id === "bsd") el("title", {}, t).textContent = "Berkeley Software Distribution";
      labels[v.id] = t;
    }
    if (v.hop === 1) {
      bars[v.id] = {
        bgB: el("rect", { x: 548, y: v.y - 3, width: 46, height: 6, rx: 3, class: "dp-bar-bg" }, gBar),
        bgD: el("rect", { x: 604, y: v.y - 3, width: 46, height: 6, rx: 3, class: "dp-bar-bg" }, gBar),
      };
      bars[v.id].B = el("rect", { x: 548, y: v.y - 3, width: 0, height: 6, rx: 3, class: "dp-bar dp-bar-b" }, gBar);
      bars[v.id].D = el("rect", { x: 604, y: v.y - 3, width: 0, height: 6, rx: 3, class: "dp-bar dp-bar-d" }, gBar);
    }
  });
  // rubric tags / ticks at edge positions
  nodes.forEach((v) => {
    const path = edges[v.id];
    const L = path.getTotalLength();
    const straight = Math.abs(v.parent.y - v.y) < .5;
    const t = straight ? .5 : v.hop === 1 ? .55 : .8;
    const pt = path.getPointAtLength(L * t);
    if (v.r) {
      const g = el("g", { class: "dp-rt", transform: `translate(${pt.x.toFixed(1)},${pt.y.toFixed(1)})` }, gTag);
      const w = v.r.length > 2 ? 22 : 17;
      el("rect", { x: -w / 2, y: -6.5, width: w, height: 13, rx: 6.5 }, g);
      el("text", { x: 0, y: 3 }, g).textContent = v.r;
      tags[v.r] = g;
    } else {
      ticks[v.id] = el("circle", { cx: pt.x.toFixed(1), cy: pt.y.toFixed(1), r: 1.5, class: "dp-tick", fill: "var(--dp-ink-2)" }, gTag);
    }
  });

  // ---------- panel helpers ----------
  const $ = (s, root = fig) => root.querySelector(s);
  const S = (view, key) => $(`.dp-pv[data-view="${view}"] [data-s="${key}"]`);
  const genList = $('[data-list="gen"]');
  const judgeList = $('[data-list="judge"]');
  const genFocus = $('[data-focus="gen"]');
  const genItems = {}, judgeItems = {};
  RIDS.forEach((id) => {
    const [a, b] = RUBRICS[id];
    const rel = `${a} → ${b}`;
    const li = document.createElement("li");
    li.innerHTML = '<span class="dp-rid"></span><span class="dp-rrel"></span>';
    li.querySelector(".dp-rid").textContent = id;
    li.querySelector(".dp-rrel").textContent = rel;
    genList.append(li); genItems[id] = li;
    const lj = li.cloneNode(true);
    const v = document.createElement("span");
    v.className = "dp-rv";
    lj.append(v);
    judgeList.append(lj); judgeItems[id] = lj;
  });

  let instant = false;
  const on = (e, k = "is-on") => e && e.classList.add(k);
  const off = (e, k = "is-on") => e && e.classList.remove(k);
  const ring = (e) => { if (!e || instant) return; e.classList.remove("is-ring"); void e.offsetWidth; e.classList.add("is-ring"); };
  const pulse = (e) => { if (!e || instant) return; e.classList.remove("is-pulse"); void e.getBBox(); e.classList.add("is-pulse"); };
  function countTo(node, value) {
    if (instant || reduce.matches) { node.textContent = value.toFixed(2); return; }
    const t0 = performance.now();
    const tick = (t) => {
      const k = Math.min(1, (t - t0) / 700);
      node.textContent = (value * (1 - (1 - k) ** 3)).toFixed(2);
      if (k < 1) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }
  function showView(i) {
    views.forEach((v, j) => v.classList.toggle("is-active", i === j));
  }
  function setVerdicts(cov, withTags = true) {
    nodes.forEach((v) => {
      const c = cov.has(v.id);
      nodeEls[v.id].classList.toggle("is-cov", c);
      nodeEls[v.id].classList.toggle("is-miss", !c);
      if (v.r && withTags) { tags[v.r].classList.toggle("is-cov", c); tags[v.r].classList.toggle("is-miss", !c); }
    });
  }
  function setDepthEdges(cov, show) {
    nodes.forEach((v) => {
      const cont = cov.has(v.id) && (v.parent === ROOT || cov.has(v.parent.id));
      edges[v.id].classList.toggle("is-dep", show && cont);
      edges[v.id].classList.toggle("is-dim", show && !cov.has(v.id));
    });
  }
  function setBars(res, which) {
    DIRS.forEach((d, i) => {
      if (which.includes("B")) bars[d.id].B.setAttribute("width", (46 * res.per[i].B).toFixed(1));
      if (which.includes("D")) bars[d.id].D.setAttribute("width", (46 * res.per[i].D).toFixed(1));
    });
  }
  function styleRow(i, res) {
    const row = $(`.dp-style[data-style="${i}"]`);
    row.querySelector(".dp-sb-b i").style.width = `${(res.B * 100).toFixed(1)}%`;
    row.querySelector(".dp-sb-d i").style.width = `${(res.D * 100).toFixed(1)}%`;
    row.querySelector('[data-v="B"]').textContent = res.B.toFixed(2);
    row.querySelector('[data-v="D"]').textContent = res.D.toFixed(2);
    fig.querySelectorAll(".dp-style").forEach((r, j) => r.classList.toggle("is-cur", j === i));
  }
  const o3Res = score(O3);
  const styleRes = STYLES.map(score);

  // ---------- reset ----------
  function resetAll() {
    fig.classList.remove("is-locked");
    svg.querySelectorAll(".is-on, .is-cov, .is-miss, .is-dep, .is-dim, .is-gone, .is-faded, .g1, .g2, .g3, .is-pulse").forEach((e) =>
      e.classList.remove("is-on", "is-cov", "is-miss", "is-dep", "is-dim", "is-gone", "is-faded", "g1", "g2", "g3", "is-pulse"));
    Object.values(bars).forEach((b) => { b.B.setAttribute("width", 0); b.D.setAttribute("width", 0); });
    fig.querySelectorAll(".dp-pv [data-s], .dp-rlist li").forEach((e) => e.classList.remove("is-on", "is-cur", "is-cov", "is-miss", "is-ring"));
    fig.querySelectorAll(".dp-rv").forEach((e) => { e.textContent = ""; e.className = "dp-rv"; });
    fig.querySelectorAll(".dp-style").forEach((r) => {
      r.classList.remove("is-cur");
      r.querySelectorAll(".dp-sb i").forEach((i) => { i.style.width = "0"; });
      r.querySelectorAll("em").forEach((e) => { e.textContent = ""; });
    });
    fig.querySelectorAll("[data-val]").forEach((e) => { e.textContent = "0.00"; });
    genFocus.textContent = "";
    $("[data-sum]").textContent = "";
    showView(-1);
  }

  // ---------- phases ----------
  const labelled = nodes.filter((v) => v.label);
  const unl = nodes.filter((v) => !v.label);
  const byHop = (h) => nodes.filter((v) => v.hop === h);
  const PHASES = [
    {
      cap: "Start from a Wikipedia root entity and expand its links into a local knowledge tree.",
      steps: [
        [900, () => { showView(0); on(rootTex); on(rootTexLab); on(S(0, "root")); ring(S(0, "root")); }],
        [900, () => { on(S(0, "l1")); on(hopRings[0]); tex[0].forEach((e) => on(e)); }],
        [1000, () => { on(S(0, "l2")); cand.forEach((c) => on(c)); }],
        [1100, () => { on(S(0, "l3")); on(hopRings[1]); tex[1].forEach((e) => on(e)); }],
        [1900, () => { on(S(0, "l4")); on(hopRings[2]); tex[2].forEach((e) => on(e)); }],
      ],
    },
    {
      cap: "Sample a research scope: the root plus 10 complete branches, each one research direction.",
      steps: [
        [500, () => { showView(1); on(S(1, "groups")); ring(S(1, "groups")); }],
        [1500, () => { GROUPS.forEach((g, k) => g.forEach((i) => cand[i].classList.add("g" + (k + 1)))); cand.forEach((c, i) => { if (!GROUPS.flat().includes(i)) c.classList.add("is-faded"); }); }],
        [1000, () => { cand.forEach((c, i) => { if (!GROUPS[0].includes(i)) c.classList.add("is-gone"); }); tex.flat().forEach((e) => e.classList.add("is-gone")); hopRings.forEach((g) => g.classList.add("is-gone")); }],
        [700, () => { cand.forEach((c) => c.classList.add("is-gone")); [rootTex, rootTexLab].forEach((e) => e.classList.add("is-gone")); heads.forEach((h) => on(h)); on(rootEl); on(rootLab); byHop(1).forEach((v) => { on(edges[v.id]); on(nodeEls[v.id]); }); }],
        [700, () => { byHop(2).forEach((v) => { on(edges[v.id]); on(nodeEls[v.id]); }); byHop(1).forEach((v) => on(labels[v.id])); }],
        [1600, () => { byHop(3).forEach((v) => { on(edges[v.id]); on(nodeEls[v.id]); }); labelled.forEach((v) => on(labels[v.id])); on(S(1, "scope")); ring(S(1, "scope")); }],
      ],
    },
    {
      cap: "From the same subtree, generate the research query and one evidence-grounded rubric per edge.",
      steps: [
        [1800, () => { showView(2); on(S(2, "query")); ring(S(2, "query")); }],
        [500, () => { on(S(2, "rubrics")); ring(S(2, "rubrics")); }],
        ...RIDS.map((id) => [620, () => {
          Object.values(genItems).forEach((li) => li.classList.remove("is-cur"));
          on(genItems[id]); genItems[id].classList.add("is-cur"); on(tags[id]);
          const [a, b, text] = RUBRICS[id];
          genFocus.textContent = `${id} · ${a} → ${b}: “${text}”`;
        }]),
        [1600, () => { unl.forEach((v) => on(ticks[v.id])); Object.values(genItems).forEach((li) => li.classList.remove("is-cur")); genFocus.textContent = "Every other edge gets its own rubric too (dots); a real case has 236–310."; }],
      ],
    },
    {
      cap: "The agent receives only the query and writes a report. The tree and the rubrics stay hidden.",
      steps: [
        [600, () => { showView(3); fig.classList.add("is-locked"); on(S(3, "q2")); }],
        [800, () => { on(S(3, "agent")); ring(S(3, "agent")); }],
        [500, () => { on(S(3, "report")); }],
        ...["h1", "h2", "h3", "h4", "h5", "h6"].map((k) => [480, () => on(S(3, k))]),
        [1200, () => {}],
      ],
    },
    {
      cap: "A judge checks each rubric against the report. Covered relations light up on the tree.",
      steps: [
        [900, () => { showView(4); fig.classList.remove("is-locked"); on(S(4, "judge")); ring(S(4, "judge")); on(S(4, "verdicts")); }],
        ...RIDS.map((id) => [420, () => {
          const v = labelled.find((n) => n.r === id);
          const c = RUBRICS[id][3];
          const li = judgeItems[id];
          on(li); li.classList.add(c ? "is-cov" : "is-miss");
          const mark = li.querySelector(".dp-rv");
          mark.textContent = c ? "✓" : "✗"; mark.classList.add(c ? "is-cov" : "is-miss");
          nodeEls[v.id].classList.add(c ? "is-cov" : "is-miss"); pulse(nodeEls[v.id]);
          tags[id].classList.add(c ? "is-cov" : "is-miss");
          $("[data-sum]").textContent = `${RIDS.slice(0, RIDS.indexOf(id) + 1).filter((r) => RUBRICS[r][3]).length} of ${RIDS.indexOf(id) + 1} satisfied`;
        }]),
        [1800, () => { unl.forEach((v) => nodeEls[v.id].classList.add(O3.has(v.id) ? "is-cov" : "is-miss")); }],
      ],
    },
    {
      cap: "Aggregate bottom-up over the tree: breadth across directions, depth along chains, overall = √(B·D).",
      steps: [
        [400, () => { showView(5); }],
        [1500, () => { on(band); H1.forEach((id) => { if (O3.has(id)) on(rings[id]); }); on(barHeads[0]); Object.values(bars).forEach((b) => on(b.bgB)); setBars(o3Res, "B"); on(S(5, "mb")); ring(S(5, "mb")); countTo($('[data-val="B"]'), o3Res.B); }],
        [1700, () => { off(band); H1.forEach((id) => off(rings[id])); setDepthEdges(O3, true); on(barHeads[1]); Object.values(bars).forEach((b) => on(b.bgD)); setBars(o3Res, "D"); on(S(5, "md")); ring(S(5, "md")); countTo($('[data-val="D"]'), o3Res.D); }],
        [1100, () => { on(S(5, "mo")); ring(S(5, "mo")); countTo($('[data-val="S"]'), o3Res.S); }],
        [2200, () => { on(S(5, "mnote")); }],
      ],
    },
    {
      cap: "Same number of satisfied rubrics, different shapes. Breadth and depth tell the reports apart.",
      steps: [
        [700, () => { showView(6); on(S(6, "sh")); RIDS.forEach((id) => off(tags[id])); unl.forEach((v) => off(ticks[v.id])); }],
        ...STYLES.map((cov, i) => [2600, () => {
          setVerdicts(cov, false); setDepthEdges(cov, true); setBars(styleRes[i], "BD");
          on(S(6, ["sA", "sB", "sC"][i])); styleRow(i, styleRes[i]);
        }]),
        [2600, () => { on(S(6, "snote")); fig.querySelectorAll(".dp-style").forEach((r) => r.classList.remove("is-cur")); }],
      ],
    },
  ];

  // ---------- engine ----------
  let token = 0, paused = false, inView = false, started = false, current = 0;
  function setTab(i) {
    current = i;
    tabs.forEach((t, j) => {
      t.setAttribute("aria-selected", String(i === j));
      t.tabIndex = i === j ? 0 : -1;
      t.querySelector("i").style.width = j < i ? "100%" : "0";
    });
    caption.textContent = PHASES[i].cap;
  }
  function wait(ms, my, bar, base, total) {
    return new Promise((resolve) => {
      let acc = 0, last = performance.now();
      const step = (t) => {
        if (my !== token) return resolve(false);
        const dt = t - last; last = t;
        if (!paused && inView && !document.hidden) acc += dt;
        bar.style.width = `${Math.min(100, ((base + acc) / total) * 100)}%`;
        if (acc >= ms) return resolve(true);
        requestAnimationFrame(step);
      };
      requestAnimationFrame(step);
    });
  }
  function applyInstant(upTo) {
    instant = true;
    fig.classList.add("dp-instant");
    for (let p = 0; p <= upTo; p++) PHASES[p].steps.forEach((s) => s[1]());
    void fig.offsetWidth;
    fig.classList.remove("dp-instant");
    instant = false;
  }
  async function run(from) {
    const my = ++token;
    resetAll();
    if (from > 0) applyInstant(from - 1);
    for (let p = from; p < PHASES.length; p++) {
      setTab(p);
      const steps = PHASES[p].steps;
      const total = steps.reduce((a, s) => a + s[0], 0);
      const bar = tabs[p].querySelector("i");
      let base = 0;
      for (const [ms, fn] of steps) {
        fn();
        if (!(await wait(ms, my, bar, base, total))) return;
        base += ms;
      }
      bar.style.width = "100%";
    }
    if (my === token) setTimeout(() => { if (my === token) run(0); }, 1500);
  }
  function showStatic(p) {
    token++;
    resetAll();
    applyInstant(p);
    setTab(p);
    tabs.forEach((t, j) => { t.querySelector("i").style.width = j <= p ? "100%" : "0"; });
  }
  function setPaused(v) {
    paused = v;
    fig.classList.toggle("is-paused", v);
    playBtn.setAttribute("aria-label", v ? "Play animation" : "Pause animation");
  }
  tabs.forEach((t, i) => t.addEventListener("click", () => {
    if (reduce.matches) return showStatic(i);
    setPaused(false);
    run(i);
  }));
  fig.querySelector(".dp-fig-tabs").addEventListener("keydown", (e) => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    const n = (current + (e.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    tabs[n].focus(); tabs[n].click();
  });
  playBtn.addEventListener("click", () => {
    if (reduce.matches) return;
    setPaused(!paused);
  });
  if (reduce.matches) {
    setPaused(true);
    showStatic(5);
    return;
  }
  setTab(0);
  new IntersectionObserver(([entry]) => {
    inView = entry.isIntersecting;
    if (inView && !started) { started = true; run(0); }
  }, { threshold: 0.25 }).observe(fig);
  window.__dpFig = { run, showStatic, score: () => ({ o3: o3Res, styles: styleRes }), setPaused };
})();
