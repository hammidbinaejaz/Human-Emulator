"use strict";

// Living side gutters: circuit traces that carry light pulses into the dashboard.
// They are driven by real engine activity — model tokens, approvals, verified
// steps, failures and undo — so the edges show what Human Emulator is doing.
window.Edges = (() => {
  const GRID = 22;
  const MIN_SIDE = 56;
  const RAIL_SIDE = 104;
  const COLORS = {
    lime: "212, 255, 58",
    white: "237, 243, 228",
    cyan: "110, 231, 255",
    amber: "255, 192, 70",
    red: "255, 107, 90",
  };
  const MODES = {
    idle: { rate: 0.45, speed: 150, color: "lime", scan: 9, label: "Standby" },
    think: { rate: 2.2, speed: 260, color: "lime", scan: 4.5, label: "Thinking" },
    await: { rate: 0.7, speed: 130, color: "amber", scan: 6, label: "Awaiting you" },
    act: { rate: 3.2, speed: 330, color: "lime", scan: 3.2, label: "Acting" },
    undo: { rate: 3.2, speed: 300, color: "cyan", scan: 3.2, label: "Rewinding", reverse: true },
  };

  const reduced = matchMedia("(prefers-reduced-motion: reduce)");
  const sprites = new Map();
  const sides = ["left", "right"].map((name, i) => ({
    name,
    canvas: document.querySelector(`.edge-canvas.${name}`),
    rail: document.querySelector(`.edge-rail.${name}`),
    seed: 0x5eed + i * 7919,
    visible: false,
    traces: [],
    pulses: [],
    queue: [],
    spawn: 0,
  }));

  let mode = "idle";
  let pointer = null;
  let frame = 0;
  let last = 0;
  let scan = 0.35;
  let flash = { amount: 0, color: "lime" };
  let tokenGate = 0;
  let tokenTurn = 0;
  let resizeFrame = 0;

  // ------------------------------------------------------------ geometry

  function rng(seed) {
    let a = seed >>> 0;
    return () => {
      a = (a + 0x6d2b79f5) >>> 0;
      let t = Math.imul(a ^ (a >>> 15), a | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  function makeTrace(pts, stub) {
    const lens = [0];
    for (let i = 1; i < pts.length; i++) {
      lens.push(lens[i - 1] + Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]));
    }
    const [x, y] = pts[pts.length - 1];
    return { pts, lens, length: lens[lens.length - 1], stub, node: { x, y, glow: 0, color: "lime" } };
  }

  function pointAt(trace, d) {
    const { pts, lens } = trace;
    const at = Math.max(0, Math.min(trace.length, d));
    let i = 1;
    while (i < lens.length - 1 && lens[i] < at) i++;
    const k = (at - lens[i - 1]) / (lens[i] - lens[i - 1] || 1);
    return [pts[i - 1][0] + (pts[i][0] - pts[i - 1][0]) * k, pts[i - 1][1] + (pts[i][1] - pts[i - 1][1]) * k];
  }

  function traceSegment(ctx, trace, from, to) {
    const [x0, y0] = pointAt(trace, from);
    ctx.moveTo(x0, y0);
    for (let i = 1; i < trace.pts.length - 1; i++) {
      if (trace.lens[i] > from && trace.lens[i] < to) ctx.lineTo(trace.pts[i][0], trace.pts[i][1]);
    }
    const [x1, y1] = pointAt(trace, to);
    ctx.lineTo(x1, y1);
  }

  // Traces leave a vertical bus, run inward with 45° jogs and end in a port
  // beside the dashboard. Jogs only go down and never past the next trace, so
  // traces never cross.
  function build(side, railed) {
    const rand = rng(side.seed);
    const { w, h } = side;
    const bus = railed ? 34 : 10;
    const end = w - 18;
    const top = GRID * 3;
    const bottom = h - GRID * 3;
    const snap = (v) => bus + Math.round((v - bus) / (GRID / 2)) * (GRID / 2);

    const ys = [];
    for (let y = top + Math.floor(rand() * 2) * GRID; y < bottom; y += GRID * (2 + Math.floor(rand() * 3))) ys.push(y);

    side.traces = ys.map((y, i) => {
      const lead = GRID * (1 + Math.floor(rand() * 5));
      const startY = Math.max(8, Math.min(h - 8, rand() < 0.5 ? y - lead : y + lead));
      const pts = [[bus, startY], [bus, y]];
      const stub = end - bus > 70 && rand() < 0.16;
      const stop = stub ? snap(bus + (end - bus) * (0.35 + rand() * 0.3)) : end;
      const jogs = stop - bus > 220 ? 2 : stop - bus > 60 ? 1 : 0;
      let budget = (ys[i + 1] ?? bottom + GRID * 2) - y - GRID;
      let x = bus;
      let cy = y;
      for (let j = 0; j < jogs; j++) {
        const xa = snap(bus + (stop - bus) * ((j + 0.3 + rand() * 0.45) / (jogs + 0.25)));
        const dy = Math.min(Math.floor(budget / GRID), rand() < 0.35 ? 0 : 1 + Math.floor(rand() * 2)) * GRID;
        if (xa <= x + 6 || !dy || xa + dy >= stop - 10) continue;
        pts.push([xa, cy]);
        cy += dy;
        budget -= dy;
        x = xa + dy;
        pts.push([x, cy]);
      }
      pts.push([stop, cy]);
      return makeTrace(pts, stub);
    });

    side.bus = bus;
    side.wires = new Path2D();
    side.vias = new Path2D();
    side.ports = new Path2D();
    for (const trace of side.traces) {
      trace.pts.forEach(([px, py], i) => (i ? side.wires.lineTo(px, py) : side.wires.moveTo(px, py)));
      for (const [px, py] of trace.pts.slice(2, -1)) {
        side.vias.moveTo(px + 1.4, py);
        side.vias.arc(px, py, 1.4, 0, Math.PI * 2);
      }
      const { x: nx, y: ny } = trace.node;
      if (trace.stub) side.ports.rect(nx - 2.5, ny - 2.5, 5, 5);
      else {
        side.ports.moveTo(nx + 3, ny);
        side.ports.arc(nx, ny, 3, 0, Math.PI * 2);
      }
    }

    side.ticks = new Path2D();
    if (railed) {
      for (let y = GRID; y < h - GRID; y += GRID) {
        const major = Math.round(y / GRID) % 5 === 0;
        side.ticks.moveTo(bus - (major ? 7 : 3), y + 0.5);
        side.ticks.lineTo(bus, y + 0.5);
      }
    }
    side.brackets = new Path2D();
    for (const [cy, dir] of [[8, 1], [h - 8, -1]]) {
      side.brackets.moveTo(8, cy + dir * 14);
      side.brackets.lineTo(8, cy);
      side.brackets.lineTo(22, cy);
    }
    side.pulses = [];
    side.queue = [];
  }

  function layout() {
    const shell = document.querySelector(".shell");
    if (!shell) return;
    const box = shell.getBoundingClientRect();
    const style = getComputedStyle(shell);
    const vw = document.documentElement.clientWidth;
    const vh = window.innerHeight;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const space = {
      left: box.left + parseFloat(style.paddingLeft),
      right: vw - box.right + parseFloat(style.paddingRight),
    };
    for (const side of sides) {
      const w = Math.floor(space[side.name] - 2);
      side.visible = w >= MIN_SIDE;
      side.canvas.classList.toggle("on", side.visible);
      const railed = side.visible && w >= RAIL_SIDE;
      side.rail.hidden = !railed;
      if (!side.visible) continue;
      Object.assign(side, { w, h: vh, dpr, ctx: side.canvas.getContext("2d") });
      side.canvas.style.width = `${w}px`;
      side.canvas.style.height = `${vh}px`;
      side.canvas.width = Math.round(w * dpr);
      side.canvas.height = Math.round(vh * dpr);
      side.x0 = side.name === "left" ? 0 : vw;
      build(side, railed);
      draw(side, performance.now(), 0);
    }
  }

  // ------------------------------------------------------------ pulses

  function sprite(color) {
    if (sprites.has(color)) return sprites.get(color);
    const c = document.createElement("canvas");
    c.width = c.height = 48;
    const g = c.getContext("2d");
    const grad = g.createRadialGradient(24, 24, 0, 24, 24, 24);
    grad.addColorStop(0, "rgba(255, 255, 255, 0.95)");
    grad.addColorStop(0.18, `rgba(${COLORS[color]}, 0.85)`);
    grad.addColorStop(0.5, `rgba(${COLORS[color]}, 0.18)`);
    grad.addColorStop(1, `rgba(${COLORS[color]}, 0)`);
    g.fillStyle = grad;
    g.fillRect(0, 0, 48, 48);
    sprites.set(color, c);
    return c;
  }

  function spawn(side, { trace, color, speed, reverse } = {}) {
    if (!side.visible || !side.traces.length) return;
    const M = MODES[mode];
    let pick = trace;
    if (!pick) {
      const pool = Math.random() < 0.15 ? side.traces : side.traces.filter((t) => !t.stub);
      pick = pool[Math.floor(Math.random() * pool.length)] || side.traces[0];
    }
    const dir = (reverse ?? M.reverse) ? -1 : 1;
    if (dir < 0) Object.assign(pick.node, { glow: 1, color: color || M.color });
    side.pulses.push({
      trace: pick,
      d: 0,
      dir,
      color: color || M.color,
      speed: (speed || M.speed) * (0.8 + Math.random() * 0.45),
    });
    if (side.pulses.length > 90) side.pulses.shift();
  }

  function wave(color, { reverse = false, bottomUp = false, gap = 38 } = {}) {
    const now = performance.now();
    for (const side of sides) {
      if (!side.visible) continue;
      const list = side.traces.filter((t) => !t.stub);
      list.forEach((trace, i) => {
        const order = bottomUp ? list.length - 1 - i : i;
        side.queue.push({ at: now + order * gap, trace, color, reverse, speed: 440 });
      });
      side.queue.sort((a, b) => a.at - b.at);
    }
  }

  function glowAll(color, amount) {
    for (const side of sides) {
      for (const trace of side.traces) {
        trace.node.glow = Math.max(trace.node.glow, amount);
        trace.node.color = color;
      }
    }
  }

  // ------------------------------------------------------------ drawing

  function brightWires(side, color, alpha) {
    const { ctx } = side;
    ctx.strokeStyle = `rgba(${COLORS[color]}, ${alpha})`;
    ctx.stroke(side.wires);
    ctx.fillStyle = `rgba(${COLORS[color]}, ${alpha})`;
    ctx.fill(side.vias);
  }

  function draw(side, now, dt) {
    const { ctx, w, h, dpr } = side;
    const M = MODES[mode];
    const still = reduced.matches;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    if (side.name === "right") {
      ctx.translate(w, 0);
      ctx.scale(-1, 1);
    }
    ctx.clearRect(0, 0, w, h);
    ctx.globalCompositeOperation = "source-over";
    ctx.lineWidth = 1;

    const bus = ctx.createLinearGradient(0, 0, 0, h);
    bus.addColorStop(0, "rgba(212, 255, 58, 0)");
    bus.addColorStop(0.08, "rgba(212, 255, 58, 0.2)");
    bus.addColorStop(0.92, "rgba(212, 255, 58, 0.2)");
    bus.addColorStop(1, "rgba(212, 255, 58, 0)");
    ctx.strokeStyle = bus;
    ctx.beginPath();
    ctx.moveTo(side.bus + 0.5, 0);
    ctx.lineTo(side.bus + 0.5, h);
    ctx.stroke();
    ctx.strokeStyle = "rgba(212, 255, 58, 0.16)";
    ctx.stroke(side.ticks);
    ctx.strokeStyle = "rgba(212, 255, 58, 0.35)";
    ctx.stroke(side.brackets);
    brightWires(side, "lime", 0.1);
    ctx.strokeStyle = "rgba(212, 255, 58, 0.28)";
    ctx.stroke(side.ports);

    if (still) return;

    // Scan beam — the same "eye" that reads the screen sweeps the edges.
    // It fades out toward the dashboard so it never leaves a hard edge.
    const sy = -90 + scan * (h + 180);
    const fade = ctx.createLinearGradient(0, 0, w, 0);
    fade.addColorStop(0, "rgba(212, 255, 58, 1)");
    fade.addColorStop(0.55, "rgba(212, 255, 58, 0.45)");
    fade.addColorStop(1, "rgba(212, 255, 58, 0)");
    ctx.fillStyle = fade;
    for (let i = 0; i < 11; i++) {
      ctx.globalAlpha = 0.075 * ((i + 1) / 11) ** 1.6;
      ctx.fillRect(0, sy - 110 + i * 10, w, 10);
    }
    ctx.globalAlpha = 0.65;
    ctx.fillRect(0, sy, w, 1);
    ctx.globalAlpha = 1;
    for (const [reach, alpha] of [[60, 0.16], [26, 0.3], [8, 0.5]]) {
      ctx.save();
      ctx.beginPath();
      ctx.rect(0, sy - reach * 1.6, w, reach * 1.6 + 4);
      ctx.clip();
      brightWires(side, "lime", alpha);
      ctx.restore();
    }

    if (pointer) {
      const px = side.name === "left" ? pointer.x : side.x0 - pointer.x;
      if (px > -20 && px < w + 60) {
        const halo = ctx.createRadialGradient(px, pointer.y, 0, px, pointer.y, 110);
        halo.addColorStop(0, "rgba(212, 255, 58, 0.09)");
        halo.addColorStop(1, "rgba(212, 255, 58, 0)");
        ctx.fillStyle = halo;
        ctx.fillRect(px - 110, pointer.y - 110, 220, 220);
        ctx.save();
        ctx.beginPath();
        ctx.arc(px, pointer.y, 90, 0, Math.PI * 2);
        ctx.clip();
        brightWires(side, "lime", 0.42);
        ctx.restore();
      }
    }

    if (flash.amount > 0.01) {
      brightWires(side, flash.color, 0.6 * flash.amount);
      ctx.strokeStyle = `rgba(${COLORS[flash.color]}, ${0.5 * flash.amount})`;
      ctx.stroke(side.ports);
    }

    ctx.globalCompositeOperation = "lighter";
    ctx.lineWidth = 1.6;
    for (const p of side.pulses) {
      const len = p.trace.length;
      const head = p.dir > 0 ? p.d : len - p.d;
      const tail = 22 + p.speed * 0.09;
      for (let k = 0; k < 5; k++) {
        const a = head - p.dir * (tail * k) / 5;
        const b = head - p.dir * (tail * (k + 1)) / 5;
        const from = Math.max(0, Math.min(a, b));
        const to = Math.min(len, Math.max(a, b));
        if (to <= from) continue;
        ctx.beginPath();
        traceSegment(ctx, p.trace, from, to);
        ctx.strokeStyle = `rgba(${COLORS[p.color]}, ${0.85 * (1 - k / 5)})`;
        ctx.stroke();
      }
      const [hx, hy] = pointAt(p.trace, head);
      ctx.drawImage(sprite(p.color), hx - 10, hy - 10, 20, 20);
    }

    const breathe = mode === "await" ? 0.22 + 0.2 * Math.sin(now / 380) : 0;
    for (const trace of side.traces) {
      const node = trace.node;
      if (Math.abs(node.y - sy) < 4) node.glow = Math.max(node.glow, 0.5);
      if (breathe && !trace.stub) {
        node.color = "amber";
        node.glow = Math.max(node.glow, breathe);
      }
      if (node.glow < 0.02) continue;
      ctx.globalAlpha = Math.min(1, node.glow);
      const size = trace.stub ? 18 : 28;
      ctx.drawImage(sprite(node.color), node.x - size / 2, node.y - size / 2, size, size);
      ctx.globalAlpha = 1;
      node.glow *= Math.exp(-dt * 2.6);
    }
    ctx.globalCompositeOperation = "source-over";
  }

  function step(side, now, dt) {
    const M = MODES[mode];
    side.spawn += dt * M.rate * Math.max(0.6, side.traces.length / 14);
    while (side.spawn >= 1) {
      side.spawn -= 1;
      spawn(side);
    }
    while (side.queue.length && side.queue[0].at <= now) spawn(side, side.queue.shift());

    side.pulses = side.pulses.filter((p) => {
      p.d += p.speed * dt;
      if (p.d < p.trace.length) return true;
      if (p.dir > 0) Object.assign(p.trace.node, { glow: 1, color: p.color });
      return false;
    });
    draw(side, now, dt);
  }

  function tick(now) {
    frame = requestAnimationFrame(tick);
    const dt = last ? Math.min(0.05, (now - last) / 1000) : 0;
    last = now;
    scan = (scan + dt / MODES[mode].scan) % 1;
    flash.amount *= Math.exp(-dt * 2.4);
    for (const side of sides) if (side.visible) step(side, now, dt);
  }

  function start() {
    if (frame || reduced.matches || document.hidden) return;
    last = 0;
    frame = requestAnimationFrame(tick);
  }

  function stop() {
    cancelAnimationFrame(frame);
    frame = 0;
  }

  // ------------------------------------------------------------ public API

  function setMode(next) {
    if (!MODES[next] || next === mode) return;
    const prev = mode;
    mode = next;
    document.documentElement.dataset.edge = next;
    const label = document.getElementById("edge-mode");
    if (label) label.textContent = MODES[next].label;
    if (reduced.matches) return;
    if (next === "act" && prev !== "act") wave("lime");
    if (next === "await") glowAll("amber", 0.8);
  }

  function burst(kind) {
    if (reduced.matches) return;
    if (kind === "verify") {
      flash = { amount: 0.55, color: "lime" };
      for (const side of sides) for (let i = 0; i < 3; i++) spawn(side, { color: "lime", speed: 460 });
    } else if (kind === "complete") {
      flash = { amount: 0.8, color: "lime" };
      wave("lime", { gap: 30 });
    } else if (kind === "fail") {
      flash = { amount: 1, color: "red" };
      glowAll("red", 1);
    } else if (kind === "undone") {
      flash = { amount: 0.7, color: "cyan" };
      wave("cyan", { reverse: true, bottomUp: true, gap: 30 });
    }
  }

  function setPhase(name) {
    const rail = sides[1].rail;
    if (!rail) return;
    rail.classList.toggle("idle", !name);
    for (const el of rail.querySelectorAll(".ph")) el.classList.toggle("on", el.dataset.ph === name);
  }

  function token() {
    if (reduced.matches) return;
    const now = performance.now();
    if (now < tokenGate) return;
    tokenGate = now + 45;
    const visible = sides.filter((s) => s.visible);
    if (visible.length) spawn(visible[tokenTurn++ % visible.length], { color: "white", speed: 380, reverse: false });
  }

  // ------------------------------------------------------------ wiring

  function tickClock() {
    const el = document.getElementById("edge-clock");
    if (el) el.textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  }

  window.addEventListener("resize", () => {
    cancelAnimationFrame(resizeFrame);
    resizeFrame = requestAnimationFrame(layout);
  });
  window.addEventListener("pointermove", (e) => { pointer = { x: e.clientX, y: e.clientY }; }, { passive: true });
  document.addEventListener("pointerout", (e) => { if (!e.relatedTarget) pointer = null; });
  window.addEventListener("pointerdown", (e) => {
    if (e.target !== document.body && e.target !== document.documentElement) return;
    for (const side of sides) {
      if (!side.visible) continue;
      const px = side.name === "left" ? e.clientX : side.x0 - e.clientX;
      if (px < 0 || px > side.w + 10) continue;
      const near = [...side.traces].sort((a, b) => Math.abs(a.node.y - e.clientY) - Math.abs(b.node.y - e.clientY)).slice(0, 3);
      for (const trace of near) spawn(side, { trace, color: "lime", speed: 520 });
    }
  });
  document.addEventListener("visibilitychange", () => (document.hidden ? stop() : start()));
  reduced.addEventListener("change", () => {
    stop();
    layout();
    start();
  });

  document.documentElement.dataset.edge = mode;
  setPhase(null);
  tickClock();
  setInterval(tickClock, 1000);
  layout();
  start();

  return { mode: setMode, burst, phase: setPhase, token };
})();
