"use strict";

document.documentElement.classList.add("js");

// Use explicit identifiers so selected buttons and displayed figures always agree.
const domainLabels = { overall: "Overall Coverage", breadth: "Breadth", depth: "Depth" };
const overallDomainCopy = document.getElementById("domain-copy").textContent;
document.querySelectorAll("[data-domain]").forEach((button) => {
  button.addEventListener("click", () => {
    const metric = button.dataset.domain;
    const title = `${domainLabels[metric]} (%) across 24 domains`;
    const source = `assets/images/domain-${metric}.webp`;
    document.querySelectorAll("[data-domain]").forEach((candidate) => {
      candidate.setAttribute("aria-pressed", String(candidate === button));
    });
    const image = document.getElementById("domain-image");
    image.src = source;
    image.alt = `${domainLabels[metric]} (%) for 16 agents across 24 domains; each agent–domain score is averaged over six cases.`;
    Object.assign(document.getElementById("domain-figure").dataset, { zoom: source, title });
    document.getElementById("domain-metric-label").textContent = `${domainLabels[metric]} (%)`;
    document.getElementById("domain-copy").textContent = metric === "overall" ? overallDomainCopy : `${domainLabels[metric]} varies across the 24 domains, with each agent–domain score averaged over six cases. Agent coverage varies across domains, revealing domain-specific strengths and gaps.`;
  });
});

// Independent, manually controlled carousels; without JS every slide stays readable.
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
document.querySelectorAll("[data-carousel]").forEach((carousel) => {
  const track = carousel.querySelector("[data-carousel-track]");
  const slides = [...track.querySelectorAll("[data-carousel-slide]")];
  const dots = [...carousel.querySelectorAll("[data-carousel-go]")];
  const status = carousel.querySelector("[data-carousel-status]");
  let currentIndex = 0;
  let scrollTimer;
  function updatePagination(index) {
    currentIndex = index;
    dots.forEach((dot, position) => {
      if (position === index) dot.setAttribute("aria-current", "true");
      else dot.removeAttribute("aria-current");
    });
    slides.forEach((slide, position) => {
      // Keep offscreen links and zoom controls out of the keyboard focus order.
      slide.inert = position !== index;
    });
    const analysisFrame = slides[index].querySelector(".analysis-image-frame");
    if (analysisFrame) {
      const visual = analysisFrame.querySelector(".figure-scroll") || analysisFrame;
      const bounds = visual.getBoundingClientRect();
      const arrow = carousel.querySelector("[data-carousel-previous]");
      const top = bounds.top - carousel.getBoundingClientRect().top + (bounds.height - arrow.offsetHeight) / 2;
      carousel.style.setProperty("--analysis-arrow-top", `${top}px`);
    }
    track.dataset.activeSlide = String(index);
    status.textContent = `${slides[index].querySelector("h3").textContent}, ${index + 1} of ${slides.length}.`;
  }
  function showSlide(index, instant = false) {
    const position = (index + slides.length) % slides.length;
    updatePagination(position);
    track.scrollTo({
      left: slides[position].offsetLeft,
      behavior: instant || reducedMotion.matches ? "instant" : "smooth",
    });
  }
  carousel.querySelector("[data-carousel-previous]").addEventListener("click", () => showSlide(currentIndex - 1));
  carousel.querySelector("[data-carousel-next]").addEventListener("click", () => showSlide(currentIndex + 1));
  dots.forEach((dot) => dot.addEventListener("click", () => showSlide(Number(dot.dataset.carouselGo))));
  track.addEventListener("keydown", (event) => {
    if (event.target !== track) return;
    if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      if (event.key === "ArrowLeft") showSlide(currentIndex - 1);
      if (event.key === "ArrowRight") showSlide(currentIndex + 1);
      if (event.key === "Home") showSlide(0);
      if (event.key === "End") showSlide(slides.length - 1);
    }
  });
  track.addEventListener("scroll", () => {
    clearTimeout(scrollTimer);
    scrollTimer = setTimeout(() => {
      const distances = slides.map((slide) => Math.abs(track.scrollLeft - slide.offsetLeft));
      updatePagination(distances.indexOf(Math.min(...distances)));
    }, 120);
  }, { passive: true });
  new ResizeObserver(() => showSlide(currentIndex, true)).observe(track);
  updatePagination(0);
});

// Native modal behavior provides Escape, focus containment, and focus restoration.
const dialog = document.getElementById("figure-dialog");
const dialogImage = document.getElementById("figure-dialog-image");
document.querySelectorAll("[data-zoom]").forEach((button) => {
  button.disabled = false;
  button.addEventListener("click", () => {
    dialogImage.src = button.dataset.zoom;
    dialogImage.alt = button.dataset.title;
    document.getElementById("figure-dialog-title").textContent = button.dataset.title;
    dialog.showModal();
    document.body.classList.add("dialog-open");
    const viewport = dialog.querySelector(".dialog-image-scroll");
    viewport.scrollTop = 0;
    viewport.scrollLeft = 0;
  });
});
dialog.querySelector(".dialog-close").addEventListener("click", () => dialog.close());
dialog.addEventListener("click", (event) => {
  if (event.target !== dialog) return;
  const bounds = dialog.getBoundingClientRect();
  if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) dialog.close();
});
dialog.addEventListener("close", () => document.body.classList.remove("dialog-open"));
