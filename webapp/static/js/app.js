/* The four-step wizard, mirroring the desktop app's flow. */
/* global React, ReactDOM, htm, api */

const { useState, useEffect, useMemo } = React;
const html = htm.bind(React.createElement);

const ANONYMIZE_NOTE =
  "ANONYMIZE replaces each person with a realistic, invented name - " +
  '"John Michael Smith" becomes something like "Tamsin Quentin Middleton", ' +
  "consistently in every document in this batch, and family members keep a " +
  "shared surname. The document still reads like a normal pleading, so a " +
  "reader may not realize it has been altered unless you tell them. " +
  "Everything that is not a person's name (SSNs, accounts, addresses, case " +
  "numbers) becomes a tagged placeholder such as [SSN-1].";

const REDACT_NOTE =
  "REDACT removes the text outright and marks it [REDACTED] on a black bar. " +
  "Nothing is invented and nothing is recoverable from the file.";

const PDF_NOTE =
  "PDFs are always redacted, never anonymized: the glyphs are physically " +
  "deleted from the page and a black box is drawn over the space.";

const OPTION_LABELS = [
  ["metadata", "Document metadata (author, company, timestamps, custom properties)"],
  ["comments", "Comments and tracked changes"],
  ["embedded", "Hyperlink targets, bookmarks, attachments, embedded scripts"],
  ["filenames", "Client identifiers in the file names themselves"],
  ["images", "Every embedded image and drawn-ink handwriting (blacked out whole)"],
  ["ocr", "OCR scanned PDFs that have no text layer"],
  ["ner", "Ask the offline model to suggest additional names"],
  ["labels", "Label each PDF black box with its category"],
];

const DEFAULT_OPTIONS = {
  docx_mode: "anonymize", metadata: true, comments: true, embedded: true,
  filenames: true, images: true, ocr: true, ner: true, labels: false,
  allowlist: "",
};

function generatePassword(length = 20) {
  const alphabet =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789!@#$%^&*-_=+";
  const bytes = new Uint32Array(length);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => alphabet[b % alphabet.length]).join("");
}

function parseNameLines(text) {
  const out = [];
  for (const raw of text.split("\n")) {
    let line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    let category = "person";
    if (line.includes("|")) {
      const cut = line.lastIndexOf("|");
      const marker = line.slice(cut + 1).trim().toLowerCase();
      line = line.slice(0, cut).trim();
      if (["minor", "minor child", "child"].includes(marker)) category = "minor";
      else if (["organization", "org", "employer", "school"].includes(marker)) category = "organization";
      else if (["location", "place", "address", "gpe"].includes(marker)) category = "location";
    }
    if (line) out.push({ name: line, category });
  }
  return out;
}

function nameLine(name, category) {
  return category === "person" ? name : `${name} | ${category}`;
}

/* ------------------------------------------------------------ components -- */

function Stepper({ step, unlocked, go }) {
  const labels = ["Documents & options", "Names", "Review", "Run"];
  return html`<nav class="stepper">
    ${labels.map((label, index) => html`
      <div key=${index}
           class=${"step" + (index === step ? " active" : "") + (index > unlocked ? " locked" : "")}
           onClick=${() => index <= unlocked && go(index)}>
        <span class="n">${index + 1}</span> ${label}
      </div>`)}
  </nav>`;
}

function Progress({ busy }) {
  if (!busy.active) return null;
  const known = busy.fraction > 0;
  return html`<div class="progress">
    <div class="track">
      <div class=${"fill" + (known ? "" : " pulse")}
           style=${{ width: known ? `${Math.round(busy.fraction * 100)}%` : "30%" }}></div>
    </div>
    <span class="msg">${busy.message}</span>
  </div>`;
}

