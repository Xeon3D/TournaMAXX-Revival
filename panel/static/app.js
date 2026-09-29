"use strict";
// TournaMAXX-Revival control panel.  Everything a cabinet sent (handles,
// cities, file names) is put on the page as text, never as HTML.

// ------------------------------------------------------------------ helpers

const $ = (s, r = document) => r.querySelector(s);

function el(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "value") e.value = v;
    else if (k === "checked") e.checked = !!v;
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat(Infinity)) {
    if (k === null || k === undefined || k === false) continue;
    e.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
  return e;
}

async function api(path, opts = {}) {
  const init = { method: opts.method || (opts.body !== undefined || opts.raw ? "POST" : "GET"), headers: {} };
  if (init.method === "POST") init.headers["X-TMX"] = "1";
  if (opts.raw) { init.body = opts.raw; init.headers["Content-Type"] = "application/octet-stream"; }
  else if (opts.body !== undefined) { init.body = JSON.stringify(opts.body); init.headers["Content-Type"] = "application/json"; }
  const r = await fetch(path, init);
  if (r.status === 401 && path !== "/api/login") { showLogin(); throw new Error("not logged in"); }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}

// replaceChildren, skipping empty parts (null, false, nested arrays).
const fill = (node, ...kids) => node.replaceChildren(...kids.flat(Infinity).filter((k) => k !== null && k !== undefined && k !== false));

const op = (o) => api("/api/op", { body: o });

let toastTimer;
function toast(msg, bad) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (bad ? " bad" : "");
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), bad ? 7000 : 3000);
}

async function guard(fn, okMsg) {
  try {
    const r = await fn();
    if (okMsg) toast(okMsg);
    return r;
  } catch (e) {
    toast(e.message, true);
    throw e;
  }
}

const pad = (n) => String(n).padStart(2, "0");
// Seconds as m:ss.
const mss = (t) => (t == null ? "—" : `${Math.floor(t / 60)}:${String(t % 60).padStart(2, "0")}`);

function fmtTime(t) {
  if (!t) return "—";
  const d = new Date(t * 1000);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
function ago(t) {
  if (!t) return "never";
  const s = Math.round(Date.now() / 1000 - t);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}
function dur(s) {
  if (s < 3600) return `${Math.floor(s / 60)} min`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`;
  return `${Math.floor(s / 86400)} d ${Math.floor((s % 86400) / 3600)} h`;
}
function toLocalInput(t) {
  const d = new Date(t * 1000);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
const fromLocalInput = (s) => Math.floor(new Date(s).getTime() / 1000);
function bytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1048576).toFixed(1)} MB`;
}

// The game numbers the docs name; the rest show as numbers.
// The game numbers: the order of MERIT2\DATA\GAMEDATA.DAT (Emerald 2 V9.01),
// and where the older releases have another game at a number, by protocol.
const GAMES = ["SOLITAIRE", "RUN21", "ROYAL FLASH", "TRIVIA", "MATCH 'EM UP", "MEMOREE", "TRI-TOWERS",
  "FOURPLAY", "CONQUEST", "STRIPCLUB", "ELEVEN UP", "MYST. PHRAZE", "HOOP JONES", "ZIP21", "CHECKERZ",
  "QUIK MATCH", "PWR SOLITAIRE", "PIX MIX", "PHOTOHUNT", "QUIK CELL", "TAI-PLAY", "GOLF", "TENNIS",
  "PUCK SHOT", "PILE ON", "TAKE 2", "DBL SOLITAIRE", "LINK TRIVIA", "MERRY MAIDENS", "ELEVEN BALL",
  "CHUG21", "FUNKY MONKEY", "HOOTER", "POWER TRIVIA", "HEAD-TO-HEAD PHUNT", "TRIP-FLIP",
  "HEAD-TO-HEAD TRIV", "3 BLIND MICE", "ROUTE 66", "SUPER RTE 66", "FAST LANE", "SNAPSHOT", "LOOK OUT",
  "MONSTER MAD", "GOOOAL", "AIR SHOT", "PHARAOH'S NINE", "PILE HIGH", "WILD 8's", "QB ZONE",
  "WILD APE's", "QUINTZEE", "JUMBLE CROSSWORD", "JUMBLE", "ASTRO JOE", "JUMBLE SAFARI", "OUTER SPADES",
  "CRAZY HEARTS", "QUIZ SHOW", "BOXXI", "FOXY BOXXI", "MOONDROP", "EUCHRE NIGHTS", "BATTLE 31",
  "BOX GLIDE", "BACK JAMMIN", "QUIK CHESS", "GENDER BENDER", "BOWLING", "QUIZ HOT TOPICS", "CHIPAWAY",
  "SPEED DRAW", "FLASH 10", "EROTIC MATCH'EM UP", "EROTIC MEMOREE", "EROTIC MYST. PHRAZE",
  "EROTIC PIX MIX", "EROTIC PHOTOHUNT", "EROTIC LOOK OUT", "EROTIC TRIVIA", "EROTIC POWER TRIVIA",
  "SUPER SNUBBEL", "EROTIC H-H PHUNT", "MYST HOT TOPICS"];
const GAMES_OLDER = {
  7: { 52: "BASEBALL", 53: "DRIVE", 69: "SKAT", 81: "SNUBBEL", 83: "HEAD-HEAD SNAPSHOT" },
  6: { 34: "HC ELEVEN UP", 36: "HC TRI-TOWERS", 52: "BASEBALL", 53: "DRIVE", 63: "THIRTY-ONE",
    64: "GEM SWIPE", 66: "SPEED CHESS" },
  3: { 34: "B-BRICKS", 36: "JOEPARDY", 52: "BASEBALL", 53: "DRIVE" },
};
// A game's name on a cabinet with that login protocol (none: Emerald 2's).
const gameName = (g, protocol) => {
  const n = GAMES_OLDER[protocol]?.[g] ?? GAMES[g];
  return n ? `${n} (#${g})` : `Game #${g}`;
};

const STATUS = { 1: ["announced", "warn"], 2: ["running", "ok"], 4: ["final", "plain"], 5: ["removed", "plain"] };
function tStatus(t) {
  const now = Date.now() / 1000;
  if (now < t.start) return 1;
  if (now < t.end) return 2;
  if (now < t.end + (t.final_days ?? 7) * 86400) return 4;
  return 5;
}
const pill = (text, kind) => el("span", { class: `pill ${kind || ""}` }, text);

function table(headers, rows, opts = {}) {
  if (!rows.length) return el("div", { class: "empty" }, opts.empty || "Nothing here yet.");
  return el("div", { class: "table-wrap" },
    el("table", {},
      el("thead", {}, el("tr", {}, headers.map((h) => el("th", { class: h.num ? "num" : null }, h.label ?? h)))),
      el("tbody", {}, rows)));
}

function field(label, input, cls) {
  return el("label", { class: cls || null }, label, input);
}
const input = (name, value, attrs = {}) => el("input", { name, value: value ?? "", ...attrs });

function formValues(form) {
  const out = {};
  for (const e of form.elements) {
    if (!e.name) continue;
    out[e.name] = e.type === "checkbox" ? e.checked : e.value;
  }
  return out;
}

// A dialog: build(form) fills it and returns a submit function; resolves
// with the submit's result, or null when cancelled.
function dialog(title, build, okText = "Save") {
  const d = $("#dialog");
  const f = $("#dialog-form");
  f.replaceChildren();
  f.append(el("h2", {}, title));
  const body = el("div", { class: "stack" });
  f.append(body);
  const err = el("p", { class: "error" });
  const submit = build(body);
  const ok = el("button", { class: "primary", type: "submit", value: "ok" }, okText);
  f.append(err, el("div", { class: "foot" }, el("button", { type: "button", onclick: () => d.close() }, "Cancel"), ok));
  return new Promise((resolve) => {
    f.onsubmit = async (ev) => {
      ev.preventDefault();
      ok.disabled = true;
      err.textContent = "";
      try {
        const r = await submit();
        d.close();
        resolve(r ?? true);
      } catch (e) {
        err.textContent = e.message;
      } finally {
        ok.disabled = false;
      }
    };
    d.onclose = () => resolve(null);
    d.showModal();
  });
}

function confirmBox(title, text, okText = "Confirm") {
  return dialog(title, (b) => { b.append(el("p", {}, text)); return () => true; }, okText);
}

// ------------------------------------------------------------------ app shell

let ME = null;
let STATE = null;
let timer = null;

async function showLogin() {
  ME = null;
  $("#app").hidden = true;
  $("#login").hidden = false;
  clearInterval(timer);
  // A fresh install: make the first user instead.
  const st = await fetch("/api/setup").then((r) => r.json()).catch(() => ({}));
  $("#login-form").hidden = !!st.needed;
  $("#setup-form").hidden = !st.needed;
  if (st.needed && !st.open) {
    $("#setup-note").textContent = `First-run setup is closed: it stays open ${st.minutes} minutes after the panel starts. ` +
      "Restart the panel (or its container) and make the first user then.";
    $("#setup-form").querySelector("button").disabled = true;
  }
}

$("#setup-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const v = formValues(ev.target);
  $("#setup-error").textContent = "";
  if (v.password !== v.again) return ($("#setup-error").textContent = "The two passwords differ.");
  try {
    const r = await api("/api/setup", { body: { user: v.user, password: v.password } });
    ME = r.user;
    ev.target.reset();
    start();
  } catch (e) {
    $("#setup-error").textContent = e.message;
  }
});

