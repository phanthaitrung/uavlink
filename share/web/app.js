/* uavlink setup page. Plain JavaScript, no framework, no network except the same-origin API. */
(function () {
"use strict";

var LANGS = ["vi", "en"];
var STEPS = ["role", "preset", "settings", "precheck", "apply"];
var KIND_ORDER = ["file", "param", "service", "firewall", "nm", "package", "user", "udev"];
var SECRET_PASSPHRASE = "MAVLINK_SIGNING_PASSPHRASE";
// Check groups by the first part of the check id (ARCHITECTURE.md 4.1); anything else is "other".
var GROUP_ORDER = ["x25", "lan", "vps", "gcs", "cm5", "other"];
var GROUP_OF = { x25: "x25", lan: "lan", eth: "lan", internet: "vps", vps: "vps", gcs: "gcs",
  cm5: "cm5", tailscale: "cm5", router: "cm5", ssh: "cm5", ufw: "cm5", signing: "cm5" };
var STATE_RANK = { fail: 0, warn: 1, ok: 2 };

var S = {
  lang: "vi",
  ui: { vi: {}, en: {} },
  srv: null,
  page: "wizard",          // "wizard" | "status"
  step: 1,
  role: "cm5",
  preset: null,
  values: {},
  secrets: {},
  errors: {},              // client-side, per key
  srvErrors: {},           // from the server, per key
  tab: null,
  propsOff: false,
  pre: null,               // pre-check run
  app: null,               // apply run
  closed: false,
  status: null,            // status page data
  refs: {}
};

/* ---------- helpers ---------- */

function $(sel, root) { return (root || document).querySelector(sel); }

function h(tag, attrs) {
  var el = document.createElement(tag);
  if (attrs) {
    Object.keys(attrs).forEach(function (k) {
      var v = attrs[k];
      if (v === null || v === undefined || v === false) return;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k.slice(0, 2) === "on") el.addEventListener(k.slice(2), v);
      else if (v === true) el.setAttribute(k, "");
      else el.setAttribute(k, v);
    });
  }
  for (var i = 2; i < arguments.length; i++) add(el, arguments[i]);
  return el;
}

function add(el, c) {
  if (c === null || c === undefined || c === false) return;
  if (Array.isArray(c)) { c.forEach(function (x) { add(el, x); }); return; }
  el.appendChild(typeof c === "object" ? c : document.createTextNode(String(c)));
}

function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); }

function fill(str, args) {
  return String(str).replace(/\{(\w+)\}/g, function (m, k) {
    return args && args[k] !== undefined && args[k] !== null ? String(args[k]) : m;
  });
}

/* UI string from vi.json / en.json. Literal keys only: tests/test_i18n_web.py scans the source for calls to t. */
function t(key, args) {
  var s = S.ui[S.lang][key];
  if (s === undefined) s = S.ui.en[key];
  return s === undefined ? key : fill(s, args);
}

/* Dynamic key (family + id) with a fallback text. */
function tt(prefix, id, fallback) {
  var key = prefix + id;
  var s = S.ui[S.lang][key];
  if (s === undefined) s = S.ui.en[key];
  return s === undefined ? (fallback === undefined ? id : fallback) : s;
}

/* Bilingual object {vi, en} from the schema, presets or the server. */
function bl(obj) {
  if (obj === null || obj === undefined) return "";
  if (typeof obj === "string") return obj;
  return obj[S.lang] || obj.en || obj.vi || "";
}

/* Text of an engine event: translated by code, else the English msg. */
function evText(ev) {
  var map = (S.srv && S.srv.i18n && S.srv.i18n.engine) || {};
  if (ev.code && map[ev.code]) return fill(bl(map[ev.code]), ev.args);
  return ev.msg || ev.code || ev.id || "";
}

function pad(n) { return n < 10 ? "0" + n : String(n); }

function fmtTime(s) {
  if (!s) return "";
  var d = new Date(s);
  if (isNaN(d.getTime())) return String(s);
  return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + " " + pad(d.getHours()) + ":" + pad(d.getMinutes());
}

/* ---------- API ---------- */

function api(method, path, body) {
  var opt = { method: method, credentials: "same-origin", headers: {} };
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  return fetch(path, opt).then(function (r) {
    return r.text().then(function (txt) {
      var data = {};
      try { data = txt ? JSON.parse(txt) : {}; } catch (e) { data = {}; }
      if (!r.ok) {
        var err = new Error("http " + r.status);
        err.status = r.status;
        err.data = data || {};
        throw err;
      }
      return data;
    });
  }, function () {
    var err = new Error("network");
    err.status = 0;
    err.data = {};
    throw err;
  });
}

function errText(e) {
  var d = (e && e.data) || {};
  var msg;
  if (e.status === 0) msg = t("err.network");
  else if (e.status === 401) msg = t("err.auth");
  else if (e.status === 409) msg = t("err.busy");
  else if (e.status === 423) msg = t("err.preview_only");
  else if (e.status === 403 || e.status === 415 || e.status === 421) msg = t("err.forbidden", { status: e.status });
  else {
    var key = d.error ? "err." + d.error : "";
    if (key && (S.ui[S.lang][key] || S.ui.en[key])) msg = t(key);
    else if (e.status === 400) msg = t("err.invalid");
    else msg = t("err.http", { status: e.status || "-" });
  }
  if (d.detail && e.status !== 0) msg += " (" + d.detail + ")";
  return msg;
}

function showAlert(text, kind) {
  var el = $("#alert");
  clear(el);
  el.className = "alert " + (kind || "err");
  el.appendChild(document.createTextNode(text));
  el.appendChild(h("button", { type: "button", class: "alert-x", "aria-label": t("btn.close"), text: "×", onclick: hideAlert }));
}

function hideAlert() { var el = $("#alert"); el.className = "alert hidden"; clear(el); }

function showError(e) { showAlert(errText(e), "err"); }

/* ---------- schema, values, validation ---------- */

function schemaKeys(role) {
  var keys = (S.srv.schema && S.srv.schema.keys) || {};
  return Object.keys(keys).filter(function (k) {
    var r = keys[k].roles;
    return !r || r.indexOf(role) >= 0;
  });
}

function keyDef(k) { return S.srv.schema.keys[k]; }

function presetsFor(role) {
  return (S.srv.presets || []).filter(function (p) { return p.role === role; });
}

function sortedPresets(role) {
  var list = presetsFor(role).slice();
  var rank = function (p) { return p.star ? 0 : (p.id === "running" ? 1 : 2); };
  return list.map(function (p, i) { return { p: p, i: i }; }).sort(function (a, b) {
    return rank(a.p) - rank(b.p) || a.i - b.i;
  }).map(function (x) { return x.p; });
}

function findPreset(id) {
  var l = S.srv.presets || [];
  for (var i = 0; i < l.length; i++) if (l[i].id === id) return l[i];
  return null;
}

/* defaults <- preset values; secrets are never part of values. */
function applyPreset(id) {
  var p = findPreset(id);
  S.preset = id;
  S.values = {};
  S.secrets = {};
  S.errors = {};
  S.srvErrors = {};
  schemaKeys(S.role).forEach(function (k) {
    var d = keyDef(k);
    S.values[k] = d.default === undefined || d.default === null ? "" : String(d.default);
  });
  if (p && p.values) {
    Object.keys(p.values).forEach(function (k) {
      if (k in S.values) S.values[k] = String(p.values[k]);
    });
  }
  S.pre = null;
  S.app = null;
}

function isShown(def) {
  var c = def.show_if;
  if (!c) return true;
  return Object.keys(c).every(function (k) { return String(S.values[k]) === String(c[k]); });
}

var RE_HOST = /^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$/;

function isIPv4(s) {
  var p = s.split(".");
  if (p.length !== 4) return false;
  return p.every(function (x) { return /^\d{1,3}$/.test(x) && +x <= 255; });
}

function isCidr(s) {
  var p = s.split("/");
  return p.length === 2 && isIPv4(p[0]) && /^\d{1,2}$/.test(p[1]) && +p[1] <= 32;
}

function tokens(s) { return s.split(/\s+/).filter(Boolean); }

/* Mirror of the schema types; the server validates again. Returns null or {code, args}. */
function checkValue(def, v) {
  v = v === undefined || v === null ? "" : String(v);
  if (v.trim() === "") {
    if (def.allow_empty) return null;
    return def.required ? { code: "required" } : null;
  }
  var bad = { code: "type." + def.type };
  switch (def.type) {
    case "string": return null;
    case "int":
    case "port": {
      if (!/^-?\d+$/.test(v)) return bad;
      var n = parseInt(v, 10);
      var min = def.type === "port" ? 1 : def.min;
      var max = def.type === "port" ? 65535 : def.max;
      if ((min !== undefined && n < min) || (max !== undefined && n > max)) {
        return { code: "range", args: { min: min === undefined ? "-" : min, max: max === undefined ? "-" : max } };
      }
      return null;
    }
    case "bool": return v === "0" || v === "1" ? null : bad;
    case "enum": {
      var ok = (def.options || []).some(function (o) { return String(typeof o === "object" ? o.value : o) === v; });
      return ok ? null : { code: "enum" };
    }
    case "ipv4": return isIPv4(v) ? null : bad;
    case "host": return isIPv4(v) || (v.length <= 253 && RE_HOST.test(v)) ? null : bad;
    case "iface": return /^[A-Za-z0-9_.:@-]{1,32}$/.test(v) ? null : bad;
    case "path": return v.charAt(0) === "/" && !/\s/.test(v) ? null : bad;
    case "cidr_list": return tokens(v).every(isCidr) ? null : bad;
    case "udp_targets": return tokens(v).every(function (x) {
      var m = /^([A-Za-z0-9_.-]+)=([^:]+):(\d+)$/.exec(x);
      return m && isIPv4(m[2]) && +m[3] >= 1 && +m[3] <= 65535;
    }) ? null : bad;
    case "source_list": return v === "any" || tokens(v).every(function (x) { return isIPv4(x) || isCidr(x); }) ? null : bad;
    case "passphrase": return /^[A-Za-z0-9._-]{12,}$/.test(v) ? null : bad;
    default: return null;
  }
}