function FilesStep({ state, set, actions }) {
  const [over, setOver] = useState(false);
  const pick = () => document.getElementById("filepick").click();
  const onFiles = (list) => list.length && actions.upload(list);
  return html`
    <div class="columns">
      <div class="card">
        <h2>Documents (.docx and .pdf)</h2>
        <div class=${"dropzone" + (over ? " over" : "")}
             onClick=${pick}
             onDragOver=${(e) => { e.preventDefault(); setOver(true); }}
             onDragLeave=${() => setOver(false)}
             onDrop=${(e) => { e.preventDefault(); setOver(false); onFiles([...e.dataTransfer.files]); }}>
          Drop documents here, or click to choose. They stay on this computer.
          <input id="filepick" type="file" multiple accept=".docx,.pdf" hidden
                 onChange=${(e) => { onFiles([...e.target.files]); e.target.value = ""; }} />
        </div>
        <ul class="files">
          ${state.files.map((f, i) => html`<li key=${i}>
            <span class="kind">${(f.kind || "?").toUpperCase()}</span>
            <span class="name">${f.name}</span>
            <span class="hint">${f.kind === "docx" ? "anonymize or redact" : "redact"}</span>
            <button onClick=${() => actions.removeFile(i)}>Remove</button>
          </li>`)}
        </ul>
      </div>
      <div>
        <div class="card">
          <h2>What to do with DOCX files</h2>
          <label class="check"><input type="radio" name="mode"
            checked=${state.options.docx_mode === "anonymize"}
            onChange=${() => set.option("docx_mode", "anonymize")} /> Anonymize (realistic fake names)</label>
          <label class="check"><input type="radio" name="mode"
            checked=${state.options.docx_mode === "redact"}
            onChange=${() => set.option("docx_mode", "redact")} /> Redact (black bars, nothing invented)</label>
          <p class=${state.options.docx_mode === "anonymize" ? "warn" : "hint"}>
            ${state.options.docx_mode === "anonymize" ? ANONYMIZE_NOTE : REDACT_NOTE}</p>
          <p class="hint">${PDF_NOTE}</p>
        </div>
        <div class="card">
          <h2>Also scrub</h2>
          ${OPTION_LABELS.map(([key, label]) => html`
            <label class="check" key=${key}>
              <input type="checkbox" checked=${state.options[key]}
                     onChange=${(e) => set.option(key, e.target.checked)} /> ${label}
            </label>`)}
        </div>
        <div class="card">
          <h2>Mapping key password</h2>
          <div style=${{ display: "flex", gap: "8px" }}>
            <input type=${state.showPassword ? "text" : "password"} value=${state.password}
                   onInput=${(e) => set.password(e.target.value)} />
            <button onClick=${() => set.password(generatePassword())}>New</button>
          </div>
          <label class="check"><input type="checkbox" checked=${state.showPassword}
                 onChange=${(e) => set.showPassword(e.target.checked)} /> Show</label>
          <p class="hint">The original-to-replacement table is encrypted with this
            password and offered as its own download, never inside the archive.
            Copy it somewhere safe - without it the mapping cannot be recovered.</p>
        </div>
        <div class="card">
          <h2>Never change these terms (one per line)</h2>
          <textarea rows="3" value=${state.options.allowlist}
                    onInput=${(e) => set.option("allowlist", e.target.value)}></textarea>
          <p class="hint">Courts, judges, commissioners, statutes, rules and
            reported citations are already protected automatically.</p>
        </div>
      </div>
    </div>
    <div class="bar">
      <span class="spacer"></span>
      <button class="primary" disabled=${state.busy.active}
              onClick=${actions.toNames}>Continue to names →</button>
    </div>`;
}

// What the run will deliberately leave alone, and which ticked names were
// folded into a longer one. Both are decisions made for the operator, so both
// get said out loud rather than happening quietly.
function GuardPanel({ state }) {
  const bench = state.officials || [];
  const notes = [...(state.guardNotes || []),
                 ...(state.overlaps || []).map((o) => o.note)];
  if (!bench.length && !notes.length) return null;
  return html`
    <div class="card guard">
      <h2>Left alone</h2>
      ${bench.length ? html`
        <p>Judicial officers found in these documents. A judge is not a party, and
          an order that comes back with the bench renamed reads as tampered with -
          so every written form of these names survives the run.</p>
        <ul class="bench">
          ${bench.map((o, i) => html`<li key=${i}><strong>${o.name}</strong>
            <span class="hint">${o.title}${o.confidence === "medium" ? " · from the signature block" : ""}</span></li>`)}
        </ul>` : null}
      ${notes.length ? html`<ul class="guard-notes">
        ${notes.map((note, i) => html`<li key=${i}>${note}</li>`)}
      </ul>` : null}
    </div>`;
}