$("#login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const v = formValues(ev.target);
  $("#login-error").textContent = "";
  try {
    const r = await api("/api/login", { body: v });
    ME = r.user;
    ev.target.reset();
    start();
  } catch (e) {
    $("#login-error").textContent = e.message;
  }
});

$("#logout").addEventListener("click", async () => {
  await api("/api/logout", { body: {} }).catch(() => {});
  showLogin();
});

async function refreshPill() {
  try {
    const o = await api("/api/overview");
    const s = o.service;
    const p = $("#svc-pill");
    p.textContent = s.active ? "Server running" : "Server stopped";
    p.className = "pill " + (s.active ? "ok" : "bad");
    $("#host").textContent = o.host;
    return o;
  } catch { return null; }
}

function start() {
  $("#login").hidden = true;
  $("#app").hidden = false;
  $("#who").textContent = `(${ME})`;
  refreshPill();
  route();
}

window.addEventListener("hashchange", route);

const PAGES = {};
async function route() {
  if (!ME) return;
  clearInterval(timer);
  const [page, arg] = (location.hash.slice(1) || "dashboard").split("/");
  for (const a of document.querySelectorAll("#nav a")) a.classList.toggle("on", a.getAttribute("href") === `#${page}`);
  const main = $("#main");
  const fn = PAGES[page] || PAGES.dashboard;
  try {
    await fn(main, arg ? decodeURIComponent(arg) : null);
  } catch (e) {
    if (e.message !== "not logged in") fill(main, el("div", { class: "note warn" }, e.message));
  }
}

async function loadState() {
  STATE = await api("/api/state");
  return STATE;
}

function header(title, sub, ...actions) {
  return el("div", { class: "head" }, el("div", {}, el("h1", {}, title), sub ? el("p", {}, sub) : null),
    el("div", { class: "row" }, actions));
}

// ------------------------------------------------------------------ dashboard

PAGES.dashboard = async (main) => {
  const o = await api("/api/overview");
  const s = o.service;
  const svcBtn = (action, label, cls) => el("button", {
    class: cls, onclick: async (ev) => {
      if (action !== "start" && !(await confirmBox(`${label} the server?`,
        "A cabinet on a call now would be cut off; it rolls the call back and sends it again next time.", label))) return;
      ev.target.disabled = true;
      await guard(() => api("/api/service", { body: { action } }), `Server: ${action} done`).catch(() => {});
      refreshPill();
      route();
    },
  }, label);

  const ports = [
    el("dt", {}, "Modem calls"), el("dd", {}, el("code", {}, `TCP ${o.server.port}`), el("small", {}, " — what the emulators dial")),
    el("dt", {}, "Direct TournaMAXX"), el("dd", {}, o.server.tcp_ports?.length
      ? el("code", {}, o.server.tcp_ports.map((p) => `TCP ${p}`).join(", ")) : el("span", { class: "muted" }, "off")),
    el("dt", {}, "Admin (local)"), el("dd", {}, el("code", {}, `127.0.0.1:${o.server.admin_port}`)),
  ];

  const c = o.counts;
  const tiles = [["Cabinets", c.cabinets], ["Running tournaments", c.running], ["Tournaments", c.tournaments],
    ["Players", c.players], ["Score uploads", c.scores], ["Outbox items waiting", c.outbox]]
    .map(([l, n]) => el("div", { class: "card tile" }, el("div", { class: "n" }, n), el("div", { class: "l" }, l)));

  const cabRows = o.cabinets.map((cb) => el("tr", { class: "click", onclick: () => (location.hash = `cabinets/${encodeURIComponent(cb.serial)}`) },
    el("td", {}, el("code", {}, cb.serial)), el("td", {}, cb.name || el("span", { class: "muted" }, "—")),
    el("td", {}, cb.release || "—", cb.version ? ` · V${cb.version}` : ""),
    el("td", {}, ago(cb.last)), el("td", { class: "num" }, cb.outbox || "")));

  const callRows = o.calls.map((k) => el("tr", {},
    el("td", {}, k.start.slice(0, 8)), el("td", {}, el("code", {}, k.from)),
    el("td", {}, k.port === 17751 ? "update" : k.port === 15000 ? "initial connection" : k.direct ? "direct TCP" : "—"),
    el("td", {}, k.completed ? pill("complete", "ok") : k.end ? pill("broke off", "warn") : pill("on line", "warn"))));

  fill(main, 
    header("Dashboard", `${o.host} · log ${bytes(o.log_size)}`),
    o.error ? el("div", { class: "note warn" }, o.error) : null,
    el("div", { class: "grid cols-2" },
      el("div", { class: "card stack" },
        el("div", { class: "row spread" }, el("h2", {}, "Server"),
          pill(s.active ? "running" : "stopped", s.active ? "ok" : "bad")),
        el("dl", { class: "kv" },
          el("dt", {}, "State"), el("dd", {}, s.state),
          el("dt", {}, "Up for"), el("dd", {}, s.since ? dur(o.now - s.since) : "—"),
          el("dt", {}, "Process"), el("dd", {}, s.pid ?? "—"),
          el("dt", {}, "Runs as"), el("dd", {}, s.mode === "systemd" ? `systemd unit ${s.unit}` : "a child of the panel")),
        el("div", { class: "row" }, s.active ? [svcBtn("restart", "Restart", "primary"), svcBtn("stop", "Stop", "danger")]
          : svcBtn("start", "Start", "primary"))),
      el("div", { class: "card stack" }, el("h2", {}, "Listening"), el("dl", { class: "kv" }, ports),
        el("p", { class: "muted" }, "Change these under ", el("a", { href: "#settings" }, "Settings"), "."))),
    el("div", { class: "tiles" }, tiles),
    el("div", { class: "grid cols-2" },
      el("div", { class: "card" }, el("h2", {}, "Cabinets"),
        table(["Serial", "Location", "Release", "Last call", { label: "Outbox", num: true }], cabRows,
          { empty: "No cabinet has called yet." })),
      el("div", { class: "card" }, el("div", { class: "row spread" }, el("h2", {}, "Recent calls"), el("a", { href: "#log" }, "Log")),
        table(["Time", "From", "Kind", "Outcome"], callRows, { empty: "No calls in the log yet." }))),
  );
  timer = setInterval(() => { if (!$("#dialog").open) route(); }, 15000);
};

// ------------------------------------------------------------------ tournaments

function standings(state, tid) {
  const all = {};
  for (const s of state.scores || []) {
    if (s.tournament !== tid) continue;
    (all[s.player] ||= []).push(...s.scores.filter((x) => x));
  }
  return Object.entries(all).map(([pid, sc]) => {
    const best = sc.sort((a, b) => b - a).concat([0, 0, 0, 0, 0]).slice(0, 5);
    const total = best.reduce((a, b) => a + b, 0);
    return { pid, best, total, p: state.players?.[pid] || {} };
  }).sort((a, b) => b.total - a.total);
}

