"use strict";

const $ = (id) => document.getElementById(id);

const state = {
  currentId: null,
  missions: new Map(),
  streamMission: null,
  streamStep: null,
  status: null,
  timer: null,
  routines: [],
  voiceMissions: new Set(),
  routineDraft: null,
  deleted: new Set(),
};

const KIND = {
  create_folder: "FILES", write_file: "FILES", open_path: "OPEN", create_note: "NOTES",
  create_reminder: "REMINDERS", create_event: "CALENDAR", read_agenda: "CALENDAR",
  read_screen: "SCREEN", compose: "MODEL", build_webpage: "BUILD", open_app: "APPS",
  set_volume: "SYSTEM", set_dark_mode: "SYSTEM", copy_to_clipboard: "CLIPBOARD",
  speak: "VOICE", mac_health: "SYSTEM", open_in_vscode: "CODE", pages_create: "PAGES",
  pages_read: "PAGES", pages_append: "PAGES", send_whatsapp: "WHATSAPP", reply: "REPLY",
  calculate: "CALCULATOR", press_button: "ANY APP", describe_screen: "ACCESSIBILITY",
};

const MISSION_PILL = {
  planning: ["Planning", "live"], awaiting_approval: ["Awaiting approval", "warn"], running: ["Running", "live"],
  completed: ["Verified", "good"], failed: ["Halted safely", "bad"], cancelled: ["Rejected by you", ""],
  rejected: ["Out of scope", "warn"], aborted: ["Stopped", "warn"], undoing: ["Undoing", "cyan"],
  undone: ["Undone", "cyan"], partially_undone: ["Partly undone", "warn"], interrupted: ["Interrupted", "bad"],
  scheduled: ["Routine saved", "good"],
};

const STEP_STATE = {
  pending: "Queued", running: "Acting", verifying: "Checking", verified: "Verified", failed: "Failed",
  skipped: "Skipped", undoing: "Undoing", undone: "Undone", kept: "Kept", undo_failed: "Undo failed",
  scheduled: "Scheduled",
};

const RISK_LABEL = { read: "Read-only", reversible: "Reversible", "one-way": "One-way" };
const LIVE = new Set(["planning", "running", "undoing"]);
const EDGE_MODE = { planning: "think", awaiting_approval: "await", running: "act", undoing: "undo" };
const EDGE_PHASE = { observe: "observe", decide: "decide", act: "act", verify: "verify", done: "verify", undo: "undo" };

// ---------------------------------------------------------------- helpers

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", "X-Case-Client": "dashboard", ...(options.headers || {}) },
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `Request failed (${response.status})`);
  return body;
}

let toastTimer = null;
function toast(message, bad = false) {
  const el = $("toast");
  el.textContent = message;
  el.className = `toast show${bad ? " bad" : ""}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = "toast"; }, 3600);
}

const clock = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
const seconds = (ms) => `${(ms / 1000).toFixed(1)}s`;

function ago(t) {
  const s = Math.max(0, Date.now() / 1000 - t);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return new Date(t * 1000).toLocaleDateString();
}

// ---------------------------------------------------------------- status

function setTile(id, value, sub, tone = "") {
  const tile = $(id);
  tile.className = `tile ${tone}`;
  tile.querySelector(".value").textContent = value;
  tile.querySelector(".sub").textContent = sub;
}

const PERMISSION_LABEL = { accessibility: "Accessibility", screen: "Screen Recording" };
const PERMISSION_USE = { accessibility: "WhatsApp, VS Code and app control", screen: "reading the screen" };

async function fixPermission(which) {
  try {
    const { host_app: host } = await api(`/api/permissions/${which}/open`, { method: "POST" });
    toast(`System Settings opened — switch on “${host}” under ${PERMISSION_LABEL[which]}.`);
  } catch (error) {
    toast(error.message, true);
  }
}

function renderSetup(status) {
  const granted = status.permissions || {};
  const missing = Object.keys(PERMISSION_LABEL).filter((key) => granted[key] === false);
  const box = $("setup");
  box.hidden = !missing.length;
  if (!missing.length) return;
  box.replaceChildren(
    h("span", { class: "tag" }, "SETUP"),
    h("span", { class: "setup-text" },
      `macOS hasn't allowed ${status.host_app} yet: `,
      missing.map((key) => `${PERMISSION_LABEL[key]} (for ${PERMISSION_USE[key]})`).join(" and "), "."),
    ...missing.map((key) => h("button", { class: "btn small", type: "button", onclick: () => fixPermission(key) }, `Fix ${PERMISSION_LABEL[key]}`)),
  );
}

