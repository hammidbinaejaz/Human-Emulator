"use strict";

// Mission deck: the example missions, ranked best-first (#1 = best), with
// category tabs, sorting, search, pins and live stats from your own history.
// Loaded after app.js, so it can use its helpers ($, h, toast, state).
window.Deck = (() => {
  const MISSIONS = [
    { rank: 1, cat: "work", tag: "SETUP", label: "Viva prep kit", risk: "reversible", est: 15,
      why: "Four apps from one sentence — every step verified, all of it undoable.",
      goal: "Set up my viva prep: create a Viva Prep folder on the Desktop with a checklist of 5 things to rehearse, remind me tomorrow at 9am to rehearse, and block 45 minutes on Thursday at 4pm for a mock viva" },
    { rank: 2, cat: "apps", tag: "PAGES", label: "Essay in Pages", risk: "reversible", est: 30,
      why: "Writes on-device, builds a formatted Pages document and exports a PDF.",
      goal: "Write a one-page essay about why on-device AI agents matter in Pages and export it as a PDF" },
    { rank: 3, cat: "apps", tag: "CALCULATOR", label: "128 × 46, live", risk: "read", est: 7,
      why: "Watch it press Calculator's keys, then read the answer back.",
      goal: "Use Calculator to work out 128 × 46" },
    { rank: 4, cat: "messages", tag: "WHATSAPP", label: "Diwali wish", risk: "one-way", est: 12, pick: "Sam",
      why: "Writes the wish, confirms the chat header, sends, then finds the new bubble.",
      goal: "Wish Sam a very happy Diwali on WhatsApp" },
    { rank: 5, cat: "routines", tag: "ROUTINE", label: "Morning briefing", risk: "read", est: 4,
      why: "Approve once — it replays every morning with no model call.",
      goal: "Every day at 9am, tell me what's on my calendar today out loud" },
    { rank: 6, cat: "screen", tag: "ACCESSIBILITY", label: "Describe my screen", risk: "read", est: 18,
      why: "Reads the front window's controls and text aloud — an accessibility co-pilot.",
      goal: "Describe my screen" },
    { rank: 7, cat: "screen", tag: "SCREEN", label: "Screen → note", risk: "reversible", est: 16,
      why: "On-device OCR → summary → a real Apple Notes note.",
      goal: "Read my screen and save the key points as a note" },
    { rank: 8, cat: "apps", tag: "ANY APP", label: "Press a button", risk: "one-way", est: 2,
      why: "Presses a named button in any app through Accessibility.",
      goal: "Press Battery in System Settings" },
    { rank: 9, cat: "apps", tag: "CODE", label: "Project → VS Code", risk: "one-way", est: 5,
      why: "Finds the folder even when misspelt, then checks the window title.",
      goal: "Open my final year project folder in VS Code" },
    { rank: 10, cat: "system", tag: "FOCUS", label: "Deep work mode", risk: "reversible", est: 10,
      why: "Dark mode, volume and a break reminder — undo puts it all back.",
      goal: "Deep work mode: switch to dark mode, set volume to 15, and remind me in 25 minutes to take a break" },
    { rank: 11, cat: "messages", tag: "WHATSAPP", label: "WhatsApp hi", risk: "one-way", est: 8, pick: "Sam",
      why: "The simplest message — the contact must match the chat exactly.",
      goal: "Say hi to Sam on WhatsApp" },
    { rank: 12, cat: "system", tag: "HEALTH", label: "Mac health, aloud", risk: "one-way", est: 30,
      why: "Battery, storage, memory and uptime, spoken aloud.",
      goal: "How is my Mac doing? Tell me out loud" },
    { rank: 13, cat: "safety", tag: "SAFETY", label: "Order pizza", risk: "refused", est: 1,
      why: "Out of scope on purpose — it refuses instead of improvising.",
      goal: "Order a pizza for dinner and email my boss that I'm running late" },
  ];
  const CATS = {
    all: "All", pinned: "★ Pinned", work: "Productivity", apps: "Apps", messages: "Messages",
    screen: "Screen", routines: "Routines", system: "System", safety: "Safety",
  };
  const HUE = {
    work: "#d4ff3a", apps: "#6ee7ff", messages: "#5cf2a6", screen: "#8fb3ff",
    routines: "#c7a6ff", system: "#ffd36e", safety: "#ff8a6b",
  };
  const SORTS = { best: "Best first", used: "Most used", fast: "Fastest", az: "A–Z" };
  const RISK = { read: "Read-only", reversible: "Undoable", "one-way": "One-way", refused: "Refuses" };
  const OK = new Set(["completed", "undone", "partially_undone", "scheduled"]);
  const STORE = "he.deck";
  const reduced = matchMedia("(prefers-reduced-motion: reduce)");

  const grid = $("chips");
  const tabsEl = $("deck-tabs");
  const sortEl = $("deck-sort");
  const search = $("deck-search");
  const summary = $("deck-summary");
  const more = $("deck-more");
  const ink = h("span", { class: "ink", "aria-hidden": "true" });
  const nodes = new Map();

  const saved = (() => {
    try { return JSON.parse(localStorage.getItem(STORE)) || {}; } catch { return {}; }
  })();
  let tab = saved.tab in CATS ? saved.tab : "all";
  let sort = saved.sort in SORTS ? saved.sort : "best";
  let expanded = Boolean(saved.expanded);
  const uses = saved.uses && typeof saved.uses === "object" ? saved.uses : {};
  const pins = new Set(Array.isArray(saved.pins) ? saved.pins : []);
  let typing = 0;

  const save = () => {
    try {
      localStorage.setItem(STORE, JSON.stringify({ tab, sort, expanded, uses, pins: [...pins] }));
    } catch {
      // Private mode — preferences just aren't remembered.
    }
  };
  const escape = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  for (const c of MISSIONS) {
    c.key = c.label;
    c.re = new RegExp(`^${c.pick ? escape(c.goal).replace(escape(c.pick), ".+?") : escape(c.goal)}\\s*$`, "i");
    c.secs = c.est;
  }

  // ------------------------------------------------------------ stats

  const median = (xs) => {
    const s = [...xs].sort((a, b) => a - b);
    return s.length % 2 ? s[(s.length - 1) / 2] : (s[s.length / 2 - 1] + s[s.length / 2]) / 2;
  };
  const fmt = (s) => (s < 0.1 ? "<0.1s" : s >= 90 ? `${Math.round(s / 60)}m` : s >= 10 ? `${Math.round(s)}s` : `${s.toFixed(1)}s`);

  function stats() {
    const out = new Map();
    for (const m of state.missions.values()) {
      const c = MISSIONS.find((x) => x.re.test((m.goal || "").trim()));
      if (!c || ["planning", "running", "undoing", "awaiting_approval"].includes(m.status)) continue;
      const s = out.get(c.key) || { ok: 0, times: [], last: null };
      const t = m.telemetry || {};
      const good = OK.has(m.status) || (c.risk === "refused" && m.status === "rejected");
      if (good) {
        s.ok += 1;
        if (t.plan_ms != null) s.times.push(((t.plan_ms || 0) + (t.run_ms || 0)) / 1000);
      }
      if (!s.last || m.created_at > s.last.created_at) s.last = m;
      out.set(c.key, s);
    }
    return out;
  }

  // Update tiles in place (cheap; safe to call on every history change).
  function paint() {
    const all = stats();
    for (const c of MISSIONS) {
      const el = nodes.get(c.key);
      const s = all.get(c.key);
      c.measured = Boolean(s?.times.length);
      c.secs = c.measured ? median(s.times) : c.est;
      const time = el.querySelector(".c-time");
      time.textContent = `${RISK[c.risk]} · ${c.measured ? fmt(c.secs) : `~${fmt(c.est)}`}`;
      time.classList.toggle("est", !c.measured);
      el.querySelector(".c-ok").textContent = sort === "used" && uses[c.key] ? `×${uses[c.key]}` : s?.ok ? `✓${s.ok}` : "";
      el.classList.toggle("pinned", pins.has(c.key));
      el.querySelector(".pin").textContent = pins.has(c.key) ? "★" : "☆";
      const last = s?.last ? `Last run: ${(MISSION_PILL[s.last.status] || [s.last.status])[0]}, ${ago(s.last.created_at)}` : "Not run yet";
      const timing = c.measured ? `Median ${fmt(c.secs)} across ${s.times.length} verified run(s)` : `About ${fmt(c.est)} (estimate)`;
      el.title = `#${c.rank} · ${c.goal}\n\n${c.why}\n${timing} · ${last}`;
    }
  }

  // ------------------------------------------------------------ build

  function build(c) {
    const el = h("div", {
      class: `chip cat-${c.cat}${c.rank === 1 ? " best" : ""}`,
      role: "button", tabindex: "0", "data-key": c.key, style: `--hue: ${HUE[c.cat]}`,
      onclick: () => load(c),
      onkeydown: (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          // Enter on the card that was just loaded means "go": plan it rather than reload it.
          if (event.key === "Enter" && lastLoaded === c.key) {
            flush();
            $("goal").focus();
            submit(null);
            return;
          }
          load(c);
        }
      },
      onpointermove: (event) => {
        const r = el.getBoundingClientRect();
        el.style.setProperty("--mx", `${event.clientX - r.left}px`);
        el.style.setProperty("--my", `${event.clientY - r.top}px`);
      },
    },
      h("div", { class: "c-top" },
        h("span", { class: `rank${c.rank <= 3 ? ` top r${c.rank}` : ""}` }, `#${c.rank}`),
        h("span", { class: "tag" }, c.tag),
        h("button", {
          class: "pin", type: "button", "aria-label": `Pin ${c.label}`, title: "Pin to the front",
          onclick: (event) => {
            event.stopPropagation();
            togglePin(c);
          },
        }, "☆"),
      ),
      h("div", { class: "c-label" }, c.label),
      h("div", { class: "c-meta" }, h("i", { class: `rdot ${c.risk}` }), h("span", { class: "c-time" }), h("span", { class: "c-ok" })),
    );
    return el;
  }

  // ------------------------------------------------------------ list

  function filtered() {
    const q = search.value.trim().toLowerCase();
    const by = {
      best: (a, b) => a.rank - b.rank,
      used: (a, b) => (uses[b.key] || 0) - (uses[a.key] || 0) || a.rank - b.rank,
      fast: (a, b) => a.secs - b.secs || a.rank - b.rank,
      az: (a, b) => a.label.localeCompare(b.label),
    }[sort];
    return MISSIONS
      .filter((c) => tab === "all" || (tab === "pinned" ? pins.has(c.key) : c.cat === tab))
      .filter((c) => !q || `${c.label} ${c.tag} ${c.goal} ${CATS[c.cat]} ${RISK[c.risk]}`.toLowerCase().includes(q))
      .sort((a, b) => pins.has(b.key) - pins.has(a.key) || by(a, b));
  }

  function perRow() {
    return Math.max(1, getComputedStyle(grid).gridTemplateColumns.split(" ").filter(Boolean).length);
  }

  function shown() {
    const items = filtered();
    const collapsible = tab === "all" && !search.value.trim();
    return { items: collapsible && !expanded ? items.slice(0, perRow()) : items, total: items.length, collapsible };
  }

  function render({ animate = true } = {}) {
    paint();
    const { items, total, collapsible } = shown();
    const before = new Map();
    if (animate && !reduced.matches) {
      for (const el of grid.children) if (el.dataset.key) before.set(el.dataset.key, el.getBoundingClientRect());
    }
    const q = search.value.trim();
    const empty = h("div", { class: "deck-empty" },
      `No example mission matches “${q}”. `,
      h("button", { class: "btn small", type: "button", onclick: () => planAnyway(q) }, `Plan “${q}” anyway →`),
    );
    grid.replaceChildren(...(items.length ? items.map((c) => nodes.get(c.key)) : [empty]));
    items.forEach((c, i) => {
      const el = nodes.get(c.key);
      el.dataset.n = i < 9 ? `⌥${i + 1}` : "";
    });

    if (animate && !reduced.matches) {
      items.forEach((c, i) => {
        const el = nodes.get(c.key);
        const prev = before.get(c.key);
        el.getAnimations().forEach((a) => a.cancel());
        if (prev) {
          const now = el.getBoundingClientRect();
          const dx = prev.left - now.left;
          const dy = prev.top - now.top;
          if (dx || dy) {
            el.animate([{ transform: `translate(${dx}px, ${dy}px)` }, { transform: "none" }],
              { duration: 460, easing: "cubic-bezier(.2,.8,.2,1)" });
          }
        } else {
          el.animate([{ opacity: 0, transform: "translateY(10px) scale(.96)" }, { opacity: 1, transform: "none" }],
            { duration: 380, delay: Math.min(i, 12) * 32, easing: "cubic-bezier(.2,.8,.2,1)", fill: "backwards" });
        }
      });
    }

    more.hidden = !collapsible || total <= perRow();
    more.textContent = expanded ? "Show top picks ▴" : `Show all ${total} ▾`;
    const label = { best: "ranked best first", used: "most used first", fast: "fastest first", az: "A to Z" }[sort];
    summary.textContent = `${items.length} of ${MISSIONS.length} missions · ${label}${pins.size ? ` · ${pins.size} pinned` : ""}`;
    renderTabs();
    renderSort();
  }

  function renderTabs() {
    const counts = { all: MISSIONS.length, pinned: pins.size };
    for (const c of MISSIONS) counts[c.cat] = (counts[c.cat] || 0) + 1;
    const keys = Object.keys(CATS).filter((k) => k === "all" || counts[k]);
    if (!keys.includes(tab)) tab = "all";
    tabsEl.replaceChildren(ink, ...keys.map((k) => h("button", {
      class: `tab${k === tab ? " on" : ""}`, type: "button", role: "tab", "aria-selected": String(k === tab),
      style: k in HUE ? `--hue: ${HUE[k]}` : null,
      onclick: () => {
        tab = k;
        save();
        render();
      },
    }, CATS[k], h("span", { class: "n" }, counts[k]))));
    requestAnimationFrame(moveInk);
  }

  function moveInk() {
    const on = tabsEl.querySelector(".tab.on");
    if (!on) return;
    Object.assign(ink.style, {
      left: `${on.offsetLeft}px`, top: `${on.offsetTop}px`, width: `${on.offsetWidth}px`, height: `${on.offsetHeight}px`,
    });
  }

  function renderSort() {
    sortEl.replaceChildren(...Object.entries(SORTS).map(([k, label]) => h("button", {
      class: k === sort ? "on" : "", type: "button", role: "radio", "aria-checked": String(k === sort),
      onclick: () => {
        sort = k;
        save();
        render();
      },
    }, label)));
  }

  // ------------------------------------------------------------ actions

  function togglePin(c) {
    if (pins.has(c.key)) pins.delete(c.key);
    else pins.add(c.key);
    save();
    render();
    toast(pins.has(c.key) ? `Pinned “${c.label}” to the front.` : `Unpinned “${c.label}”.`);
  }

  function typeInto(text, done) {
    const goal = $("goal");
    const token = ++typing;
    const brief = $("brief");
    brief.classList.remove("primed");
    void brief.offsetWidth;
    brief.classList.add("primed");
    if (reduced.matches) {
      goal.value = text;
      done();
      return;
    }
    const per = Math.max(1, Math.ceil(text.length / 24));
    let i = 0;
    goal.value = "";
    // Finish instantly if Enter/Engage is pressed mid-animation, or if the tab throttles animation frames.
    const finish = () => {
      if (token !== typing || pending === null) return;
      pending = null;
      goal.value = text;
      done();
    };
    pending = finish;
    setTimeout(finish, 650);
    const step = () => {
      if (token !== typing || pending !== finish) return;
      i = Math.min(text.length, i + per);
      goal.value = text.slice(0, i);
      goal.scrollTop = goal.scrollHeight;
      if (i < text.length) requestAnimationFrame(step);
      else finish();
    };
    requestAnimationFrame(step);
  }

  let pending = null;
  let lastLoaded = null;
  function flush() {
    if (pending) pending();
  }

  function load(c) {
    uses[c.key] = (uses[c.key] || 0) + 1;
    save();
    paint();
    const el = nodes.get(c.key);
    el.classList.remove("chosen");
    void el.offsetWidth;
    el.classList.add("chosen");
    lastLoaded = c.key;
    // Focus the brief once the click settles, so a following Enter plans the mission.
    $("goal").focus();
    setTimeout(() => $("goal").focus(), 0);
    typeInto(c.goal, () => {
      const goal = $("goal");
      goal.focus();
      if (c.pick) {
        // Select the example name so typing the real contact replaces it.
        const at = c.goal.indexOf(c.pick);
        goal.setSelectionRange(at, at + c.pick.length);
        toast("Type the contact's name, then press Enter.");
      } else {
        goal.setSelectionRange(goal.value.length, goal.value.length);
        toast(`#${c.rank} ${c.label} loaded — press Enter to plan it.`);
      }
    });
  }

  function planAnyway(text) {
    if (!text) return;
    typeInto(text, () => {
      $("goal").focus();
      toast("Loaded — press Enter to plan it.");
    });
  }

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  async function surprise() {
    const items = shown().items;
    if (!items.length) return;
    const pick = items[Math.floor(Math.random() * items.length)];
    if (!reduced.matches && items.length > 1) {
      const els = items.map((c) => nodes.get(c.key));
      let at = Math.floor(Math.random() * els.length);
      let delay = 45;
      for (let n = 0; n < 13; n++) {
        els.forEach((el) => el.classList.remove("roll"));
        at = (at + 1 + Math.floor(Math.random() * (els.length - 1))) % els.length;
        (n === 12 ? nodes.get(pick.key) : els[at]).classList.add("roll");
        window.Edges?.token();
        await sleep(delay);
        delay *= 1.2;
      }
      els.forEach((el) => el.classList.remove("roll"));
    }
    load(pick);
  }

  // ------------------------------------------------------------ wiring

  for (const c of MISSIONS) nodes.set(c.key, build(c));
  search.addEventListener("input", () => render());
  search.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      const first = shown().items[0];
      if (first) load(first);
      else planAnyway(search.value.trim());
    } else if (event.key === "Escape") {
      search.value = "";
      render();
      search.blur();
    }
  });
  more.addEventListener("click", () => {
    expanded = !expanded;
    save();
    render();
  });
  $("deck-surprise").addEventListener("click", surprise);
  document.addEventListener("keydown", (event) => {
    const typingIn = event.target.closest?.("input, textarea, [contenteditable]");
    if (event.altKey && !event.metaKey && !event.ctrlKey && /^Digit[1-9]$/.test(event.code)) {
      const c = shown().items[Number(event.code.slice(5)) - 1];
      if (c) {
        event.preventDefault();
        load(c);
      }
    } else if (event.key === "/" && !typingIn && !event.metaKey && !event.ctrlKey) {
      event.preventDefault();
      search.focus();
    }
  });
  let resize = 0;
  window.addEventListener("resize", () => {
    cancelAnimationFrame(resize);
    resize = requestAnimationFrame(() => {
      moveInk();
      if (!expanded) render({ animate: false });
    });
  });

  render();

  return { refresh: paint, flush };
})();
