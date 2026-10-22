/* ACC Central browser view. Uses only the local snapshot and action API. */
(function () {
  "use strict";

  var API = "/api/snapshot";
  var POLL_MS = 2000;
  var TIMEOUT_MS = 8000;
  var ATTACH_POLL_MS = 1000;
  var ATTACH_POLLS = 120;
  var BUTTONS = ["message", "pause", "resume", "stop", "attach"];
  var state = {
    token: null, snapshot: null, busy: false, failed: null,
    select: null, placeholder: null, options: {}, selected: "", everSelected: false,
    loading: false, reloadAfter: false, timer: null,
    // Reply/Stop buttons rendered inside the lists; updateControls() keeps them current.
    itemButtons: [],
    // Bumped after every action: a snapshot requested before it is dropped.
    actionEpoch: 0,
    announced: {}, announcements: [], open: {}, lastGood: null, lost: false
  };

  function $(id) { return document.getElementById(id); }

  function clear(node) {
    while (node && node.firstChild) node.removeChild(node.firstChild);
  }

  function text(value) {
    if (value === null || value === undefined) return "";
    return String(value);
  }

  function make(tag, className, content) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined && content !== null) node.textContent = text(content);
    return node;
  }

  function button(label, className) {
    var node = make("button", className || "secondary", label);
    node.type = "button";
    return node;
  }

  function hasClass(node, name) {
    return (" " + text(node.className) + " ").indexOf(" " + name + " ") >= 0;
  }

  function setClass(node, name, on) {
    if (!node) return;
    var names = text(node.className).split(/\s+/).filter(function (n) { return n && n !== name; });
    if (on) names.push(name);
    node.className = names.join(" ");
  }

  function kv(parent, label, value) {
    var dt = make("dt", null, label);
    var dd = document.createElement("dd");
    if (value && typeof value === "object") dd.appendChild(value); else dd.textContent = text(value);
    parent.appendChild(dt);
    parent.appendChild(dd);
  }

  function list(value) {
    if (Array.isArray(value)) return value;
    if (value && typeof value === "object") return Object.values(value);
    return [];
  }

  function coordinators(snapshot) {
    return list(snapshot && snapshot.coordinators).filter(function (row) { return row && typeof row === "object"; });
  }

  function shortId(id) { return text(id).slice(0, 8); }

  function label(row) {
    return text(row && row.repo_label || "coordinator") + " · " + shortId(row && row.id);
  }

  function rowById(id) {
    return coordinators(state.snapshot).filter(function (row) { return row.id === id; })[0] || { id: id };
  }

  // --- formatting -----------------------------------------------------------

  var dateFormat = null;
  try {
    dateFormat = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });
  } catch (error) { dateFormat = null; }

  function timeNode(value, fallback) {
    var date = value ? new Date(value) : null;
    if (!date || isNaN(date.getTime())) return make("span", null, fallback || "unknown");
    var node = make("time", null, dateFormat ? dateFormat.format(date) : text(value));
    node.setAttribute("datetime", text(value));
    return node;
  }

  function fmtNumber(value) {
    if (value === null || value === undefined) return "unknown";
    if (typeof value === "boolean") return value ? "yes" : "no";
    if (typeof value === "number") {
      if (!isFinite(value)) return "unknown";
      if (Number.isInteger(value)) return String(value);
      return value.toPrecision(3);
    }
    return String(value);
  }

  function fmtRatio(value) {
    if (typeof value !== "number" || !isFinite(value)) return "unknown";
    if (value >= 0 && value <= 1) return (value * 100).toFixed(0) + "%";
    return fmtNumber(value);
  }

  function fmtDuration(value) {
    if (typeof value !== "number" || !isFinite(value)) return "unknown";
    var s = Math.max(0, Math.round(value));
    if (s < 60) return s + "s";
    if (s < 3600) return Math.floor(s / 60) + "m " + (s % 60) + "s";
    if (s < 86400) return Math.floor(s / 3600) + "h " + Math.floor((s % 3600) / 60) + "m";
    return Math.floor(s / 86400) + "d " + Math.floor((s % 86400) / 3600) + "h";
  }

  // How exact a token count is: estimates and missing coverage are always said.
  function qualifier(estimated, coverage) {
    if (coverage === "incomplete" || coverage === "unknown") return " · partial (some usage not reported)";
    if (estimated || coverage === "estimated") return " · includes estimates";
    return "";
  }

  function tokens(value, estimated, coverage) {
    return fmtNumber(value) + " tokens" + qualifier(estimated, coverage);
  }

  function rowCoverage(row) {
    var models = list(row.models);
    if (models.some(function (m) { return m && (m.coverage === "incomplete" || m.coverage === "unknown"); })) return "incomplete";
    if (models.some(function (m) { return m && (m.estimated || m.coverage === "estimated"); })) return "estimated";
    return "measured";
  }

  // --- announcements and feedback -------------------------------------------

  function announce(key, message) {
    if (state.announced[key]) return;
    state.announced[key] = true;
    var node = $("announcer");
    if (node) node.textContent = message;
  }

  function feedback(message, kind) {
    var node = $("feedback");
    if (!node) return node;
    clear(node);
    node.textContent = message;
    node.className = "feedback" + (kind ? " " + kind : "");
    if (kind === "error") {
      // Errors stay until the next action or until dismissed.
      var dismiss = button("Dismiss");
      dismiss.addEventListener("click", function () { clear(node); node.textContent = ""; node.className = "feedback"; });
      node.appendChild(dismiss);
    }
    return node;
  }

  function showCommand(intro, command, kind) {
    var node = feedback(intro, kind || "info");
    if (!node || !command) return;
    var code = make("code", "command", command);
    node.appendChild(code);
    var copy = button("Copy");
    copy.addEventListener("click", function () {
      var clipboard = typeof navigator !== "undefined" && navigator.clipboard;
      if (!clipboard) { copy.textContent = "Select and copy"; return; }
      clipboard.writeText(command).then(function () { copy.textContent = "Copied"; },
        function () { copy.textContent = "Select and copy"; });
    });
    node.appendChild(copy);
  }

  function setConnection(ok) {
    var node = $("connection");
    if (ok) {
      if (state.lost) delete state.announced["connection-lost"];
      state.lost = false;
      if (node) { clear(node); node.textContent = ""; }
      setClass($("app"), "stale", false);
      return;
    }
    state.lost = true;
    if (node) {
      clear(node);
      node.textContent = "Connection lost; retrying. Showing stale data";
      if (state.lastGood) { node.appendChild(make("span", null, " from")); node.appendChild(timeNode(state.lastGood)); }
    }
    setClass($("app"), "stale", true);
    announce("connection-lost", "Connection to ACC Central lost; the page shows the last data received.");
  }

  // --- rendering ------------------------------------------------------------

  function renderAlerts(snapshot) {
    var section = $("alerts-section");
    var root = $("alerts");
    clear(root);
    var alerts = list(snapshot.alerts);
    if (section) section.hidden = !alerts.length;
    alerts.forEach(function (alert) {
      var li = make("li", "alert");
      if (!alert || typeof alert !== "object") { li.textContent = text(alert); root.appendChild(li); return; }
      var row = rowById(alert.coordinator_id);
      var reason = alert.reason === "quota" ? "stopped by quota" : alert.reason === "stall" ? "stalled" : text(alert.reason);
      li.appendChild(make("strong", null, label(row) + ": " + reason));
      var when = make("span", "muted");
      when.appendChild(make("span", null, " stopped "));
      when.appendChild(timeNode(alert.stopped_at));
      when.appendChild(make("span", null, ", quota resets "));
      when.appendChild(timeNode(alert.quota_reset_at));
      when.appendChild(make("span", null, ", policy " + text(alert.policy || "ask")));
      li.appendChild(when);
      var stop = button("Stop…", "danger");
      state.itemButtons.push(stop);
      stop.addEventListener("click", function () { confirmStop(row.id, stop); });
      li.appendChild(stop);
      root.appendChild(li);
      announce("alert:" + text(alert.coordinator_id) + ":" + text(alert.reason) + ":" + text(alert.stopped_at),
        label(row) + " " + reason + ".");
    });
  }

  function renderQuestions(snapshot) {
    var root = $("questions");
    clear(root);
    var questions = list(snapshot.questions).filter(function (q) { return q && typeof q === "object"; });
    if (!questions.length) { root.appendChild(make("p", "muted", "No pending questions.")); return; }
    questions.forEach(function (q) {
      var row = rowById(q.coordinator_id);
      var item = make("article", "item question");
      item.appendChild(make("h3", null, label(row) + " asks"));
      if (q.body) item.appendChild(make("p", "body", q.body));
      var refs = list(q.task_refs);
      if (refs.length) item.appendChild(make("p", "muted", "Tasks: " + refs.map(text).join(", ")));
      var reply = button("Reply", "primary");
      state.itemButtons.push(reply);
      reply.addEventListener("click", function () {
        if (!state.token || state.busy) return;
        var answer = window.prompt("Reply to " + label(row) + ": " + text(q.body).slice(0, 200));
        if (answer === null || answer === undefined || !String(answer).trim()) return;
        act("/api/answer", { coordinator_id: q.coordinator_id, question_id: q.id, text: answer }, "Reply recorded.");
      });
      item.appendChild(reply);
      root.appendChild(item);
      announce("question:" + text(q.id), "New question from " + label(row) + ".");
    });
  }

  function renderCoordinators(snapshot) {
    var root = $("coordinators");
    clear(root);
    var rows = coordinators(snapshot);
    var questions = list(snapshot.questions);
    rows.forEach(function (row) {
      var selected = row.id === state.selected;
      var card = make("article", "card" + (selected ? " selected" : ""));
      if (selected) card.setAttribute("aria-current", "true");
      var head = make("div", "card-head");
      head.appendChild(make("h3", null, label(row)));
      head.appendChild(make("span", "badge state-" + text(row.state || "unknown"), row.state || "unknown"));
      card.appendChild(head);
      var pending = questions.filter(function (q) { return q && q.coordinator_id === row.id; }).length;
      var summary = make("p", "summary",
        text(row.accepted) + "/" + text(row.total) + " pieces accepted · " +
        pending + (pending === 1 ? " question" : " questions") + " pending" +
        (row.stale ? " · stale record" : "") + (row.worktree_available === false ? " · worktree unavailable" : ""));
      card.appendChild(summary);
      if (row.last_error) card.appendChild(make("p", "error", "Error: " + text(row.last_error)));
      var choose = button(selected ? "Selected" : "Select");
      choose.setAttribute("aria-pressed", selected ? "true" : "false");
      choose.addEventListener("click", function () { choose_(row.id); });
      card.appendChild(choose);

      var details = make("details");
      if (state.open[row.id]) details.open = true;
      details.addEventListener("toggle", function () { state.open[row.id] = !!details.open; });
      details.appendChild(make("summary", null, "Details"));
      var dl = make("dl", "kv");
      kv(dl, "id", text(row.id));
      kv(dl, "cli", text(row.cli));
      kv(dl, "backend", text(row.backend));
      var metrics = row.metrics && typeof row.metrics === "object" ? row.metrics : {};
      var coverage = rowCoverage(row);
      if (metrics.free_piece_share) kv(dl, "free pieces", fmtRatio(metrics.free_piece_share.value));
      if (metrics.free_token_share) kv(dl, "free tokens", fmtRatio(metrics.free_token_share.value) + qualifier(false, coverage));
      if (metrics.mean_score) kv(dl, "score", fmtNumber(metrics.mean_score.value));
      details.appendChild(dl);
      list(row.active_pieces).forEach(function (p) {
        if (!p || typeof p !== "object") return;
        details.appendChild(make("p", "piece", text(p.ref) + " " + text(p.title) + " · " +
          text(p.work_type || p.type) + " · " + text(p.executor) + " · " + text(p.status) +
          " · " + fmtDuration(p.elapsed_s)));
      });
      card.appendChild(details);
      root.appendChild(card);
    });
    if (!rows.length) root.appendChild(make("p", "muted", "No coordinators yet."));
  }

  function renderHistory(snapshot) {
    var root = $("history");
    clear(root);
    var any = false;
    coordinators(snapshot).forEach(function (row) {
      var jobs = list(row.jobs).filter(function (j) { return j && typeof j === "object"; });
      if (!jobs.length) return;
      any = true;
      var item = make("div", "item");
      item.appendChild(make("h3", null, label(row)));
      jobs.forEach(function (j) {
        var line = make("p", "muted", text(j.id) + " " + text(j.title) + " · " + text(j.state || "state not reported") + " · started ");
        line.appendChild(timeNode(j.start, "not reported"));
        line.appendChild(make("span", null, " · ended "));
        line.appendChild(timeNode(j.end, "not finished"));
        item.appendChild(line);
      });
      root.appendChild(item);
    });
    if (!any) root.appendChild(make("p", "muted", "No job history."));
  }

  function renderCharts(snapshot) {
    var root = $("charts");
    clear(root);
    var rows = coordinators(snapshot);
    var totals = snapshot.totals && typeof snapshot.totals === "object" ? snapshot.totals : {};
    // Prefer the shared aggregate: it deduplicates jobs referenced by two views.
    var models;
    if (Array.isArray(totals.models)) models = totals.models;
    else if (rows.length === 1) models = list(rows[0].models);
    else if (rows.length > 1) { root.appendChild(make("p", "muted", "Aggregate model data unavailable.")); return; }
    else models = [];
    models = models.filter(function (m) { return m && typeof m === "object"; });
    if (!models.length) { root.appendChild(make("p", "muted", "No model token data.")); return; }
    var max = 0;
    models.forEach(function (m) {
      if (typeof m.tokens_total === "number" && isFinite(m.tokens_total) && m.tokens_total > max) max = m.tokens_total;
    });
    models.forEach(function (m) {
      var known = typeof m.tokens_total === "number" && isFinite(m.tokens_total);
      var line = make("div", "chart-row");
      line.appendChild(make("div", "chart-label", text(m.role) + "/" + text(m.model) + " (" + text(m.tier) + ")"));
      var lane = make("div", "lane");
      var bar = make("div", "chart-bar tier-" + text(m.tier));
      bar.hidden = !known;
      bar.style.width = (known && max > 0 ? Math.max(0, Math.min(100, m.tokens_total / max * 100)) : 0) + "%";
      lane.appendChild(bar);
      line.appendChild(lane);
      line.appendChild(make("div", "chart-value", tokens(m.tokens_total, m.estimated, m.coverage)));
      root.appendChild(line);
    });
    var tiers = totals.tier_tokens || totals.tokens_by_tier;
    if (tiers && typeof tiers === "object") {
      var summary = make("div", "item");
      summary.appendChild(make("h3", null, "Tokens by tier" + qualifier(totals.estimated, totals.coverage_label)));
      Object.keys(tiers).forEach(function (k) { summary.appendChild(make("p", "muted", text(k) + ": " + fmtNumber(tiers[k]))); });
      root.appendChild(summary);
    }
  }

  var SCORE_COLUMNS = [["type", "type"], ["executor", "executor"], ["score", "score"], ["n", "n"],
    ["attempts_per_pass", "attempts/pass"], ["mean_tokens", "mean tokens"],
    ["mean_duration_s", "mean duration"], ["last", "last graded"]];

  function scoreCell(entry, key) {
    var value = entry[key];
    if (key === "mean_tokens") return value === null || value === undefined ? "—" : (entry.tokens_estimated ? "~" : "") + fmtNumber(Math.round(value));
    if (key === "mean_duration_s") return value === null || value === undefined ? "—" : fmtDuration(value);
    if (key === "last") return value ? text(value).slice(0, 10) : "—";
    if (key === "score" || key === "attempts_per_pass") return value === null || value === undefined ? "—" : fmtNumber(Math.round(value * 10) / 10);
    return text(value);
  }

  function renderScoreboard(snapshot) {
    var root = $("scoreboard");
    clear(root);
    var any = false;
    coordinators(snapshot).forEach(function (row) {
      var entries = list(row.scoreboard_rows).filter(function (e) { return e && typeof e === "object"; });
      var legacy = typeof row.scoreboard === "string" && row.scoreboard.trim() && !Array.isArray(row.scoreboard_rows);
      if (!entries.length && !legacy) return;
      any = true;
      var item = make("div", "item");
      item.appendChild(make("h3", null, label(row)));
      if (legacy) {
        item.appendChild(make("pre", null, row.scoreboard));
      } else {
        var wrap = make("div", "table-wrap");
        var table = make("table");
        var head = make("tr");
        SCORE_COLUMNS.forEach(function (c) { var th = make("th", null, c[1]); th.setAttribute("scope", "col"); head.appendChild(th); });
        var thead = make("thead"); thead.appendChild(head); table.appendChild(thead);
        var tbody = make("tbody");
        entries.forEach(function (entry) {
          var tr = make("tr");
          SCORE_COLUMNS.forEach(function (c) { tr.appendChild(make("td", null, scoreCell(entry, c[0]))); });
          tbody.appendChild(tr);
        });
        table.appendChild(tbody);
        wrap.appendChild(table);
        item.appendChild(wrap);
      }
      root.appendChild(item);
    });
    if (!any) root.appendChild(make("p", "muted", "No scoreboard data."));
  }

  function renderTotals(snapshot) {
    var root = $("totals");
    clear(root);
    var totals = snapshot.totals;
    if (!totals || typeof totals !== "object") return;
    kv(root, "coordinators", fmtNumber(totals.coordinators));
    kv(root, "jobs", fmtNumber(totals.jobs));
    kv(root, "questions", fmtNumber(totals.questions));
    kv(root, "pieces", fmtNumber(totals.accepted_pieces) + "/" + fmtNumber(totals.pieces) + " accepted");
    kv(root, "score", fmtNumber(totals.mean_score));
    kv(root, "total tokens", tokens(totals.total_tokens, totals.estimated, totals.coverage_label));
    if (typeof totals.free_token_share === "number") kv(root, "free tokens", fmtRatio(totals.free_token_share) + qualifier(totals.estimated, totals.coverage_label));
  }

  function renderDiagnostics(snapshot) {
    var root = $("diagnostics");
    clear(root);
    var section = $("diagnostics-section");
    var diags = list(snapshot.diagnostics);
    if (section) section.hidden = !diags.length;
    diags.forEach(function (d) {
      root.appendChild(make("li", null, d && typeof d === "object" ? text(d.record) + ": " + text(d.message) : text(d)));
    });
  }

  function render(snapshot) {
    if (!snapshot || typeof snapshot !== "object") snapshot = {};
    state.snapshot = snapshot;
    state.itemButtons = [];
    updateSelect();
    renderAlerts(snapshot);
    renderQuestions(snapshot);
    renderCoordinators(snapshot);
    renderHistory(snapshot);
    renderCharts(snapshot);
    renderScoreboard(snapshot);
    renderTotals(snapshot);
    renderDiagnostics(snapshot);
    updateControls();
    var clock = $("clock");
    if (clock) {
      clear(clock);
      clock.textContent = "Updated ";
      clock.appendChild(timeNode(snapshot.generated_at));
    }
  }

  // --- selection and controls -----------------------------------------------

  function choose_(id) {
    state.selected = id || "";
    if (state.selected) state.everSelected = true;
    if (state.select) state.select.value = state.selected;
    if (state.snapshot) renderCoordinators(state.snapshot);
    updateControls();
  }

  function updateSelect() {
    var rows = coordinators(state.snapshot);
    var ids = rows.map(function (row) { return row.id; });
    if (state.selected && ids.indexOf(state.selected) < 0) {
      // Never retarget: the owner picks the next coordinator explicitly.
      feedback("The selected coordinator " + label(rowById(state.selected)) + " is gone; choose another.", "error");
      state.selected = "";
    }
    if (!state.selected && !state.everSelected && rows.length === 1) {
      state.selected = rows[0].id;
      state.everSelected = true;
    }
    var select = state.select;
    if (!select) return;
    Object.keys(state.options).forEach(function (id) {
      if (ids.indexOf(id) < 0) { select.removeChild(state.options[id]); delete state.options[id]; }
    });
    rows.forEach(function (row) {
      var option = state.options[row.id];
      if (!option) {
        option = make("option");
        option.value = row.id;
        select.appendChild(option);
        state.options[row.id] = option;
      }
      option.textContent = label(row);
    });
    select.value = state.selected;
  }

  function updateControls() {
    BUTTONS.forEach(function (name) {
      var node = $("btn-" + name);
      if (node) node.disabled = !state.token || state.busy || !state.selected;
    });
    state.itemButtons.forEach(function (node) { node.disabled = !state.token || state.busy; });
  }

  // --- actions --------------------------------------------------------------

  async function sendAction(route, payload, pendingText) {
    if (!state.token || state.busy) {
      // Never lose what the owner typed: keep it on screen to send again.
      if (payload.text) showCommand("Another action is still running, so this was not sent. Your text:", payload.text, "error");
      return null;
    }
    var signature = JSON.stringify([route, payload]);
    // A lost answer is retried with the same id, so the server applies it once.
    var id = state.failed && state.failed.signature === signature ? state.failed.id : window.crypto.randomUUID();
    state.busy = true;
    state.failed = { signature: signature, id: id };
    feedback(pendingText || "Sending…", "pending");
    updateControls();
    try {
      var response = await fetch(route, { method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(Object.assign({}, payload, { csrf_token: state.token, action_id: id })) });
      if (!response.ok) { state.failed = null; feedback("Action refused by the server.", "error"); return null; }
      var result = await response.json();
      state.failed = null;
      state.actionEpoch += 1;
      return result;
    } catch (error) {
      feedback("Action unavailable. Retry the same action to resend safely.", "error");
      return null;
    } finally {
      state.busy = false;
      updateControls();
      refreshNow();
    }
  }

  function act(route, payload, done) {
    return sendAction(route, payload).then(function (result) {
      if (result) feedback(done, "ok");
      return result;
    });
  }

  function confirmStop(cid, opener) {
    if (!cid) return;
    var name = label(rowById(cid));
    var dialog = $("stop-dialog");
    if (!dialog || typeof dialog.showModal !== "function") {
      if (window.confirm("Stop " + name + "? It finishes its current round, then stops.")) {
        act("/api/stop", { coordinator_id: cid }, "Stop requested for " + name + ".");
      }
      return;
    }
    $("stop-dialog-name").textContent = name;
    dialog.returnValue = "";
    function closed() {
      dialog.removeEventListener("close", closed);
      var done = dialog.returnValue === "stop"
        ? act("/api/stop", { coordinator_id: cid }, "Stop requested for " + name + ".") : Promise.resolve();
      // After the action: a busy (disabled) button cannot take focus back.
      done.then(function () { if (opener && opener.focus) opener.focus(); });
    }
    dialog.addEventListener("close", closed);
    dialog.showModal();
    $("stop-cancel").focus();
  }

  function attach(cid) {
    sendAction("/api/attach", { coordinator_id: cid }, "Requesting the terminal session…").then(function (result) {
      if (!result) return;
      if (result.queued) {
        feedback("Attach requested; queued for the terminal view.", "pending");
        pollAttach(result.attach_id, 0, "queued");
      } else {
        showCommand("The browser cannot open the session. Run this in your terminal:", result.command);
      }
    });
  }

  // A request may wait queued while another session runs, and a session may stay
  // open for hours: only consecutive unanswered polls give up.
  function pollAttach(id, misses, last) {
    if (misses >= ATTACH_POLLS) { feedback("Lost track of the attach request; check the terminal view.", "error"); return; }
    setTimeout(function () {
      fetch("/api/attach/" + encodeURIComponent(id), { credentials: "same-origin", cache: "no-store" })
        .then(function (response) { return response.ok ? response.json() : null; })
        .then(function (result) {
          if (!result) return pollAttach(id, misses + 1, last);
          if (result.state === "queued" || result.state === "opened") {
            if (result.state === "opened" && last !== "opened") feedback("The session is open in the terminal.", "ok");
            return pollAttach(id, 0, result.state);
          }
          if (result.state === "closed") {
            var code = typeof result.exit_code === "number" && result.exit_code !== 0 ? " (exit code " + result.exit_code + ")" : "";
            feedback("The terminal session closed" + code + ".", "ok");
          } else showCommand("Attach failed: " + text(result.reason) + ". Run this in your terminal instead:", result.command, "error");
        }, function () { pollAttach(id, misses + 1, last); });
    }, ATTACH_POLL_MS);
  }

  function initializeActions() {
    // Viewing still works without interactive browser capabilities.
    if (typeof window === "undefined" || !window.crypto || !window.crypto.randomUUID) return;
    var select = make("select");
    select.setAttribute("aria-label", "Coordinator for the actions");
    state.placeholder = make("option", null, "Choose a coordinator");
    state.placeholder.value = "";
    select.appendChild(state.placeholder);
    state.select = select;
    $("btn-message").parentNode.insertBefore(select, $("btn-message"));
    select.addEventListener("change", function () { choose_(select.value); });
    $("btn-message").addEventListener("click", function () {
      if (!state.selected || state.busy || !state.token) return;
      var cid = state.selected;
      var body = window.prompt("Message to " + label(rowById(cid)));
      if (body === null || body === undefined || !String(body).trim()) return;
      act("/api/message", { coordinator_id: cid, text: body }, "Message recorded.");
    });
    [["pause", "/api/pause", "Pause requested."], ["resume", "/api/resume", "Resume requested."]].forEach(function (item) {
      $("btn-" + item[0]).addEventListener("click", function () {
        if (state.selected) act(item[1], { coordinator_id: state.selected }, item[2]);
      });
    });
    $("btn-stop").addEventListener("click", function () { confirmStop(state.selected, $("btn-stop")); });
    $("btn-attach").addEventListener("click", function () { if (state.selected) attach(state.selected); });
    fetch("/api/csrf", { credentials: "same-origin", cache: "no-store" })
      .then(function (response) { if (!response.ok) throw new Error("authorization unavailable"); return response.json(); })
      .then(function (data) {
        if (typeof data.csrf_token !== "string" || !data.csrf_token) throw new Error("authorization unavailable");
        state.token = data.csrf_token;
        updateControls();
        if (state.snapshot) render(state.snapshot);
      })
      .catch(function () { feedback("Actions are unavailable; viewing continues.", "error"); });
  }

  // --- refresh: one request in flight, the next one scheduled after it ------

  function schedule(delay) {
    if (state.timer !== null) clearTimeout(state.timer);
    state.timer = setTimeout(load, delay);
  }

  function refreshNow() {
    if (state.loading) state.reloadAfter = true; else schedule(0);
  }

  function load() {
    state.timer = null;
    if (state.loading) { state.reloadAfter = true; return; }
    state.loading = true;
    var epoch = state.actionEpoch;
    var timer = null;
    var timeout = new Promise(function (resolve, reject) {
      timer = setTimeout(function () { reject(new Error("timeout")); }, TIMEOUT_MS);
    });
    var request = fetch(API, { credentials: "same-origin", cache: "no-store" }).then(function (response) {
      if (!response.ok) throw new Error("HTTP " + response.status);
      return response.json();
    });
    Promise.race([request, timeout]).then(function (snapshot) {
      // Requested before the latest action: a fresher answer follows.
      if (epoch !== state.actionEpoch) { state.reloadAfter = true; return; }
      setConnection(true);
      render(snapshot);
      state.lastGood = snapshot && snapshot.generated_at;
    }, function () {
      setConnection(false);
    }).then(function () {
      clearTimeout(timer);
      state.loading = false;
      if (state.reloadAfter) { state.reloadAfter = false; schedule(0); } else schedule(POLL_MS);
    });
  }

  initializeActions();
  load();
})();