function renderStatus(status) {
  state.status = status;
  renderSetup(status);
  const model = status.model;
  const offline = status.network === "offline";

  $("signal-network").textContent = offline ? "Offline" : "Online · unused";
  $("signal-dot").className = model.up ? "dot" : "dot bad";

  if (!model.up) {
    setTile("tile-model", "Model offline", "run: ollama serve", "warn");
  } else if (!model.installed) {
    setTile("tile-model", model.model, `not installed — ollama pull ${model.model}`, "warn");
  } else {
    const facts = [model.parameters, model.quantization, model.size_gb && `${model.size_gb} GB`].filter(Boolean).join(" · ");
    setTile("tile-model", model.model, `${facts} · ${model.loaded ? "in memory" : "cold"}`, model.loaded ? "hot" : "");
  }
  setTile(
    "tile-network",
    offline ? "Offline" : "Online",
    offline ? "not needed" : "not used — try Wi-Fi off",
    offline ? "hot" : "",
  );
  setTile("tile-cloud", status.cloud_services.length ? status.cloud_services.join(", ") : "None", model.local ? `model at ${new URL(model.url).host}` : "remote model endpoint", status.cloud_services.length ? "warn" : "hot");

  const engine = $("engine-state");
  let label = "Engine idle";
  let tone = "";
  if (!model.up) { label = "Model offline"; tone = "bad"; }
  else if (status.busy) { label = "Engine running"; }
  else if (!model.loaded && status.warmup.model === "loading") { label = "Loading model"; tone = "warn"; }
  engine.className = `engine-state eyebrow${status.busy ? " live" : ""}`;
  engine.replaceChildren(h("span", { class: `dot ${tone}` }), h("span", {}, label));
  $("ready-label").textContent = status.busy ? "Engine busy" : model.up ? "Ready when you are" : "Start Ollama first";
  $("engage").disabled = status.busy || !model.up;

  const hw = status.hardware;
  $("hardware").textContent = `${hw.chip} · ${hw.memory_gb} GB · macOS ${hw.macos}`;
}

async function refreshStatus() {
  try {
    renderStatus(await api("/api/status"));
  } catch {
    // renderStatus replaces the label element, so rebuild it rather than look it up.
    $("engine-state").className = "engine-state eyebrow";
    $("engine-state").replaceChildren(h("span", { class: "dot bad" }), h("span", {}, "Server unreachable"));
  }
}

function renderSpeed() {
  const latest = [...state.missions.values()]
    .filter((m) => m.telemetry && m.telemetry.tokens_per_s)
    .sort((a, b) => b.created_at - a.created_at)[0];
  if (latest) {
    setTile("tile-speed", `${latest.telemetry.tokens_per_s} tok/s`, `last plan ${seconds(latest.telemetry.plan_ms)} · ${latest.telemetry.plan_tokens} tokens`, "hot");
  } else {
    setTile("tile-speed", "—", "tokens per second");
  }
}

// ---------------------------------------------------------------- mission

function proof(kind, label, text) {
  return h("div", { class: `proof ${kind}` }, h("span", { class: "k" }, label), " ", text);
}

function renderStep(step) {
  const n = step.index + 1;
  const mark = { verified: "✓", failed: "✕", undo_failed: "✕", undone: "↺", kept: "!" }[step.status] || n;
  const body = h("div", {},
    h("div", { class: "step-top" },
      h("span", { class: "kind" }, KIND[step.action] || "ACTION"),
      h("span", { class: `risk ${step.risk}` }, RISK_LABEL[step.risk] || step.risk),
      step.duration_ms != null ? h("span", { class: "card-sub" }, seconds(step.duration_ms)) : null,
    ),
    h("div", { class: "label" }, step.label),
  );

  if (step.action === "write_file" && step.args.content && step.status === "pending") {
    body.append(h("div", { class: "preview" }, step.args.content.slice(0, 400)));
  }
  if (step.action === "send_whatsapp" && step.args.message && ["pending", "skipped"].includes(step.status)) {
    body.append(h("div", { class: "preview whatsapp" },
      h("div", { class: "wa-to" }, `To: ${step.args.contact}`),
      h("div", { class: "wa-bubble" }, step.args.message),
    ));
  }
  if (step.produces_text && step.output && !["read_screen", "reply"].includes(step.action)) {
    body.append(h("div", { class: "preview" }, step.output.slice(0, 600)));
  }
  if (step.evidence && step.status !== "failed") body.append(proof("", "✓ PROOF", step.evidence));
  if (step.error) body.append(proof("err", "✕ ERROR", step.error));
  if (step.undo_evidence) {
    body.append(proof(step.status === "kept" ? "kept" : "undo", step.status === "kept" ? "! KEPT" : "↺ UNDO", step.undo_evidence));
  }
  if (step.artifact) {
    const art = step.artifact;
    if (art.kind === "screenshot") {
      body.append(h("div", { class: "artifact" },
        h("a", { href: art.url, target: "_blank", rel: "noopener" }, h("img", { src: art.url, alt: "Screen captured for on-device OCR" })),
        h("span", { class: "card-sub" }, "captured & read on this Mac"),
      ));
    } else if (art.path) {
      body.append(h("div", { class: "artifact" }, h("span", { class: "path" }, art.path.replace(/^\/Users\/[^/]+/, "~"))));
    }
  }

  return h("li", { class: `step ${step.status}` },
    h("div", { class: "num" }, mark),
    body,
    h("div", { class: "state" }, STEP_STATE[step.status] || step.status),
  );
}