PAGES.tournaments = async (main) => {
  const st = await loadState();
  const tours = st.tournaments || [];
  const rows = [...tours].sort((a, b) => b.start - a.start).map((t) => {
    const [sl, sk] = STATUS[tStatus(t)];
    const n = standings(st, t.id).length;
    return el("tr", {},
      el("td", { class: "num" }, t.id), el("td", {}, el("b", {}, t.name), el("br"), el("small", {}, t.desc)),
      el("td", {}, gameName(t.game)), el("td", {}, pill(sl, sk)),
      el("td", {}, fmtTime(t.start), el("br"), el("small", {}, "to ", fmtTime(t.end))),
      el("td", { class: "num" }, t.credits), el("td", { class: "num" }, n),
      el("td", {}, el("div", { class: "row" },
        el("button", { class: "small", onclick: () => editTournament(t) }, "Edit"),
        el("button", { class: "small", onclick: () => showStandings(st, t) }, "Standings"),
        tStatus(t) <= 2 ? el("button", { class: "small", onclick: () => endTournament(t) }, "End now") : null,
        el("button", { class: "small danger", onclick: () => deleteTournament(t) }, "Delete"))));
  });
  fill(main, 
    header("Tournaments", "Sent to every cabinet at its update call. Status follows the clock.",
      el("button", { class: "primary", onclick: () => editTournament(null) }, "New tournament")),
    el("div", { class: "card" }, table(["ID", "Tournament", "Game", "Status", "When", { label: "Credits", num: true },
      { label: "Players", num: true }, ""], rows, { empty: "No tournaments." })),
    el("p", { class: "note" }, "After it ends a tournament is final for its “final days” (winners shown), then removed from the cabinets. ",
      "Delete one only once it shows as removed, or cabinets that had it keep it."));
};

async function saveTournaments(mutate) {
  const st = await api("/api/state");
  const list = st.tournaments || [];
  mutate(list);
  await op({ op: "put", path: ["tournaments"], value: list });
}

async function editTournament(t) {
  const isNew = !t;
  const now = Math.floor(Date.now() / 1000);
  t = t || {
    id: Math.max(0, ...(STATE.tournaments || []).map((x) => x.id)) + 1, game: 48, credits: 1, gameopts: 0,
    name: "", desc: "", start: now, end: now + 7 * 86400, showdate: now, final_days: 7,
    randseed: Math.floor(Math.random() * 1e9), seedinc: 1,
    groups: ["NATIONAL", "REGIONAL", "LOCAL"], prizes: ["", "", ""],
  };
  const r = await dialog(isNew ? "New tournament" : `Tournament ${t.id}`, (b) => {
    const f = el("div", { class: "form" },
      field("ID", input("id", t.id, { type: "number", min: 1, required: true, readonly: !isNew })),
      field("Game number", input("game", t.game, { type: "number", min: 0, max: 255, required: true })),
      field("Credits to play", input("credits", t.credits, { type: "number", min: 0, max: 99 })),
      field("Game options", input("gameopts", t.gameopts, { type: "number", min: 0 })),
      field("Name (shown as the event)", input("name", t.name, { maxlength: 50, required: true }), "wide"),
      field("Description", input("desc", t.desc, { maxlength: 100 }), "wide"),
      field("Starts", input("start", toLocalInput(t.start), { type: "datetime-local", required: true })),
      field("Ends", input("end", toLocalInput(t.end), { type: "datetime-local", required: true })),
      field("Shown from", input("showdate", toLocalInput(t.showdate ?? t.start), { type: "datetime-local" })),
      field("Final days (winners shown)", input("final_days", t.final_days ?? 7, { type: "number", min: 0, max: 365 })),
      field("Random seed", input("randseed", t.randseed, { type: "number", min: 0 })),
      field("Seed increment", input("seedinc", t.seedinc, { type: "number", min: 0 })),
      ...[0, 1, 2].map((i) => field(`Group ${i + 1}`, input(`group${i}`, t.groups?.[i], { maxlength: 50 }))),
      ...[0, 1, 2].map((i) => field(`Prize ${i + 1}`, input(`prize${i}`, t.prizes?.[i], { maxlength: 50 }))),
    );
    b.append(f, el("p", { class: "muted" }, gameName(t.game), " — every cabinet deals the same games from the random seed."));
    return async () => {
      const v = Object.fromEntries([...f.querySelectorAll("[name]")].map((e) => [e.name, e.value]));
      const n = {
        ...t, id: +v.id, game: +v.game, credits: +v.credits, gameopts: +v.gameopts, name: v.name, desc: v.desc,
        start: fromLocalInput(v.start), end: fromLocalInput(v.end), showdate: fromLocalInput(v.showdate || v.start),
        final_days: +v.final_days, randseed: +v.randseed, seedinc: +v.seedinc,
        groups: [v.group0, v.group1, v.group2], prizes: [v.prize0, v.prize1, v.prize2],
      };
      delete n.status;
      if (n.end <= n.start) throw new Error("It has to end after it starts.");
      await saveTournaments((list) => {
        const i = list.findIndex((x) => x.id === n.id);
        if (isNew && i >= 0) throw new Error(`There is already a tournament ${n.id}.`);
        if (i >= 0) list[i] = n; else list.push(n);
      });
    };
  });
  if (r) { toast("Tournament saved"); route(); }
}

async function endTournament(t) {
  if (!(await confirmBox(`End “${t.name}” now?`, "Its end becomes now; cabinets get the final at their next update call.", "End now"))) return;
  await guard(() => saveTournaments((list) => {
    const x = list.find((y) => y.id === t.id);
    const now = Math.floor(Date.now() / 1000);
    x.end = now;
    if (x.start > now) x.start = now - 60;
  }), "Tournament ended");
  route();
}

async function deleteTournament(t) {
  if (!(await confirmBox(`Delete “${t.name}”?`, "It is taken out of the state file. Scores stay. Cabinets that had it are not told.", "Delete"))) return;
  await guard(() => saveTournaments((list) => list.splice(list.findIndex((y) => y.id === t.id), 1)), "Tournament deleted");
  route();
}

function showStandings(st, t) {
  const rows = standings(st, t.id).map((s, i) => el("tr", {},
    el("td", { class: "num" }, i + 1), el("td", {}, s.p.handle || `#${s.pid}`),
    el("td", {}, [s.p.city, s.p.state].filter(Boolean).join(", ")), el("td", {}, el("code", {}, s.p.cabinet || "")),
    el("td", { class: "num" }, s.best.join(" · ")), el("td", { class: "num" }, Math.floor(s.total / 5))));
  dialog(`Standings: ${t.name}`, (b) => {
    b.append(table(["#", "Player", "From", "Cabinet", { label: "Best five", num: true }, { label: "Shown", num: true }],
      rows, { empty: "No scores yet." }));
    return () => true;
  }, "Close");
}

// ------------------------------------------------------------------ cabinets

PAGES.cabinets = async (main, serial) => {
  const st = await loadState();
  const serials = Object.keys(st._cabinets).sort();
  serial = serial && serials.includes(serial) ? serial : serials[0];
  const list = el("div", { class: "card list" }, serials.length ? serials.map((s) => {
    const l = st.logins?.[s];
    return el("button", { class: s === serial ? "on" : null, onclick: () => (location.hash = `cabinets/${encodeURIComponent(s)}`) },
      el("code", {}, s), " ", st.locations?.[s]?.name || "",
      el("small", {}, st._cabinets[s].release || "not called yet", l ? ` · ${ago(l.at)}` : ""));
  }) : el("div", { class: "empty" }, "No cabinet has called yet."));
  fill(main, 
    header("Cabinets", "What each cabinet reported at its last update call, and what it gets next time.",
      serials.length ? el("button", { onclick: () => messageAll(serials) }, "Message all cabinets") : null),
    el("div", { class: "split" }, list, serial ? cabinetDetail(st, serial) : el("div")));
};

function cabinetDetail(st, serial) {
  const cab = st._cabinets[serial];
  const login = st.logins?.[serial] || {};
  const rep = cab.reports || {};
  const box = st.outbox?.[serial] || [];
  const done = (st.outbox_done?.[serial] || []).slice(-30).reverse();
  const wrap = el("div", { class: "stack" });
  const tabsEl = el("div", { class: "tabs" });
  const pane = el("div");
  const tabs = {
    Overview: () => cabOverview(st, serial, login, cab, box, done),
    Location: () => cabLocation(st, serial),
    "Operator settings": () => cabSettings(serial, rep["0202"], login),
    Prices: () => cabPrices(serial, rep["0212"], login.protocol),
    "Dial-up": () => cabDialup(serial, rep["0222"]),
    Actions: () => cabActions(serial),
    Reports: () => cabReports(rep, login.protocol),
    Files: () => cabFiles(serial, cab.files),
  };
  let cur = sessionStorage.getItem("cabtab") || "Overview";
  if (!tabs[cur]) cur = "Overview";
  const show = (name) => {
    cur = name;
    try { sessionStorage.setItem("cabtab", name); } catch {}
    for (const b of tabsEl.children) b.classList.toggle("on", b.textContent === name);
    pane.replaceChildren(tabs[name]());
  };
  for (const name of Object.keys(tabs)) tabsEl.append(el("button", { onclick: () => show(name) }, name));
  wrap.append(el("div", { class: "card" },
    el("div", { class: "row spread" },
      el("div", {}, el("h2", {}, `Cabinet ${serial}`),
        el("p", { class: "muted" }, cab.release || "not called yet",
          login.version ? ` · V${login.version}` : "", login.at ? ` · last call ${fmtTime(login.at)}` : "")),
      box.length ? pill(`${box.length} waiting`, "warn") : pill("outbox empty", "plain")),
    tabsEl, pane));
  show(cur);
  return wrap;
}

