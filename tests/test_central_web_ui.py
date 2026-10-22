"""Browser view behaviour, run in Node against a small fake DOM (optional runtime).

Each test feeds central.js scripted fetch answers and fake timers, then checks
what the page shows and sends: one snapshot request in flight at a time, no
stale overwrite after an action, a stale banner on failure, stable selection,
proportional bars, estimate/coverage labels, deduplicated announcements,
per-question replies, a confirmed stop and correlated attach results.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "skill" / "crew-chief" / "web"

HARNESS = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const elements = {};
class Element {
  constructor(tag) {
    this.tag = tag; this.children = []; this.style = {}; this.listeners = {}; this.attributes = {};
    this._text = ''; this.className = ''; this.hidden = false; this.disabled = false; this.value = '';
    this.parentNode = null; this.focused = false;
  }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return [this._text, ...this.children.map(c => c.textContent)].filter(Boolean).join(' '); }
  set id(v) { this._id = v; elements[v] = this; }
  get id() { return this._id; }
  get firstChild() { return this.children[0]; }
  appendChild(c) { if (c.parentNode) c.parentNode.removeChild(c); c.parentNode = this; this.children.push(c); return c; }
  insertBefore(c, ref) { if (c.parentNode) c.parentNode.removeChild(c); c.parentNode = this;
    const i = this.children.indexOf(ref); this.children.splice(i < 0 ? this.children.length : i, 0, c); return c; }
  removeChild(c) { const i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); c.parentNode = null; return c; }
  setAttribute(k, v) { this.attributes[k] = String(v); }
  getAttribute(k) { return k in this.attributes ? this.attributes[k] : null; }
  removeAttribute(k) { delete this.attributes[k]; }
  addEventListener(k, f) { (this.listeners[k] ||= []).push(f); }
  removeEventListener(k, f) { this.listeners[k] = (this.listeners[k] || []).filter(x => x !== f); }
  focus() { focused = this; }
  click() { (this.listeners.click || []).slice().forEach(f => f({ preventDefault() {}, currentTarget: this })); }
  set innerHTML(v) { throw new Error('unsafe DOM'); }
  all() { return [this, ...this.children.flatMap(c => c.all())]; }
  find(pred) { return this.all().find(pred); }
  findAll(pred) { return this.all().filter(pred); }
}
let focused = null;
const document = {
  getElementById(id) { if (!elements[id]) { const e = new Element('div'); e.id = id; } return elements[id]; },
  createElement(tag) { return new Element(tag); },
};
// Static page skeleton the script expects (ids from index.html).
for (const id of ['toolbar', 'btn-message', 'btn-pause', 'btn-resume', 'btn-stop', 'btn-attach'])
  document.getElementById(id);
for (const id of ['btn-message', 'btn-pause', 'btn-resume', 'btn-stop', 'btn-attach'])
  elements.toolbar.appendChild(elements[id]);

let timers = [], timerId = 0;
const setTimeout = (f, ms) => { const t = { id: ++timerId, f, ms }; timers.push(t); return t.id; };
const clearTimeout = (id) => { timers = timers.filter(t => t.id !== id); };
async function fire(filter) {  // run the due timers once
  const due = timers.filter(filter || (() => true)); timers = timers.filter(t => !due.includes(t));
  for (const t of due) t.f(); await settle();
}
const settle = async () => { for (let i = 0; i < 20; i++) await new Promise(setImmediate); };

const calls = [], pending = {}, answers = {}, inFlight = {};
function respond(url, body, ok = true) { (answers[url] ||= []).push({ ok, body }); }
function defer(url) { let resolve; const p = new Promise(r => resolve = r); (answers[url] ||= []).push({ deferred: p }); return resolve; }
const fetch = async (url, opts = {}) => {
  calls.push({ url, opts, body: opts.body ? JSON.parse(opts.body) : null });
  const queue = answers[url] || [];
  if (!queue.length) throw new Error('no answer for ' + url);
  const next = queue.shift();
  // Like a browser: an aborted request settles at once and is no longer pending.
  const aborted = new Promise((_, reject) => opts.signal && opts.signal.addEventListener('abort', () => reject(new Error('aborted'))));
  aborted.catch(() => {});
  inFlight[url] = (inFlight[url] || 0) + 1;
  try {
    const value = next.deferred ? await Promise.race([next.deferred, aborted]) : next;
    if (value instanceof Error) throw value;
    return { ok: value.ok, status: value.ok ? 200 : 400, json: async () => value.body };
  } finally { inFlight[url] -= 1; }
};
let prompts = [], confirms = [];
const window = { crypto: { randomUUID: () => '00000000-0000-4000-8000-' + String(calls.length).padStart(12, '0') + Math.random().toString(16).slice(2, 6) },
  prompt(q) { prompts.push(q); return 'typed text'; }, confirm(q) { confirms.push(q); return confirmAnswer; } };
let confirmAnswer = true;
const text = id => document.getElementById(id).textContent;
function run() {
  vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'),
    { document, window, fetch, setTimeout, clearTimeout, AbortController, Date, Intl, Promise, console });
}
const posts = route => calls.filter(c => c.url === route);
// A native <dialog> for the stop confirmation: close() fires "close" like a form submit.
function nativeDialog() {
  const dialog = document.getElementById('stop-dialog');
  dialog.showModal = () => { dialog.open = true; };
  dialog.close = (value) => { dialog.open = false; dialog.returnValue = value;
    (dialog.listeners.close || []).slice().forEach(f => f()); };
  return dialog;
}
const snap = (coordinators, extra = {}) => Object.assign(
  { generated_at: '2026-01-01T00:00:00+00:00', coordinators, questions: [], alerts: [], totals: {} }, extra);
const row = (id, extra = {}) => Object.assign({ id, repo_label: 'demo', state: 'running', accepted: 1, total: 2 }, extra);
const A = 'aaaaaaaa-0000-4000-8000-000000000001', B = 'bbbbbbbb-0000-4000-8000-000000000002';
"""