function missionTelemetry(m) {
  const t = m.telemetry || {};
  const bits = [];
  if (t.replay) bits.push("routine replay · no model call");
  else if (t.plan_ms != null) bits.push(`plan ${seconds(t.plan_ms)}`);
  if (t.plan_tokens) bits.push(`${t.plan_tokens} tokens`);
  if (t.tokens_per_s) bits.push(`${t.tokens_per_s} tok/s`);
  if (t.plan_passes > 1) bits.push("self-repaired");
  if (t.run_ms != null) bits.push(`run ${seconds(t.run_ms)}`);
  if (t.verified != null) bits.push(`${t.verified}/${m.steps.length} verified`);
  return bits.join(" · ");
}

async function command(id, verb) {
  try {
    const mission = await api(`/api/missions/${id}/${verb}`, { method: "POST" });
    upsert(mission);
  } catch (error) {
    toast(error.message, true);
  }
}

function renderActions(m) {
  const box = h("div", { class: "actions" });
  if (m.status === "awaiting_approval") {
    box.append(
      h("button", { class: "btn primary", type: "button", onclick: () => command(m.id, "approve") }, m.routine ? "Save routine →" : "Approve & engage →"),
      h("button", { class: "btn danger", type: "button", onclick: () => command(m.id, "cancel") }, "Reject plan"),
    );
  }
  if (m.status === "running") {
    box.append(h("button", { class: "btn danger", type: "button", onclick: () => command(m.id, "abort") }, "Stop after this step"));
  }
  if (m.can_undo) {
    box.append(
      h("button", { class: "btn undo", type: "button", onclick: () => command(m.id, "undo") }, "↺ Undo entire mission"),
      h("span", { class: "hint" }, "reverses every verified change, newest first"),
    );
  }
  if (["completed", "undone"].includes(m.status) && m.steps.length && !m.routine) {
    if (state.routineDraft === m.id) {
      const input = h("input", { class: "routine-input", type: "text", placeholder: "every weekday at 8:30am", "aria-label": "Schedule" });
      const save = async (event) => {
        event.preventDefault();
        try {
          const routine = await api(`/api/missions/${m.id}/routine`, { method: "POST", body: JSON.stringify({ when: input.value }) });
          state.routineDraft = null;
          toast(`Routine saved — ${routine.describe}.`);
          loadRoutines();
          renderMission(m);
        } catch (error) {
          toast(error.message, true);
        }
      };
      box.append(h("form", { class: "routine-form", onsubmit: save },
        input,
        h("button", { class: "btn primary small", type: "submit" }, "Save"),
        h("button", { class: "btn small", type: "button", onclick: () => { state.routineDraft = null; renderMission(m); } }, "Cancel"),
      ));
      setTimeout(() => input.focus(), 0);
    } else {
      box.append(h("button", { class: "btn", type: "button", onclick: () => { state.routineDraft = m.id; renderMission(m); } }, "⏱ Make it a routine"));
    }
  }
  if (deletable(m) && m.status !== "awaiting_approval") {
    box.append(h("button", { class: "btn small ghost delete", type: "button", onclick: () => deleteMission(m.id) }, "Delete mission"));
  }
  return box.childElementCount ? box : null;
}