function checkSecret(def, v) {
  if (v === "") return null;
  return checkValue(def, v);
}

function validateAll() {
  var errs = {};
  schemaKeys(S.role).forEach(function (k) {
    var d = keyDef(k);
    if (!isShown(d)) return;
    var e = d.secret ? checkSecret(d, S.secrets[k] || "") : checkValue(d, S.values[k]);
    if (e) errs[k] = e;
  });
  S.errors = errs;
  return errs;
}

function errMessage(err) {
  var key = "validate." + err.code;
  var s = S.ui[S.lang][key] || S.ui.en[key];
  return s ? fill(s, err.args) : err.code;
}

function errorFor(k) { return S.errors[k] || S.srvErrors[k] || null; }

/* ---------- jobs: event model and SSE ---------- */

function JobView() {
  this.stage = "";
  this.steps = [];
  this.stages = [];
  this.checks = [];
  this.changes = [];
  this.infos = [];
  this.log = [];
  this.logShown = 0;
  this.rollback = null;
  this.confirm = null;
  this.result = null;
  this.state = "running";
  this.lost = false;
}

JobView.prototype.add = function (ev) {
  var i;
  switch (ev.t) {
    case "stage":
      if (ev.state === "start") this.stage = ev.id;
      for (i = 0; i < this.stages.length; i++) if (this.stages[i].id === ev.id) { this.stages[i] = ev; return; }
      this.stages.push(ev);
      break;
    case "step":
      ev.stage = this.stage;
      for (i = 0; i < this.steps.length; i++) {
        if (this.steps[i].id === ev.id && this.steps[i].stage === this.stage) {
          if (ev.state === "start" && this.steps[i].state !== "start") { this.steps.push(ev); return; }
          this.steps[i] = ev;
          return;
        }
      }
      this.steps.push(ev);
      break;
    case "check":
      for (i = 0; i < this.checks.length; i++) if (this.checks[i].id === ev.id) { this.checks[i] = ev; return; }
      this.checks.push(ev);
      break;
    case "change": this.changes.push(ev); break;
    case "info": this.infos.push(ev); break;
    case "log":
      this.log.push(ev.line);
      if (this.log.length > 3000) { this.log.shift(); this.logShown = Math.max(0, this.logShown - 1); }
      break;
    case "rollback": this.rollback = ev; break;
    case "result": this.result = ev; break;
    default: break;
  }
};

JobView.prototype.check = function (id) {
  for (var i = 0; i < this.checks.length; i++) if (this.checks[i].id === id) return this.checks[i];
  return null;
};

/* Follows /api/jobs/<id>/stream. EventSource reconnects by itself and the server replays all events: skip those seen. */
function streamJob(jobId, jv, onChange, onEnd) {
  var seen = 0;
  var pos = 0;
  var finished = false;
  var es = new EventSource("/api/jobs/" + encodeURIComponent(jobId) + "/stream");
  var poller = null;

  function finish(state) {
    if (finished) return;
    finished = true;
    es.close();
    if (poller) clearTimeout(poller);
    jv.state = state || "failed";
    jv.lost = false;
    onChange();
    onEnd(jv.state);
  }

  function poll() {
    poller = null;
    if (finished) return;
    api("GET", "/api/jobs/" + encodeURIComponent(jobId)).then(function (j) {
      var evs = j.events || [];
      for (; seen < evs.length; seen++) jv.add(evs[seen]);
      jv.lost = false;
      onChange();
      if (j.state && j.state !== "running") finish(j.state);
      else poller = setTimeout(poll, 2000);
    }, function () {
      jv.lost = true;
      onChange();
      poller = setTimeout(poll, 3000);
    });
  }

  es.onopen = function () { pos = 0; jv.lost = false; if (poller) { clearTimeout(poller); poller = null; } };
  es.onmessage = function (m) {
    pos++;
    if (pos <= seen) return;
    seen = pos;
    var ev;
    try { ev = JSON.parse(m.data); } catch (e) { return; }
    jv.add(ev);
    if (onChange.ev) onChange.ev(ev);
    onChange();
  };
  es.addEventListener("end", function (m) {
    var st = "failed";
    try { st = JSON.parse(m.data).state || "failed"; } catch (e) { st = "failed"; }
    finish(st);
  });
  es.onerror = function () {
    if (finished) return;
    jv.lost = true;
    onChange();
    if (es.readyState === 2 && !poller) poller = setTimeout(poll, 1500);
  };
  return { close: function () { finished = true; es.close(); if (poller) clearTimeout(poller); } };
}

/* Throttled re-render of the live parts of a run. */
function liveUpdater(jv, run, paint) {
  var queued = false;
  var f = function () {
    if (queued) return;
    queued = true;
    requestAnimationFrame(function () { queued = false; paint(jv, run); });
  };
  return f;
}

/* ---------- small UI parts ---------- */

function badge(text, kind) { return h("span", { class: "badge " + (kind || ""), text: text }); }

function stateIcon(st) {
  var map = { ok: "✓", fail: "✗", warn: "!", skip: "–", start: "…", running: "…" };
  return h("span", { class: "ico " + st, "aria-hidden": "true", text: map[st] || "·" });
}

function card(title, body, extra) {
  return h("section", { class: "panel" + (extra ? " " + extra : "") }, title ? h("h3", { text: title }) : null, body);
}

function note(kind, text) { return h("div", { class: "note " + kind, text: text }); }

function kv(rows) {
  var dl = h("dl", { class: "kv" });
  rows.forEach(function (r) {
    if (r[1] === "" || r[1] === null || r[1] === undefined) return;
    dl.appendChild(h("dt", { text: r[0] }));
    dl.appendChild(typeof r[1] === "object" ? h("dd", null, r[1]) : h("dd", { text: r[1] }));
  });
  return dl;
}

function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
  return new Promise(function (resolve, reject) {
    var ta = h("textarea", { class: "sr-only", "aria-hidden": "true" });
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    document.body.removeChild(ta);
    if (ok) resolve(); else reject(new Error("copy"));
  });
}

function copyButton(getText) {
  var btn = h("button", { type: "button", class: "btn small", text: t("btn.copy") });
  btn.addEventListener("click", function () {
    copyText(getText()).then(function () {
      btn.textContent = t("btn.copied");
      setTimeout(function () { btn.textContent = t("btn.copy"); }, 1500);
    }, function () { showAlert(t("err.copy"), "warn"); });
  });
  return btn;
}

function downloadText(name, text) {
  var url = URL.createObjectURL(new Blob([text], { type: "text/plain;charset=utf-8" }));
  var a = h("a", { href: url, download: name, class: "sr-only" });
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(function () { URL.revokeObjectURL(url); }, 2000);
}

function qrSvg(text) {
  var NS = "http://www.w3.org/2000/svg";
  var qr = qrcodegen.QrCode.encodeText(text, qrcodegen.QrCode.Ecc.MEDIUM);
  var border = 3;
  var size = qr.size + border * 2;
  var d = [];
  for (var y = 0; y < qr.size; y++) {
    for (var x = 0; x < qr.size; x++) {
      if (qr.getModule(x, y)) d.push("M" + (x + border) + "," + (y + border) + "h1v1h-1z");
    }
  }
  var svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 " + size + " " + size);
  svg.setAttribute("class", "qr");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", t("qr.alt"));
  var bg = document.createElementNS(NS, "rect");
  bg.setAttribute("width", size);
  bg.setAttribute("height", size);
  bg.setAttribute("fill", "#ffffff");
  var path = document.createElementNS(NS, "path");
  path.setAttribute("d", d.join(""));
  path.setAttribute("fill", "#000000");
  svg.appendChild(bg);
  svg.appendChild(path);
  return svg;
}

/* ---------- job rendering (shared by pre-check, apply, verify, restore) ---------- */

function stageLabel(id) { return tt("stage.", id); }

/* UI name of a step, else the engine's text for "step.<id>" or "<id>" (x25 and net steps), else the id. */
function stepLabel(id) {
  var map = (S.srv && S.srv.i18n && S.srv.i18n.engine) || {};
  var e = map["step." + id] || map[id];
  return tt("stepname.", id, e ? bl(e) : id);
}

