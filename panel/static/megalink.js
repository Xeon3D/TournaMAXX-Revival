"use strict";
// The public Mega-Link page: the rooms this server's switch holds, how full
// they are, and how to connect.  Everything from the server goes in as text.

const $ = (s) => document.querySelector(s);

function el(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") e.className = v;
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat(Infinity)) {
    if (k === null || k === undefined || k === false) continue;
    e.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
  return e;
}

function since(t, now) {
  const m = Math.max(0, Math.round((now - t) / 60));
  return m < 1 ? "just joined" : m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60} min`;
}

function room(r, d) {
  const n = r.cabinets.length;
  const seats = el("div", { class: "seats", "aria-label": `${n} of ${r.max} cabinets` },
    Array.from({ length: r.max }, (_, i) => el("span", { class: i < n ? "seat on" : "seat" })));
  return el("div", { class: "card room" },
    el("div", { class: "row spread" }, el("h3", {}, r.name),
      el("span", { class: `pill ${n >= r.max ? "bad" : n ? "ok" : ""}` }, n >= r.max ? "full" : `${n} / ${r.max}`)),
    r.description ? el("p", { class: "desc" }, r.description) : null,
    seats,
    el("dl", {},
      el("dt", {}, "Host"), el("dd", {}, `${d.host}:${d.port}`),
      el("dt", {}, "Secret"), el("dd", {}, r.secret || "(none)"),
      n ? [el("dt", {}, "In play"), el("dd", {}, since(Math.min(...r.cabinets.map((c) => c.since)), d.now))] : null));
}

function howTo(d) {
  const ex = d.rooms[0];
  return el("div", { class: "card stack" },
    el("h2", {}, "How to connect"),
    el("ol", { class: "steps" },
      el("li", {}, "In MegaPPBox, open the cabinet image's settings, then ", el("b", {}, "Network"), "."),
      el("li", {}, "Fit a network card, and set its type to ", el("b", {}, "Remote Switch (Mega-Link over the internet)"), "."),
      el("li", {}, "Remote switch: ", el("code", {}, `${d.host}:${d.port}`), "."),
      el("li", {}, "Secret: the room's secret, above", ex && ex.secret ? [" (for example ", el("code", {}, ex.secret), ")"] : "",
        ". Everyone in a room uses the same one."),
      el("li", {}, "Give the cabinet its own Ethernet IP address in its setup screens: two cabinets with the same one cannot link."),
      el("li", {}, "Start the cabinet and open Mega-Link: the others in the room show up as linked cabinets.")),
    el("p", { class: "muted" }, "Only UDP port ", el("code", {}, String(d.port)), " is used, from your side outwards; ",
      "nothing needs opening on your router. Every cabinet in a room should run the same game release."));
}

async function load() {
  const main = $("#pub");
  let d;
  try {
    const r = await fetch("/api/public/megalink", { cache: "no-store" });
    if (!r.ok) throw new Error(r.status === 404 ? "This server has no public Mega-Link page." : `The server answered ${r.status}.`);
    d = await r.json();
  } catch (e) {
    $("#status").textContent = e.message;
    return;
  }
  const playing = d.rooms.reduce((a, r) => a + r.cabinets.length, 0);
  main.replaceChildren(
    el("header", {},
      el("div", { class: "brand big" }, el("span", { class: "logo" }, "T"),
        el("div", {}, el("b", {}, "Mega-Link"), el("small", {}, "linked play for Megatouch MAXX cabinets"))),
      el("h1", {}, "Mega-Link rooms"),
      el("p", { class: "lead" }, d.intro || "Link emulated Megatouch MAXX cabinets over the internet for head-to-head and linked games.")),
    el("div", { class: "row spread" },
      el("span", { class: `pill ${d.online ? "ok" : "bad"}` }, d.online ? "Switch online" : "Switch offline"),
      el("span", { class: "muted" }, `${playing} cabinet${playing === 1 ? "" : "s"} linked · updated ${new Date(d.now * 1000).toLocaleTimeString()}`)),
    d.rooms.length ? el("div", { class: "rooms" }, d.rooms.map((r) => room(r, d)))
      : el("div", { class: "card empty" }, "No public rooms right now."),
    howTo(d),
    el("footer", {}, "TournaMAXX-Revival · the list refreshes every 15 seconds"));
}

load();
setInterval(load, 15000);