function NamesStep({ state, set, actions, meta }) {
  const rows = useMemo(() => [
    ...state.captions.map((c, i) => ({
      id: `cap-${i}`, name: c.name, category: state.suggCats[`cap-${i}`] || c.category,
      role: c.role, extra: c.confidence, where: c.source,
    })),
    ...state.suggestions.map((s, i) => ({
      id: `ner-${i}`, name: s.text, category: state.suggCats[`ner-${i}`] || s.category,
      role: "", extra: `x${s.count}`, where: (s.documents || []).join("; "),
    })),
  ], [state.captions, state.suggestions, state.suggCats]);

  const nameCategories = meta.name_categories || ["person", "minor", "organization", "location"];
  const labelFor = (key) =>
    (meta.categories.find((c) => c.key === key) || { label: key }).label;

  return html`
    <div class="card">
      <p class="hint">Every name is matched in all of its written forms - full
        name, first or surname alone, "Smith, John", "J. Smith", "Mr. Smith",
        the possessive and the plural - plus close typos, and "John Smith"
        counts as the same person as "John Michael Smith".</p>
    </div>
    <div class="columns">
      <div class="card">
        <h2>Suggestions</h2>
        <div class="tablewrap"><table>
          <thead><tr><th></th><th>Name</th><th>Type</th><th>Found as</th><th>Where</th></tr></thead>
          <tbody>
            ${rows.map((row) => html`<tr key=${row.id}>
              <td><input type="checkbox" checked=${state.suggChecked.has(row.id)}
                         onChange=${() => actions.toggleSuggestion(row.id)} /></td>
              <td>${row.name}</td>
              <td><select value=${row.category}
                          onChange=${(e) => actions.retypeSuggestion(row, e.target.value)}>
                ${nameCategories.map((key) => html`<option key=${key} value=${key}>${labelFor(key)}</option>`)}
              </select></td>
              <td>${row.role || row.extra}</td>
              <td class="hint">${row.where}</td>
            </tr>`)}
          </tbody>
        </table></div>
        <div class="bar">
          <button onClick=${() => actions.addTicked(rows)}>Add ticked to the name list →</button>
          <button onClick=${() => actions.tickAllSuggestions(rows, true)}>Tick all</button>
          <button onClick=${() => actions.tickAllSuggestions(rows, false)}>Untick all</button>
          <span class="spacer"></span>
          <button disabled=${state.busy.active} onClick=${actions.readCaptions}>Re-read captions</button>
          <button disabled=${state.busy.active} onClick=${actions.scanSuggestions}>Scan documents for more names</button>
        </div>
      </div>
      <div class="card">
        <h2>Name list - one full name per line</h2>
        <textarea rows="18" spellcheck="false" value=${state.namesText}
                  onInput=${(e) => set.namesText(e.target.value)}></textarea>
        <p class="hint">Mark special types after a pipe: <code>Tommy Smith | minor</code>,
          <code>Zions Bank | organization</code>, <code>Sandy | location</code>.</p>
      </div>
    </div>
    <${GuardPanel} state=${state} />
    <div class="bar">
      <button onClick=${() => actions.go(0)}>← Back</button>
      <span class="spacer"></span>
      <button class="primary" disabled=${state.busy.active}
              onClick=${actions.toReview}>Continue to review →</button>
    </div>`;
}

function RowEditor({ row, meta, onClose, onSave }) {
  const [category, setCategory] = useState(row.category);
  const [replacement, setReplacement] = useState(row.replacement);
  const [errorType, setErrorType] = useState(meta.error_types[0]);
  return html`<div class="overlay" onClick=${(e) => e.target === e.currentTarget && onClose()}>
    <div class="modal">
      <h3>Edit item</h3>
      <div class="row"><span>Found</span><div class="found">${row.canonical}</div></div>
      <div class="row"><span>Type</span>
        <select value=${category} onChange=${(e) => setCategory(e.target.value)}>
          ${meta.categories.map((c) => html`<option key=${c.key} value=${c.key}>${c.label}</option>`)}
        </select></div>
      <div class="row"><span>Replace with</span>
        <input type="text" value=${replacement} onInput=${(e) => setReplacement(e.target.value)} /></div>
      <div class="row"><span>What went wrong?</span>
        <select value=${errorType} onChange=${(e) => setErrorType(e.target.value)}>
          ${meta.error_types.map((t) => html`<option key=${t} value=${t}>${t}</option>`)}
        </select></div>
      <div class="bar">
        <span class="spacer"></span>
        <button onClick=${onClose}>Cancel</button>
        <button class="primary" onClick=${() => onSave({ category, replacement, errorType })}>Save</button>
      </div>
    </div>
  </div>`;
}