function renderProgress(jv) {
  var box = h("div", { class: "progress-list" });
  if (!jv.steps.length && !jv.stages.length) {
    box.appendChild(h("p", { class: "muted", text: jv.state === "running" ? t("job.waiting") : t("job.no_steps") }));
    return box;
  }
  var order = [];
  jv.stages.forEach(function (s) { order.push(s.id); });
  jv.steps.forEach(function (s) { if (order.indexOf(s.stage) < 0) order.push(s.stage); });
  order.forEach(function (sid) {
    var stg = null;
    jv.stages.forEach(function (s) { if (s.id === sid) stg = s; });
    var st = stg ? stg.state : "start";
    var group = h("div", { class: "stage" });
    if (sid) group.appendChild(h("div", { class: "stage-h" }, stateIcon(st), h("strong", { text: stageLabel(sid) }),
      st === "skip" ? badge(t("state.skip"), "muted") : null));
    var ul = h("ul", { class: "steps" });
    jv.steps.forEach(function (s) {
      if (s.stage !== sid) return;
      var text = evText(s);
      ul.appendChild(h("li", { class: "step " + s.state },
        stateIcon(s.state), h("span", { class: "step-n", text: stepLabel(s.id) }),
        s.state === "fail" || s.state === "warn" || s.state === "skip" ? h("span", { class: "step-msg", text: text }) : null));
    });
    if (ul.children.length) group.appendChild(ul);
    box.appendChild(group);
  });
  return box;
}

/* "ok", "warn" (failed, but only a warning: severity "warn") or "fail". */
function checkState(c) {
  if (c.ok === true) return "ok";
  return c.severity === "warn" || c.warn || c.ok === "warn" ? "warn" : "fail";
}

function countChecks(checks) {
  var n = { ok: 0, warn: 0, fail: 0 };
  checks.forEach(function (c) { n[checkState(c)]++; });
  return n;
}

/* Checks grouped by area: failures first inside a group, groups with failures (then warnings) first. */
function checkGroups(checks) {
  var by = {};
  checks.forEach(function (c, i) {
    var id = GROUP_OF[String(c.id || "").split(".")[0]] || "other";
    var g = by[id] || (by[id] = { id: id, rows: [], n: { ok: 0, warn: 0, fail: 0 } });
    var st = checkState(c);
    g.rows.push({ c: c, st: st, i: i });
    g.n[st]++;
  });
  var rank = function (g) { return g.n.fail ? 0 : (g.n.warn ? 1 : 2); };
  return Object.keys(by).map(function (k) {
    by[k].rows.sort(function (a, b) { return STATE_RANK[a.st] - STATE_RANK[b.st] || a.i - b.i; });
    return by[k];
  }).sort(function (a, b) { return rank(a) - rank(b) || GROUP_ORDER.indexOf(a.id) - GROUP_ORDER.indexOf(b.id); });
}

function checkCounts(n) {
  return h("span", { class: "counts" },
    badge(t("checks.n_ok", { n: n.ok }), n.ok ? "ok" : "zero"),
    badge(t("checks.n_warn", { n: n.warn }), n.warn ? "warn" : "zero"),
    badge(t("checks.n_fail", { n: n.fail }), n.fail ? "fail" : "zero"));
}

function renderChecks(jv) {
  if (!jv.checks.length) return h("p", { class: "muted", text: t("checks.none") });
  var box = h("div", { class: "cgroups" });
  checkGroups(jv.checks).forEach(function (g) {
    var tb = h("tbody");
    g.rows.forEach(function (r) {
      tb.appendChild(h("tr", { class: "row-" + r.st },
        h("td", { class: "c-state" }, stateIcon(r.st), h("span", { class: "sr-only", text: tt("state.", r.st) })),
        h("td", { text: evText(r.c) }),
        h("td", { class: "c-id" }, h("code", { text: r.c.id }))));
    });
    box.appendChild(h("div", { class: "cgroup" },
      h("div", { class: "cgroup-h" }, h("h4", { text: tt("group.", g.id) }), checkCounts(g.n)),
      h("div", { class: "table-wrap" }, h("table", { class: "tbl" }, tb))));
  });
  return box;
}

/* ---------- X25 status panel (info event x25.status, ARCHITECTURE.md 4.3) ---------- */

function x25Status(jv) {
  for (var i = jv.infos.length - 1; i >= 0; i--) {
    var ev = jv.infos[i];
    if (ev.code === "x25.status" && ev.data && typeof ev.data === "object") return ev.data;
  }
  return null;
}

function num(v, digits) {
  if (v === null || v === undefined || v === "" || !isFinite(Number(v))) return null;
  return Number(v).toFixed(digits);
}

function gpsFix(f) {
  if (f === null || f === undefined || f === "") return "";
  if (typeof f !== "number") return String(f).replace(/^GPS_FIX_TYPE_/, "");
  if (f <= 0) return t("x25s.fix_nogps");
  if (f === 1) return t("x25s.fix_none");
  return ["", "", "2D", "3D", "DGPS", "RTK float", "RTK fixed", "static", "PPP"][f] || String(f);
}

function sensorState(s) {
  if (s.present === false) return { kind: "zero", text: t("x25s.absent") };
  if (s.enabled === false) return { kind: "zero", text: t("x25s.disabled") };
  return s.healthy ? { kind: "ok", text: t("x25s.healthy") } : { kind: "fail", text: t("x25s.unhealthy") };
}

function renderSensors(list) {
  if (!Array.isArray(list) || !list.length) return null;
  var rank = { fail: 0, ok: 1, zero: 2 };
  var items = list.filter(function (s) { return s && typeof s === "object"; }).map(function (s, i) {
    return { s: s, st: sensorState(s), i: i };
  }).sort(function (a, b) { return rank[a.st.kind] - rank[b.st.kind] || a.i - b.i; });
  return h("div", { class: "sensors" }, items.map(function (x) {
    var id = String(x.s.id || "");
    return badge(tt("sensor.", id.toLowerCase(), id) + ": " + x.st.text, x.st.kind);
  }));
}

function netEntries(net) {
  var out = [];
  if (Array.isArray(net)) {
    net.forEach(function (x) { if (x && typeof x === "object") out.push([String(x.name || x.id || ""), x.value]); });
  } else if (net && typeof net === "object") {
    Object.keys(net).sort().forEach(function (k) { out.push([k, net[k]]); });
  }
  return out;
}

function renderX25Status(jv) {
  var d = x25Status(jv);
  if (!d) return null;
  var gps = d.gps || {};
  var bat = d.battery || {};
  var ekf = d.ekf || {};
  var v = Number(bat.voltage) > 0 ? num(bat.voltage, 2) : null;  // 0.0: the board has no battery monitor
  var a = bat.current === null || bat.current === undefined || bat.current < 0 ? null : num(bat.current, 1);
  var pct = bat.remaining === null || bat.remaining === undefined || bat.remaining < 0 ? null : num(bat.remaining, 0);
  var hdop = num(gps.hdop, 2);
  var gpsText = [gpsFix(gps.fix), gps.sats === null || gps.sats === undefined ? "" : t("x25s.sats", { n: gps.sats }),
    hdop === null ? "" : "HDOP " + hdop].filter(Boolean).join(" · ");
  var batText = [v === null ? "" : v + " V", a === null ? "" : a + " A", pct === null ? "" : pct + "%"].filter(Boolean).join(" · ");
  var ekfNode = null;
  if (typeof ekf.ok === "boolean") {
    ekfNode = h("span", null, badge(ekf.ok ? t("x25s.healthy") : t("x25s.unhealthy"), ekf.ok ? "ok" : "fail"),
      typeof ekf.flags === "number" ? h("code", { class: "muted", text: " 0x" + ekf.flags.toString(16).toUpperCase() }) : null);
  }
  var armed = d.armed === true ? badge(t("x25s.armed_yes"), "fail") : (d.armed === false ? badge(t("x25s.armed_no"), "ok") : null);
  var prearm = Array.isArray(d.prearm) ? (d.prearm.length
    ? h("ul", { class: "prearm" }, d.prearm.map(function (m) { return h("li", { text: String(m) }); }))
    : t("x25s.prearm_none")) : null;
  var body = h("div", null, kv([
    [t("x25s.firmware"), d.firmware ? String(d.firmware) : ""],
    [t("x25s.board"), d.board ? String(d.board) : ""],
    [t("x25s.vehicle"), d.vehicle ? String(d.vehicle) : ""],
    [t("x25s.system_status"), d.system_status === undefined || d.system_status === null ? "" : String(d.system_status)],
    [t("x25s.armed"), armed],
    [t("x25s.sensors"), renderSensors(d.sensors)],
    [t("x25s.gps"), gpsText],
    [t("x25s.battery"), batText],
    [t("x25s.ekf"), ekfNode],
    [t("x25s.prearm"), prearm]
  ]));
  var net = netEntries(d.net);
  if (net.length) {
    body.appendChild(h("h4", { text: t("x25s.net") }));
    body.appendChild(h("div", { class: "netp" }, net.map(function (p) {
      return h("div", { class: "np" }, h("code", { text: p[0] }), h("span", { text: p[1] === null || p[1] === undefined ? "" : String(p[1]) }));
    })));
  }
  return card(t("x25s.title"), body, "x25s");
}

function diffBlock(diff) {
  var pre = h("pre", { class: "diff" });
  String(diff).split("\n").forEach(function (line) {
    var cls = line.charAt(0) === "+" && line.slice(0, 3) !== "+++" ? "add" : (line.charAt(0) === "-" && line.slice(0, 3) !== "---" ? "del" : (line.charAt(0) === "@" ? "hunk" : ""));
    pre.appendChild(h("span", { class: "dl " + cls, text: line + "\n" }));
  });
  return pre;
}