def _run(body: str) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("optional Node runtime unavailable")
    script = HARNESS + "\n(async () => {\n" + body + "\nconsole.log('ui ok');\n})()" \
        ".catch(e => { console.error(e); process.exitCode = 1; });\n"
    done = subprocess.run([node, "-e", script, str(WEB / "central.js")],
                          capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert done.returncode == 0 and "ui ok" in done.stdout, done.stderr + done.stdout


def test_one_refresh_in_flight_and_no_stale_overwrite_after_an_action():
    _run(r"""
    respond('/api/csrf', { csrf_token: 'tok' });
    respond('/api/snapshot', snap([row(A)], { generated_at: '2026-01-01T00:00:01+00:00' }));
    run(); await settle();
    // A slow refresh is in flight; an action meanwhile must not start a second one.
    const old = defer('/api/snapshot');
    await fire(t => t.ms === 2000);
    assert.equal(posts('/api/snapshot').length, 2);
    respond('/api/message', { ok: true, message_id: 'm' });
    respond('/api/snapshot', snap([row(A)], { generated_at: '2026-01-01T00:00:09+00:00' }));
    elements['btn-message'].click(); await settle();
    assert.equal(posts('/api/message').length, 1);
    assert.equal(posts('/api/snapshot').length, 2);
    old({ ok: true, body: snap([row(A)], { generated_at: '2026-01-01T00:00:02+00:00' }) }); await settle();
    assert(!/00:00:02/.test(document.getElementById('clock').find(e => e.tag === 'time').attributes.datetime));
    // The answer requested before the action is dropped; a fresh one follows.
    await fire(t => t.ms === 0);
    assert.equal(posts('/api/snapshot').length, 3);
    assert.match(document.getElementById('clock').find(e => e.tag === 'time').attributes.datetime, /00:00:09/);
    assert.equal(timers.filter(t => t.ms === 2000).length, 1);
    """)


def test_failure_keeps_the_last_snapshot_marked_stale_and_announces_once():
    _run(r"""
    respond('/api/csrf', { csrf_token: 'tok' });
    respond('/api/snapshot', snap([row(A, { repo_label: 'kept-repo' })]));
    run(); await settle();
    answers['/api/snapshot'] = [{ deferred: Promise.resolve(new Error('down')) }, { deferred: Promise.resolve(new Error('down')) }];
    await fire(); await fire();
    assert.match(text('coordinators'), /kept-repo/);
    assert.match(text('connection'), /stale/i);
    assert.match(document.getElementById('app').className, /stale/);
    assert.match(text('announcer'), /lost/i);
    document.getElementById('announcer').textContent = '';
    await fire();
    assert.equal(text('announcer'), '');
    """)


def test_timeout_releases_the_refresh():
    _run(r"""
    respond('/api/csrf', { csrf_token: 'tok' });
    defer('/api/snapshot');
    run(); await settle();
    await fire(t => t.ms === 8000);
    assert.match(text('connection'), /stale|lost/i);
    assert.equal(timers.filter(t => t.ms === 2000).length, 1);
    // The timed-out request is cancelled, so the next cycle is the only one pending.
    assert.equal(inFlight['/api/snapshot'], 0);
    defer('/api/snapshot');
    await fire(t => t.ms === 2000);
    assert.equal(posts('/api/snapshot').length, 2);
    assert.equal(inFlight['/api/snapshot'], 1);
    """)


def test_same_repo_cards_differ_and_a_vanished_selection_is_not_retargeted():
    _run(r"""
    respond('/api/csrf', { csrf_token: 'tok' });
    respond('/api/snapshot', snap([row(A), row(B)]));
    run(); await settle();
    const coords = text('coordinators');
    assert.match(coords, /aaaaaaaa/); assert.match(coords, /bbbbbbbb/);
    const select = elements.toolbar.find(e => e.tag === 'select');
    select.value = B; select.listeners.change.forEach(f => f());
    assert(document.getElementById('coordinators').find(e => /selected/.test(e.className) && /bbbbbbbb/.test(e.textContent)));
    respond('/api/snapshot', snap([row(A)]));
    await fire();
    assert.equal(select.value, '');
    assert(elements['btn-message'].disabled && elements['btn-stop'].disabled);
    assert.match(text('feedback'), /bbbbbbbb.*gone|gone.*bbbbbbbb/);
    elements['btn-message'].click(); await settle();
    assert.equal(posts('/api/message').length, 0);
    """)


def test_bars_are_proportional_and_values_say_when_they_are_estimates():
    _run(r"""
    respond('/api/csrf', { csrf_token: 'tok' });
    const models = [
      { role: 'exec', model: 'zero', tier: 'free', tokens_total: 0, coverage: 'measured' },
      { role: 'exec', model: 'one', tier: 'free', tokens_total: 1, coverage: 'measured' },
      { role: 'exec', model: 'big', tier: 'paid', tokens_total: 10000, estimated: true, coverage: 'estimated' },
      { role: 'exec', model: 'part', tier: 'paid', tokens_total: 5000, coverage: 'incomplete' }];
    respond('/api/snapshot', snap([row(A)], { totals: { models, total_tokens: 15001, estimated: true,
      coverage_label: 'incomplete', tokens_by_tier: { free: 1, paid: 15000 } } }));
    run(); await settle();
    const bars = document.getElementById('charts').findAll(e => /chart-bar/.test(e.className)).map(e => e.style.width);
    assert.deepEqual(bars, ['0%', '0.01%', '100%', '50%']);
    const charts = text('charts');
    assert.match(charts, /10000 tokens · includes estimates/);
    assert.match(charts, /5000 tokens · partial/);
    assert.match(text('totals'), /15001 tokens · partial/);
    """)


def test_clock_is_outside_the_live_region_and_new_questions_are_announced_once():
    _run(r"""
    respond('/api/csrf', { csrf_token: 'tok' });
    const question = { id: 'q-1', coordinator_id: A, body: 'Pick one?' };
    respond('/api/snapshot', snap([row(A)], { questions: [question] }));
    run(); await settle();
    assert.equal(document.getElementById('clock').getAttribute('aria-live'), null);
    assert.match(text('announcer'), /question/i);
    assert(!/2026/.test(text('announcer')));
    document.getElementById('announcer').textContent = '';
    respond('/api/snapshot', snap([row(A)], { questions: [question], generated_at: '2026-01-01T00:00:05+00:00' }));
    await fire();
    assert.equal(text('announcer'), '');
    """)


def test_reply_is_bound_to_its_own_question():
    _run(r"""
    respond('/api/csrf', { csrf_token: 'tok' });
    respond('/api/snapshot', snap([row(A), row(B)], { questions: [
      { id: 'q-a', coordinator_id: A, body: 'first' }, { id: 'q-b', coordinator_id: B, body: 'second' }] }));
    run(); await settle();
    const replies = document.getElementById('questions').findAll(e => e.tag === 'button' && /Reply/.test(e.textContent));
    assert.equal(replies.length, 2);
    respond('/api/answer', { ok: true, message_id: 'm' });
    respond('/api/snapshot', snap([row(A), row(B)]));
    replies[1].click(); await settle();
    const sent = posts('/api/answer')[0].body;
    assert.equal(sent.question_id, 'q-b'); assert.equal(sent.coordinator_id, B); assert.equal(sent.text, 'typed text');
    assert.match(prompts[0], /second/);
    """)


def test_reply_is_unavailable_while_another_action_runs_and_never_drops_text():
    _run(r"""
    respond('/api/csrf', { csrf_token: 'tok' });
    respond('/api/snapshot', snap([row(A)], { questions: [{ id: 'q-a', coordinator_id: A, body: 'first' }] }));
    run(); await settle();
    const reply = () => document.getElementById('questions').find(e => e.tag === 'button' && /Reply/.test(e.textContent));
    // A message is in flight: the rendered Reply is disabled and asks for nothing.
    const sent = defer('/api/message');
    elements['btn-message'].click(); await settle();
    assert(reply().disabled);
    const asked = prompts.length;
    reply().click(); await settle();
    assert.equal(prompts.length, asked);
    assert.equal(posts('/api/answer').length, 0);
    respond('/api/snapshot', snap([row(A)], { questions: [{ id: 'q-a', coordinator_id: A, body: 'first' }] }));
    sent({ ok: true, body: { ok: true, message_id: 'm' } }); await settle();
    assert(!reply().disabled);
    // Another action starts while the owner types: the typed text stays on screen.
    window.prompt = (q) => { prompts.push(q); elements['btn-pause'].click(); return 'keep this answer'; };
    defer('/api/pause');
    reply().click(); await settle();
    assert.equal(posts('/api/answer').length, 0);
    const feedback = document.getElementById('feedback');
    assert.match(feedback.className, /error/);
    assert(feedback.find(e => e.tag === 'code' && e.textContent === 'keep this answer'));
    """)


def test_stop_asks_for_confirmation_naming_the_session():
    _run(r"""
    respond('/api/csrf', { csrf_token: 'tok' });
    respond('/api/snapshot', snap([row(A)]));
    run(); await settle();
    confirmAnswer = false;
    elements['btn-stop'].click(); await settle();
    assert.equal(posts('/api/stop').length, 0);
    assert.match(confirms[0], /aaaaaaaa/);
    confirmAnswer = true;
    respond('/api/stop', { ok: true, control_id: 'c' });
    respond('/api/snapshot', snap([row(A)]));
    elements['btn-stop'].click(); await settle();
    assert.equal(posts('/api/stop')[0].body.coordinator_id, A);
    """)


def test_attach_failure_is_shown_with_the_server_command_and_a_copy_button():
    _run(r"""
    respond('/api/csrf', { csrf_token: 'tok' });
    respond('/api/snapshot', snap([row(A)]));
    run(); await settle();
    respond('/api/attach', { ok: true, queued: true, attach_id: 'att-1' });
    respond('/api/snapshot', snap([row(A)]));
    elements['btn-attach'].click(); await settle();
    assert.match(text('feedback'), /queued/i);
    respond('/api/attach/att-1', { state: 'queued' });
    await fire(t => t.ms === 1000);
    respond('/api/attach/att-1', { state: 'failed', reason: 'resume failed: timeout', command: 'acc attach ' + A + ' --central-dir "~/c d"' });
    await fire(t => t.ms === 1000);
    const feedback = document.getElementById('feedback');
    assert.match(feedback.textContent, /resume failed: timeout/);
    assert(feedback.find(e => e.tag === 'code' && e.textContent === 'acc attach ' + A + ' --central-dir "~/c d"'));
    assert(feedback.find(e => e.tag === 'button' && /Copy/.test(e.textContent)));
    """)


def test_attach_without_a_terminal_shows_the_copyable_command():
    _run(r"""
    respond('/api/csrf', { csrf_token: 'tok' });
    respond('/api/snapshot', snap([row(A)]));
    run(); await settle();
    respond('/api/attach', { ok: true, queued: false, command: 'acc attach ' + A });
    respond('/api/snapshot', snap([row(A)]));
    elements['btn-attach'].click(); await settle();
    const feedback = document.getElementById('feedback');
    assert.match(feedback.textContent, /terminal/);
    assert(feedback.find(e => e.tag === 'code' && e.textContent === 'acc attach ' + A));
    """)


def test_attach_waits_through_a_long_queue_and_session_then_reports_it_closed():
    _run(r"""
    respond('/api/csrf', { csrf_token: 'tok' });
    respond('/api/snapshot', snap([row(A)]));
    run(); await settle();
    respond('/api/attach', { ok: true, queued: true, attach_id: 'att-1' });
    respond('/api/snapshot', snap([row(A)]));
    elements['btn-attach'].click(); await settle();
    // Far longer than two minutes queued, then open: never reported as an error.
    for (let i = 0; i < 150; i++) { respond('/api/attach/att-1', { state: 'queued' }); await fire(t => t.ms === 1000); }
    assert.match(text('feedback'), /queued/i);
    for (let i = 0; i < 150; i++) { respond('/api/attach/att-1', { state: 'opened' }); await fire(t => t.ms === 1000); }
    assert.match(text('feedback'), /open in the terminal/i);
    assert(!/error/.test(document.getElementById('feedback').className));
    respond('/api/attach/att-1', { state: 'closed', exit_code: 130 });
    await fire(t => t.ms === 1000);
    assert.match(text('feedback'), /closed.*130/);
    assert(!/error/.test(document.getElementById('feedback').className));
    assert.equal(timers.filter(t => t.ms === 1000).length, 0);
    """)


def test_cards_are_compact_and_details_are_not_repeated():
    _run(r"""
    respond('/api/csrf', { csrf_token: 'tok' });
    respond('/api/snapshot', snap([row(A, { jobs: [{ id: 'j', title: 'job-title' }], scoreboard: 'board-text',
      models: [{ role: 'exec', model: 'model-x', tier: 'free', tokens_total: 3 }] })],
      { questions: [{ id: 'q', coordinator_id: A, body: 'b' }] }));
    run(); await settle();
    const cards = text('coordinators');
    assert(!/job-title|board-text|model-x/.test(cards));
    assert.match(cards, /1\/2/); assert.match(cards, /1 question/);
    assert(document.getElementById('coordinators').find(e => e.tag === 'details'));
    """)


def test_page_puts_questions_above_coordinators_and_has_the_new_landmarks():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    order = [html.index(f'id="{name}"') for name in ("alerts-section", "questions-section", "coordinators-section")]
    assert order == sorted(order)
    assert ">Coordinators<" in html and ">Cards<" not in html
    assert 'class="skip-link"' in html and 'href="#app"' in html
    assert 'id="announcer"' in html and 'id="feedback"' in html and 'id="clock"' in html
    clock = re.search(r'<[^>]*id="clock"[^>]*>', html).group(0)
    assert "aria-live" not in clock and "role=" not in clock
    assert '<dialog id="stop-dialog"' in html and 'id="stop-cancel"' in html
    assert 'rel="icon" href="/favicon.svg"' in html
    assert "<h4" not in (WEB / "central.js").read_text(encoding="utf-8")


def test_css_keeps_lanes_shared_and_long_names_wrapped():
    css = (WEB / "central.css").read_text(encoding="utf-8")
    assert "color-scheme: dark" in css
    rule = re.search(r"\.chart-bar\s*\{([^}]*)\}", css).group(1)
    assert "min-width" not in rule
    assert "minmax(0, 1fr)" in re.search(r"\.chart-row\s*\{([^}]*)\}", css).group(1)
    assert re.search(r"select\s*\{[^}]*max-width:\s*100%", css)
    assert "overflow-wrap: anywhere" in css


def test_favicon_is_a_packaged_route(tmp_path):
    from crew_chief import central_web
    assert "/favicon.svg" in central_web._ASSETS
    assert (WEB / "favicon.svg").read_text(encoding="utf-8").lstrip().startswith("<svg")
    _ = json  # keep import used when Node is missing