function renderMission(m) {
  const [pillText, pillTone] = MISSION_PILL[m.status] || [m.status, ""];
  const pill = $("mission-pill");
  pill.textContent = pillText;
  pill.className = `pill ${pillTone}`;
  $("close-mission").hidden = false;

  const body = $("plan-body");
  const parts = [
    h("div", { class: "mission-name" }, m.status === "planning" ? "Planning…" : m.title),
    h("div", { class: "mission-goal" }, `“${m.goal}”`),
    h("div", { class: "card-sub", id: "mission-telemetry" }, missionTelemetry(m)),
  ];

  if (m.status === "planning") {
    parts.push(h("div", { class: "banner go" },
      h("span", { class: "tag" }, "THINKING"),
      h("span", {}, "Understanding your request — rules first, the on-device model only when needed… ", h("b", { id: "plan-timer" }, "")),
    ));
  }
  if (m.routine && !m.routine.run && ["planning", "awaiting_approval"].includes(m.status)) {
    parts.push(h("div", { class: "banner routine" },
      h("span", { class: "tag" }, "ROUTINE"),
      h("span", {}, `${m.routine.describe}. You approve this plan once; it then replays on schedule with every step verified. Nothing runs now.`),
    ));
  }
  if (m.routine?.run) {
    parts.push(h("div", { class: "banner routine" },
      h("span", { class: "tag" }, "ROUTINE RUN"),
      h("span", {}, `${m.routine.describe} — replaying the plan you approved. No model call needed.`),
    ));
  }
  if (m.status === "scheduled") {
    parts.push(h("div", { class: "banner go" },
      h("span", { class: "tag" }, "SAVED"),
      h("span", {}, "This routine is live — see Routines below. You can run it now, pause it or delete it there."),
    ));
  }
  if (m.status === "awaiting_approval" && !m.routine) {
    const changes = m.steps.filter((s) => s.risk !== "read");
    const oneWay = changes.filter((s) => s.risk === "one-way").length;
    const reversible = changes.length - oneWay;
    const detail = oneWay
      ? `${reversible ? `${reversible} reversible, ` : ""}${oneWay} one-way (cannot be undone).`
      : "every change is reversible.";
    parts.push(h("div", { class: "banner go" },
      h("span", { class: "tag" }, "YOUR CALL"),
      h("span", {}, `${changes.length} step(s) will change this Mac — ${detail} Nothing has happened yet.`),
    ));
  }
  if (m.unsupported) {
    parts.push(h("div", { class: "banner warn" }, h("span", { class: "tag" }, "OUT OF SCOPE"), h("span", {}, m.unsupported)));
  }
  for (const warning of m.warnings || []) {
    parts.push(h("div", { class: "banner warn" }, h("span", { class: "tag" }, "VALIDATOR"), h("span", {}, warning)));
  }
  if (m.error && ["failed", "interrupted"].includes(m.status)) {
    parts.push(h("div", { class: "banner bad" }, h("span", { class: "tag" }, "HALTED"), h("span", {}, m.error)));
  }
  if (m.needs?.length && m.status === "failed") {
    parts.push(h("div", { class: "actions" },
      ...m.needs.map((need) => h("button", { class: "btn primary", type: "button", onclick: () => fixPermission(need) },
        `Fix: allow ${PERMISSION_LABEL[need] || need} →`)),
      h("span", { class: "hint" }, "then press Engage again — nothing was changed"),
    ));
  }
  if (m.steps.length) parts.push(h("ol", { class: "steps" }, m.steps.map(renderStep)));
  const actions = renderActions(m);
  if (actions) parts.push(actions);
  body.replaceChildren(...parts);
  tickTimer();

  const result = m.result;
  $("result-card").hidden = !result;
  if (result) {
    const replied = m.steps.length === 1 && m.steps[0].action === "reply";
    const headline = result.text.length <= 48 && !result.text.includes("\n");
    $("result-text").className = headline ? "result big" : "result";
    $("result-text").textContent = result.text;
    $("result-sub").textContent = replied ? "Human Emulator · answered on this Mac" : `from step ${result.step} · generated on this Mac`;
  }

  renderLog(m);
  streamNote(m);
}

// The model stream stays empty when no model was needed; say why instead of leaving a blank box.
function streamNote(m) {
  const stream = $("stream");
  if (state.streamMission === m.id || stream.textContent) return;
  const t = m.telemetry || {};
  let note = null;
  if (t.replay) note = "Routine replay: the plan you approved runs again — no model call needed.";
  else if (t.plan_ms != null && !t.plan_tokens) note = "Planned instantly by Human Emulator's own rules — no model guesswork for names, numbers or times. Any writing step streams its tokens here when it runs.";
  if (note) stream.replaceChildren(h("span", { class: "stream-note" }, note));
}

function renderLog(m) {
  const list = $("log");
  const entries = m.log || [];
  list.replaceChildren(...entries.map((e) =>
    h("li", { class: e.kind },
      h("span", { class: "t" }, clock(e.t)),
      h("span", { class: "k" }, e.kind),
      h("span", { class: "m" }, e.step ? `#${e.step} ` : "", e.message),
    ),
  ));
  list.scrollTop = list.scrollHeight;
  $("log-count").textContent = `${entries.length} events`;
}

function tickTimer() {
  const m = state.missions.get(state.currentId);
  const el = $("plan-timer");
  if (!m || !el) return;
  el.textContent = seconds(Date.now() - m.created_at * 1000);
}

function renderHistory() {
  const missions = [...state.missions.values()].sort((a, b) => b.created_at - a.created_at);
  const list = $("history");
  window.Deck?.refresh();
  $("clear-history").disabled = !missions.some(deletable);
  if (!missions.length) {
    list.replaceChildren(h("li", {}, h("div", { class: "h-meta" }, "No missions yet — your first one will appear here.")));
    return;
  }
  list.replaceChildren(...missions.slice(0, 40).map((m) => {
    const [pillText, pillTone] = MISSION_PILL[m.status] || [m.status, ""];
    const verified = m.steps.filter((s) => s.status === "verified").length;
    const undone = m.steps.filter((s) => s.status === "undone").length;
    const outcome = undone ? `${undone} undone` : `${verified} verified`;
    return h("li", { class: m.id === state.currentId ? "on" : "", onclick: () => select(m.id) },
      h("div", {},
        h("div", { class: "h-title" }, m.title === "New mission" ? m.goal : m.title),
        h("div", { class: "h-meta" }, `${ago(m.created_at)} · ${m.steps.length} steps · ${outcome}`),
      ),
      h("div", { class: "h-side" },
        h("span", { class: `pill ${pillTone}` }, pillText),
        deletable(m)
          ? h("button", {
            class: "icon-btn h-delete", type: "button", title: "Delete this mission", "aria-label": "Delete mission",
            onclick: (event) => { event.stopPropagation(); deleteMission(m.id); },
          }, "✕")
          : null,
      ),
    );
  }));
}

