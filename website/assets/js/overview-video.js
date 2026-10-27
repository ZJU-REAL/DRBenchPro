"use strict";

// Keep native controls available without JavaScript and after a failed play attempt.
(() => {
  const video = document.getElementById("overview-video");
  const start = document.getElementById("overview-video-start");
  const error = document.getElementById("overview-video-error");
  if (!video || !start || !error) return;

  video.controls = false;
  video.tabIndex = -1;
  start.hidden = false;

  function showPlayer() {
    const restoreFocus = document.activeElement === start;
    video.controls = true;
    video.tabIndex = 0;
    start.hidden = true;
    if (restoreFocus) video.focus({ preventScroll: true });
  }
  start.addEventListener("click", async () => {
    showPlayer();
    error.hidden = true;
    try {
      await video.play();
    } catch {
      error.hidden = false;
    }
  });
  video.addEventListener("play", () => {
    showPlayer();
    error.hidden = true;
  });
  video.addEventListener("error", () => {
    showPlayer();
    error.hidden = false;
  });
})();