var openDiffs = {};

function renderChanges(jv) {
  if (!jv.changes.length) return h("p", { class: "muted", text: t("changes.none") });
  var box = h("div", { class: "changes" });
  var kinds = KIND_ORDER.slice();
  jv.changes.forEach(function (c) { if (kinds.indexOf(c.kind) < 0) kinds.push(c.kind); });
  kinds.forEach(function (kind) {
    var list = jv.changes.filter(function (c) { return c.kind === kind; });
    if (!list.length) return;
    var sec = h("div", { class: "kind" }, h("h4", { text: tt("kind.", kind) + " (" + list.length + ")" }));
    if (kind === "param") {
      var tb = h("tbody");
      list.forEach(function (c) {
        tb.appendChild(h("tr", null, h("td", null, h("code", { text: c.target })),
          h("td", { class: "old", text: c.old === undefined ? "" : String(c.old) }),
          h("td", { class: "arrow", text: "→" }),
          h("td", { class: "new", text: c.new === undefined ? "" : String(c.new) })));
      });
      sec.appendChild(h("div", { class: "table-wrap" }, h("table", { class: "tbl params" },
        h("thead", null, h("tr", null, h("th", { text: t("changes.param") }), h("th", { text: t("changes.old") }), h("th"), h("th", { text: t("changes.new") }))), tb)));
    } else {
      list.forEach(function (c) {
        var head = [badge(tt("action.", c.action), "act-" + c.action), h("code", { text: c.target })];
        if (c.diff) {
          var dkey = c.kind + "|" + c.target;
          var det = h("details", { class: "chg", open: openDiffs[dkey] ? true : null }, h("summary", null, head), diffBlock(c.diff));
          det.addEventListener("toggle", function () { openDiffs[dkey] = det.open; });
          sec.appendChild(det);
        } else {
          sec.appendChild(h("div", { class: "chg flat" }, head, c.msg ? h("span", { class: "muted", text: " " + c.msg }) : null));
        }
      });
    }
    box.appendChild(sec);
  });
  return box;
}

function renderLog(jv, pre) {
  var near = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
  if (jv.logShown > jv.log.length) { clear(pre); jv.logShown = 0; }
  if (jv.logShown === 0) clear(pre);
  var frag = document.createDocumentFragment();
  for (; jv.logShown < jv.log.length; jv.logShown++) frag.appendChild(document.createTextNode(jv.log[jv.logShown] + "\n"));
  pre.appendChild(frag);
  if (near) pre.scrollTop = pre.scrollHeight;
}

function renderRollback(jv) {
  if (!jv.rollback) return null;
  var r = jv.rollback;
  if (r.state === "armed") {
    var c = jv.confirm;
    var txt = t("rollback.armed", { secs: r.secs });
    if (c && c.state === "ok") return note("ok", txt + " " + t("rollback.auto_ok"));
    if (c && c.state === "fail") return note("warn", txt + " " + t("rollback.auto_fail", { msg: c.msg }));
    return note("warn", txt + " " + t("rollback.auto_sending"));
  }
  if (r.state === "confirmed") return note("ok", t("rollback.confirmed"));
  if (r.state === "fired") return note("err", t("rollback.fired"));
  return null;
}

function renderInfos(jv) {
  var list = jv.infos.filter(function (i) { return i.code !== "x25.status"; });
  if (!list.length) return null;
  return h("ul", { class: "infos" }, list.map(function (i) { return h("li", { text: evText(i) }); }));
}

function startJob(jobId, jv, paint) {
  var run = { jv: jv };
  var up = liveUpdater(jv, run, paint);
  up.ev = function (ev) {
    if (ev.t === "rollback" && ev.state === "armed") autoConfirm(jv, ev.secs, up);
  };
  run.stream = streamJob(jobId, jv, up, function (state) {
    run.ended = true;
    if (run.onEnd) run.onEnd(state);
  });
  return run;
}

/* The page may still reach the server after the network change: confirm the rollback timer by itself. */
function autoConfirm(jv, secs, update) {
  var tries = 0;
  var max = Math.max(3, Math.floor((secs || 60) / 3));
  jv.confirm = { state: "sending" };
  function attempt() {
    tries++;
    api("POST", "/api/confirm", {}).then(function () {
      jv.confirm = { state: "ok" };
      update();
    }, function (e) {
      jv.confirm = { state: "fail", msg: errText(e) };
      update();
      if (tries < max) setTimeout(attempt, 3000);
    });
  }
  attempt();
}

/* ---------- page: stepper and header ---------- */

function renderChrome() {
  document.documentElement.lang = S.lang;
  document.title = t("app.title");
  Array.prototype.forEach.call(document.querySelectorAll("#lang-toggle button"), function (b) {
    b.setAttribute("aria-pressed", b.getAttribute("data-lang") === S.lang ? "true" : "false");
  });
  var nav = $("#nav-page");
  nav.textContent = S.page === "status" ? t("nav.wizard") : t("nav.status");
  var hl = $("#host-line");
  if (S.srv) {
    var ips = (S.srv.host && S.srv.host.ips) || [];
    hl.textContent = [(S.srv.host && S.srv.host.hostname) || "", ips.join(", ")].filter(Boolean).join(" · ");
  }
  var st = $("#stepper");
  clear(st);
  if (S.page !== "wizard") { st.className = "stepper hidden"; return; }
  st.className = "stepper";
  STEPS.forEach(function (name, i) {
    var n = i + 1;
    var cls = n === S.step ? "current" : (n < S.step ? "past" : "");
    var li = h("li", { class: cls });
    var label = h("span", { class: "lbl", text: tt("step.", name + ".name") });
    var content = [h("span", { class: "num", text: String(n) }), label];
    if (n < S.step && !(S.app && S.app.jv && S.app.jv.state === "running")) {
      li.appendChild(h("a", { href: "#step=" + n, "aria-label": tt("step.", name + ".name") }, content));
    } else {
      li.appendChild(h("span", { class: "plain", "aria-current": n === S.step ? "step" : null }, content));
    }
    st.appendChild(li);
  });
}

function footer(back, next) {
  var bar = h("div", { class: "nav-bar" });
  if (back) bar.appendChild(back); else bar.appendChild(h("span"));
  if (next) bar.appendChild(next);
  return bar;
}

function navBtn(label, cls, fn, disabled) {
  return h("button", { type: "button", class: "btn " + cls, text: label, onclick: fn, disabled: disabled ? true : null });
}

/* ---------- step 1: role ---------- */

function renderRole(root) {
  root.appendChild(h("h2", { text: t("step.role.title") }));
  root.appendChild(h("p", { class: "intro", text: t("step.role.intro") }));
  if (S.srv.preview_only) root.appendChild(note("warn", t("banner.preview_only")));
  var grid = h("div", { class: "cards", role: "radiogroup", "aria-label": t("step.role.title") });
  ["cm5", "vps"].forEach(function (role) {
    var sel = S.role === role;
    var c = h("button", {
      type: "button", class: "card choice" + (sel ? " selected" : ""), role: "radio",
      "aria-checked": sel ? "true" : "false",
      onclick: function () { chooseRole(role); }
    },
      h("span", { class: "card-t" }, h("strong", { text: tt("role.", role + ".name") }),
        S.srv.role_guess === role ? badge(t("role.detected"), "ok") : null),
      h("span", { class: "card-d", text: tt("role.", role + ".desc") }),
      role === "vps" ? note("info", t("role.vps.later")) : null);
    grid.appendChild(c);
  });
  root.appendChild(grid);
  root.appendChild(footer(null, navBtn(t("btn.continue"), "primary", function () { go(2); })));
}

function chooseRole(role) {
  if (role !== S.role) {
    S.role = role;
    S.preset = null;
    S.tab = null;
    S.pre = null;
    S.app = null;
  }
  render();
}

/* ---------- step 2: preset ---------- */

function renderPreset(root) {
  root.appendChild(h("h2", { text: t("step.preset.title") }));
  root.appendChild(h("p", { class: "intro", text: t("step.preset.intro") }));
  var list = sortedPresets(S.role);
  var grid = h("div", { class: "cards", role: "radiogroup", "aria-label": t("step.preset.title") });
  if (!list.length) grid.appendChild(h("p", { class: "muted", text: t("preset.none") }));
  list.forEach(function (p) {
    var sel = S.preset === p.id;
    var name = p.id === "running" ? t("preset.running") : bl(p.name);
    var c = h("button", {
      type: "button", class: "card choice" + (sel ? " selected" : "") + (p.warning ? " warn" : ""), role: "radio",
      "aria-checked": sel ? "true" : "false",
      onclick: function () { if (S.preset !== p.id) { applyPreset(p.id); S.tab = null; } render(); }
    },
      h("span", { class: "card-t" }, h("strong", { text: name }),
        p.star ? badge("★ " + t("preset.recommended"), "star") : null,
        p.warning ? badge("⚠ " + t("preset.warning"), "warn") : null),
      h("span", { class: "card-d", text: bl(p.desc) }),
      p.warning ? h("span", { class: "card-w", text: bl(p.warning) }) : null,
      p.ask && p.ask.length ? h("span", { class: "card-a", text: t("preset.ask", { n: p.ask.length }) }) : null);
    grid.appendChild(c);
  });
  root.appendChild(grid);
  root.appendChild(footer(navBtn(t("btn.back"), "", function () { go(1); }),
    navBtn(t("btn.continue"), "primary", function () { go(3); }, !S.preset)));
}