const DO_LABEL = {
  message: "message page", send_file: "send file", fetch_file: "fetch file", delete: "delete / reboot",
  settings: "operator settings", prices: "prices", dialup: "dial-up settings", isp: "ISP login",
  location_entry: "location entry", counters: "read counters",
};
function describe(item) {
  const d = { ...item };
  delete d.do; delete d.result; delete d.at; delete d.update;
  if (item.do === "delete" && !item.path) return item.reboot ? "reboot" : "nothing";
  if (item.do === "isp") d.password = "••••";
  if (item.do === "dialup" && d.set?.password) d.set = { ...d.set, password: "••••" };
  return JSON.stringify(d);
}

function cabOverview(st, serial, login, cab, box, done) {
  const loc = st.locations?.[serial] || {};
  const upd = Object.entries(st.update_status || {}).filter(([, v]) => v[serial]).map(([n, v]) =>
    el("tr", {}, el("td", {}, n), el("td", {}, v[serial])));
  const boxRows = box.map((it) => el("tr", {},
    el("td", {}, DO_LABEL[it.do] || it.do, it.update ? el("small", {}, ` (update ${it.update})`) : null),
    el("td", {}, el("code", {}, describe(it))),
    el("td", {}, it.update ? null : el("button", {
      class: "small danger", onclick: async () => {
        await guard(() => op({ op: "remove", path: ["outbox", serial], match: it }), "Removed from the outbox");
        route();
      },
    }, "Remove"))));
  const doneRows = done.map((it) => el("tr", {},
    el("td", {}, fmtTime(it.at)), el("td", {}, DO_LABEL[it.do] || it.do), el("td", {}, el("code", {}, describe(it))),
    el("td", {}, it.result)));
  return el("div", { class: "stack" },
    el("dl", { class: "kv" },
      el("dt", {}, "Location"), el("dd", {}, loc.name || "—"),
      el("dt", {}, "City, state"), el("dd", {}, loc.city_state || "—"),
      el("dt", {}, "Protocol"), el("dd", {}, login.protocol ?? "—"),
      el("dt", {}, "Last call on port"), el("dd", {}, login.port ?? "—"),
      el("dt", {}, "Players registered here"), el("dd", {},
        Object.values(st.players || {}).filter((p) => p.cabinet === serial).length),
      el("dt", {}, "Report messages kept"), el("dd", {}, st.reports?.[serial] ?? 0)),
    el("h3", {}, "Outbox: done at the next update call"),
    table(["What", "Details", ""], boxRows, { empty: "Nothing waiting." }),
    upd.length ? [el("h3", {}, "Update packages"), table(["Package", "Status"], upd)] : null,
    el("h3", {}, "Done lately"),
    table(["When", "What", "Details", "Outcome"], doneRows, { empty: "Nothing done yet." }));
}

async function queue(serials, item, what) {
  for (const s of [].concat(serials)) await op({ op: "append", path: ["outbox", s], value: item });
  toast(`${what} queued for the next update call`);
}

// modem-server.py's REG_FIELDS, less the two no screen asks for.
const REG_FIELDS = [["first_name", "First name"], ["last_name", "Last name"], ["gender", "Gender"],
  ["birthday", "Birthday"], ["address", "Address"], ["city", "City"], ["region", "Region"],
  ["postal_code", "Postal code"], ["country", "Country"], ["telephone", "Telephone"], ["e_mail", "E-mail"]];

function cabLocation(st, serial) {
  const loc = st.locations?.[serial] || {};
  const f = el("form", { class: "stack" },
    el("p", { class: "muted" }, "Shown on the cabinet's LOCATION INFO screen, on its registration page at Initial Connection, ",
      "and as the location of its players on other cabinets. Sent at every update call."),
    el("div", { class: "form" },
      field("Name", input("name", loc.name, { maxlength: 50 }), "wide"),
      field("City, state", input("city_state", loc.city_state, { maxlength: 50, placeholder: "Springfield, IL" })),
      field("Country", input("country", loc.country, { maxlength: 50 })),
      field("Telephone", input("telephone", loc.telephone, { maxlength: 50 }))),
    el("h3", {}, "Player registration"),
    el("p", { class: "muted" }, "What the cabinet's new-player form asks for, besides the login name and PIN."),
    el("div", { class: "form" }, REG_FIELDS.map(([k, label]) => field(label,
      el("select", { name: `field_${k}` }, [["", "leave as it is"], ["0", "required"], ["1", "optional"], ["2", "not asked"]]
        .map(([v, l]) => el("option", { value: v, selected: String(loc.fields?.[k] ?? "") === v || null }, l)))))),
    el("div", { class: "row" }, el("button", { class: "primary", type: "submit" }, "Save location")));
  f.onsubmit = async (ev) => {
    ev.preventDefault();
    const v = formValues(f);
    const fields = { ...loc.fields };
    for (const [k] of REG_FIELDS) {
      if (v[`field_${k}`] !== "") fields[k] = +v[`field_${k}`];
      else delete fields[k];
      delete v[`field_${k}`];
    }
    await guard(() => op({ op: "put", path: ["locations", serial], value: { ...loc, ...v, fields } }), "Location saved");
    route();
  };
  return f;
}

const SIX_STAR = [["six_star_scores", "+0C", "High scores"], ["six_star_billboard", "+0D", "Video billboard"],
  ["six_star_volume", "+0E", "Volume control"], ["six_star_calibration", "+0F", "Screen calibration"],
  ["six_star_update", "+10", "Update from server"]];

function needsReport(r, what) {
  return el("div", { class: "note" }, `This cabinet has not reported its ${what} yet. `,
    "They are read at the end of every update call (Emerald and Emerald 2 only); changes are built on that reading.");
}

function cabSettings(serial, r, login) {
  if (login.protocol && ![7, 9].includes(login.protocol)) return el("div", { class: "note" }, "This release has no operator settings over the network.");
  if (!r?.decoded) return needsReport(r, "operator settings");
  const d = r.decoded;
  const sel = (name, value, opts) => el("select", { name }, opts.map(([v, l]) => el("option", { value: v, selected: +v === value || null }, l)));
  const hours = [...Array(24).keys()].map((h) => [h, `${pad(h)}:00`]);
  const f = el("form", { class: "stack" },
    el("p", { class: "muted" }, `As read ${fmtTime(r.at)}. Only what you change is sent.`),
    el("div", { class: "form" },
      field("Volume (%)", input("volume", d.volume, { type: "number", min: 0, max: 100 })),
      field("Adult games", sel("+04", d.adult_mode, [[0, "off"], [1, "on"], [2, "on between hours"]])),
      field("Adult on from", sel("+07", d.adult_from, hours)),
      field("Adult off at", sel("+08", d.adult_to, hours)),
      field("Nudity", sel("+05", d.nudity, [[0, "not allowed"], [1, "allowed"]])),
      field("Level", sel("+06", d.fullnude, [[0, "topless"], [1, "full nude"]])),
      field("Adult attract screens", sel("+09", d.adult_attract, [[0, "mini attract loop"], [1, "on"]])),
      field("Adult content (name filter off)", sel("+13", d.adult_content, [[0, "off"], [1, "on"]])),
      field("AC level", sel("+14", d.ac_level, [[1, "1"], [2, "2"], [3, "3"], [4, "4"]])),
      field("6 Star", sel("+0B", d.six_star, [[0, "off"], [1, "on"]])),
      field("6 Star PIN (star order, 1–6)", input("pin", d.six_star_pin, { pattern: "[1-6]{4}", title: "four digits, 1 to 6" })),
    ),
    el("h3", {}, "6 Star opens"),
    el("div", { class: "row" }, SIX_STAR.map(([k, off, label]) => el("label", { class: "check" },
      el("input", { type: "checkbox", name: off, checked: !!d[k] }), label))),
    el("div", { class: "row" }, el("button", { class: "primary", type: "submit" }, "Queue changes")));
  const orig = formValues(f);
  f.onsubmit = async (ev) => {
    ev.preventDefault();
    const v = formValues(f);
    const item = { do: "settings", set: {} };
    for (const [k, val] of Object.entries(v)) {
      if (val === orig[k]) continue;
      if (k === "volume") item.volume = +val;
      else if (k === "pin") item.set16 = { "+11": +val };
      else item.set[k] = typeof val === "boolean" ? (val ? 1 : 0) : +val;
    }
    if (!Object.keys(item.set).length) delete item.set;
    if (Object.keys(item).length === 1) return toast("Nothing changed", true);
    await guard(() => queue(serial, item, "Settings"));
    route();
  };
  return el("div", { class: "stack" }, f, clearScoresForm(serial, login.protocol));
}

