/* ACC Central browser view. Uses only the local snapshot API. */
(function () {
  "use strict";

  var API = "/api/snapshot";
  var POLL_MS = 2000;

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
    if (content !== undefined && content !== null) {
      node.textContent = text(content);
    }
    return node;
  }

  function kv(parent, label, value) {
    var dt = document.createElement("dt");
    dt.textContent = text(label);
    var dd = document.createElement("dd");
    dd.textContent = text(value);
    parent.appendChild(dt);
    parent.appendChild(dd);
  }

  function renderAlerts(snapshot) {
    var section = $("alerts-section");
    var list = $("alerts");
    clear(list);
    var alerts = Array.isArray(snapshot.alerts) ? snapshot.alerts : [];
    if (!alerts.length) { section.hidden = true; return; }
    section.hidden = false;
    alerts.forEach(function (alert) {
      var li = document.createElement("li");
      if (alert && typeof alert === "object") {
        var cid = text(alert.coordinator_id);
        var reason = text(alert.reason);
        var stopped = text(alert.stopped_at || "unknown");
        var reset = text(alert.quota_reset_at || "unknown");
        li.textContent = cid + ": " + reason + " stopped=" + stopped + " reset=" + reset;
      } else {
        li.textContent = text(alert);
      }
      list.appendChild(li);
    });
  }

  function renderCards(snapshot) {
    var root = $("cards");
    clear(root);
    var coordinators = snapshot.coordinators;
    if (!Array.isArray(coordinators)) {
      if (coordinators && typeof coordinators === "object") {
        coordinators = Object.values(coordinators);
      } else {
        coordinators = [];
      }
    }
    coordinators.forEach(function (row) {
      if (!row || typeof row !== "object") return;
      var card = make("article", "card");
      var title = make("h3", null, row.repo_label || row.id || "coordinator");
      card.appendChild(title);

      var meta = make("p", "meta");
      meta.textContent = "cli=" + text(row.cli) + "  state=" + text(row.state) +
        (row.stale ? "  STALE" : "") +
        (row.worktree_available === false ? "  (unavailable)" : "");
      card.appendChild(meta);

      if (row.last_error) {
        var err = make("p", "muted", "error=" + text(row.last_error));
        card.appendChild(err);
      }

      var dl = document.createElement("dl");
      dl.className = "kv";
      kv(dl, "pieces", text(row.accepted) + "/" + text(row.total));
      var metrics = row.metrics;
      if (metrics && typeof metrics === "object") {
        var fp = metrics.free_piece_share;
        var ft = metrics.free_token_share;
        if (fp && typeof fp === "object") kv(dl, "free pieces", fmtRatio(fp.value));
        if (ft && typeof ft === "object") kv(dl, "free tokens", fmtRatio(ft.value));
        var ms = metrics.mean_score;
        if (ms && typeof ms === "object") kv(dl, "score", fmtNumber(ms.value));
      }
      card.appendChild(dl);

      var pieces = row.active_pieces;
      if (Array.isArray(pieces) && pieces.length) {
        var wrap = make("div", "pieces");
        pieces.forEach(function (p) {
          if (!p || typeof p !== "object") return;
          var line = make("div", "piece");
          line.textContent = text(p.ref) + " " + text(p.title) +
            "  type=" + text(p.work_type || p.type) +
            "  exec=" + text(p.executor) +
            "  state=" + text(p.status) +
            "  time=" + fmtSeconds(p.elapsed_s);
          wrap.appendChild(line);
        });
        card.appendChild(wrap);
      }

      var jobs = row.jobs;
      if (Array.isArray(jobs) && jobs.length) {
        var jwrap = make("div", "pieces");
        var jh = make("div", "muted", "jobs:");
        jwrap.appendChild(jh);
        jobs.forEach(function (j) {
          if (!j || typeof j !== "object") return;
          var jline = make("div", "piece");
          jline.textContent = text(j.id) + " " + text(j.title) +
            "  state=" + text(j.state) +
            "  start=" + text(j.start) +
            "  end=" + text(j.end);
          jwrap.appendChild(jline);
        });
        card.appendChild(jwrap);
      }

      var models = row.models;
      if (Array.isArray(models) && models.length) {
        var mwrap = make("div", "pieces");
        var mh = make("div", "muted", "models:");
        mwrap.appendChild(mh);
        models.forEach(function (m) {
          if (!m || typeof m !== "object") return;
          var mline = make("div", "piece");
          mline.textContent = text(m.role) + "/" + text(m.model) +
            "  tier=" + text(m.tier) +
            "  in=" + fmtNumber(m.tokens_in) +
            "  out=" + fmtNumber(m.tokens_out) +
            "  total=" + fmtNumber(m.tokens_total) +
            "  coverage=" + text(m.coverage);
          mwrap.appendChild(mline);
        });
        card.appendChild(mwrap);
      }

      var scoreboard = row.scoreboard;
      if (typeof scoreboard === "string" && scoreboard.trim()) {
        var sb = make("div", "pieces");
        var sh = make("div", "muted", "scoreboard:");
        sb.appendChild(sh);
        var pre = make("pre", null, scoreboard);
        sb.appendChild(pre);
        card.appendChild(sb);
      }

      root.appendChild(card);
    });
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
    if (value === null || value === undefined) return "unknown";
    if (typeof value !== "number" || !isFinite(value)) return "unknown";
    if (value >= 0 && value <= 1) return (value * 100).toFixed(0) + "%";
    return fmtNumber(value);
  }

  function fmtSeconds(value) {
    if (value === null || value === undefined) return "unknown";
    if (typeof value !== "number" || !isFinite(value)) return "unknown";
    if (value < 0) value = 0;
    if (value === Math.trunc(value) && value < 1e9) return Math.trunc(value) + "s";
    return value.toFixed(1) + "s";
  }

  function renderQuestions(snapshot) {
    var root = $("questions");
    clear(root);
    var questions = Array.isArray(snapshot.questions) ? snapshot.questions : [];
    if (!questions.length) {
      var none = make("p", "muted", "No pending questions.");
      root.appendChild(none);
      return;
    }
    questions.forEach(function (q) {
      if (!q || typeof q !== "object") return;
      var item = make("div", "item");
      var h = make("h4", null, text(q.id) + " (coordinator " + text(q.coordinator_id) + ")");
      item.appendChild(h);
      if (q.body) {
        var body = make("p", null, text(q.body));
        item.appendChild(body);
      }
      var refs = q.task_refs;
      if (Array.isArray(refs) && refs.length) {
        var refp = make("p", "muted", "refs=" + refs.map(text).join(","));
        item.appendChild(refp);
      }
      root.appendChild(item);
    });
  }

  function renderHistory(snapshot) {
    var root = $("history");
    clear(root);
    var coordinators = Array.isArray(snapshot.coordinators) ? snapshot.coordinators : [];
    var hasAny = false;
    coordinators.forEach(function (row) {
      if (!row || !Array.isArray(row.jobs) || !row.jobs.length) return;
      hasAny = true;
      var item = make("div", "item");
      var h = make("h4", null, text(row.repo_label || row.id));
      item.appendChild(h);
      row.jobs.forEach(function (j) {
        if (!j || typeof j !== "object") return;
        var line = make("div", "muted");
        line.textContent = text(j.id) + " " + text(j.title) +
          "  state=" + text(j.state) +
          "  start=" + text(j.start) +
          "  end=" + text(j.end);
        item.appendChild(line);
      });
      root.appendChild(item);
    });
    if (!hasAny) {
      root.appendChild(make("p", "muted", "No job history."));
    }
  }

  function renderCharts(snapshot) {
    var root = $("charts");
    clear(root);
    var coordinators = Array.isArray(snapshot.coordinators) ? snapshot.coordinators : [];
    // Aggregate model rows across coordinators.
    var rows = [];
    coordinators.forEach(function (c) {
      if (!c || !Array.isArray(c.models)) return;
      c.models.forEach(function (m) {
        if (!m || typeof m !== "object") return;
        rows.push(m);
      });
    });
    // Also totals-level model chart if present in totals? none.
    if (!rows.length) {
      root.appendChild(make("p", "muted", "No model token data."));
      return;
    }
    // Group by model+tier for totals.
    var maxTotal = 0;
    rows.forEach(function (m) {
      var t = (typeof m.tokens_total === "number" && isFinite(m.tokens_total)) ? m.tokens_total : 0;
      if (t > maxTotal) maxTotal = t;
    });
    if (maxTotal <= 0) maxTotal = 1;
    rows.forEach(function (m) {
      var row = make("div", "chart-row");
      var label = make("div", null, text(m.role) + "/" + text(m.model) + " (" + text(m.tier) + ")");
      row.appendChild(label);
      var barWrap = make("div", null);
      var bar = make("div", "chart-bar");
      var t = (typeof m.tokens_total === "number" && isFinite(m.tokens_total)) ? m.tokens_total : 0;
      bar.style.width = Math.max(2, (t / maxTotal) * 100) + "%";
      if (m.tier === "free") bar.style.background = "var(--free)";
      if (m.tier === "paid") bar.style.background = "var(--paid)";
      barWrap.appendChild(bar);
      row.appendChild(barWrap);
      var val = make("div", null, fmtNumber(t) + " tok");
      row.appendChild(val);
      root.appendChild(row);
    });
    // Tier summary
    var totals = snapshot.totals;
    if (totals && typeof totals === "object") {
      var tierTokens = totals.tier_tokens || totals.tokens_by_tier;
      if (tierTokens && typeof tierTokens === "object") {
        var summary = make("div", "item");
        var h = make("h4", null, "Tokens by tier");
        summary.appendChild(h);
        Object.keys(tierTokens).forEach(function (k) {
          var v = tierTokens[k];
          var line = make("div", "muted", text(k) + ": " + fmtNumber(v));
          summary.appendChild(line);
        });
        root.appendChild(summary);
      }
    }
  }

  function renderScoreboard(snapshot) {
    var root = $("scoreboard");
    clear(root);
    var coordinators = Array.isArray(snapshot.coordinators) ? snapshot.coordinators : [];
    var hasAny = false;
    coordinators.forEach(function (c) {
      if (!c || typeof c.scoreboard !== "string" || !c.scoreboard.trim()) return;
      hasAny = true;
      var item = make("div", "item");
      var h = make("h4", null, text(c.repo_label || c.id));
      item.appendChild(h);
      item.appendChild(make("pre", null, c.scoreboard));
      root.appendChild(item);
    });
    if (!hasAny) {
      root.appendChild(make("p", "muted", "No scoreboard data."));
    }
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
    kv(root, "total tokens", fmtNumber(totals.total_tokens));
  }

  function renderDiagnostics(snapshot) {
    var root = $("diagnostics");
    clear(root);
    var section = $("diagnostics-section");
    var diags = Array.isArray(snapshot.diagnostics) ? snapshot.diagnostics : [];
    if (!diags.length) { section.hidden = true; return; }
    section.hidden = false;
    diags.forEach(function (d) {
      var li = document.createElement("li");
      if (d && typeof d === "object") {
        li.textContent = text(d.record) + ": " + text(d.message);
      } else {
        li.textContent = text(d);
      }
      root.appendChild(li);
    });
  }

  function render(snapshot) {
    if (!snapshot || typeof snapshot !== "object") snapshot = {};
    renderAlerts(snapshot);
    renderCards(snapshot);
    renderQuestions(snapshot);
    renderHistory(snapshot);
    renderCharts(snapshot);
    renderScoreboard(snapshot);
    renderTotals(snapshot);
    renderDiagnostics(snapshot);
    var status = $("status");
    if (status) status.textContent = "Updated " + text(snapshot.generated_at);
  }

  function load() {
    fetch(API, { credentials: "same-origin" })
      .then(function (res) {
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      })
      .then(render)
      .catch(function (err) {
        var status = $("status");
        if (status) status.textContent = "Snapshot unavailable";
      });
  }

  load();
  setInterval(load, POLL_MS);
})();
