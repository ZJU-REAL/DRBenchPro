"use strict";
// Small page behaviours: copy buttons, top-bar state, active section link (original code, MIT).
(() => {
  document.querySelectorAll(".dp-copy").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const text = btn.parentElement.querySelector("code").textContent;
      let ok = false;
      try {
        // Some embedded contexts leave the clipboard promise pending; fall back after a short wait.
        await Promise.race([
          navigator.clipboard.writeText(text),
          new Promise((_, reject) => setTimeout(() => reject(new Error("clipboard timeout")), 900)),
        ]);
        ok = true;
      } catch {
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.setAttribute("readonly", "");
        ta.style.cssText = "position:fixed;opacity:0;top:0;left:0";
        document.body.append(ta);
        ta.select();
        try { ok = document.execCommand("copy"); } catch { ok = false; }
        ta.remove();
      }
      btn.textContent = ok ? "Copied" : "Press ⌘C";
      btn.classList.toggle("is-done", ok);
      setTimeout(() => { btn.textContent = "Copy"; btn.classList.remove("is-done"); }, 1800);
    });
  });

  const top = document.querySelector(".dp-top");
  const onScroll = () => top.classList.toggle("is-scrolled", window.scrollY > 8);
  window.addEventListener("scroll", onScroll, { passive: true });
  onScroll();

  const links = [...document.querySelectorAll(".dp-top-links a")].map((a) => [document.getElementById(a.getAttribute("href").slice(1)), a]).filter(([s]) => s);
  let ticking = false;
  const mark = () => {
    ticking = false;
    let cur = null;
    links.forEach(([s, a]) => { if (s.getBoundingClientRect().top <= 140) cur = a; });
    links.forEach(([, a]) => a.classList.toggle("is-active", a === cur));
  };
  window.addEventListener("scroll", () => { if (!ticking) { ticking = true; requestAnimationFrame(mark); } }, { passive: true });
  mark();
})();