function clearScoresForm(serial, protocol) {
  const f = el("form", { class: "stack" },
    el("h3", {}, "Clear high scores"),
    el("div", { class: "form" },
      field("Game number (blank: every game)", input("game", "", { type: "number", min: 0, max: 84 })),
      field("Category (trivia games; blank: all)", input("cat", "", { type: "number", min: 0, max: 254 }))),
    el("div", { class: "row" }, el("button", { class: "danger", type: "submit" }, "Queue clear")));
  f.onsubmit = async (ev) => {
    ev.preventDefault();
    const v = formValues(f);
    const game = v.game === "" ? 0x54 : +v.game;
    const cat = v.cat === "" ? 255 : +v.cat;
    if (!(await confirmBox("Clear high scores?", v.game === "" ? "Every game's high scores on this cabinet." : `${gameName(game, protocol)}'s high scores.`, "Queue clear"))) return;
    await guard(() => queue(serial, { do: "settings", clear_scores: [[game, cat]] }, "High-score clear"));
    route();
  };
  return f;
}

function cabPrices(serial, r, protocol) {
  if (!r?.decoded) return needsReport(r, "prices");
  const prices = r.decoded.prices;
  const rows = Object.entries(prices).sort((a, b) => a[0] - b[0]).map(([g, c]) => el("tr", { hidden: !c },
    el("td", {}, gameName(g, protocol)),
    el("td", {}, el("input", { type: "number", min: 0, max: 15, value: c, "data-game": g, "data-orig": c }))));
  const t = table(["Game", "Credits per play (0: not offered)"], rows, { empty: "No games reported." });
  const all = el("input", { type: "checkbox" });
  all.onchange = () => rows.forEach((r) => (r.hidden = !all.checked && r.querySelector("input").dataset.orig === "0"));
  const btn = el("button", {
    class: "primary", onclick: async () => {
      const set = {};
      for (const i of t.querySelectorAll("input")) if (i.value !== i.dataset.orig) set[i.dataset.game] = +i.value;
      if (!Object.keys(set).length) return toast("Nothing changed", true);
      await guard(() => queue(serial, { do: "prices", set }, "Prices"));
      route();
    },
  }, "Queue price changes");
  return el("div", { class: "stack" }, el("p", { class: "muted" }, `As read ${fmtTime(r.at)}. While a tournament runs, its credits replace the game's price.`),
    el("label", { class: "check" }, all, "Show games not offered"), t, btn);
}

function cabDialup(serial, r) {
  if (!r?.decoded) return needsReport(r, "dial-up settings");
  const d = r.decoded;
  const f = el("form", { class: "stack" },
    el("p", { class: "muted" }, `The Dial-Up Network screen, as read ${fmtTime(r.at)}.`),
    el("div", { class: "form" },
      field("Daily update hour", input("update_hour", d.update_hour, { type: "number", min: 0, max: 23 })),
      field("Access number", input("phone", d.phone, { maxlength: 40 })),
      field("Dial prefix", input("prefix", d.prefix, { maxlength: 10 })),
      field("Server", input("server", d.server, { maxlength: 40 })),
      field("ISP login", input("login", d.login, { maxlength: 40 })),
      field("ISP password", input("password", d.password, { maxlength: 40 })),
      field("DNS 1", input("dns1", d.dns1, { maxlength: 15 })),
      field("DNS 2", input("dns2", d.dns2, { maxlength: 15 })),
      field("Modem init string", input("init", d.init, { maxlength: 99 }), "wide")),
    el("div", { class: "note warn" }, "A wrong number, server or init string can leave the cabinet unable to call back."),
    el("div", { class: "row" }, el("button", { class: "primary", type: "submit" }, "Queue changes")));
  const orig = formValues(f);
  f.onsubmit = async (ev) => {
    ev.preventDefault();
    const v = formValues(f);
    const set = {};
    for (const [k, val] of Object.entries(v)) if (val !== orig[k]) set[k] = k === "update_hour" ? +val : val;
    if (!Object.keys(set).length) return toast("Nothing changed", true);
    await guard(() => queue(serial, { do: "dialup", set }, "Dial-up settings"));
    route();
  };
  return f;
}

function cabActions(serial) {
  const act = (label, desc, fn, cls) => el("div", { class: "card stack" },
    el("div", {}, el("b", {}, label), el("p", { class: "muted" }, desc)),
    el("div", {}, el("button", { class: cls || null, onclick: async () => { if (await fn()) route(); } }, label)));
  return el("div", { class: "grid cols-2" },
    act("Send a message", "A page of up to 15 lines the cabinet shows (as its registration page).", () => messageDialog([serial])),
    act("Send a file", "Upload a file and have it written to a path on the cabinet.", () => sendFileDialog(serial)),
    act("Fetch a file", "Copy a file from the cabinet; it appears under Files.", () => simpleDialog(serial, "Fetch a file",
      [["path", "Path on the cabinet", "C:\\DEBUG.DAT"]], (v) => ({ do: "fetch_file", path: v.path }))),
    act("Delete a file", "Delete a path on the cabinet, optionally rebooting after the call.", () => simpleDialog(serial, "Delete a file",
      [["path", "Path on the cabinet", "C:\\"], ["reboot", "Reboot after the call", false]],
      (v) => ({ do: "delete", path: v.path, reboot: v.reboot })), "danger"),
    act("Reboot", "Reboot the cabinet at the end of its next update call.", async () => {
      if (!(await confirmBox("Reboot the cabinet?", "At the end of its next update call.", "Queue reboot"))) return false;
      await guard(() => queue(serial, { do: "delete", path: "", reboot: true }, "Reboot"));
      return true;
    }),
    act("Read counters", "Coin, bill and licensed-game counters. The cabinet clears the per-report counts once sent.", async () => {
      await guard(() => queue(serial, { do: "counters" }, "Counter read"));
      return true;
    }),
    act("Set the ISP login", "For cabinets using Merit's own ISP account (0x0A01).", () => simpleDialog(serial, "ISP login",
      [["login", "Login", ""], ["password", "Password", ""]], (v) => ({ do: "isp", login: v.login, password: v.password }))),
    act("Add a location entry", "A row in the cabinet's location database (what rankings show for players).", () => simpleDialog(serial, "Location entry",
      [["id", "ID (a cabinet's serial)", ""], ["name", "Name", ""], ["city", "City", ""], ["state", "State", ""], ["country", "Country", ""]],
      (v) => ({ do: "location_entry", id: +v.id, name: v.name, city: v.city, state: v.state, country: v.country }))),
  );
}

function simpleDialog(serial, title, fields, make) {
  return dialog(title, (b) => {
    const f = el("div", { class: "form" }, fields.map(([n, l, d]) => typeof d === "boolean"
      ? el("label", { class: "check wide" }, el("input", { type: "checkbox", name: n, checked: d }), l)
      : field(l, input(n, d, { required: n !== "password" }), "wide")));
    b.append(f);
    return async () => {
      const v = {};
      for (const e of f.querySelectorAll("[name]")) v[e.name] = e.type === "checkbox" ? e.checked : e.value;
      await queue(serial, make(v), title);
    };
  }, "Queue");
}

function messageDialog(serials) {
  return dialog(serials.length > 1 ? `Message to ${serials.length} cabinets` : "Send a message", (b) => {
    const ta = el("textarea", { rows: 10, maxlength: 1500, required: true, placeholder: "Up to 15 lines of up to 99 characters" });
    b.append(ta, el("p", { class: "muted" }, "Shown at the cabinet's next update call."));
    return async () => {
      const lines = ta.value.split("\n");
      if (lines.length > 15 || lines.some((l) => l.length > 99)) throw new Error("At most 15 lines of 99 characters.");
      await queue(serials, { do: "message", text: ta.value }, "Message");
    };
  }, "Queue message");
}

async function messageAll(serials) {
  if (await messageDialog(serials)) route();
}