/* ---------- step 3: settings ---------- */

function tabsFor(role) {
  var keys = schemaKeys(role);
  var used = {};
  keys.forEach(function (k) { used[keyDef(k).tab] = true; });
  var tabs = ((S.srv.schema && S.srv.schema.tabs) || []).filter(function (tb) { return used[tb.id]; });
  Object.keys(used).forEach(function (id) {
    if (!tabs.some(function (tb) { return tb.id === id; })) tabs.push({ id: id, label: { vi: id, en: id } });
  });
  return tabs;
}

function optionLabel(o) {
  if (typeof o !== "object") return String(o);
  return o.label ? bl(o.label) : String(o.value);
}

function detectedChips(k, def, setter) {
  var det = S.srv.detect || {};
  var vals = [];
  var label = "";
  if (def.type === "iface") {
    vals = (det.ifaces || []).map(function (i) { return typeof i === "string" ? i : i.name; });
    label = t("field.detected_ifaces");
  } else if (def.type === "path" && /(PORT|DEV|SERIAL)/.test(k) && det.x25_ports && det.x25_ports.length) {
    vals = det.x25_ports;
    label = t("field.detected_x25");
  }
  if (!vals.length) return null;
  var wrap = h("div", { class: "chips" }, h("span", { class: "muted", text: label + " " }));
  vals.forEach(function (v) {
    wrap.appendChild(h("button", { type: "button", class: "chip", text: v, onclick: function () { setter(v); } }));
  });
  return wrap;
}

function buildField(k) {
  var def = keyDef(k);
  var id = "f-" + k;
  var isAsk = (findPreset(S.preset) || {}).ask && findPreset(S.preset).ask.indexOf(k) >= 0;
  var input;
  var setInput;
  var row = h("div", { class: "field" + (isAsk ? " ask" : ""), "data-key": k });

  if (def.secret) {
    var present = S.srv.current && S.srv.current.secrets_present && S.srv.current.secrets_present[k];
    input = h("input", {
      id: id, type: "password", autocomplete: "new-password", spellcheck: "false",
      // only the signing passphrase is generated when left empty (config.save)
      placeholder: present ? t("field.secret_keep") : (def.type === "passphrase" ? t("field.secret_generate") : null),
      value: S.secrets[k] || ""
    });
    input.addEventListener("input", function () { S.secrets[k] = input.value; afterChange(k); });
    setInput = function (v) { input.value = v; S.secrets[k] = v; };
  } else if (def.type === "bool") {
    input = h("input", { id: id, type: "checkbox", class: "switch" });
    input.checked = S.values[k] === "1";
    input.addEventListener("change", function () { S.values[k] = input.checked ? "1" : "0"; afterChange(k); });
    setInput = function (v) { input.checked = v === "1"; S.values[k] = v; };
  } else if (def.type === "enum") {
    input = h("select", { id: id });
    (def.options || []).forEach(function (o) {
      var ov = String(typeof o === "object" ? o.value : o);
      var opt = h("option", { value: ov, text: optionLabel(o) });
      input.appendChild(opt);
    });
    input.value = S.values[k];
    input.addEventListener("change", function () { S.values[k] = input.value; afterChange(k); });
    setInput = function (v) { input.value = v; S.values[k] = v; };
  } else {
    var isNum = def.type === "int" || def.type === "port";
    input = h("input", {
      id: id, type: isNum ? "number" : "text", autocomplete: "off", spellcheck: "false",
      inputmode: isNum ? "numeric" : null,
      min: def.type === "port" ? 1 : (def.min !== undefined ? def.min : null),
      max: def.type === "port" ? 65535 : (def.max !== undefined ? def.max : null),
      placeholder: def.placeholder ? (typeof def.placeholder === "object" ? bl(def.placeholder) : def.placeholder) : null
    });
    input.value = S.values[k];
    input.addEventListener("input", function () { S.values[k] = input.value; afterChange(k); });
    setInput = function (v) { input.value = v; S.values[k] = v; afterChange(k); };
  }

  var errEl = h("div", { class: "ferr", id: id + "-err", role: "status" });
  input.setAttribute("aria-describedby", id + "-err");
  var labelEl = h("label", { for: id }, bl(def.label), def.required ? h("span", { class: "req", "aria-hidden": "true", text: " *" }) : null,
    isAsk ? badge(t("field.ask"), "ask") : null, def.secret ? badge(t("field.secret"), "muted") : null);

  if (def.type === "bool") {
    row.appendChild(h("div", { class: "bool-row" }, input, labelEl));
  } else {
    row.appendChild(labelEl);
    row.appendChild(input);
  }
  if (def.help) row.appendChild(h("div", { class: "help", text: bl(def.help) }));
  if (!def.secret) {
    var chips = detectedChips(k, def, setInput);
    if (chips) row.appendChild(chips);
  }
  row.appendChild(errEl);
  S.refs.fields[k] = { row: row, err: errEl, input: input, def: def };
  return row;
}

function paintField(k) {
  var f = S.refs.fields && S.refs.fields[k];
  if (!f) return;
  var e = errorFor(k);
  f.err.textContent = e ? errMessage(e) : "";
  f.row.classList.toggle("invalid", !!e);
  if (e) f.input.setAttribute("aria-invalid", "true"); else f.input.removeAttribute("aria-invalid");
}

function paintSettingsState() {
  var f = S.refs.fields || {};
  var perTab = {};
  Object.keys(f).forEach(function (k) {
    var shown = isShown(f[k].def);
    f[k].row.classList.toggle("hidden", !shown);
    paintField(k);
    if (shown && errorFor(k)) perTab[f[k].def.tab] = (perTab[f[k].def.tab] || 0) + 1;
  });
  var tabs = S.refs.tabbtns || {};
  Object.keys(tabs).forEach(function (id) {
    var n = perTab[id] || 0;
    var b = tabs[id].querySelector(".tab-err");
    if (b) { b.textContent = n ? String(n) : ""; b.classList.toggle("hidden", !n); }
  });
  var adv = S.refs.advs || {};
  Object.keys(adv).forEach(function (id) {
    var any = Array.prototype.some.call(adv[id].querySelectorAll(".field"), function (r) { return r.classList.contains("invalid") && !r.classList.contains("hidden"); });
    if (any) adv[id].open = true;
  });
}

var validateTimer = null;

function afterChange(k) {
  var d = keyDef(k);
  delete S.srvErrors[k];
  var e = d.secret ? checkSecret(d, S.secrets[k] || "") : checkValue(d, S.values[k]);
  if (e) S.errors[k] = e; else delete S.errors[k];
  S.pre = null;
  S.app = null;
  paintSettingsState();
  clearTimeout(validateTimer);
  validateTimer = setTimeout(serverValidate, 600);
}

function serverValidate() {
  if (S.page !== "wizard" || S.step !== 3) return Promise.resolve(null);
  return api("POST", "/api/validate", { role: S.role, values: S.values }).then(function (r) {
    S.srvErrors = r.errors || {};
    paintSettingsState();
    return r;
  }, function (e) { showError(e); return null; });
}

function renderSettings(root) {
  root.appendChild(h("h2", { text: t("step.settings.title") }));
  root.appendChild(h("p", { class: "intro", text: t("step.settings.intro") }));
  var preset = findPreset(S.preset);
  if (preset && preset.warning) root.appendChild(note("warn", bl(preset.warning)));
  if (preset && preset.ask && preset.ask.length) root.appendChild(note("info", t("settings.ask_note")));

  S.refs = { fields: {}, tabbtns: {}, advs: {} };
  var tabs = tabsFor(S.role);
  if (!S.tab || !tabs.some(function (tb) { return tb.id === S.tab; })) S.tab = tabs.length ? tabs[0].id : null;

  var tabbar = h("div", { class: "tabs", role: "tablist" });
  var panels = h("div", { class: "tabpanels" });
  tabs.forEach(function (tb) {
    var keys = schemaKeys(S.role).filter(function (k) { return keyDef(k).tab === tb.id; });
    var hasAsk = preset && preset.ask && keys.some(function (k) { return preset.ask.indexOf(k) >= 0; });
    var btn = h("button", {
      type: "button", role: "tab", id: "tab-" + tb.id, "aria-controls": "panel-" + tb.id,
      "aria-selected": tb.id === S.tab ? "true" : "false", class: "tab" + (tb.id === S.tab ? " active" : ""),
      onclick: function () { S.tab = tb.id; render(); }
    }, bl(tb.label), hasAsk ? h("span", { class: "dot", title: t("field.ask") }) : null, h("span", { class: "tab-err hidden" }));
    btn.addEventListener("keydown", function (ev) {
      var i = tabs.indexOf(tb);
      var j = ev.key === "ArrowRight" ? i + 1 : (ev.key === "ArrowLeft" ? i - 1 : -1);
      if (j < 0 || j >= tabs.length) return;
      S.tab = tabs[j].id;
      render();
      var nb = $("#tab-" + tabs[j].id);
      if (nb) nb.focus();
    });
    tabbar.appendChild(btn);
    S.refs.tabbtns[tb.id] = btn;

    var panel = h("div", { role: "tabpanel", id: "panel-" + tb.id, "aria-labelledby": "tab-" + tb.id, class: "tabpanel" + (tb.id === S.tab ? "" : " hidden") });
    var basic = h("div", { class: "fields" });
    var adv = h("div", { class: "fields" });
    keys.forEach(function (k) { (keyDef(k).advanced ? adv : basic).appendChild(buildField(k)); });
    panel.appendChild(basic);
    if (adv.children.length) {
      var det = h("details", { class: "adv" }, h("summary", { text: t("settings.advanced") + " (" + adv.children.length + ")" }), adv);
      S.refs.advs[tb.id] = det;
      if (hasAsk && Array.prototype.some.call(adv.children, function (r) { return r.classList.contains("ask"); })) det.open = true;
      panel.appendChild(det);
    }
    panels.appendChild(panel);
  });
  root.appendChild(tabbar);
  root.appendChild(panels);
  paintSettingsState();

  root.appendChild(footer(navBtn(t("btn.back"), "", function () { go(2); }),
    navBtn(t("btn.continue"), "primary", continueFromSettings)));
}