function ReviewStep({ state, actions, meta }) {
  const [sort, setSort] = useState({ key: null, desc: false });
  const rows = useMemo(() => {
    const out = [...state.entities];
    if (sort.key) {
      out.sort((a, b) => {
        const x = a[sort.key], y = b[sort.key];
        const cmp = typeof x === "number" ? x - y
          : String(x).localeCompare(String(y), undefined, { sensitivity: "base" });
        return sort.desc ? -cmp : cmp;
      });
    }
    return out;
  }, [state.entities, sort]);
  const header = (key, label) => html`<th onClick=${() =>
    setSort((s) => ({ key, desc: s.key === key && !s.desc }))}>${label}
    ${sort.key === key ? (sort.desc ? " ↓" : " ↑") : ""}</th>`;

  return html`
    <div class="card">
      <p class="hint">Everything below will change. Untick anything that should
        stay; the pencil edits an item's type and replacement. Nothing has been
        written yet.</p>
      ${state.risky.length ? html`<p class="warn">Common words used as names -
        check these did not over-match: ${state.risky.join(", ")}</p>` : null}
      ${state.unused ? html`<p class="warn">${state.unused} entr${state.unused === 1 ? "y" : "ies"}
        did not appear in any document.</p>` : null}
      <div class="tablewrap"><table>
        <thead><tr><th></th>${header("label", "Type")}${header("canonical", "Found")}
          ${header("replacement", "Replaced with")}${header("occurrences", "Times")}
          ${header("source", "How found")}<th></th></tr></thead>
        <tbody>
          ${rows.map((row) => html`<tr key=${row.key}>
            <td><input type="checkbox" checked=${row.enabled}
                       onChange=${(e) => actions.setEnabled(row, e.target.checked)} /></td>
            <td>${row.label}</td>
            <td>${row.canonical}</td>
            <td>${row.replacement}</td>
            <td class="num">${row.occurrences}</td>
            <td class="hint">${row.source}</td>
            <td><button title="Edit type or replacement"
                        onClick=${() => actions.openEditor(row)}>✎ Edit</button></td>
          </tr>`)}
        </tbody>
      </table></div>
      <div class="bar">
        <button onClick=${() => actions.setAll(true)}>Tick all</button>
        <button onClick=${() => actions.setAll(false)}>Untick all</button>
        <button disabled=${state.busy.active} onClick=${actions.toReview}>Rescan documents</button>
      </div>
    </div>
    <div class="bar">
      <button onClick=${() => actions.go(1)}>← Back</button>
      <span class="spacer"></span>
      <button class="primary" disabled=${state.busy.active}
              onClick=${actions.toRun}>Continue to run →</button>
    </div>
    ${state.editing ? html`<${RowEditor} row=${state.editing} meta=${meta}
        onClose=${() => actions.closeEditor()}
        onSave=${(changes) => actions.saveEditor(state.editing, changes)} />` : null}`;
}

function RunStep({ state, actions }) {
  const active = state.entities.filter((e) => e.enabled).length;
  const result = state.runInfo;
  return html`
    <div class="card">
      <h2>Run</h2>
      <p>${state.files.length} document(s), ${active} item(s) will be replaced.
         DOCX: ${state.options.docx_mode}. PDF: redact (always).</p>
      <div class="bar">
        <button class="primary" disabled=${state.busy.active}
                onClick=${actions.run}>Run</button>
        <button disabled=${!result} onClick=${() =>
          navigator.clipboard.writeText(state.password)}>Copy mapping-key password</button>
      </div>
      ${result ? html`
        <pre class="log">${result.outcomes.map((o) =>
          `${o.source}: ${o.status}${o.delivered ? `  ->  ${o.delivered}` : ""}` +
          `  (${o.hits} change(s))` +
          (o.error ? `\n    ${o.error}` : "") +
          (o.warnings || []).map((w) => `\n    WARNING: ${w}`).join("")
        ).join("\n")}</pre>
        <p class=${result.failed ? "status-bad" : "status-ok"}>
          ${result.failed
            ? `${result.failed} file(s) were refused or failed and are NOT in the archive.`
            : "Finished."}</p>
        <div class="downloads">
          ${result.archive ? html`<a href="/api/download/archive"><button class="primary">Download archive</button></a>` : null}
          ${result.key ? html`<a href="/api/download/key"><button>Download mapping key</button></a>` : null}
          ${result.report ? html`<a href="/api/download/report"><button>Download report</button></a>` : null}
        </div>
        ${result.key ? html`<p class="warn">Keep the mapping key and the report
          away from anything you deliver.</p>` : null}` : null}
    </div>
    <div class="bar">
      <button onClick=${() => actions.go(2)}>← Back</button>
    </div>`;
}