// ---------------------------------------------------------------- deleting

const deletable = (m) => !LIVE.has(m.status);

function renderEmpty() {
  const pill = $("mission-pill");
  pill.textContent = "Idle";
  pill.className = "pill";
  $("close-mission").hidden = true;
  $("plan-body").replaceChildren(h("div", { class: "empty" },
    "Standing by for your next command.",
    h("small", {}, "Pick a mission above or describe your own."),
  ));
  $("result-card").hidden = true;
  $("log").replaceChildren();
  $("log-count").textContent = "0 events";
  $("stream").replaceChildren();
  $("stream-pill").textContent = "Quiet";
  $("stream-pill").className = "pill";
  state.streamMission = null;
}

function closeMission() {
  state.currentId = null;
  renderEmpty();
  renderHistory();
  syncEdges();
}

function removeMissions(ids) {
  for (const id of ids) {
    state.missions.delete(id);
    state.deleted.add(id);
    state.voiceMissions.delete(id);
  }
  if (ids.includes(state.currentId)) closeMission();
  renderHistory();
  renderSpeed();
  syncEdges();
}

async function deleteMission(id) {
  const m = state.missions.get(id);
  if (!m) return;
  const name = m.title === "New mission" ? m.goal : m.title;
  const warning = m.can_undo
    ? `Delete “${name}”?\n\nIts changes can still be undone — deleting it removes that option. (Files already in the undo bin are kept.)`
    : m.status === "awaiting_approval" ? `Delete “${name}”? It has not run, so nothing changes on this Mac.` : null;
  if (warning && !confirm(warning)) return;
  try {
    await api(`/api/missions/${id}/delete`, { method: "POST" });
    removeMissions([id]);
    toast("Mission deleted.");
  } catch (error) {
    toast(error.message, true);
  }
}

async function clearHistory() {
  const missions = [...state.missions.values()].filter(deletable);
  if (!missions.length) return;
  const undoable = missions.filter((m) => m.can_undo).length;
  const message = `Delete ${missions.length} mission${missions.length === 1 ? "" : "s"} from history?`
    + (undoable ? `\n\n${undoable} of them can still be undone — clearing removes that option.` : "")
    + "\n\nRoutines and files in the undo bin are kept.";
  if (!confirm(message)) return;
  try {
    const { deleted } = await api("/api/history/clear", { method: "POST" });
    removeMissions(deleted);
    toast(`Cleared ${deleted.length} mission${deleted.length === 1 ? "" : "s"}.`);
  } catch (error) {
    toast(error.message, true);
  }
}

// ---------------------------------------------------------------- restart

async function restartServer() {
  if (!confirm("Restart Human Emulator?\n\nThe server restarts in place and the dashboard reconnects by itself in a few seconds.")) return;
  let before;
  try {
    ({ started_at: before } = await api("/api/restart", { method: "POST" }));
  } catch (error) {
    toast(error.message, true);
    return;
  }
  $("restart-hud").hidden = false;
  const started = Date.now();
  const poll = async () => {
    try {
      const status = await fetch("/api/status", { cache: "no-store" }).then((r) => r.json());
      if (status.started_at && status.started_at !== before) {
        $("restart-state").textContent = "Back online";
        setTimeout(() => location.reload(), 400);
        return;
      }
    } catch {
      // Server is between processes — keep waiting.
    }
    if (Date.now() - started > 45000) {
      $("restart-state").textContent = "Still restarting…";
      $("restart-text").textContent = "If this lasts, run “hi human emulator” in Terminal.";
    }
    setTimeout(poll, 700);
  };
  setTimeout(poll, 800);
}

// The animated side edges follow the mission that is actually live (or the one
// on screen waiting for approval), and react to transitions between snapshots.
function syncEdges() {
  const edges = window.Edges;
  if (!edges) return;
  const current = state.missions.get(state.currentId);
  const focus = [...state.missions.values()].find((m) => LIVE.has(m.status))
    || (current?.status === "awaiting_approval" ? current : null);
  edges.mode(focus ? EDGE_MODE[focus.status] : "idle");
  const latest = focus && (focus.log || []).findLast((e) => EDGE_PHASE[e.kind]);
  edges.phase(latest ? EDGE_PHASE[latest.kind] : null);
}

