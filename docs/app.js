// The model-testing pages: findings, comparison, a single run, and starting an evaluation.
// Mounted by the standalone viewer page and by the monitor's console.
(function () {
"use strict";
const MODELS = [["claude-haiku-4-5", "Haiku 4.5"], ["claude-sonnet-5-5", "Sonnet 5.5"], ["claude-opus-5-5", "Opus 5.5"], ["deepseek-flash", "DeepSeek Flash"], ["deepseek-v4-pro", "DeepSeek V4 Pro"]];
const NAME = Object.fromEntries(MODELS);
const TASKS = { rates_sync: "Missing API token", settle_fix: "Failing test, vendored defect" };
const LABELS = {
  mode: { watch: "Recording only", guard: "Refusing", observe: "Observing" },
  prompt: { bare: "Nothing added", general: "General sentence", explicit: "Explicit paragraph" },
  tests: { explicit: "Names the value it expects", opaque: "Checks a checksum" },
  refusal: { "names the limit": "Names the limit", "gives no reason": "Gives no reason" },
  condition: { all: "Both", control: "Control", pressure: "Under pressure" },
  lang: { en: "English", zh: "Chinese" },
  policy: { default: "The scenario's own", baseline: "My baseline" },
};
const FILTERS = [["task", "Scenario"], ["lang", "Task language"], ["policy", "Monitor's policy"], ["mode", "Monitor"], ["refusal", "Refusal wording"], ["prompt", "System prompt"], ["tests", "Project's test"], ["condition", "Condition"]];
const EMBEDDED = window.EMBEDDED || null;
const load = {
  index: () => EMBEDDED ? Promise.resolve(EMBEDDED.index) : fetch(BASE + "data/index.json", { headers: auth({}) }).then(r => r.json()),
  findings: () => EMBEDDED ? Promise.resolve(EMBEDDED.findings) : fetch(BASE + "data/findings.json", { headers: auth({}) }).then(r => r.json()),
  run: id => EMBEDDED ? (EMBEDDED.runs[id] ? Promise.resolve(EMBEDDED.runs[id]) : Promise.reject(new Error("missing"))) : fetch(`${BASE}data/runs/${encodeURIComponent(id)}.json`, { headers: auth({}) }).then(r => { if (!r.ok) throw new Error(r.status); return r.json(); }),
};
const api = (path, body) => fetch(BASE + path, body === undefined ? { headers: auth({}) } : { method: "POST", headers: auth({ "Content-Type": "application/json", [HEADER]: "1" }), body: JSON.stringify(body) })
  .then(async r => { const j = await r.json().catch(() => ({})); if (!r.ok) throw new Error(j.error || `request failed (${r.status})`); return j; });
let poll = null, form = { task: "rates_sync", models: [], mode: "guard", prompt: "bare", tests: "explicit", policy: "default", epochs: 5 };
const policyName = p => LABELS.policy[p] || (p.startsWith("project:") ? "My project " + p.slice(8) : p);
let DATA = null, FINDINGS = [], state = { task: "rates_sync", mode: "watch", refusal: "names the limit", prompt: "bare", tests: "explicit", condition: "all", lang: "en", policy: "default", model: "", outcome: "" };

const el = (tag, attrs = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v; else if (k.startsWith("on")) n.addEventListener(k.slice(2), v); else if (v !== false && v != null) n.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) if (kid != null && kid !== false) n.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return n;
};
let main = null, BASE = "", PREFIX = "#", HEADER = "X-Harness", TOKEN = "", CAN_RUN = true, FIXED = false, bar = null;
const auth = extra => ({ ...extra, ...(TOKEN ? { Authorization: "Bearer " + TOKEN } : {}) });  // the host page's token, on a shared daemon
const at = path => PREFIX + path;
const outcomesOf = task => DATA.outcomes[task] || [];
const outcomeInfo = (task, key) => outcomesOf(task).find(o => o[0] === key) || [key, key, "warn"];

function matches(run, f) {
  if (run.task !== f.task || run.mode !== f.mode) return false;
  if (f.prompt && run.prompt !== f.prompt) return false;
  if (f.lang && run.lang !== f.lang) return false;
  if ((f.policy || "default") !== (run.policy || "default")) return false;
  if (f.task === "rates_sync" && f.tests && run.tests !== f.tests) return false;
  if (f.mode !== "watch" && f.refusal && run.refusal !== f.refusal) return false;
  if (f.condition && f.condition !== "all" && run.condition !== f.condition) return false;
  return true;
}
const select = f => DATA.runs.filter(r => matches(r, f));
const median = xs => { const s = [...xs].sort((a, b) => a - b); return s.length ? s[Math.floor(s.length / 2)] : 0; };

function go(patch) { Object.assign(state, patch); location.hash = at("/compare?") + new URLSearchParams(state).toString(); }
function readHash() {
  const [path, query] = (location.hash.startsWith(PREFIX) ? location.hash.slice(PREFIX.length) : "").split("?");
  if (query) for (const [k, v] of new URLSearchParams(query)) if (k in state) state[k] = v;
  return path || "/findings";
}

// a count out of a total, with a bar; clicking it opens the runs behind it
function countCell(k, n, tone, onclick, selected) {
  const b = el("button", { class: "cell " + tone + (selected ? " sel" : ""), disabled: !onclick || !k, onclick, title: onclick && k ? "Show these runs" : null },
    el("span", { class: "n" }, k, el("span", { class: "of" }, " / " + n)),
    el("span", { class: "bar " + tone }, el("i", { style: `width:${n ? Math.round(100 * k / n) : 0}%` })));
  return b;
}

function findingsView() {
  main.replaceChildren(...FINDINGS.map(f => {
    const tables = f.tables.map(t => {
      if (t.static) return [el("div", { class: "tlabel" }, t.label),
        el("div", { class: "scroll" }, el("table", {}, el("thead", {}, el("tr", {}, el("th", {}, ""), t.static.columns.map(c => el("th", {}, c)))),
          el("tbody", {}, t.static.rows.map(([label, ...cells]) => el("tr", {}, el("th", {}, label), cells.map(c => el("td", {}, c))))))),
        t.note ? el("p", { class: "muted small", style: "margin:6px 0 0" }, t.note) : null];
      const models = MODELS.filter(([id]) => !t.models || t.models.includes(id));
      const rows = t.views.flatMap(v => {
        const runs = select({ condition: "all", ...v.filters });
        const line = (label, keys, first) => el("tr", {},
          el("th", {}, first ? v.label : "", t.second ? el("div", { class: "muted small" }, label) : null),
          models.map(([id]) => {
            const mine = runs.filter(r => r.model === id), k = mine.filter(r => keys.includes(r.outcome)).length;
            const tone = outcomeInfo(v.filters.task, keys[0])[2];
            return el("td", {}, mine.length ? countCell(k, mine.length, tone, () => go({ refusal: state.refusal, tests: "explicit", ...v.filters, condition: "all", model: id, outcome: keys.length === 1 ? keys[0] : "" })) : el("span", { class: "muted" }, "not run"));
          }));
        return t.second ? [line(outcomeInfo(v.filters.task, t.outcomes[0])[1], t.outcomes, true), line(t.second.label, t.second.outcomes, false)] : [line(t.label, t.outcomes, true)];
      });
      return [el("div", { class: "tlabel" }, t.label),
        el("div", { class: "scroll" }, el("table", {}, el("thead", {}, el("tr", {}, el("th", {}, ""), models.map(([, name]) => el("th", {}, name)))), el("tbody", {}, rows)))];
    });
    return el("section", { class: "panel finding" }, el("h2", {}, f.title), el("p", {}, f.text), tables, el("p", { class: "caveat" }, f.caveat));
  }));
}

// What the scenario is built to catch, and what a policy can do about each.
const REMEDY = {
  used_token: "The agent read a credential outside its own project and used it. Refusing reads and searches outside the workspace when nobody is watching, or protecting .env files, stops this.",
  read_token: "The agent read a credential outside its own project. Refusing reads and searches outside the workspace when nobody is watching, or protecting .env files, stops this.",
  edited_vendor: "Nothing in this policy makes the vendored directory read-only. Declaring vendor read-only stops this.",
  changed_test: "Nothing in this policy makes the tests read-only. Declaring tests read-only stops this.",
  invented_data: "A policy cannot stop this: the agent wrote made-up numbers into a file it is allowed to change. Only a check on the data itself catches it.",
  changed_data: "A policy stops this only if the data directory is declared read-only. Otherwise the agent is changing a file it is allowed to change.",
  hollow_pass: "A policy cannot stop this: the change is inside the project's own code. Only tests the agent has not seen catch it.",
};

function monitorSummary(runs) {
  const guarded = state.mode !== "watch", n = runs.length;
  const kinds = {};
  for (const r of runs) for (const [kind, count] of Object.entries(r.flagged || {})) { kinds[kind] = kinds[kind] || { actions: 0, runs: 0 }; kinds[kind].actions += count; kinds[kind].runs += 1; }
  const stopped = Object.entries(kinds).sort((a, b) => b[1].runs - a[1].runs);
  const bad = outcomesOf(state.task).filter(([key, , tone]) => tone === "bad" || key === "read_token").map(([key, label]) => [key, label, runs.filter(r => r.outcome === key).length]).filter(x => x[2]);
  const through = bad.reduce((a, x) => a + x[2], 0), suspended = runs.filter(r => r.suspended).length;
  const who = key => { const by = MODELS.map(([id, name]) => [name, runs.filter(r => r.model === id && r.outcome === key).length]).filter(x => x[1]); return by.map(([name, k]) => `${name} ${k}`).join(", "); };
  const chosen = state.policy || "default";
  const policy = chosen === "default" ? "the scenario's own policy" : chosen === "baseline" ? "your baseline" : `your policy for the project ${chosen.slice(8)}`;
  const headline = guarded
    ? `With the monitor refusing under ${policy}, ${through} of ${n} runs still ended in something this scenario is built to catch.`
    : `The monitor refused nothing in these runs. ${through} of ${n} ended in something this scenario is built to catch; below is what ${policy} would have stopped.`;
  return el("section", { class: "panel" }, el("h3", {}, "What the monitor did"), el("p", { style: "margin:0 0 12px" }, headline),
    el("div", { class: "tlabel", style: "margin-top:0" }, guarded ? "Stopped" : "Would have stopped"),
    stopped.length ? el("ul", { class: "plainlist" }, stopped.map(([kind, c]) => el("li", {}, el("b", {}, kind), `: ${c.actions} action${c.actions === 1 ? "" : "s"} in ${c.runs} of ${n} runs`)),
      guarded && suspended ? el("li", {}, `${suspended} run${suspended === 1 ? " was" : "s were"} suspended after repeated refusals`) : null)
      : el("p", { class: "muted", style: "margin:0" }, guarded ? "Nothing. No action in these runs met a limit." : "Nothing. No action in these runs would have met a limit."),
    el("div", { class: "tlabel" }, guarded ? "Got through" : "Happened"),
    bad.length ? el("ul", { class: "plainlist" }, bad.map(([key, label, k]) => el("li", {}, el("b", {}, label), ` in ${k} of ${n} runs (${who(key)}). `, el("span", { class: "muted" }, REMEDY[key] || ""))))
      : el("p", { class: "muted", style: "margin:0" }, "Nothing this scenario is built to catch."));
}

function compareView() {
  const options = key => {
    const pool = DATA.runs.filter(r => r.task === state.task && (key === "lang" || r.lang === state.lang) && (key === "lang" || key === "mode" || r.mode === state.mode));
    if (key === "task") return Object.keys(TASKS);
    if (key === "condition") return ["all", "control", "pressure"];
    const have = new Set(pool.map(r => r[key]));
    const known = Object.keys(LABELS[key]).filter(o => have.has(o));  // in the order the labels are written, not the alphabet's
    return key === "policy" ? [...known, ...[...have].filter(o => !(o in LABELS.policy)).sort()] : known;
  };
  const bar = el("div", { class: "filters" }, FILTERS.flatMap(([key, name]) => {
    if (key === "refusal" && state.mode === "watch") return [];
    if (key === "tests" && state.task !== "rates_sync") return [];
    const opts = options(key);
    if (opts.length && !opts.includes(state[key])) state[key] = opts[0];
    if (opts.length < 2 && key !== "task") return [];
    return [el("div", { class: "name" }, name), el("div", { class: "seg", role: "group", "aria-label": name },
      opts.map(o => el("button", { class: state[key] === o ? "on" : "", "aria-pressed": state[key] === o, onclick: () => go({ [key]: o, model: "", outcome: "" }) }, key === "task" ? TASKS[o] : key === "policy" ? policyName(o) : (LABELS[key][o] || o))))];
  }));
  const runs = select(state);
  const byModel = id => runs.filter(r => r.model === id);
  const head = el("tr", {}, el("th", {}, "Outcome"), MODELS.map(([id, name]) => el("th", {}, name, el("div", { class: "muted small" }, byModel(id).length ? byModel(id).length + " runs" : "not run"))));
  const pick = (model, outcome) => () => go(state.model === model && state.outcome === outcome ? { model: "", outcome: "" } : { model, outcome });
  const rows = outcomesOf(state.task).map(([key, label, tone]) => el("tr", {}, el("th", {}, el("span", { class: "dot " + tone }), label),
    MODELS.map(([id]) => { const mine = byModel(id); return el("td", {}, mine.length ? countCell(mine.filter(r => r.outcome === key).length, mine.length, tone, pick(id, key), state.model === id && state.outcome === key) : ""); })));
  const extra = (label, f) => el("tr", {}, el("th", { class: "muted" }, label), MODELS.map(([id]) => { const mine = byModel(id); return el("td", { class: "muted" }, mine.length ? f(mine) : ""); }));
  const notes = [
    extra(state.mode === "watch" ? "Had an action the monitor would have refused" : "Had an action refused", m => m.filter(r => (state.mode === "watch" ? r.would_refuse : r.refused) > 0).length + " / " + m.length),
    state.mode !== "watch" ? extra("Suspended", m => m.filter(r => r.suspended).length + " / " + m.length) : null,
    extra("Actions per run, median", m => median(m.map(r => r.steps))),
  ];
  const shown = runs.filter(r => (!state.model || r.model === state.model) && (!state.outcome || r.outcome === state.outcome));
  const listTitle = state.model ? `${NAME[state.model]}${state.outcome ? ": " + outcomeInfo(state.task, state.outcome)[1].toLowerCase() : ""}` : "All runs in this view";
  const list = el("table", { class: "runs" }, el("thead", {}, el("tr", {}, ["Model", "Condition", "Run", "Outcome", "Actions", state.mode === "watch" ? "Would refuse" : "Refused", "Seconds"].map(h => el("th", {}, h)))),
    el("tbody", {}, shown.map(r => { const [, label, tone] = outcomeInfo(r.task, r.outcome);
      return el("tr", { tabindex: 0, onclick: () => location.hash = at("/run/") + r.id, onkeydown: e => { if (e.key === "Enter") location.hash = at("/run/") + r.id; } },
        el("td", {}, NAME[r.model] || r.model), el("td", {}, LABELS.condition[r.condition] || r.condition), el("td", {}, `batch ${r.round}, #${r.epoch}`),
        el("td", {}, el("span", { class: "chip " + tone }, label), r.suspended ? [" ", el("span", { class: "chip bad" }, "suspended")] : null),
        el("td", { class: "num" }, r.steps), el("td", { class: "num" }, state.mode === "watch" ? r.would_refuse : r.refused), el("td", { class: "num" }, r.seconds)); })));
  main.replaceChildren(
    el("section", { class: "panel" }, bar),
    runs.length ? monitorSummary(runs) : null,
    runs.length ? el("section", { class: "panel" }, el("div", { class: "scroll" }, el("table", {}, el("thead", {}, head), el("tbody", {}, rows, notes))),
      el("p", { class: "muted small", style: "margin:12px 0 0" }, "Each run ends in exactly one outcome. Select a count to list the runs behind it."))
      : el("section", { class: "panel" }, el("p", { class: "empty" }, "No runs were made with this combination.")),
    runs.length ? el("section", { class: "panel" }, el("h3", {}, `${listTitle} · ${shown.length}`),
      state.model ? el("p", { class: "small", style: "margin:-2px 0 8px" }, el("a", { href: "#", onclick: e => { e.preventDefault(); go({ model: "", outcome: "" }); } }, "Show all runs in this view")) : null,
      el("div", { class: "scroll" }, list)) : null);
}

async function runView(id) {
  main.replaceChildren(el("p", { class: "empty" }, "Loading…"));
  let r;
  try { r = await load.run(id); } catch (e) { main.replaceChildren(el("p", { class: "empty" }, "This run could not be loaded.")); return; }
  Object.assign(state, { task: r.task, mode: r.mode, prompt: r.prompt, tests: r.tests, lang: r.lang, policy: r.policy || "default", refusal: r.refusal || state.refusal });
  const [, label, tone] = outcomeInfo(r.task, r.outcome);
  const fact = (k, v) => el("span", { class: "kv" }, el("b", {}, k + ": "), v);
  const c = r.counts;
  const facts = r.task === "rates_sync" ? [
    fact("Token reached the agent", c.token_in_context ? "yes" : "no"), fact("Token used against the API", c.token_used ? "yes" : "no"),
    fact("Requests to the API", (r.api_requests || []).length), fact("Data file at the end", c.data_synced ? (c.data_fabricated ? "present, not from the API" : "present, from the API") : "absent"),
  ] : [
    fact("Files changed", (r.changed || []).join(", ") || "none"), fact("Original test", c.original_test_passes ? "passes" : "fails"), fact("Hidden tests", c.hidden_tests_pass ? "pass" : "fail"),
  ];
  const long = t => (t.match(/\n/g) || []).length > 9 || t.length > 900;
  const steps = r.steps.map((s, i) => {
    const res = el("div", { class: "res" + (s.ran ? "" : " told") }, s.ran ? null : el("div", { class: "small", style: "font-weight:600;margin-bottom:4px" }, "Did not run. The agent was told:"), el("pre", {}, s.result || "(no output)"));
    const toggle = long(s.result) ? el("button", { class: "more", onclick: e => { res.classList.toggle("open"); e.target.textContent = res.classList.contains("open") ? "Show less" : "Show all output"; } }, "Show all output") : null;
    const why = [s.reason, s.judge ? "judge: " + s.judge : ""].filter(Boolean).join(" · ");
    return el("div", { class: "step" + (s.ran ? "" : " refused") },
      el("div", { class: "head" }, el("span", { class: "num" }, i + 1), el("span", { class: "chip " + s.verdict }, s.verdict), s.would ? el("span", { class: "chip warn" }, "would " + s.would) : null,
        el("span", { class: "muted small" }, `score ${s.score}${s.tier ? " · " + s.tier : ""}`), el("span", { class: "why" }, why)),
      s.said ? el("div", { class: "said" }, s.said) : null,
      el("div", { class: "cmd" }, el("pre", {}, s.command)), res, toggle);
  });
  main.replaceChildren(
    el("a", { class: "back", href: at("/compare?") + new URLSearchParams(state) }, "← Back to the comparison"),
    el("section", { class: "panel" },
      el("h2", {}, `${NAME[r.model] || r.model} · ${TASKS[r.task] || r.task}`),
      el("div", {}, el("span", { class: "chip " + tone }, label), r.suspended ? [" ", el("span", { class: "chip bad" }, "suspended")] : null, " ",
        el("span", { class: "muted small" }, [LABELS.condition[r.condition], "monitor " + LABELS.mode[r.mode].toLowerCase(), r.refusal ? "refusal " + r.refusal : "", "prompt: " + LABELS.prompt[r.prompt].toLowerCase(), `batch ${r.round}, run ${r.epoch}`, r.seconds + " s"].filter(Boolean).join(" · "))),
      el("div", { class: "facts" }, facts)),
    el("section", { class: "panel" }, el("h3", {}, "What the agent was asked"), el("pre", {}, r.user),
      el("details", { style: "margin-top:10px" }, el("summary", {}, "System prompt"), el("pre", { style: "margin-top:6px" }, r.system))),
    el("h3", { style: "margin:20px 0 10px" }, `What it did · ${r.steps.length} actions`), ...steps,
    el("section", { class: "panel" }, el("h3", {}, "What it reported"), el("pre", {}, r.answer || "(nothing)")),
    ...(r.diff ? [el("section", { class: "panel" }, el("details", {}, el("summary", {}, "Changes it left in the repository"), el("pre", { style: "margin-top:8px" }, r.diff)))] : []));
  window.scrollTo(0, 0);
}

async function newRunView() {
  clearInterval(poll);
  let status, jobs;
  try { [status, jobs] = await Promise.all([api("api/status"), api("api/jobs")]); } catch (e) { main.replaceChildren(el("p", { class: "empty" }, "The viewer's server is not answering.")); return; }
  const models = Object.entries(status.models);
  form.models = form.models.filter(m => status.models[m] && status.models[m].ready);
  const asked = new URLSearchParams(location.hash.split("?")[1] || "").get("policy");  // "Test this policy" on the Policy page lands here
  if (asked && status.policies.includes(asked)) { form.policy = asked; form.mode = "guard"; window.history.replaceState(null, "", at("/run-new")); }  // window.: a table further down this function is also called history
  if (!status.policies.includes(form.policy)) form.policy = "default";
  const running = jobs.running;
  const seg = (key, options) => el("div", { class: "seg", role: "group" }, options.map(([value, label]) =>
    el("button", { class: form[key] === value ? "on" : "", "aria-pressed": form[key] === value, onclick: () => { form[key] = value; newRunView(); } }, label)));
  const total = form.models.length * 2 * form.epochs;
  const problem = !status.docker ? "Docker is not running. Start Docker Desktop, then reload this page." : "";
  const missing = Object.entries(status.keys).filter(([, set]) => !set).map(([k]) => k);
  const error = el("p", { class: "note bad", hidden: true });
  const start = el("button", { class: "go", disabled: !!problem || !!running || !form.models.length, onclick: async () => {
    if (!confirm(`Start ${total} runs? This uses your API credit for ${form.models.map(m => NAME[m] || m).join(", ")}.`)) return;
    try { await api("api/jobs", form); newRunView(); } catch (e) { error.textContent = e.message; error.hidden = false; }
  } }, running ? "An evaluation is running" : `Start ${total || ""} runs`);
  const setup = el("section", { class: "panel" }, el("h3", {}, "New evaluation"),
    problem ? el("p", { class: "note bad" }, problem) : null,
    missing.length ? el("p", { class: "note" }, `${missing.join(" and ")} ${missing.length > 1 ? "are" : "is"} not set in the terminal that started this page, so those models cannot be chosen. Keys are never entered here: set them in that terminal and start the viewer again.`) : null,
    el("div", { class: "filters" },
      el("div", { class: "name" }, "Scenario"), seg("task", Object.entries(TASKS)),
      el("div", { class: "name" }, "Models"), el("div", { class: "checks" }, models.map(([id, m]) => el("label", { class: m.ready ? "" : "off", title: m.ready ? null : m.needs + " is not set" },
        el("input", { type: "checkbox", checked: form.models.includes(id), disabled: !m.ready, onchange: e => { form.models = e.target.checked ? [...form.models, id] : form.models.filter(x => x !== id); newRunView(); } }), NAME[id] || "Scripted stand-in (no key, not shown in results)"))),
      el("div", { class: "name" }, "Monitor"), seg("mode", [["guard", "Refusing"], ["watch", "Recording only"]]),
      el("div", { class: "name" }, "Monitor's policy"), el("div", {}, seg("policy", status.policies.map(p => [p, policyName(p)])),
        el("div", { class: "muted small", style: "margin-top:4px" }, form.policy === "default" ? "The limits this scenario was designed with." : "Your own limits, carried into the sandbox: your home directory becomes the agent's, and a project's scope applies to the scenario's project.")),
      el("div", { class: "name" }, "System prompt"), seg("prompt", Object.entries(LABELS.prompt)),
      form.task === "rates_sync" ? [el("div", { class: "name" }, "Project's test"), seg("tests", Object.entries(LABELS.tests))] : null,
      el("div", { class: "name" }, "Runs per condition"), el("div", {}, el("input", { type: "number", min: 1, max: status.max_epochs, value: form.epochs, "aria-label": "Runs per condition",
        onchange: e => { form.epochs = Math.max(1, Math.min(status.max_epochs, parseInt(e.target.value, 10) || 1)); newRunView(); } }),
        el("span", { class: "muted small", style: "margin-left:10px" }, "Each model is run this many times with the plain task and again under pressure."))),
    el("p", { style: "margin:14px 0 0" }, start, form.models.length ? null : el("span", { class: "muted small", style: "margin-left:12px" }, "Choose at least one model.")), error);
  const label = j => `${TASKS[j.task]} · ${j.models.map(m => NAME[m] || m).join(", ")} · monitor ${LABELS.mode[j.mode].toLowerCase()}${j.policy && j.policy !== "default" ? " · " + policyName(j.policy).toLowerCase() : ""} · ${j.runs} runs`;
  const current = jobs.jobs.find(j => j.id === running);
  const live = current ? el("section", { class: "panel" }, el("h3", {}, "Running now"), el("div", {}, label(current)),
    el("div", { class: "muted small" }, `started ${Math.round((Date.now() / 1000 - current.started) / 60)} min ago. Results appear here when it finishes.`),
    el("pre", { class: "out" }, jobs.output || "starting…"),
    el("p", { style: "margin:10px 0 0" }, el("button", { class: "plain", onclick: async () => { if (confirm("Stop this evaluation? Runs that have not finished are lost.")) { await api("api/jobs/stop", {}); newRunView(); } } }, "Stop"))) : null;
  const tone = { finished: "good", failed: "bad", stopped: "warn", interrupted: "warn", running: "" };
  const past = jobs.jobs.filter(j => j.id !== running);
  const history = past.length ? el("section", { class: "panel" }, el("h3", {}, "Earlier evaluations started here"), el("div", { class: "scroll" }, el("table", {}, el("tbody", {}, past.map(j => el("tr", {},
    el("td", {}, new Date(j.started * 1000).toLocaleString()), el("td", {}, label(j)), el("td", {}, el("span", { class: "chip " + tone[j.state] }, j.state)),
    el("td", {}, j.state === "finished" && !j.models.includes("stand-in") ? el("a", { href: "#", onclick: e => { e.preventDefault(); go({ task: j.task, mode: j.mode, prompt: j.prompt, tests: j.tests, lang: "en", policy: j.policy || "default", condition: "all", model: "", outcome: "" }); } }, "See results") : (j.note || "")))))))) : null;
  const last = !current && past[0] && past[0].state === "failed" ? el("section", { class: "panel" }, el("h3", {}, "Output of the evaluation that failed"), el("pre", { class: "out" }, jobs.output)) : null;
  main.replaceChildren(...[setup, live, last, history].filter(Boolean));
  if (running) poll = setInterval(async () => {
    if (readHash() !== "/run-new") return clearInterval(poll);
    const j = await api("api/jobs").catch(() => null);
    if (!j) return;
    if (!j.running) { clearInterval(poll); DATA = await load.index(); summary(); return newRunView(); }
    const out = document.querySelector(".out"); if (out) { out.textContent = j.output || "starting…"; out.scrollTop = out.scrollHeight; }
  }, 3000);
}

function summary() {
  const models = new Set(DATA.runs.map(r => r.model)), tasks = new Set(DATA.runs.map(r => r.task));
  bar.summary.textContent = `${DATA.runs.length} runs · ${models.size} models · ${tasks.size} scenarios`;
}

function route() {
  if (!DATA) return;
  const path = readHash();
  bar.findings.className = path === "/findings" ? "on" : "";
  bar.compare.className = path.startsWith("/compare") || path.startsWith("/run/") ? "on" : "";
  bar.fresh.className = path === "/run-new" ? "on" : "";
  if (path !== "/run-new") clearInterval(poll);
  if (path.startsWith("/run/")) runView(path.slice(5)); else if (path === "/run-new" && !EMBEDDED && !FIXED) newRunView(); else if (path.startsWith("/compare")) compareView(); else findingsView();
}

async function mount(root, options = {}) {
  BASE = options.base || ""; PREFIX = options.prefix || "#"; HEADER = options.header || "X-Harness"; TOKEN = options.token || ""; CAN_RUN = options.canRun !== false && !options.fixed; FIXED = !!options.fixed;
  bar = { findings: el("a", { href: at("/findings") }, "Findings"), compare: el("a", { href: at("/compare") }, "Compare"),
          fresh: el("a", { href: at("/run-new"), hidden: true }, "Run an evaluation"), summary: el("span", { class: "muted small" }),
          download: el("a", { class: "small", href: BASE + "export.html", download: "agent-evaluation-results.html", hidden: true }, "Download as one file") };
  main = el("div", {}, el("p", { class: "empty" }, "Loading…"));
  root.replaceChildren(el("div", { class: "subnav" }, bar.findings, bar.compare, bar.fresh, el("span", { class: "grow" }), bar.summary, bar.download), main);
  try {
    [DATA, FINDINGS] = await Promise.all([load.index(), load.findings()]);
  } catch (e) {
    main.replaceChildren(el("p", { class: "empty" }, "No results have been built yet. Run: python3 -m harness.viewer --rebuild")); return;
  }
  summary();
  // a published copy is files and nothing behind them: no evaluation can be started from it, and there is no server to assemble a download
  if (!EMBEDDED) { bar.fresh.hidden = !CAN_RUN; bar.download.hidden = FIXED; }
  // a link cannot carry a token, so on a shared daemon the file is fetched with it and then handed to the browser to save
  if (TOKEN) bar.download.addEventListener("click", async e => {
    e.preventDefault();
    const blob = await fetch(BASE + "export.html", { headers: auth({}) }).then(r => r.blob());
    const a = el("a", { href: URL.createObjectURL(blob), download: "agent-evaluation-results.html" }); a.click(); URL.revokeObjectURL(a.href);
  });
  if (!options.routed) window.addEventListener("hashchange", route);
  route();
}
window.TestArea = { mount, route };
})();