function sendFileDialog(serial) {
  return dialog("Send a file", (b) => {
    const file = el("input", { type: "file", required: true });
    const to = input("to", "", { required: true, placeholder: "C:\\PATH\\FILE.EXT" });
    file.onchange = () => { if (!to.value && file.files[0]) to.value = `C:\\${file.files[0].name.toUpperCase()}`; };
    b.append(field("File", file), field("Destination on the cabinet (DOS 8.3 names)", to));
    return async () => {
      const f = file.files[0];
      const up = await api(`/api/upload?name=${encodeURIComponent(f.name)}`, { raw: await f.arrayBuffer() });
      await queue(serial, { do: "send_file", from: up.path, to: to.value }, `${f.name} (${bytes(up.size)})`);
    };
  }, "Upload and queue");
}

function cabReports(rep, protocol) {
  const parts = [];
  const hist = rep["00E2"]?.decoded?.calls || [];
  parts.push(el("h3", {}, "The cabinet's last calls", rep["00E2"] ? ` (read ${fmtTime(rep["00E2"].at)})` : ""),
    table(["Start", "End", { label: "Status", num: true }, { label: "Error", num: true }],
      hist.map((c) => el("tr", {}, el("td", {}, fmtTime(c.start)), el("td", {}, fmtTime(c.end)),
        el("td", { class: "num" }, c.status), el("td", { class: "num" }, c.error))), { empty: "Not reported." }));
  const cnt = rep["00CA"]?.decoded?.counters || [];
  parts.push(el("h3", {}, "Counters", rep["00CA"] ? ` (read ${fmtTime(rep["00CA"].at)})` : ""),
    table(["What", { label: "Input", num: true }, { label: "Current", num: true }, { label: "Lifetime", num: true },
      { label: "Since last report", num: true }],
    cnt.map((c) => el("tr", {}, el("td", {}, c.what), el("td", { class: "num" }, c.index), el("td", { class: "num" }, c.current),
      el("td", { class: "num" }, c.lifetime), el("td", { class: "num" }, c.since_report))),
    { empty: "Not read yet: queue “Read counters” under Actions." }));
  for (const [typ, label] of [["00C2", "Game statistics, current period"], ["00C3", "Game statistics, lifetime"]]) {
    const d = rep[typ]?.decoded;
    const g = d?.games || [];
    parts.push(el("h3", {}, label, rep[typ] ? ` (read ${fmtTime(rep[typ].at)})` : ""),
      d ? el("p", { class: "muted" },
        d.total_credits != null ? `Total credits ${d.total_credits} · ` : `Credits of the games below ${d.games_credits} · `,
        `free ${d.free_credits} · played ${d.credits_played} · meter pulses ${d.meter_pulses.join(" / ")}`,
        d.tournament_plays != null ? ` · TournaMAXX games ${d.tournament_plays} (${d.tournament_credits} credits)` : "",
        d.months?.length ? ` · tournament credits ${d.months.map((m) => `${m.year}-${String(m.month).padStart(2, "0")}: ${m.credits}`).join(", ")}` : "")
        : null,
      table(["Game", { label: "Price", num: true }, { label: "Share %", num: true }, { label: "Plays", num: true },
        { label: "Credits", num: true }, { label: "Shortest", num: true }, { label: "Average", num: true },
        { label: "Longest", num: true }, { label: "Linked / 1P–4P", num: true }],
        g.map((x) => el("tr", {}, el("td", {}, gameName(x.game, protocol)), el("td", { class: "num" }, x.price),
          el("td", { class: "num" }, x.share), el("td", { class: "num" }, x.plays),
          el("td", { class: "num" }, x.credits ?? "—"), el("td", { class: "num" }, mss(x.shortest)),
          el("td", { class: "num" }, mss(x.average)), el("td", { class: "num" }, mss(x.longest)),
          el("td", { class: "num" }, x.by_players ? [x.linked, ...x.by_players].join(" / ") : "—"))),
        { empty: "Not reported." }));
  }
  parts.push(el("p", { class: "note" }, "The statistics' per-game layout is read from the cabinet's code and not yet checked against a report; the raw bytes are kept in the state file."));
  return el("div", {}, parts);
}

function cabFiles(serial, files) {
  return table(["File", { label: "Size", num: true }, "Fetched", ""], files.map((f) => el("tr", {},
    el("td", {}, el("code", {}, f.path)), el("td", { class: "num" }, bytes(f.size)), el("td", {}, fmtTime(f.at)),
    el("td", {}, el("a", { href: `/api/download?kind=file&serial=${encodeURIComponent(serial)}&path=${encodeURIComponent(f.path)}` }, "Download")))),
  { empty: "No files fetched from this cabinet." });
}

// ------------------------------------------------------------------ players

PAGES.players = async (main) => {
  const st = await loadState();
  const q = el("input", { type: "search", placeholder: "Search handle, city, cabinet…" });
  const body = el("div");
  const players = Object.entries(st.players || {}).map(([id, p]) => ({ id, ...p }));
  const counts = {};
  for (const s of st.scores || []) counts[s.player] = (counts[s.player] || 0) + 1;
  const draw = () => {
    const needle = q.value.toLowerCase();
    const rows = players.filter((p) => !needle || [p.id, p.handle, p.city, p.state, p.cabinet].join(" ").toLowerCase().includes(needle))
      .sort((a, b) => +b.id - +a.id).slice(0, 500)
      .map((p) => el("tr", {},
        el("td", { class: "num" }, p.id), el("td", {}, el("b", {}, p.handle)), el("td", {}, [p.city, p.state].filter(Boolean).join(", ")),
        el("td", {}, el("code", {}, p.cabinet || "")), el("td", { class: "num" }, counts[p.id] || 0),
        el("td", {}, el("button", { class: "small", onclick: () => editPlayer(p) }, "Edit"))));
    body.replaceChildren(table([{ label: "ID", num: true }, "Handle", "City, state", "Home cabinet", { label: "Score uploads", num: true }, ""],
      rows, { empty: "No players registered yet." }));
  };
  q.oninput = draw;
  draw();
  fill(main, header("Players", `${players.length} registered. Next ID: ${st.next_player_id ?? "—"}.`),
    el("div", { class: "card stack" }, q, body));
};

async function editPlayer(p) {
  const r = await dialog(`Player ${p.id}`, (b) => {
    const f = el("div", { class: "form" },
      field("Handle", input("handle", p.handle, { maxlength: 12, required: true })),
      field("New PIN (blank: keep)", input("pin", "", { maxlength: 4, pattern: "[0-9]{4}" })),
      field("City", input("city", p.city, { maxlength: 30 })),
      field("State", input("state", p.state, { maxlength: 35 })));
    b.append(f, el("p", { class: "muted" }, "Every cabinet is sent the player again at its next update call."));
    return async () => {
      const v = Object.fromEntries([...f.querySelectorAll("[name]")].map((e) => [e.name, e.value]));
      if (!v.pin) delete v.pin;
      await api("/api/player", { body: { id: p.id, ...v } });
    };
  });
  if (r) { toast("Player saved"); route(); }
}

// ------------------------------------------------------------------ updates

PAGES.updates = async (main) => {
  const st = await loadState();
  const ups = st.updates || {};
  const status = st.update_status || {};
  const cards = Object.entries(ups).map(([name, u]) => {
    const rows = Object.entries(status[name] || {}).map(([serial, s]) => el("tr", {},
      el("td", {}, el("code", {}, serial)), el("td", {}, s),
      el("td", {}, el("button", {
        class: "small", onclick: async () => {
          if (!(await confirmBox("Send it again?", `Cabinet ${serial} gets ${name} again at its next update call.`, "Send again"))) return;
          await guard(() => op({ op: "delete", path: ["update_status", name, serial] }), "It goes again next call");
          route();
        },
      }, "Send again"))));
    return el("div", { class: "card stack" },
      el("div", { class: "row spread" }, el("h2", {}, name),
        el("button", {
          class: "small danger", onclick: async () => {
            if (!(await confirmBox(`Withdraw ${name}?`, "No more cabinets get it. Items already queued stay queued.", "Withdraw"))) return;
            await guard(() => op({ op: "delete", path: ["updates", name] }), "Withdrawn");
            route();
          },
        }, "Withdraw")),
      el("dl", { class: "kv" },
        el("dt", {}, "For"), el("dd", {}, u.protocol ? `protocol ${u.protocol} (${({ 9: "Emerald 2", 7: "Emerald V8.04", 6: "Double Diamond", 3: "Diamond" })[u.protocol] || "?"})` : "every cabinet"),
        u.version ? [el("dt", {}, "Version"), el("dd", {}, `V${u.version} → V${u.becomes || "?"}`)] : null,
        el("dt", {}, "Files"), el("dd", {}, (u.send || []).map(([src, dst]) => el("div", {}, el("code", {}, dst), el("small", {}, ` ← ${src}`)))),
        u.result ? [el("dt", {}, "Result file"), el("dd", {}, el("code", {}, u.result))] : null),
      table(["Cabinet", "Status", ""], rows, { empty: "No cabinet has had it yet." }));
  });
  fill(main, 
    header("Update packages", "Sent once to every matching cabinet at its update call, then the cabinet reboots and installs.",
      el("button", { class: "primary", onclick: newUpdate }, "New package")),
    cards.length ? el("div", { class: "stack" }, cards) : el("div", { class: "card empty" }, "No update packages."));
};