/* ------------------------------------------------------------------- app -- */

function App() {
  const [meta, setMeta] = useState(null);
  const [step, setStep] = useState(0);
  const [unlocked, setUnlocked] = useState(0);
  const [files, setFiles] = useState([]);
  const [options, setOptions] = useState(DEFAULT_OPTIONS);
  const [password, setPassword] = useState(generatePassword());
  const [showPassword, setShowPassword] = useState(false);
  const [namesText, setNamesText] = useState("");
  const [captions, setCaptions] = useState([]);
  const [officials, setOfficials] = useState([]);
  const [guardNotes, setGuardNotes] = useState([]);
  const [overlaps, setOverlaps] = useState([]);
  const [suggestions, setSuggestions] = useState([]);
  const [suggChecked, setSuggChecked] = useState(new Set());
  const [suggCats, setSuggCats] = useState({});
  const [entities, setEntities] = useState([]);
  const [risky, setRisky] = useState([]);
  const [unused, setUnused] = useState(0);
  const [editing, setEditing] = useState(null);
  const [runInfo, setRunInfo] = useState(null);
  const [busy, setBusy] = useState({ active: false, message: "", fraction: 0 });
  const [fault, setFault] = useState("");

  useEffect(() => {
    // the launch token did its job at load; the session cookie carries auth
    // from here, so drop it from the URL rather than leave it on screen
    if (window.location.search.includes("token=")) {
      window.history.replaceState({}, "", window.location.pathname);
    }
    api.get("/api/state").then(setMeta).catch((e) => setFault(e.message));
  }, []);

  const track = (message, fraction) => setBusy({ active: true, message, fraction });
  const settle = () => setBusy({ active: false, message: "", fraction: 0 });

  // Every step that touches the name list gets a fresh read of what the run
  // will shield and what it folded together; the server recomputes both, since
  // a judge sharing a party's surname changes the answer.
  const absorbGuard = (result) => {
    if (result.officials) setOfficials(result.officials);
    if (result.guard_notes) setGuardNotes(result.guard_notes);
    if (result.overlaps) setOverlaps(result.overlaps);
  };

  async function guarded(work) {
    setFault("");
    try { return await work(); }
    catch (error) { setFault(error.message); }
    finally { settle(); }
  }

  const applyReview = (payload) => {
    setEntities(payload.entities); setRisky(payload.risky); setUnused(payload.unused);
  };

  const actions = {
    go: (target) => setStep(target),

    upload: (list) => guarded(async () => {
      const form = new FormData();
      for (const file of list) form.append("files", file);
      track("Uploading", 0);
      const result = await api.post("/api/files", form);
      setFiles(result.files);
    }),

    removeFile: (index) => guarded(async () => {
      const result = await api.del(`/api/files/${index}`);
      setFiles(result.files);
    }),

    toNames: () => guarded(async () => {
      if (!files.length) throw new Error("Add at least one DOCX or PDF first.");
      if (!password.trim()) throw new Error("Set a password for the mapping key, or press New.");
      setUnlocked((u) => Math.max(u, 1)); setStep(1);
      if (!captions.length) await actions.readCaptions();
    }),

    readCaptions: () => guarded(async () => {
      const result = await api.job(api.post("/api/captions"), track);
      setCaptions(result.captions);
      absorbGuard(result);
      setSuggChecked((prev) => {
        const next = new Set(prev);
        result.captions.forEach((c, i) => {
          if (c.confidence === "high" && !prev.has(`cap-${i}-seen`)) next.add(`cap-${i}`);
          next.add(`cap-${i}-seen`);
        });
        return next;
      });
    }),

    scanSuggestions: () => guarded(async () => {
      const body = { options, names: parseNameLines(namesText) };
      const result = await api.job(api.post("/api/suggestions", body), track);
      setSuggestions(result.suggestions);
      absorbGuard(result);
      if (!result.suggestions.length && result.notes.length) setFault(result.notes[0]);
    }),

    toggleSuggestion: (id) => setSuggChecked((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    }),

    tickAllSuggestions: (rows, on) => setSuggChecked((prev) => {
      const next = new Set(prev);
      rows.forEach((row) => (on ? next.add(row.id) : next.delete(row.id)));
      return next;
    }),

    retypeSuggestion: (row, category) =>
      setSuggCats((prev) => ({ ...prev, [row.id]: category })),

    addTicked: (rows) => {
      const existing = new Set(namesText.split("\n").map((l) => l.trim().toLowerCase()));
      const added = [];
      for (const row of rows) {
        if (!suggChecked.has(row.id)) continue;
        const line = nameLine(row.name, row.category);
        if (!existing.has(line.toLowerCase())) added.push(line);
      }
      if (added.length) setNamesText((t) => (t.trim() ? t.replace(/\s+$/, "") + "\n" : "") + added.join("\n"));
    },

    toReview: () => guarded(async () => {
      setUnlocked((u) => Math.max(u, 2)); setStep(2);
      const body = { options, names: parseNameLines(namesText) };
      const result = await api.job(api.post("/api/review", body), track);
      absorbGuard(result);
      applyReview(result);
    }),

    setEnabled: (row, enabled) => guarded(async () => {
      applyReview(await api.patch("/api/entities", { key: row.key, enabled }));
    }),

    setAll: (enabled) => guarded(async () => {
      applyReview(await api.post("/api/entities/all", { enabled }));
    }),

    openEditor: (row) => setEditing(row),
    closeEditor: () => setEditing(null),

    saveEditor: (row, changes) => guarded(async () => {
      const noError = meta.error_types[0];
      if (!changes.replacement.trim()) throw new Error("The replacement cannot be empty.");
      applyReview(await api.patch("/api/entities", {
        key: row.key,
        category: changes.category,
        replacement: changes.replacement !== row.replacement ? changes.replacement : null,
      }));
      setEditing(null);
      if (changes.errorType !== noError) {
        const logged = await api.post("/api/feedback", {
          error_type: changes.errorType,
          text: row.canonical,
          predicted_category: row.category,
          corrected_category: changes.category,
          corrected_replacement:
            changes.replacement !== row.replacement ? changes.replacement : null,
          source: row.source,
          occurrences: row.occurrences,
          documents: files.map((f) => f.name),
          origin: "review",
        });
        const send = window.confirm(
          `The report was saved to\n${logged.logged_to}\n(stored unencrypted on ` +
          "this computer - it contains the flagged text).\n\nOpen an email to " +
          `${logged.address} with this report? You will see the draft before ` +
          "anything is sent.");
        if (send) window.location.href = logged.mailto;
      }
    }),

    toRun: () => { setUnlocked((u) => Math.max(u, 3)); setStep(3); },

    run: () => guarded(async () => {
      if (!password.trim()) throw new Error("Set a mapping-key password first.");
      const result = await api.job(api.post("/api/run", { options, password }), track);
      setRunInfo(result);
    }),
  };

  const set = {
    option: (key, value) => setOptions((o) => ({ ...o, [key]: value })),
    password: setPassword, showPassword: setShowPassword, namesText: setNamesText,
  };

  if (!meta) {
    return html`<div>
      <header class="topbar"><div class="topbar-inner">
        <h1>Document Redactions & Anonymization</h1>
      </div></header>
      <div class="shell"><div class="card">${fault ? html`<p class="error">${fault}</p>`
        : html`<p class="hint">Loading…</p>`}</div></div></div>`;
  }

  const state = { files, options, password, showPassword, namesText, captions,
                  officials, guardNotes, overlaps,
                  suggestions, suggChecked, suggCats, entities, risky, unused,
                  editing, runInfo, busy };

  return html`<div>
    <header class="topbar"><div class="topbar-inner">
      <h1>Document Redactions & Anonymization</h1>
      <span class="version">${meta.version} · everything stays on this computer</span>
    </div></header>
    <div class="shell">
      <${Stepper} step=${step} unlocked=${unlocked} go=${actions.go} />
      ${fault ? html`<div class="card"><p class="error">${fault}</p></div>` : null}
      ${step === 0 ? html`<${FilesStep} state=${state} set=${set} actions=${actions} />` : null}
      ${step === 1 ? html`<${NamesStep} state=${state} set=${set} actions=${actions} meta=${meta} />` : null}
      ${step === 2 ? html`<${ReviewStep} state=${state} actions=${actions} meta=${meta} />` : null}
      ${step === 3 ? html`<${RunStep} state=${state} actions=${actions} />` : null}
      <${Progress} busy=${busy} />
    </div>
  </div>`;
}

ReactDOM.createRoot(document.getElementById("root")).render(html`<${App} />`);