function edgeBursts(prev, next) {
  const edges = window.Edges;
  if (!edges || !prev) return;
  next.steps.forEach((step, i) => {
    if (prev.steps[i]?.status === step.status) return;
    if (step.status === "verified") edges.burst("verify");
    else if (step.status === "failed" || step.status === "undo_failed") edges.burst("fail");
  });
  if (prev.status === next.status) return;
  const wordmark = window.Wordmark;
  if (next.status === "completed") {
    edges.burst("complete");
    wordmark?.flash("complete");
  } else if (next.status === "failed" || next.status === "interrupted") {
    edges.burst("fail");
    wordmark?.flash("fail");
  } else if (next.status === "undone" || next.status === "partially_undone") {
    edges.burst("undone");
    wordmark?.flash("undone");
  }
}

function upsert(mission) {
  const known = state.missions.get(mission.id);
  // HTTP responses can arrive after newer live updates; never go backwards.
  if (known && known.updated_at > mission.updated_at) return;
  if (state.deleted.has(mission.id)) return;
  edgeBursts(known, mission);
  voiceReply(known, mission);
  state.missions.set(mission.id, mission);
  if (mission.routine?.run && known && known.status !== mission.status) loadRoutines();
  syncEdges();
  if (mission.id === state.currentId) renderMission(mission);
  renderHistory();
  renderSpeed();
}

function select(id) {
  state.currentId = id;
  const mission = state.missions.get(id);
  if (state.streamMission !== id) {
    $("stream").replaceChildren();
    $("stream-pill").textContent = "Quiet";
    $("stream-pill").className = "pill";
  }
  if (mission) renderMission(mission);
  renderHistory();
  syncEdges();
}

// ---------------------------------------------------------------- routines

function until(ts) {
  const s = ts - Date.now() / 1000;
  if (s < 60) return "in under a minute";
  if (s < 3600) return `in ${Math.round(s / 60)}m`;
  if (s < 86400) return `in ${Math.floor(s / 3600)}h ${Math.round((s % 3600) / 60)}m`;
  return new Date(ts * 1000).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" });
}

async function routineCommand(id, verb) {
  try {
    const result = await api(`/api/routines/${id}/${verb}`, { method: "POST" });
    if (verb === "run") {
      upsert(result);
      select(result.id);
      toast("Routine started — replaying your approved plan.");
    }
    loadRoutines();
  } catch (error) {
    toast(error.message, true);
  }
}

function renderRoutines() {
  const list = $("routines");
  const routines = state.routines;
  $("routine-count").textContent = `${routines.length} routine${routines.length === 1 ? "" : "s"}`;
  $("routine-count").className = `pill${routines.some((r) => r.enabled) ? " live" : ""}`;
  if (!routines.length) {
    list.replaceChildren(h("li", { class: "routine-empty" },
      "No routines yet.",
      h("small", {}, "Say or type “every day at 9am, tell me my agenda out loud” — or press ⏱ on any finished mission."),
    ));
    return;
  }
  list.replaceChildren(...routines.map((r) => {
    const [lastText, lastTone] = r.last_status ? (MISSION_PILL[r.last_status] || [r.last_status, ""]) : ["Not run yet", ""];
    const meta = [
      r.enabled ? `next ${until(r.next_run_at)}` : "paused",
      `${r.runs} run${r.runs === 1 ? "" : "s"}`,
      r.missed ? `${r.missed} missed while asleep` : null,
    ].filter(Boolean).join(" · ");
    return h("li", { class: `routine${r.enabled ? "" : " off"}` },
      h("div", { class: "r-main" },
        h("div", { class: "r-when" }, h("span", { class: "r-dot" }), r.describe),
        h("div", { class: "r-title" }, r.name),
        h("div", { class: "r-steps" }, r.step_labels.join("  →  ")),
        h("div", { class: "r-meta" }, meta),
        r.last_error ? h("div", { class: "r-error" }, r.last_error) : null,
      ),
      h("div", { class: "r-side" },
        h("span", { class: `pill ${lastTone}`, title: "last run" }, lastText),
        h("div", { class: "r-actions" },
          h("button", { class: "btn small", type: "button", onclick: () => routineCommand(r.id, "run"), title: "Run now" }, "▶ Run"),
          h("button", { class: "btn small", type: "button", onclick: () => routineCommand(r.id, "toggle") }, r.enabled ? "Pause" : "Resume"),
          h("button", { class: "btn small danger", type: "button", title: "Delete routine", onclick: () => {
            if (confirm(`Delete the routine “${r.name}”?`)) routineCommand(r.id, "delete");
          } }, "✕"),
        ),
      ),
    );
  }));
}

async function loadRoutines() {
  try {
    state.routines = (await api("/api/routines")).routines;
    renderRoutines();
  } catch (error) {
    toast(error.message, true);
  }
}

// ---------------------------------------------------------------- voice