function firstErrorTab() {
  var keys = Object.keys(S.errors).concat(Object.keys(S.srvErrors));
  for (var i = 0; i < keys.length; i++) {
    var d = S.srv.schema.keys[keys[i]];
    if (d && isShown(d)) return d.tab;
  }
  return null;
}

function continueFromSettings() {
  hideAlert();
  validateAll();
  var local = Object.keys(S.errors).length;
  serverValidate().then(function (r) {
    if (local || (r && (!r.ok || Object.keys(r.errors || {}).length))) {
      var tb = firstErrorTab();
      if (tb) S.tab = tb;
      render();
      showAlert(t("settings.fix_errors"), "err");
      return;
    }
    if (r) go(4);
  });
}

/* ---------- step 4: pre-check ---------- */

function x25Present() {
  var det = S.srv.detect || {};
  if (det.x25_ports && det.x25_ports.length) return true;
  var jv = S.pre && S.pre.jv;
  var c = jv && jv.check("x25.usb");
  return !!(c && c.ok === true);
}

function x25Armed() {
  var jv = S.pre && S.pre.jv;
  var c = jv && jv.check("x25.disarmed");
  return !!(c && c.ok === false);
}

function startPrecheck() {
  validateAll();
  if (Object.keys(S.errors).length) { go(3); return; }
  var jv = new JobView();
  var run = { jv: jv, starting: true };
  S.pre = run;
  S.app = null;
  render();
  api("POST", "/api/precheck", { role: S.role, values: S.values }).then(function (r) {
    if (S.pre !== run) return;
    run.starting = false;
    run.job = r.job;
    var live = startJob(r.job, jv, function () { if (S.pre === run) paintPrecheck(); });
    run.stream = live.stream;
    live.onEnd = function () { if (S.pre === run) { paintPrecheck(); renderChrome(); } };
    paintPrecheck();
  }, function (e) {
    if (S.pre !== run) return;
    run.starting = false;
    jv.state = "failed";
    run.error = errText(e);
    if (e.data && e.data.errors) { S.srvErrors = e.data.errors; }
    render();
  });
}

function renderPrecheck(root) {
  root.appendChild(h("h2", { text: t("step.precheck.title") }));
  root.appendChild(h("p", { class: "intro", text: t("step.precheck.intro") }));
  if (!S.pre) { startPrecheck(); return; }
  S.refs = { pre: h("div", { class: "pre-live" }) };
  root.appendChild(S.refs.pre);
  var bar = footer(navBtn(t("btn.back"), "", function () { go(3); }), null);
  S.refs.preBar = bar;
  root.appendChild(bar);
  paintPrecheck();
}

function paintPrecheck() {
  var live = S.refs.pre;
  var run = S.pre;
  if (!live || !run) return;
  var jv = run.jv;
  clear(live);
  var running = jv.state === "running";
  if (S.srv.preview_only) live.appendChild(note("warn", t("banner.preview_only")));
  if (run.error) live.appendChild(note("err", run.error));
  if (run.starting || (running && !run.error)) {
    live.appendChild(h("p", { class: "busy" }, h("span", { class: "spin", "aria-hidden": "true" }), " " + t("job.running")));
  }
  if (jv.lost) live.appendChild(note("warn", t("job.lost")));
  live.appendChild(card(t("precheck.progress"), renderProgress(jv)));
  live.appendChild(card(t("precheck.checks"), renderChecks(jv)));
  add(live, renderX25Status(jv));
  live.appendChild(card(t("precheck.changes"), renderChanges(jv)));
  var inf = renderInfos(jv);
  if (inf) live.appendChild(inf);
  var rb = renderRollback(jv);
  if (rb) live.appendChild(rb);

  var armed = x25Armed();
  var n = countChecks(jv.checks);
  if (!running && !run.starting) {
    // An engine exits 3 when a check failed, which fails its stage and the job: the failed checks explain that.
    if (armed) live.appendChild(note("err", t("precheck.armed")));
    else if (n.fail) live.appendChild(note("err", t("precheck.some_failed", { n: n.fail })));
    else if (jv.state === "failed" && !run.error) live.appendChild(note("err", t("precheck.job_failed")));
    else if (n.warn) live.appendChild(note("warn", t("precheck.warnings", { n: n.warn })));
    else live.appendChild(note("ok", t("precheck.all_ok")));
  }

  var bar = S.refs.preBar;
  if (bar) {
    clear(bar);
    bar.appendChild(navBtn(t("btn.back"), "", function () { go(3); }));
    var right = h("div", { class: "right" });
    right.appendChild(navBtn(t("btn.rerun"), "", function () { S.pre = null; render(); }, running || run.starting));
    var why = null;
    var disabled = false;
    if (running || run.starting) { disabled = true; }
    else if (S.srv.preview_only) { disabled = true; why = t("precheck.why_preview"); }
    else if (armed) { disabled = true; why = t("precheck.why_armed"); }
    else if (jv.state !== "ok" && jv.state !== "failed") { disabled = true; }
    else if (jv.state === "failed" && !jv.checks.length) { disabled = true; why = t("precheck.job_failed"); }
    if (why) right.insertBefore(h("span", { class: "why", text: why }), right.firstChild);
    right.appendChild(navBtn(t("btn.continue_apply"), "primary", function () { go(5); }, disabled));
    bar.appendChild(right);
  }
}

/* ---------- step 5: apply and results ---------- */

function startApply() {
  hideAlert();
  var needProps = x25Present();
  if (needProps && !S.propsOff) { showAlert(t("apply.props_required"), "err"); return; }
  var secrets = {};
  Object.keys(S.secrets).forEach(function (k) { if (S.secrets[k]) secrets[k] = S.secrets[k]; });
  var jv = new JobView();
  var run = { jv: jv, starting: true };
  S.app = run;
  render();
  api("POST", "/api/apply", { role: S.role, preset: S.preset, values: S.values, secrets: secrets, props_off: !!S.propsOff }).then(function (r) {
    if (S.app !== run) return;
    run.starting = false;
    run.job = r.job;
    var live = startJob(r.job, jv, function () { if (S.app === run) paintApply(); });
    live.onEnd = function (state) {
      if (S.app !== run) return;
      if (state === "ok" && S.srv.schema.keys[SECRET_PASSPHRASE] !== undefined && S.values.MAVLINK_SIGNING === "1") loadPassphrase(run);
      paintApply();
      renderChrome();
    };
    run.stream = live.stream;
    paintApply();
    renderChrome();
  }, function (e) {
    if (S.app !== run) return;
    S.app = null;
    if (e.data && e.data.errors && Object.keys(e.data.errors).length) {
      S.srvErrors = e.data.errors;
      S.tab = firstErrorTab() || S.tab;
      go(3);
      showAlert(errText(e), "err");
      return;
    }
    render();
    showError(e);
  });
}

function loadPassphrase(run) {
  api("GET", "/api/secret/" + SECRET_PASSPHRASE).then(function (r) {
    run.passphrase = r.value || "";
    paintApply();
  }, function (e) { run.passphraseError = errText(e); paintApply(); });
}

function gcsHost() {
  var ips = (S.srv.host && S.srv.host.ips) || [];
  return ips.length ? ips[0] : ((S.srv.host && S.srv.host.hostname) || "");
}

function gcsRows() {
  var v = S.values;
  var tcp = v.CM5_TCP_SERVER_PORT || "";
  var udp = v.MAVLINK_LAN_PORT || "";
  var host = gcsHost();
  return { host: host, tcp: tcp, udp: udp, signing: v.MAVLINK_SIGNING === "1" };
}

function setupSheet(run) {
  var g = gcsRows();
  var lines = [];
  lines.push(t("sheet.title"));
  lines.push("=".repeat(t("sheet.title").length));
  lines.push(new Date().toISOString());
  lines.push("");
  lines.push(t("sheet.preset") + ": " + (findPreset(S.preset) ? bl(findPreset(S.preset).name) : S.preset));
  lines.push(t("gcs.host") + ": " + g.host);
  if (g.tcp) lines.push("TCP: " + g.tcp);
  if (g.udp) lines.push("UDP: " + g.udp);
  if (g.signing) {
    lines.push("");
    lines.push(t("sheet.signing"));
    lines.push(t("gcs.signing.mp"));
    lines.push(t("gcs.signing.qgc"));
    lines.push(t("gcs.signing.mavproxy"));
    lines.push(t("gcs.signing.passphrase") + ": " + (run.passphrase || "-"));
    lines.push(t("sheet.keep_safe"));
  }
  lines.push("");
  lines.push(t("sheet.checks"));
  var tag = { ok: "[ok]   ", warn: "[warn] ", fail: "[FAIL] " };
  checkGroups(run.jv.checks).forEach(function (g) {
    lines.push("");
    lines.push(tt("group.", g.id) + " (" + [t("checks.n_ok", { n: g.n.ok }), t("checks.n_warn", { n: g.n.warn }), t("checks.n_fail", { n: g.n.fail })].join(", ") + ")");
    g.rows.forEach(function (r) { lines.push(tag[r.st] + evText(r.c)); });
  });
  return lines.join("\n") + "\n";
}