async function newUpdate() {
  const r = await dialog("New update package", (b) => {
    const kind = el("select", { name: "kind" }, el("option", { value: "exe" }, "a ready NETUPDT.EXE"),
      el("option", { value: "zip" }, "a .zip laid out as the cabinet's C:\\ (built with mkupdate)"));
    const file = el("input", { type: "file", required: true });
    const f = el("div", { class: "form" },
      field("Name", input("name", "", { required: true, pattern: "[A-Za-z0-9._-]+", placeholder: "tmfix-804" })),
      field("Protocol (blank: every cabinet)", el("select", { name: "protocol" },
        [["", "every cabinet"], ["9", "9 — Emerald 2"], ["7", "7 — Emerald V8.04"], ["6", "6 — Double Diamond"], ["3", "3 — Diamond"]]
          .map(([v, l]) => el("option", { value: v }, l)))),
      field("From", kind, "wide"), field("File", file, "wide"),
      field("Result file (optional)", input("result", "", { placeholder: "C:\\TMFIX.TXT" })),
      field("Only for version (6/3)", input("version", "", { placeholder: "7.01" })),
      field("Installed when it logs in as", input("becomes", "", { placeholder: "7.20" })));
    const out = el("pre", { class: "log", hidden: true });
    b.append(f, el("div", { class: "note warn" }, "It starts going out at the next update call of every matching cabinet."), out);
    return async () => {
      const v = Object.fromEntries([...f.querySelectorAll("[name]")].map((e) => [e.name, e.value]));
      const buf = await file.files[0].arrayBuffer();
      let entry;
      if (v.kind === "zip") {
        const r = await api(`/api/updates/build?name=${encodeURIComponent(v.name)}`, { raw: buf });
        out.hidden = false;
        out.textContent = r.log.join("\n");
        entry = r.entry;
      } else {
        const r = await api(`/api/updates/upload?name=${encodeURIComponent(v.name)}`, { raw: buf });
        entry = { send: [[r.path, "C:\\NETUPDT.EXE"]] };
      }
      if (v.protocol) entry.protocol = +v.protocol;
      if (v.result) entry.result = v.result;
      if (v.version) entry.version = v.version;
      if (v.becomes) entry.becomes = v.becomes;
      if ((STATE.updates || {})[v.name]) throw new Error(`There is already a package ${v.name}.`);
      await op({ op: "put", path: ["updates", v.name], value: entry });
    };
  }, "Publish");
  if (r) { toast("Package published"); route(); }
}

// ------------------------------------------------------------------ log

PAGES.log = async (main) => {
  const q = el("input", { type: "search", placeholder: "Filter (e.g. call 12, TournaMAXX, outbox)" });
  const lines = el("select", {}, [200, 500, 2000, 5000].map((n) => el("option", { value: n, selected: n === 500 || null }, `${n} lines`)));
  const follow = el("input", { type: "checkbox", checked: true });
  const pre = el("pre", { class: "log" });
  const info = el("small");
  const load = async () => {
    const r = await api(`/api/log?lines=${lines.value}&q=${encodeURIComponent(q.value)}`);
    const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 30;
    pre.textContent = r.lines.join("\n") || "(empty)";
    info.textContent = `${r.lines.length} lines shown · log ${bytes(r.size)}`;
    if (atBottom || !pre.dataset.loaded) pre.scrollTop = pre.scrollHeight;
    pre.dataset.loaded = "1";
  };
  let debounce;
  q.oninput = () => { clearTimeout(debounce); debounce = setTimeout(load, 300); };
  lines.onchange = load;
  fill(main, 
    header("Server log", "Every call: PPP, IP, and each TournaMAXX message.",
      el("a", { href: "/api/download?kind=log" }, el("button", {}, "Download"))),
    el("div", { class: "row" }, el("div", { class: "grow" }, q), lines, el("label", { class: "check" }, follow, "Follow"), info),
    pre);
  await load();
  timer = setInterval(() => { if (follow.checked) load().catch(() => {}); }, 3000);
};

// ------------------------------------------------------------------ settings

PAGES.settings = async (main) => {
  const s = await api("/api/settings");
  const srv = el("form", { class: "card stack" },
    el("h2", {}, "Server ports"),
    el("div", { class: "form" },
      field("Modem calls (what emulators dial)", input("port", s.server.port, { type: "number", min: 1, max: 65535 })),
      field("Direct TournaMAXX ports (blank: off)", input("tcp_ports", (s.server.tcp_ports || []).join(", "), { placeholder: "15000, 17751" })),
      field("Admin port (127.0.0.1 only)", input("admin_port", s.server.admin_port, { type: "number", min: 1, max: 65535 })),
      field("Mega-Link switch, UDP (blank or 0: off)", input("switch_port", s.server.switch_port || "", { type: "number", min: 0, max: 65535, placeholder: "8086" })),
      field("DNS for cabinets on a network, UDP (blank or 0: off)", input("dns_port", s.server.dns_port || "", { type: "number", min: 0, max: 65535, placeholder: "53" })),
      field("DNS answer for us.accessmerit.com (blank: automatic)", input("dns_answer", s.server.dns_answer || "", { placeholder: "10.0.2.2 for SLiRP" }))),
    el("p", { class: "muted" }, "Direct ports take TournaMAXX straight over TCP, for cabinets that reach this host on a real network ",
      "(its DNS name is us.accessmerit.com) instead of dialing the modem port. The firewall must let these ports in."),
    el("div", { class: "row" }, el("button", { class: "primary", type: "submit" }, "Save and restart the server")));
  srv.onsubmit = async (ev) => {
    ev.preventDefault();
    const v = formValues(srv);
    const tcp = v.tcp_ports.split(/[\s,]+/).filter(Boolean).map(Number);
    const r = await guard(() => api("/api/settings", { body: { server: { port: +v.port, tcp_ports: tcp, admin_port: +v.admin_port, switch_port: +v.switch_port || 0, dns_port: +v.dns_port || 0, dns_answer: v.dns_answer.trim() }, restart: true } }));
    toast(r.restarted ? "Saved; the server restarted" : "Saved; it applies when the server starts");
    refreshPill();
  };

  const pw = el("form", { class: "card stack" },
    el("h2", {}, "Your password"),
    el("div", { class: "form" },
      field("Current password", input("old", "", { type: "password", autocomplete: "current-password", required: true })),
      field("New password (10+ characters)", input("new", "", { type: "password", autocomplete: "new-password", minlength: 10, required: true }))),
    el("div", { class: "row" }, el("button", { type: "submit" }, "Change password")));
  pw.onsubmit = async (ev) => {
    ev.preventDefault();
    await guard(() => api("/api/password", { body: formValues(pw) }), "Password changed");
    pw.reset();
  };

  const bk = el("div", { class: "card stack" },
    el("div", { class: "row spread" }, el("h2", {}, "State backups"),
      el("div", { class: "row" },
        el("a", { href: "/api/download?kind=state" }, el("button", {}, "Download current state")),
        el("button", { onclick: async () => { await guard(() => api("/api/backups/make", { body: {} }), "Backup made"); route(); } }, "Back up now"))),
    el("p", { class: "muted" }, "A copy is kept before changes (at most every 15 minutes), and before any restore or raw edit. The newest 60 stay."),
    table(["Backup", { label: "Size", num: true }, ""], s.backups.slice(0, 15).map((b) => el("tr", {},
      el("td", {}, el("code", {}, b.name)), el("td", { class: "num" }, bytes(b.size)),
      el("td", {}, el("div", { class: "row" },
        el("a", { href: `/api/download?kind=backup&name=${encodeURIComponent(b.name)}` }, "Download"),
        el("button", {
          class: "small danger", onclick: async () => {
            if (!(await confirmBox("Restore this backup?", `The state becomes ${b.name}. The current state is backed up first.`, "Restore"))) return;
            await guard(() => api("/api/backups/restore", { body: { name: b.name } }), "Restored");
            route();
          },
        }, "Restore"))))), { empty: "No backups yet." }));

  const raw = el("div", { class: "card stack" },
    el("div", { class: "row spread" }, el("h2", {}, "Raw state (advanced)"),
      el("button", {
        onclick: async (ev) => {
          const r = await guard(() => api("/api/raw"));
          const ta = el("textarea", { rows: 24, spellcheck: "false" });
          ta.value = r.text;
          const save = el("button", {
            class: "danger", onclick: async () => {
              try { JSON.parse(ta.value); } catch (e) { return toast(`Not valid JSON: ${e.message}`, true); }
              if (!(await confirmBox("Replace the whole state?", "Everything in it becomes what is in the editor. A backup is made first.", "Replace"))) return;
              await guard(() => api("/api/raw", { body: { text: ta.value } }), "State replaced");
            },
          }, "Save the whole state");
          ev.target.replaceWith(save);
          raw.append(ta);
        },
      }, "Open editor")),
    el("p", { class: "muted" }, "The server's whole state file, as docs/tournamaxx.md describes it. Cabinet-reported raw bytes are included."));

  fill(main, header("Settings", `Data in ${s.data_dir}`),
    el("div", { class: "grid cols-2" }, srv, pw), await usersCard(), bk, raw);
};

