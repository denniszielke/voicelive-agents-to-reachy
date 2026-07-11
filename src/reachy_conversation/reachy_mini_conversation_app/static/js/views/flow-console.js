/**
 * Flow console: a DevUI-inspired three-panel inspector for the talk view.
 *
 *   ┌── Agent flow ──┬── Conversation ──┬── Events ──┐
 *   │  live diagram  │  orb + timeline   │  raw log   │
 *   └────────────────┴───────────────────┴────────────┘
 *
 * It renders on a forced-white surface (independent of the app theme) and is
 * fed by the structured `message` events published on the conversation SSE
 * stream (see console.py -> ConversationEventBus.publish_message):
 *
 *   { type: "transcript",         role, text }
 *   { type: "transcript_partial", role, text }
 *   { type: "turn",               phase: "thinking" | "done" }
 *   { type: "tool_call",   id, name, kind, target, args, status: "started" }
 *   { type: "tool_result", id, name, kind, target, status: "done"|"error", result }
 *   { type: "error",              message }
 *
 * Agent (`kind: "agent"`) tool calls map to the weather / homeassistant nodes;
 * everything else is a local robot tool. When more than one agent runs at once
 * the diagram highlights the parallel fan-out.
 */

import { h } from "../ui.js";

// Node layout. `x`/`y` are the edge-anchor point (%). Edge nodes are pinned to
// the panel margins via `anchor` so wide labels never overlap or clip.
// `hosted` marks the three Azure AI Foundry hosted agents (the orchestrator and
// its two specialists); the robot tools run locally on the device.
const NODES = Object.freeze({
  user: { x: 12, y: 50, anchor: "left", label: "You", sub: "microphone", icon: "🎙️" },
  orchestrator: { x: 39, y: 50, label: "Orchestrator", sub: "router", icon: "🧭", hosted: true },
  weather: { x: 76, y: 18, anchor: "right", label: "Weather", sub: "outdoor °C", icon: "🌤️", hosted: true },
  robot: { x: 76, y: 50, anchor: "right", label: "Robot tools", sub: "on-device", icon: "🤖" },
  homeassistant: { x: 76, y: 82, anchor: "right", label: "Home Assistant", sub: "indoor °C", icon: "🏠", hosted: true },
});

const EDGES = Object.freeze([
  { id: "user__orchestrator", from: "user", to: "orchestrator", kind: "voicelive" },
  { id: "orchestrator__weather", from: "orchestrator", to: "weather" },
  { id: "orchestrator__homeassistant", from: "orchestrator", to: "homeassistant" },
  { id: "orchestrator__robot", from: "orchestrator", to: "robot" },
]);

const SVGNS = "http://www.w3.org/2000/svg";
const ACTIVE_HOLD_MS = 1200; // keep a node/edge lit briefly after completion
const MAX_EVENT_ROWS = 200;
// The orchestrator consults its specialists server-side, so reachy can't tell
// which one ran; while a turn is in flight we light both up together.
const AGENT_NODES = Object.freeze(["weather", "homeassistant"]);

function svg(tag, attrs = {}, ...children) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null) continue;
    el.setAttribute(k, String(v));
  }
  for (const c of children) if (c) el.appendChild(c);
  return el;
}

function nowLabel() {
  return new Date().toLocaleTimeString([], { hour12: false });
}