function renderApply(root) {
  root.appendChild(h("h2", { text: t("step.apply.title") }));
  root.appendChild(h("p", { class: "intro", text: t("step.apply.intro") }));
  S.refs = { app: h("div", { class: "app-live" }) };
  root.appendChild(S.refs.app);
  paintApply();
}

function paintApply() {
  var live = S.refs.app;
  if (!live) return;
  clear(live);
  var run = S.app;

  if (S.closed) {
    live.appendChild(note("ok", t("done.closed")));
    return;
  }

  if (!run) {
    // before starting
    if (S.srv.preview_only) live.appendChild(note("warn", t("banner.preview_only")));
    var ready = S.pre && S.pre.jv.state !== "running" && !S.pre.error;
    if (!ready) live.appendChild(note("warn", t("apply.no_precheck")));
    var summary = kv([
      [t("sheet.role"), tt("role.", S.role + ".name")],
      [t("sheet.preset"), findPreset(S.preset) ? (S.preset === "running" ? t("preset.running") : bl(findPreset(S.preset).name)) : ""],
      [t("apply.changes_count"), S.pre ? String(S.pre.jv.changes.length) : ""]
    ]);
    live.appendChild(card(t("apply.summary"), summary));
    var present = x25Present();
    var box = null;
    if (present) {
      var cb = h("input", { type: "checkbox", id: "props-off" });
      cb.checked = !!S.propsOff;
      cb.addEventListener("change", function () { S.propsOff = cb.checked; paintApply(); });
      box = h("div", { class: "props" }, h("div", { class: "bool-row" }, cb, h("label", { for: "props-off" }, h("strong", { text: t("apply.props_off") }))),
        h("div", { class: "help", text: t("apply.props_help") }));
    } else {
      live.appendChild(note("info", t("apply.no_x25")));
    }
    if (box) live.appendChild(card(t("apply.safety"), box, "danger"));
    var disabled = !ready || S.srv.preview_only || x25Armed() || (present && !S.propsOff);
    var bar = footer(navBtn(t("btn.back"), "", function () { go(4); }),
      h("div", { class: "right" },
        !ready ? navBtn(t("btn.goprecheck"), "", function () { go(4); }) : null,
        navBtn(t("btn.apply"), "danger", startApply, disabled)));
    live.appendChild(bar);
    return;
  }

  var jv = run.jv;
  var running = jv.state === "running" || run.starting;
  if (running) live.appendChild(h("p", { class: "busy" }, h("span", { class: "spin", "aria-hidden": "true" }), " " + t("job.running")));
  if (jv.lost) live.appendChild(note("warn", t("job.lost")));
  var rb = renderRollback(jv);
  if (rb) live.appendChild(rb);
  live.appendChild(card(t("apply.progress"), renderProgress(jv)));
  if (!run.logPre) run.logPre = h("pre", { class: "log", tabindex: "0" });
  run.logPre.setAttribute("aria-label", t("apply.log"));
  if (run.logOpen === undefined) run.logOpen = true;
  var logBox = h("details", { class: "logbox", open: run.logOpen ? true : null }, h("summary", { text: t("apply.log") }), run.logPre);
  logBox.addEventListener("toggle", function () { run.logOpen = logBox.open; });
  live.appendChild(logBox);
  renderLog(jv, run.logPre);
  run.logPre.scrollTop = run.logPre.scrollHeight;

  if (!running) {
    var n = countChecks(jv.checks);
    if (jv.state === "ok") {
      live.appendChild(note("ok", t("apply.ok")));
      if (n.fail) live.appendChild(note("err", t("results.some_failed", { n: n.fail })));
      else if (n.warn) live.appendChild(note("warn", t("results.warnings", { n: n.warn })));
      live.appendChild(card(t("results.checks"), renderChecks(jv)));
      add(live, renderX25Status(jv));
      live.appendChild(renderGcs(run));
      live.appendChild(footer(null, h("div", { class: "right" },
        navBtn(t("btn.status"), "", function () { location.hash = "#status"; }),
        navBtn(t("btn.done"), "primary", doneAndShutdown))));
    } else {
      live.appendChild(note("err", t("apply.failed")));
      live.appendChild(card(t("results.checks"), renderChecks(jv)));
      add(live, renderX25Status(jv));
      live.appendChild(footer(navBtn(t("btn.backsettings"), "", function () { S.app = null; go(3); }),
        navBtn(t("btn.retry"), "primary", function () { S.app = null; S.pre = null; go(4); })));
    }
  }
}

function renderGcs(run) {
  var g = gcsRows();
  var wrap = h("div", { class: "gcs" });
  var rows = [[t("gcs.host"), g.host]];
  if (g.tcp) rows.push(["TCP", g.tcp + " — " + t("gcs.tcp_hint")]);
  if (g.udp) rows.push(["UDP", g.udp + " — " + t("gcs.udp_hint")]);
  wrap.appendChild(card(t("gcs.title"), h("div", null, kv(rows), h("p", { class: "muted", text: t("gcs.connect_hint") }))));

  if (g.signing) {
    var body = h("div", null);
    body.appendChild(h("p", { text: t("gcs.signing.intro") }));
    body.appendChild(h("ul", { class: "gcs-list" },
      h("li", { text: t("gcs.signing.mp") }),
      h("li", { text: t("gcs.signing.qgc") }),
      h("li", { text: t("gcs.signing.mavproxy") })));
    var pp = h("div", { class: "pp" });
    if (run.passphraseError) {
      pp.appendChild(note("warn", run.passphraseError));
    } else if (run.passphrase === undefined) {
      pp.appendChild(h("p", { class: "muted", text: t("job.running") }));
    } else {
      var shown = !!run.ppShown;
      var field = h("input", { type: shown ? "text" : "password", readonly: true, class: "pp-in", value: run.passphrase, "aria-label": t("gcs.signing.passphrase"), autocomplete: "off" });
      pp.appendChild(h("label", { class: "pp-l", text: t("gcs.signing.passphrase") }));
      pp.appendChild(h("div", { class: "pp-row" }, field,
        h("button", { type: "button", class: "btn small", text: shown ? t("btn.hide") : t("btn.show"), onclick: function () { run.ppShown = !run.ppShown; paintApply(); } }),
        copyButton(function () { return run.passphrase; })));
      if (shown) {
        var qrBox = h("div", { class: "qr-box" });
        try { qrBox.appendChild(qrSvg(run.passphrase)); } catch (e) { qrBox.appendChild(h("p", { class: "muted", text: t("err.qr") })); }
        qrBox.appendChild(h("p", { class: "muted", text: t("qr.hint") }));
        pp.appendChild(qrBox);
      }
      pp.appendChild(note("warn", t("gcs.signing.keep")));
    }
    body.appendChild(pp);
    wrap.appendChild(card(t("gcs.signing.title"), body));
  }
  wrap.appendChild(card(t("sheet.title"), h("div", null, h("p", { class: "muted", text: t("sheet.hint") }),
    navBtn(t("btn.download"), "", function () { downloadText("uavlink-setup.txt", setupSheet(run)); }))));
  return wrap;
}

function doneAndShutdown() {
  if (!window.confirm(t("done.confirm"))) return;
  api("POST", "/api/shutdown", {}).then(function () {
    S.closed = true;
    render();
  }, function (e) { showError(e); });
}

/* ---------- status page ---------- */

function loadStatus() {
  S.status = { loading: true };
  render();
  Promise.all([
    api("GET", "/api/status"),
    api("GET", "/api/x25/backups").catch(function () { return { backups: [] }; })
  ]).then(function (r) {
    var keep = S.status && S.status.verify;
    S.status = { data: r[0], backups: r[1].backups || [], verify: keep };
    var lv = r[0].last_verify;
    if (lv && lv.id && !Array.isArray(lv.checks)) loadLastVerify(S.status, lv.id);
    if (S.page === "status") renderStatusBody();
  }, function (e) {
    S.status = { error: errText(e) };
    if (S.page === "status") renderStatusBody();
  });
}

/* The server reports the last verification as {id, state, ended}: its checks come from the job's events. */
function loadLastVerify(st, id) {
  api("GET", "/api/jobs/" + encodeURIComponent(id)).then(function (j) {
    var jv = new JobView();
    (j.events || []).forEach(function (ev) { jv.add(ev); });
    st.lastJob = jv;
    if (S.status === st && S.page === "status") renderStatusBody();
  }, function () { /* the job is gone after a server restart: show the summary only */ });
}

function backupName(b) { return typeof b === "string" ? b : (b.name || b.file || b.path || ""); }

function renderStatus(root) {
  root.appendChild(h("h2", { text: t("status.title") }));
  S.refs = { status: h("div", { class: "status-live" }) };
  root.appendChild(S.refs.status);
  if (!S.status) { loadStatus(); return; }
  renderStatusBody();
}