// Everyone here can do everything: users are only separate logins.
async function usersCard() {
  const { users } = await api("/api/users");
  const card = el("div", { class: "card stack" });
  const redraw = async () => card.replaceWith(await usersCard());
  const add = el("form", { class: "form" },
    field("New user", input("user", "", { required: true, pattern: "[A-Za-z0-9_.\\-]{1,32}", autocomplete: "off" })),
    field("Password (10+ characters)", input("password", "", { type: "password", minlength: 10, required: true, autocomplete: "new-password" })),
    el("div", { class: "row" }, el("button", { type: "submit" }, "Add user")));
  add.onsubmit = async (ev) => {
    ev.preventDefault();
    await guard(() => api("/api/users/add", { body: formValues(add) }), "User added");
    redraw();
  };
  const rows = users.map((u) => el("tr", {},
    el("td", {}, u.name, u.you ? el("small", { class: "muted" }, " (you)") : null),
    el("td", {}, u.you ? el("small", { class: "muted" }, "change yours under Your password") : el("div", { class: "row" },
      el("button", {
        class: "small", onclick: async () => {
          const r = await dialog(`New password for ${u.name}`, (b) => {
            const i = input("password", "", { type: "password", minlength: 10, required: true, autocomplete: "new-password" });
            b.append(field("Password (10+ characters)", i), el("p", { class: "muted" }, "They are logged out everywhere."));
            return () => api("/api/users/password", { body: { user: u.name, password: i.value } });
          }, "Set password");
          if (r) { toast("Password set"); redraw(); }
        },
      }, "Set password"),
      el("button", {
        class: "small danger", onclick: async () => {
          if (!(await confirmBox(`Delete ${u.name}?`, "They can no longer log in, and are logged out now.", "Delete"))) return;
          await guard(() => api("/api/users/delete", { body: { user: u.name } }), "User deleted");
          redraw();
        },
      }, "Delete")))));
  card.append(el("h2", {}, "Users"),
    el("p", { class: "muted" }, "Each user has the same access; they are separate logins."),
    table(["User", ""], rows), add);
  return card;
}

// ------------------------------------------------------------------ Mega-Link

PAGES.megalink = async (main) => {
  const r = await api("/api/megalink");
  const cfg = r.config || {};
  const live = Object.fromEntries((r.status?.rooms || []).map((x) => [x.name, x]));
  const rooms = (cfg.rooms || []).map((x) => ({ ...x }));

  const state = r.switch_port
    ? (r.status ? pill(`Switch on UDP ${r.switch_port}`, "ok") : pill(r.running ? "Switch not answering" : "Server stopped", "bad"))
    : pill("Switch off: set its port under Settings", "bad");

  // The rooms: each a secret; cabinets with the same one are on one Ethernet segment.
  const body = el("tbody");
  const draw = () => fill(body, rooms.map((x, i) => {
    const l = live[x.name];
    const cabs = l?.cabinets || [];
    return [el("tr", {},
      el("td", {}, el("input", { value: x.name || "", oninput: (e) => (x.name = e.target.value), placeholder: "Main" })),
      el("td", {}, el("input", { value: x.secret || "", oninput: (e) => (x.secret = e.target.value), placeholder: "(none: the open room)" })),
      el("td", {}, el("input", { type: "number", min: 2, max: 16, value: x.max || 8, oninput: (e) => (x.max = +e.target.value), class: "narrow" })),
      el("td", {}, el("input", { type: "checkbox", checked: !!x.public, onchange: (e) => (x.public = e.target.checked) })),
      el("td", {}, el("input", { value: x.description || "", oninput: (e) => (x.description = e.target.value) })),
      el("td", { class: "num" }, l ? `${cabs.length} / ${l.max}` : "—"),
      el("td", {}, el("button", { class: "small danger", type: "button", onclick: () => { rooms.splice(i, 1); draw(); } }, "Remove"))),
    cabs.length ? el("tr", {}, el("td", { colspan: 7 },
      l.ip_conflicts.length ? el("p", { class: "error" }, `Two cabinets use ${l.ip_conflicts.join(", ")}: give each its own Ethernet IP address.`) : null,
      table(["Cabinet", "IP", "MAC", { label: "In room", num: true }, { label: "Frames in / out", num: true }],
        cabs.map((c) => el("tr", {}, el("td", {}, el("code", {}, c.address)), el("td", {}, c.ips.join(", ") || "—"),
          el("td", {}, el("code", {}, c.macs.join(", ") || "—")), el("td", { class: "num" }, ago(c.since)),
          el("td", { class: "num" }, `${c.frames_in} / ${c.frames_out}`)))))) : null];
  }));
  draw();
  const save = async () => {
    const names = rooms.map((x) => x.name.trim());
    if (names.some((n) => !n)) return toast("Every room needs a name.", true);
    if (new Set(names).size !== names.length) return toast("Two rooms have the same name.", true);
    if (new Set(rooms.map((x) => x.secret || "")).size !== rooms.length) return toast("Two rooms have the same secret.", true);
    await guard(() => op({ op: "put", path: ["megalink", "rooms"], value: rooms.map((x) => ({ ...x, name: x.name.trim() })) }), "Rooms saved");
    route();
  };
  const roomsCard = el("div", { class: "card stack" },
    el("div", { class: "row spread" }, el("h2", {}, "Rooms"), state),
    el("p", { class: "muted" }, "Each room is one Mega-Link network. A cabinet joins by its card's secret (MegaPPBox: Network, ",
      "Remote Switch, this host and port, and the room's secret); with no secret it joins the open room, if there is one. ",
      "Public rooms, with their secret, are listed on the public page."),
    el("div", { class: "table-wrap" }, el("table", {},
      el("thead", {}, el("tr", {}, ["Name", "Secret", "Cabinets", "Public", "Description", "Now", ""].map((h) => el("th", {}, h)))),
      body)),
    el("div", { class: "row" },
      el("button", { type: "button", onclick: () => { rooms.push({ name: "", secret: "", max: 8, public: true }); draw(); } }, "Add room"),
      el("button", { class: "primary", type: "button", onclick: save }, "Save rooms")));

  const pub = el("form", { class: "card stack" },
    el("h2", {}, "Public page"),
    el("p", { class: "muted" }, "A page anyone can open, without logging in, at ",
      el("a", { href: "/megalink", target: "_blank" }, "/megalink"), ": the public rooms, how full they are, and how to connect. ",
      "It shows no addresses."),
    el("label", { class: "check" }, el("input", { type: "checkbox", name: "public_page", checked: !!cfg.public_page }), "Show the public page"),
    el("div", { class: "form" },
      field("Host players connect to (blank: this page's)", input("host", cfg.host, { placeholder: "megalink.example.com" }), "wide"),
      field("Text at the top", el("textarea", { name: "intro", rows: 3 }, cfg.intro || ""), "wide")),
    el("div", { class: "row" }, el("button", { class: "primary", type: "submit" }, "Save")));
  pub.onsubmit = async (ev) => {
    ev.preventDefault();
    const v = formValues(pub);
    for (const k of ["public_page", "host", "intro"])
      await guard(() => op({ op: "put", path: ["megalink", k], value: k === "public_page" ? v[k] : v[k].trim() }));
    toast("Saved");
    route();
  };

  fill(main, header("Mega-Link", "Linked play between emulated cabinets over the internet, through this server's switch.",
    el("button", { onclick: () => route() }, "Refresh")), roomsCard, pub);
};

// ------------------------------------------------------------------ boot

(async () => {
  try {
    const r = await api("/api/session");
    ME = r.user;
    start();
  } catch {
    showLogin();
  }
})();