/** Build the flow console. Returns { root, centerSlot, handleEvent, handleActivity, reset, dispose }. */
export function createFlowConsole() {
  // --- Left: agent flow diagram ------------------------------------------
  const nodeEls = new Map();
  const edgeEls = new Map();

  const svgEl = svg("svg", {
    class: "flow-graph__svg",
    viewBox: "0 0 100 100",
    preserveAspectRatio: "none",
    "aria-hidden": "true",
  });
  for (const edge of EDGES) {
    const a = NODES[edge.from];
    const b = NODES[edge.to];
    const base = svg("line", {
      x1: a.x, y1: a.y, x2: b.x, y2: b.y,
      class: `flow-edge__base${edge.kind === "voicelive" ? " flow-edge__base--voice" : ""}`,
      "vector-effect": "non-scaling-stroke",
    });
    const flow = svg("line", {
      x1: a.x, y1: a.y, x2: b.x, y2: b.y,
      class: `flow-edge__flow${edge.kind === "voicelive" ? " flow-edge__flow--voice" : ""}`,
      "vector-effect": "non-scaling-stroke",
    });
    svgEl.append(base, flow);
    edgeEls.set(edge.id, flow);
  }

  const nodeLayer = h("div", { class: "flow-graph__nodes" });
  for (const [id, n] of Object.entries(NODES)) {
    const style =
      n.anchor === "left"
        ? { left: "4%", top: `${n.y}%` }
        : n.anchor === "right"
        ? { right: "4%", top: `${n.y}%` }
        : { left: `${n.x}%`, top: `${n.y}%` };
    const el = h(
      "div",
      { class: "flow-node", dataset: { node: id, anchor: n.anchor || "center", hosted: n.hosted ? "true" : "false" }, style },
      n.hosted ? h("span", { class: "flow-node__badge" }, "Foundry hosted") : null,
      h("span", { class: "flow-node__icon", "aria-hidden": "true" }, n.icon),
      h(
        "span",
        { class: "flow-node__text" },
        h("span", { class: "flow-node__label" }, n.label),
        h("span", { class: "flow-node__sub" }, n.sub)
      )
    );
    nodeLayer.appendChild(el);
    nodeEls.set(id, el);
  }

  // Label the You -> Orchestrator link as the VoiceLive audio channel.
  const voiceLabel = h("span", { class: "flow-graph__voice" }, "🔊 VoiceLive audio");

  const parallelBadge = h("span", { class: "flow-graph__parallel", hidden: "" }, "parallel fan-out");
  const graph = h(
    "div",
    { class: "flow-graph" },
    svgEl,
    nodeLayer,
    voiceLabel,
    parallelBadge
  );
  const legend = h(
    "div",
    { class: "flow-legend" },
    h("span", { class: "flow-legend__item flow-legend__item--hosted" }, "Azure AI Foundry hosted agent"),
    h("span", { class: "flow-legend__item flow-legend__item--voice" }, "VoiceLive audio stream"),
    h("span", { class: "flow-legend__item flow-legend__item--local" }, "On-device tools")
  );
  const flowPanel = panel("Voice live flow", "flow-panel--graph", graph, legend);

  // --- Center: orb + timeline --------------------------------------------
  const centerSlot = h("div", { class: "flow-center__orb" });
  const timeline = h("div", { class: "flow-timeline", role: "log", "aria-live": "polite" });
  const timelineEmpty = h("p", { class: "flow-empty" }, "The conversation timeline will appear here.");
  timeline.appendChild(timelineEmpty);
  const centerPanel = h(
    "section",
    { class: "flow-panel flow-panel--center" },
    centerSlot,
    h("div", { class: "flow-panel__head" }, h("h2", { class: "flow-panel__title" }, "Conversation")),
    timeline
  );

  // --- Right: raw events --------------------------------------------------
  const eventsList = h("div", { class: "flow-events", role: "log", "aria-live": "off" });
  const eventsEmpty = h("p", { class: "flow-empty" }, "Structured events stream here.");
  eventsList.appendChild(eventsEmpty);
  const eventsPanel = panel("Events", "flow-panel--events", eventsList);

  const root = h("div", { class: "flow-console" }, flowPanel, centerPanel, eventsPanel);

  // --- State --------------------------------------------------------------
  const activeAgents = new Set(); // targets of in-flight agent calls
  const pendingById = new Map(); // call id -> { node, edge }
  const holdTimers = new Map(); // key -> timeout id

  function setActive(kind, key, on) {
    const map = kind === "node" ? nodeEls : edgeEls;
    const el = map.get(key);
    if (!el) return;
    const timerKey = `${kind}:${key}`;
    if (holdTimers.has(timerKey)) {
      clearTimeout(holdTimers.get(timerKey));
      holdTimers.delete(timerKey);
    }
    if (kind === "node") el.classList.toggle("is-active", on);
    else el.classList.toggle("is-flowing", on);
  }

  function holdOff(kind, key, status) {
    const timerKey = `${kind}:${key}`;
    if (holdTimers.has(timerKey)) clearTimeout(holdTimers.get(timerKey));
    const el = (kind === "node" ? nodeEls : edgeEls).get(key);
    if (el && status) {
      el.classList.toggle("is-error", status === "error");
      el.classList.toggle("is-done", status === "done");
    }
    holdTimers.set(
      timerKey,
      setTimeout(() => {
        holdTimers.delete(timerKey);
        if (!el) return;
        el.classList.remove(kind === "node" ? "is-active" : "is-flowing", "is-error", "is-done");
      }, ACTIVE_HOLD_MS)
    );
  }

  function refreshParallel() {
    const parallel = activeAgents.size > 1;
    parallelBadge.hidden = !parallel;
    graph.classList.toggle("is-parallel", parallel);
  }

  function targetNode(kind, target) {
    if (kind === "agent") return NODES[target] ? target : "orchestrator";
    return "robot";
  }
  function edgeFor(node) {
    return `orchestrator__${node}`;
  }

  // --- Timeline helpers ---------------------------------------------------
  let partialEl = null;
  function clearTimelineEmpty() {
    if (timelineEmpty.isConnected) timelineEmpty.remove();
  }
  function addBubble(role, text) {
    clearTimelineEmpty();
    const bubble = h(
      "div",
      { class: `flow-msg flow-msg--${role}` },
      h("span", { class: "flow-msg__role" }, role === "user" ? "You" : "Reachy"),
      h("span", { class: "flow-msg__text" }, text)
    );
    timeline.appendChild(bubble);
    scrollToEnd(timeline);
    return bubble;
  }
  function addSpan(label, kind) {
    clearTimelineEmpty();
    const span = h(
      "div",
      { class: `flow-span flow-span--${kind}`, dataset: { state: "running" } },
      h("span", { class: "flow-span__dot", "aria-hidden": "true" }),
      h("span", { class: "flow-span__label" }, label),
      h("span", { class: "flow-span__state" }, "running…")
    );
    timeline.appendChild(span);
    scrollToEnd(timeline);
    return span;
  }

  // --- Events log ---------------------------------------------------------
  function addEventRow(type, detail, level) {
    if (eventsEmpty.isConnected) eventsEmpty.remove();
    const row = h(
      "div",
      { class: `flow-event flow-event--${level || "info"}` },
      h("span", { class: "flow-event__time" }, nowLabel()),
      h("span", { class: "flow-event__type" }, type),
      h("span", { class: "flow-event__detail" }, detail || "")
    );
    eventsList.appendChild(row);
    while (eventsList.children.length > MAX_EVENT_ROWS) eventsList.firstChild.remove();
    scrollToEnd(eventsList);
  }

  // --- Public: handle a structured message event -------------------------
  const spansById = new Map();
  let turnSpan = null;

  function handleEvent(payload) {
    if (!payload || typeof payload !== "object") return;
    switch (payload.type) {
      case "transcript_partial": {
        if (payload.role !== "user") return;
        pulseNode("user");
        if (!partialEl) {
          clearTimelineEmpty();
          partialEl = addBubble("user", "");
          partialEl.classList.add("is-partial");
        }
        partialEl.querySelector(".flow-msg__text").textContent = payload.text || "";
        scrollToEnd(timeline);
        break;
      }
      case "transcript": {
        if (partialEl && payload.role === "user") {
          partialEl.classList.remove("is-partial");
          partialEl.querySelector(".flow-msg__text").textContent = payload.text || "";
          partialEl = null;
        } else {
          addBubble(payload.role, payload.text || "");
        }
        if (payload.role === "user") pulseNode("user");
        else setActive("node", "orchestrator", false);
        addEventRow(payload.role === "user" ? "user.transcript" : "assistant.transcript", payload.text);
        break;
      }
      case "turn": {
        if (payload.phase === "thinking") {
          setActive("node", "orchestrator", true);
          setActive("edge", "user__orchestrator", true);
          holdOff("edge", "user__orchestrator", "done");
          // Reachy can't see which specialist the orchestrator consults
          // (they run server-side), so show both active while it works.
          for (const t of AGENT_NODES) {
            setActive("node", t, true);
            setActive("edge", edgeFor(t), true);
            activeAgents.add(t);
          }
          refreshParallel();
          if (!turnSpan) turnSpan = addSpan("Consulting Weather + Home Assistant", "agent");
          addEventRow("turn.thinking", "orchestrator consulting specialists");
        } else if (payload.phase === "done") {
          holdOff("node", "orchestrator", "done");
          for (const t of AGENT_NODES) {
            holdOff("node", t, "done");
            holdOff("edge", edgeFor(t), "done");
            activeAgents.delete(t);
          }
          refreshParallel();
          if (turnSpan) {
            turnSpan.dataset.state = "done";
            turnSpan.querySelector(".flow-span__state").textContent = "done";
            turnSpan = null;
          }
          addEventRow("turn.done", "");
        }
        break;
      }
      case "agent_call": {
        // Server-side sub-agent call surfaced for observability only.
        addEventRow("agent.call", `${payload.name} ${payload.args || ""}`);
        break;
      }
      case "tool_call": {
        const node = targetNode(payload.kind, payload.target);
        const edge = edgeFor(node);
        pendingById.set(payload.id, { node, edge, kind: payload.kind, target: payload.target });
        setActive("node", "orchestrator", true);
        setActive("node", node, true);
        setActive("edge", edge, true);
        if (payload.kind === "agent") {
          activeAgents.add(payload.target || node);
          refreshParallel();
        }
        const label =
          payload.kind === "agent"
            ? `${NODES[node]?.label || payload.target} · ${shortArgs(payload.args)}`
            : `${payload.name} · ${shortArgs(payload.args)}`;
        const span = addSpan(label, payload.kind === "agent" ? "agent" : "tool");
        if (payload.id) spansById.set(payload.id, span);
        addEventRow(payload.kind === "agent" ? "agent.call" : "tool.call", `${payload.name} ${payload.args || ""}`);
        break;
      }
      case "tool_result": {
        const info = payload.id ? pendingById.get(payload.id) : null;
        const node = info?.node || targetNode(payload.kind, payload.target);
        const edge = info?.edge || edgeFor(node);
        holdOff("node", node, payload.status);
        holdOff("edge", edge, payload.status);
        holdOff("node", "orchestrator", "done");
        if (payload.kind === "agent") {
          activeAgents.delete(payload.target || node);
          refreshParallel();
        }
        if (payload.id) pendingById.delete(payload.id);
        const span = payload.id ? spansById.get(payload.id) : null;
        if (span) {
          span.dataset.state = payload.status === "error" ? "error" : "done";
          span.querySelector(".flow-span__state").textContent =
            payload.status === "error" ? "failed" : "done";
          spansById.delete(payload.id);
        }
        addEventRow(
          payload.status === "error" ? "tool.error" : "tool.result",
          `${payload.name}: ${summarizeResult(payload.result)}`,
          payload.status === "error" ? "error" : "ok"
        );
        break;
      }
      case "error": {
        addEventRow("error", payload.message, "error");
        break;
      }
      default:
        addEventRow(String(payload.type || "event"), "");
    }
  }

  function pulseNode(id) {
    setActive("node", id, true);
    holdOff("node", id, null);
  }

  // --- Public: activity reasons (coarse orb states) ----------------------
  function handleActivity(reason) {
    if (reason === "user_speech_started") pulseNode("user");
  }

  function reset() {
    timeline.replaceChildren(timelineEmpty);
    eventsList.replaceChildren(eventsEmpty);
    partialEl = null;
    turnSpan = null;
    activeAgents.clear();
    pendingById.clear();
    spansById.clear();
    refreshParallel();
    for (const el of nodeEls.values()) el.classList.remove("is-active", "is-error", "is-done");
    for (const el of edgeEls.values()) el.classList.remove("is-flowing", "is-error", "is-done");
  }

  function dispose() {
    for (const t of holdTimers.values()) clearTimeout(t);
    holdTimers.clear();
  }

  return { root, centerSlot, handleEvent, handleActivity, reset, dispose };
}

// --- helpers --------------------------------------------------------------

function panel(title, extraClass, ...body) {
  return h(
    "section",
    { class: `flow-panel ${extraClass || ""}` },
    h("div", { class: "flow-panel__head" }, h("h2", { class: "flow-panel__title" }, title)),
    ...body
  );
}

function scrollToEnd(el) {
  el.scrollTop = el.scrollHeight;
}

function shortArgs(argsJson) {
  if (!argsJson) return "";
  try {
    const obj = JSON.parse(argsJson);
    const q = obj.question || obj.query || obj.text;
    if (typeof q === "string") return q.length > 48 ? `${q.slice(0, 48)}…` : q;
    const compact = JSON.stringify(obj);
    return compact.length > 48 ? `${compact.slice(0, 48)}…` : compact;
  } catch {
    return argsJson.length > 48 ? `${argsJson.slice(0, 48)}…` : argsJson;
  }
}

function summarizeResult(result) {
  if (result == null) return "";
  let text;
  if (typeof result === "string") text = result;
  else {
    try {
      text = JSON.stringify(result);
    } catch {
      text = String(result);
    }
  }
  return text.length > 80 ? `${text.slice(0, 80)}…` : text;
}