function stateKind(st) {
  var s = String(st);
  if (/^(active|running|enabled|ok)/.test(s)) return "ok";
  if (/^(failed|dead|error)/.test(s)) return "fail";
  return "muted";
}

function renderStatusBody() {
  var live = S.refs && S.refs.status;
  if (!live) return;
  clear(live);
  var st = S.status;
  if (!st || st.loading) { live.appendChild(h("p", { class: "busy" }, h("span", { class: "spin", "aria-hidden": "true" }), " " + t("job.running"))); return; }
  if (st.error) { live.appendChild(note("err", st.error)); }
  var d = st.data || {};

  var svc = d.services || {};
  var tb = h("tbody");
  Object.keys(svc).forEach(function (n) {
    tb.appendChild(h("tr", null, h("td", null, h("code", { text: n })), h("td", null, badge(String(svc[n]), stateKind(svc[n])))));
  });
  live.appendChild(card(t("status.services"), Object.keys(svc).length ? h("div", { class: "table-wrap" }, h("table", { class: "tbl" }, tb)) : h("p", { class: "muted", text: t("status.none") })));

  var ap = d.applied;
  live.appendChild(card(t("status.applied"), ap ? kv([
    [t("sheet.role"), ap.role ? tt("role.", ap.role + ".name") : ""],
    [t("sheet.preset"), ap.preset ? ((findPreset(ap.preset) ? bl(findPreset(ap.preset).name) : ap.preset)) : ""],
    [t("status.time"), fmtTime(ap.time)],
    [t("status.version"), ap.version || ""]
  ]) : h("p", { class: "muted", text: t("status.not_applied") })));

  var lv = d.last_verify;
  var vbody = h("div", null);
  var vrun = st.verify;
  if (vrun) {
    add(vbody, vrun.jv.state === "running" ? h("p", { class: "busy" }, h("span", { class: "spin", "aria-hidden": "true" }), " " + t("job.running")) : null);
    vbody.appendChild(renderChecks(vrun.jv));
  } else if (lv) {
    var info = [];
    var vj = st.lastJob || null;
    var ok = lv.ok !== undefined ? lv.ok : (lv.state === "ok" ? true : (lv.state === "failed" ? false : undefined));
    if (lv.time || lv.ended) info.push(fmtTime(lv.time || lv.ended));
    if (ok !== undefined) info.push(ok ? t("status.verify_ok") : t("status.verify_fail"));
    vbody.appendChild(h("p", { class: "muted", text: info.join(" · ") }));
    if (Array.isArray(lv.checks)) {
      vj = new JobView();
      lv.checks.forEach(function (c) { vj.add(Object.assign({ t: "check" }, c)); });
    }
    if (vj) vbody.appendChild(renderChecks(vj));
  } else {
    vbody.appendChild(h("p", { class: "muted", text: t("status.verify_none") }));
  }
  vbody.appendChild(h("div", { class: "row-btns" },
    navBtn(t("btn.verify"), "primary", runVerify, !!(vrun && vrun.jv.state === "running")),
    navBtn(t("btn.refresh"), "", loadStatus)));
  live.appendChild(card(t("status.verify"), vbody));

  var bk = st.backups || [];
  var bbody = h("div", null);
  if (!bk.length) bbody.appendChild(h("p", { class: "muted", text: t("status.no_backups") }));
  else {
    var bt = h("tbody");
    bk.forEach(function (b) {
      var name = backupName(b);
      var when = typeof b === "object" ? (b.time || b.mtime || "") : "";
      var restoreBtn = navBtn(t("btn.restore"), "small danger-o", function () { restoreBackup(name); });
      restoreBtn.setAttribute("aria-label", t("btn.restore") + " " + name);
      bt.appendChild(h("tr", null, h("td", null, h("code", { text: name })), h("td", { text: when ? fmtTime(when) : "" }), h("td", { class: "r" }, restoreBtn)));
    });
    bbody.appendChild(h("div", { class: "table-wrap" }, h("table", { class: "tbl" }, bt)));
  }
  if (st.restore) {
    bbody.appendChild(h("h4", { text: t("status.restore_run") + " " + st.restore.name }));
    bbody.appendChild(renderProgress(st.restore.jv));
    bbody.appendChild(renderChecks(st.restore.jv));
    if (st.restore.jv.state === "ok") bbody.appendChild(note("ok", t("status.restore_ok")));
    else if (st.restore.jv.state === "failed") bbody.appendChild(note("err", t("status.restore_fail")));
  }
  live.appendChild(card(t("status.backups"), bbody));
}

function runVerify() {
  hideAlert();
  var jv = new JobView();
  var run = { jv: jv };
  S.status.verify = run;
  renderStatusBody();
  api("POST", "/api/verify", {}).then(function (r) {
    var live = startJob(r.job, jv, function () { if (S.page === "status") renderStatusBody(); });
    live.onEnd = function () { if (S.page === "status") renderStatusBody(); };
  }, function (e) { S.status.verify = null; renderStatusBody(); showError(e); });
}

function restoreBackup(name) {
  if (!window.confirm(t("status.restore_confirm", { name: name }))) return;
  hideAlert();
  var jv = new JobView();
  S.status.restore = { jv: jv, name: name };
  renderStatusBody();
  api("POST", "/api/x25/restore", { file: name }).then(function (r) {
    var live = startJob(r.job, jv, function () { if (S.page === "status") renderStatusBody(); });
    live.onEnd = function () { if (S.page === "status") renderStatusBody(); };
  }, function (e) { S.status.restore = null; renderStatusBody(); showError(e); });
}

/* ---------- routing and boot ---------- */

function parseHash() {
  var hs = location.hash.replace(/^#/, "");
  if (hs === "status") return { page: "status" };
  var m = /(?:^|&)step=(\d)/.exec(hs);
  var n = m ? parseInt(m[1], 10) : 1;
  return { page: "wizard", step: n >= 1 && n <= 5 ? n : 1 };
}

function go(n) {
  if (location.hash === "#step=" + n) { route(); return; }
  location.hash = "#step=" + n;
}

function route() {
  var r = parseHash();
  S.page = r.page;
  if (r.page === "wizard") S.step = r.step;
  hideAlert();
  render();
}

function render() {
  if (!S.srv) return;
  renderChrome();
  var main = $("#main");
  clear(main);
  S.refs = {};
  if (S.page === "status") renderStatus(main);
  else if (S.closed) { main.appendChild(note("ok", t("done.closed"))); }
  else {
    if (S.step >= 2 && !S.preset) {
      var list = sortedPresets(S.role);
      if (list.length) applyPreset(list[0].id);
    }
    if (S.step === 3 && !S.preset) S.step = 2;
    [renderRole, renderPreset, renderSettings, renderPrecheck, renderApply][S.step - 1](main);
  }
  renderChrome();
}

function pickLang() {
  var q = new URLSearchParams(location.search).get("lang");
  if (q === "vi" || q === "en") return q;
  try {
    var saved = localStorage.getItem("uavlink.lang");
    if (saved === "vi" || saved === "en") return saved;
  } catch (e) { /* storage may be blocked */ }
  var nav = (navigator.language || "").toLowerCase();
  if (nav.indexOf("vi") === 0) return "vi";
  if (nav.indexOf("en") === 0) return "en";
  return "vi";
}

function setLang(l) {
  S.lang = l;
  try { localStorage.setItem("uavlink.lang", l); } catch (e) { /* ignore */ }
  var u = new URL(location.href);
  if (u.searchParams.has("lang")) { u.searchParams.set("lang", l); history.replaceState(null, "", u.toString()); }
  render();
}

function loadUi() {
  return Promise.all(LANGS.map(function (l) {
    return fetch("i18n/" + l + ".json", { credentials: "same-origin" }).then(function (r) { return r.json(); }).then(function (j) { S.ui[l] = j; });
  }));
}

function boot() {
  S.lang = pickLang();
  Array.prototype.forEach.call(document.querySelectorAll("#lang-toggle button"), function (b) {
    b.addEventListener("click", function () { setLang(b.getAttribute("data-lang")); });
  });
  $("#nav-page").addEventListener("click", function () {
    if (S.page === "status") location.hash = "#step=" + S.step;
    else { S.status = null; location.hash = "#status"; }
  });
  window.addEventListener("hashchange", function () { if (S.page === "status" || parseHash().page === "status") S.status = null; route(); });

  loadUi().then(function () {
    return api("GET", "/api/state");
  }).then(function (st) {
    S.srv = st;
    S.role = st.role_guess === "vps" ? "vps" : "cm5";
    var cur = st.current && st.current.preset;
    var list = sortedPresets(S.role);
    var first = null;
    if (cur && list.some(function (p) { return p.id === cur; })) first = cur;
    else if (list.length) first = list[0].id;
    if (first) applyPreset(first);
    route();
  }).catch(function (e) {
    if (!S.ui[S.lang] || !Object.keys(S.ui[S.lang]).length) {
      $("#main").textContent = "Cannot load the setup page / Không tải được trang cấu hình (" + (e && e.message) + ")";
      return;
    }
    S.srv = null;
    renderChrome();
    clear($("#main"));
    $("#main").appendChild(note("err", e && e.status !== undefined ? errText(e) : t("err.network")));
  });
}

boot();
})();