// Approval must be an unambiguous yes from start to finish; a negation or a
// hesitation anywhere ("okay, cancel that", "go back", "sure, wait") never approves.
const YES = /^(?:yes|yeah|yep|yup|sure|ok|okay|go|go ahead|do it|approve|approved|engage|confirm|proceed|run it|send it|save it|affirmative|please do|absolutely)(?: (?:and )?(?:yes|please|now|thanks|thank you|go ahead|do it|send it|save it|go for it))*$/;
const NEGATION = /\b(no|nope|not|don'?t|do not|cancel|stop|wait|hold|never ?mind|abort|back|reject|forget|later|hang on)\b/;
const NO = /\b(no|nope|cancel|reject|don'?t|do not|never ?mind|abort|forget it)\b/;
const STOP = /^(stop|halt|abort|cancel)\b/;
const UNDO = /^(undo|revert|roll ?back|put it back|take it back)\b/;

async function speak(text, wait = false) {
  try {
    await api("/api/voice/say", { method: "POST", body: JSON.stringify({ text, wait }) });
  } catch {
    // Spoken replies are a nicety; the dashboard already shows everything.
  }
}

function spokenLabel(step) {
  return step.label.replace(/[“”"]/g, "").replace(/ · /g, ", ").replace(/~\//g, "");
}

function planSpeech(m) {
  const n = m.steps.length;
  const labels = m.steps.slice(0, 4).map(spokenLabel).join(". ");
  if (m.routine) return `Routine ready: ${m.routine.describe}. ${labels}. Say yes to save it.`;
  const oneWay = m.steps.filter((s) => s.risk === "one-way").length;
  const safety = oneWay ? `${oneWay === n ? "This" : `${oneWay} of these`} can't be undone.` : "All of it can be undone.";
  return `Plan ready. ${n} step${n === 1 ? "" : "s"}. ${labels}. ${safety} Shall I go ahead?`;
}

// Missions started by voice get spoken updates, like a co-pilot.
async function voiceReply(prev, next) {
  if (!state.voiceMissions.has(next.id) || !prev || prev.status === next.status) return;
  const verified = next.steps.filter((s) => s.status === "verified").length;
  if (next.status === "awaiting_approval") {
    await speak(planSpeech(next), true);
    if (state.currentId === next.id && state.missions.get(next.id)?.status === "awaiting_approval") {
      window.Voice.start({ followUp: true });
    }
  } else if (next.status === "completed") {
    if (next.steps.length === 1 && next.steps[0].action === "reply") return speak(next.result?.text || "");
    if (next.steps.some((s) => s.action === "describe_screen")) return;
    const spoke = next.steps.some((s) => s.action === "speak");
    const result = next.result?.text && !spoke ? ` ${next.result.text.replace(/^[-•*]\s*/gm, "").slice(0, 280)}` : "";
    speak(`Done. ${verified} of ${next.steps.length} steps verified.${result}`);
  } else if (next.status === "failed" || next.status === "interrupted") {
    speak(`I stopped safely. ${(next.error || "A step could not be verified.").slice(0, 160)}`);
  } else if (next.status === "rejected") {
    speak(next.unsupported || "That's outside what I'm allowed to do.");
  } else if (next.status === "cancelled") {
    speak("Cancelled. Nothing changed.");
  } else if (next.status === "undone") {
    speak("Undone. Everything is back the way it was.");
  } else if (next.status === "scheduled") {
    speak(`Routine saved. ${next.routine?.describe || ""}.`);
  } else if (next.status === "aborted") {
    speak("Stopped. Finished steps can still be undone.");
  }
}

function routeVoice(raw) {
  const said = raw.toLowerCase().replace(/[.!?,]/g, " ").replace(/\s+/g, " ").trim()
    .replace(/^(hey |ok |okay )?(human emulator|emulator|jarvis)\s*/, "");
  const goal = raw.replace(/^(hey |ok |okay )?(human emulator|emulator|jarvis)[,.!]?\s*/i, "").trim();
  const m = state.missions.get(state.currentId);
  const short = said.split(" ").length <= 5;
  if (m?.status === "awaiting_approval" && short && NEGATION.test(said)) {
    state.voiceMissions.add(m.id);
    if (NO.test(said)) return command(m.id, "cancel");
    return speak("Okay, I'll wait. Say yes or press approve when you're ready.");
  }
  if (m?.status === "awaiting_approval" && YES.test(said)) {
    state.voiceMissions.add(m.id);
    speak(m.routine ? "Saving the routine." : "Engaging.");
    return command(m.id, "approve");
  }
  if (m?.status === "running" && short && STOP.test(said)) {
    state.voiceMissions.add(m.id);
    return command(m.id, "abort");
  }
  if (short && UNDO.test(said)) {
    if (m?.can_undo) {
      state.voiceMissions.add(m.id);
      speak("Undoing.");
      return command(m.id, "undo");
    }
    return speak("There's nothing to undo.");
  }
  if (YES.test(said) || /^(no|nope|cancel|never ?mind|forget it|no thanks)$/.test(said)) return speak("There's nothing waiting for an answer.");
  if (!goal) return null;
  $("goal").value = goal;
  return submit(null, { voice: true });
}

function appendToken(event) {
  if (event.mission_id !== state.currentId) return;
  const stream = $("stream");
  if (state.streamMission !== event.mission_id) {
    stream.replaceChildren();
    state.streamMission = event.mission_id;
    state.streamStep = null;
  }
  const marker = event.phase === "step" ? event.step : "plan";
  if (state.streamStep !== marker) {
    state.streamStep = marker;
    const heading = marker === "plan" ? "── planning ──\n" : `\n\n── step ${marker} ──\n`;
    stream.append(h("span", { class: "mono", style: "color:#5f6856" }, heading));
  }
  stream.querySelector(".cursor")?.remove();
  stream.append(document.createTextNode(event.text), h("span", { class: "cursor" }));
  if (stream.textContent.length > 12000) {
    stream.replaceChildren(document.createTextNode(stream.textContent.slice(-8000)), h("span", { class: "cursor" }));
  }
  stream.scrollTop = stream.scrollHeight;
  const pill = $("stream-pill");
  pill.textContent = "Generating";
  pill.className = "pill live";
  clearTimeout(appendToken.quiet);
  appendToken.quiet = setTimeout(() => {
    pill.textContent = "Quiet";
    pill.className = "pill";
    stream.querySelector(".cursor")?.remove();
  }, 1500);
}

function connect() {
  const source = new EventSource("/api/stream");
  source.onopen = () => loadMissions();
  source.onmessage = (message) => {
    const event = JSON.parse(message.data);
    if (event.type === "mission") upsert(event.mission);
    else if (event.type === "token") {
      window.Edges?.token();
      appendToken(event);
    }
    else if (event.type === "engine") refreshStatus();
    else if (event.type === "routines") loadRoutines();
    else if (event.type === "deleted") removeMissions(event.ids || []);
  };
}

// ---------------------------------------------------------------- boot

async function loadMissions() {
  try {
    const { missions } = await api("/api/missions");
    for (const m of missions) {
      const known = state.missions.get(m.id);
      if (!known || known.updated_at <= m.updated_at) {
        edgeBursts(known, m);
        state.missions.set(m.id, m);
      }
    }
    if (!state.currentId && missions.length) state.currentId = missions[0].id;
    syncEdges();
    if (state.currentId && state.missions.has(state.currentId)) renderMission(state.missions.get(state.currentId));
    renderHistory();
    renderSpeed();
  } catch (error) {
    toast(error.message, true);
  }
}

async function loadSkills() {
  const { skills } = await api("/api/skills");
  $("skill-count").textContent = `${skills.length} actions`;
  $("skills").replaceChildren(...skills.map((s) =>
    h("div", { class: "skill" },
      h("div", { class: "s-name" }, s.name),
      h("div", { class: "s-sum" }, s.summary),
      h("span", { class: `risk ${s.risk}` }, RISK_LABEL[s.risk]),
    ),
  ));
}

async function submit(event, { voice = false } = {}) {
  event?.preventDefault();
  window.Deck?.flush?.();
  const goal = $("goal").value.trim();
  if (!goal) {
    toast("Describe an outcome first.");
    $("goal").focus();
    return;
  }
  $("engage").disabled = true;
  try {
    const mission = await api("/api/missions", { method: "POST", body: JSON.stringify({ goal }) });
    if (voice) {
      state.voiceMissions.add(mission.id);
      speak(mission.routine ? `Planning a routine: ${mission.routine.describe}.` : "On it.");
    }
    upsert(mission);
    state.streamMission = null;
    select(mission.id);
    $("stream").replaceChildren();
    refreshStatus();
  } catch (error) {
    toast(error.message, true);
    if (voice) speak(error.message);
    $("engage").disabled = false;
  }
}

function boot() {
  $("brief").addEventListener("submit", (event) => submit(event));
  $("goal").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) submit(event);
  });
  $("copy-result").addEventListener("click", async () => {
    await navigator.clipboard.writeText($("result-text").textContent);
    toast("Result copied.");
  });
  window.Voice.on("onText", (text) => routeVoice(text));
  window.Voice.on("onError", (message) => toast(message, true));
  $("clear-history").addEventListener("click", clearHistory);
  $("close-mission").addEventListener("click", closeMission);
  $("restart").addEventListener("click", restartServer);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !window.Voice.listening) api("/api/voice/stop", { method: "POST" }).catch(() => {});
  });

  refreshStatus();
  loadSkills().catch((error) => toast(error.message, true));
  loadRoutines();
  connect();
  setInterval(refreshStatus, 4000);
  setInterval(() => {
    tickTimer();
    renderHistory.lastTick = (renderHistory.lastTick || 0) + 1;
    if (renderHistory.lastTick % 30 === 0) renderHistory();
    if (renderHistory.lastTick % 150 === 0) renderRoutines();
  }, 200);
}

boot();
