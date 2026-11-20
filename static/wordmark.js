"use strict";

// The animated "Human Emulator" wordmark. A decoder sweep runs through it on its own,
// one letter at a time — H, u, m, a, n, E, m, u, l, a, t, o, r — each letter flickering
// through glyphs before it locks back in, then the sweep starts again from H.
// Pointer: letters lift and glow, and the one you touch scrambles.
// It also reacts to the engine: faster while planning or acting, amber while waiting
// for approval, a light wave when a mission completes, a red pulse when one halts.
window.Wordmark = (() => {
  const root = document.querySelector(".wordmark");
  const reduced = matchMedia("(prefers-reduced-motion: reduce)");
  const GLYPHS = "ABCDEFGHJKLMNPQRSTUVWXYZ0123456789#$%&*+=<>/\\|?!@▚▞▟▙▛▜░▒▓";
  const letters = [];
  let sweepTimer = 0;
  let next = 0;

  if (!root) return { flash() {} };

  for (const word of root.querySelectorAll(".wm-word")) {
    const text = word.textContent;
    word.textContent = "";
    for (const ch of text) {
      const span = document.createElement("span");
      span.className = "ch";
      span.textContent = ch;
      span.setAttribute("aria-hidden", "true");
      word.append(span);
      letters.push(span);
    }
  }
  letters.forEach((el, k) => el.style.setProperty("--k", k));

  const glyph = () => GLYPHS[Math.floor(Math.random() * GLYPHS.length)];

  // A scrambling letter keeps its real character for layout and shows the
  // random glyph on top, so the word never shifts while it decodes.
  function scrambleLetter(el, ms, every = 45) {
    const until = performance.now() + ms;
    clearInterval(el._spin);
    el.classList.add("scr");
    el.classList.remove("lock");
    el.dataset.g = glyph();
    el._spin = setInterval(() => {
      if (performance.now() >= until) {
        clearInterval(el._spin);
        el.classList.remove("scr");
        void el.offsetWidth;
        el.classList.add("lock");
        return;
      }
      el.dataset.g = glyph();
    }, every);
  }

  // How long each letter decodes: brisk while the engine works, calm when idle.
  function pace() {
    const mode = document.documentElement.dataset.edge;
    return mode === "think" || mode === "act" || mode === "undo" ? 230 : 440;
  }

  function sweep() {
    clearTimeout(sweepTimer);
    if (reduced.matches) return;
    if (document.hidden) {
      sweepTimer = setTimeout(sweep, 600);
      return;
    }
    const ms = pace();
    scrambleLetter(letters[next], ms);
    next = (next + 1) % letters.length;
    // The next letter starts once this one has locked; after the last R, a short breath, then H again.
    sweepTimer = setTimeout(sweep, next === 0 ? ms + 1300 : ms + 70);
  }

  function flash(kind = "complete") {
    if (reduced.matches) return;
    root.classList.remove("flash", "fail");
    void root.offsetWidth;
    root.classList.add("flash");
    if (kind === "fail") root.classList.add("fail");
    setTimeout(() => root.classList.remove("flash", "fail"), 1000 + letters.length * 38 + 150);
  }

  // Pointer: letters lift and glow by distance; the one you touch scrambles.
  let frame = 0;
  root.addEventListener("pointermove", (event) => {
    if (reduced.matches) return;
    cancelAnimationFrame(frame);
    frame = requestAnimationFrame(() => {
      const size = parseFloat(getComputedStyle(root).fontSize) || 80;
      for (const el of letters) {
        const r = el.getBoundingClientRect();
        const d = Math.hypot(event.clientX - (r.left + r.width / 2), (event.clientY - (r.top + r.height / 2)) * 1.6);
        el.style.setProperty("--lift", Math.max(0, 1 - d / (size * 1.9)).toFixed(3));
      }
    });
  });
  root.addEventListener("pointerleave", () => {
    cancelAnimationFrame(frame);
    for (const el of letters) el.style.setProperty("--lift", "0");
  });
  root.addEventListener("pointerover", (event) => {
    const el = event.target.closest?.(".ch");
    if (el && !reduced.matches && !el.classList.contains("scr")) scrambleLetter(el, 160, 40);
  });

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) sweep();
  });
  reduced.addEventListener("change", () => {
    for (const el of letters) {
      clearInterval(el._spin);
      el.classList.remove("scr", "lock");
    }
    if (!reduced.matches) sweep();
  });

  root.classList.add("ready");
  sweepTimer = setTimeout(sweep, 500);

  return { flash };
})();
