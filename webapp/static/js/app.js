/* The window's four stages, every set taken through each stage together.

   Five to eleven cases a night were each walked through all four steps on
   their own: click into the set, choose the options again, wait for its
   names, review it, queue it, then the next set. Here the stages are the
   window's, not the set's. Every set's documents go in on one screen and are
   read in the background, in the order the sets were made; once every set is
   read, the names are worked through set after set; every review list is
   built in one go and looked over set after set; and one button queues them
   all, to run one at a time. The options are chosen once, for every set.
   Each set keeps its own documents, names, review decisions and mapping-key
   password, exactly as if it had been run alone. */
/* global React, ReactDOM, htm, api */

const { useState, useEffect, useMemo, useRef } = React;
const html = htm.bind(React.createElement);

const ANONYMIZE_NOTE =
  "ANONYMIZE replaces each person with a realistic, invented name - " +
  '"John Michael Smith" becomes something like "Ezra Lars Holm", ' +
  "consistently in every document in this batch, and family members keep a " +
  "shared surname. The document still reads like a normal pleading, so a " +
  "reader may not realize it has been altered unless you tell them. " +
  "Everything that is not a person's name (SSNs, accounts, addresses, case " +
  "numbers) becomes a tagged placeholder such as [SSN-1].";

const REDACT_NOTE =
  "REDACT removes the text outright and marks it [REDACTED] on a black bar. " +
  "Nothing is invented and nothing is recoverable from the file.";

const PDF_REDACT_NOTE =
  "REDACT physically deletes the glyphs from the page and draws a black box " +
  "over the space. Copy, paste and text extraction find nothing.";

const PDF_ANONYMIZE_NOTE =
  "ANONYMIZE deletes the original glyphs and writes the replacement in their " +
  "place - the same invented names and placeholders as the Word files - so " +
  "the page still reads as a statement, to a person or to a language model. " +
  "The file's properties say it is an anonymized copy; the pages do not. " +
  "Scanned pages and images are still blacked out.";

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
  docx_mode: "anonymize", pdf_mode: "redact", metadata: true, comments: true, embedded: true,
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

// The clipboard: the modern call where the web view allows it, the old
// select-and-copy where it does not - an embedded web view can refuse the
// first outright, and a copy button that silently copies nothing loses a
// mapping-key password.
async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (error) {
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    let copied = false;
    try { copied = document.execCommand("copy"); } catch (e) { copied = false; }
    area.remove();
    return copied;
  }
}

const n = (value) => (value || 0).toLocaleString();
const plural = (count, word) => `${n(count)} ${word}${count === 1 ? "" : "s"}`;

/* ------------------------------------------------------------ components -- */

const STAGES = ["Documents & options", "Names", "Review", "Run"];

function Stepper({ stage, reachable, go }) {
  return html`<nav class="stepper">
    ${STAGES.map((label, index) => html`
      <div key=${index}
           class=${"step" + (index === stage ? " active" : "") + (reachable[index] ? "" : " locked")}
           onClick=${() => reachable[index] && go(index)}>
        <span class="n">${index + 1}</span> ${label}
      </div>`)}
  </nav>`;
}

// "Page 37 of 412 · about 6 min left". The time is in the pages - a scanned
// page takes seconds to read, a typed one a blink - so the estimate is the
// pace of this step so far, and only once there is enough of it to go on.
function pageCount(pages) {
  if (!pages || !pages.total) return "";
  const { done, total, seconds } = pages;
  let text = `Page ${Math.min(done, total).toLocaleString()} of ${total.toLocaleString()}`;
  if (done >= 3 && done < total && seconds >= 10) {
    const left = (seconds / done) * (total - done);
    text += left < 60 ? " · under a minute left"
      : ` · about ${Math.round(left / 60).toLocaleString()} min left`;
  }
  return text;
}

function Progress({ busy }) {
  if (!busy.active) return null;
  const pages = busy.pages && busy.pages.total ? busy.pages : null;
  const fraction = pages ? Math.min(pages.done / pages.total, 1) : busy.fraction;
  const known = fraction > 0;
  return html`<div class="progress">
    <div class="track">
      <div class=${"fill" + (known ? "" : " pulse")}
           style=${{ width: known ? `${Math.round(fraction * 100)}%` : "30%" }}></div>
    </div>
    <span class="msg">${busy.message}</span>
    ${pages && html`<span class="pages">${pageCount(pages)}</span>`}
  </div>`;
}

// Everything in a drop, folders walked, each file with its path inside what
// was dropped ("Case/2024/statement.pdf"). The entries are taken from the
// event before the first await: the browser empties dataTransfer once the
// handler returns.
function takeDrop(dataTransfer) {
  const entries = [...(dataTransfer.items || [])]
    .map((item) => (item.webkitGetAsEntry ? item.webkitGetAsEntry() : null))
    .filter(Boolean);
  return { entries, files: [...dataTransfer.files] };
}

async function readDrop({ entries, files }) {
  if (!entries.length) return files.map((file) => ({ file, path: file.name }));
  const out = [];
  const walk = async (entry, prefix) => {
    if (entry.isFile) {
      const file = await new Promise((ok, fail) => entry.file(ok, fail));
      out.push({ file, path: prefix + entry.name });
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      for (;;) {                      // readEntries hands back a batch at a time
        const batch = await new Promise((ok, fail) => reader.readEntries(ok, fail));
        if (!batch.length) break;
        for (const child of batch) await walk(child, `${prefix}${entry.name}/`);
      }
    }
  };
  for (const entry of entries) await walk(entry, "");
  return out;
}

function folderCount(files) {
  return new Set(files.map((f) => f.name.includes("/") ? f.name.slice(0, f.name.lastIndexOf("/")) : ""))
    .size;
}

// What the app reads: Word, PDF, and photos or screenshots of documents
// (made into PDFs on the way in). Kept in step with redactor/photo.py.
const PHOTO_TYPES = ["avif", "bmp", "dib", "gif", "heic", "heif", "hif", "j2k", "jfif", "jp2", "jpe", "jpeg", "jpf", "jpg", "jpx", "png", "tif", "tiff", "webp"];
const READABLE = new RegExp(`\\.(pdf|docx|${PHOTO_TYPES.join("|")})$`, "i");

// The set's name, typed into a draft of its own and saved when the field is
// left or Enter is pressed. Saved on every keystroke, each answer came back
// trimmed and snapped the field to it mid-word: "Smith v Jones" became
// "SmithvJones" - and the name is the results folder's.
function SetName({ name, onSave, disabled }) {
  const [draft, setDraft] = useState(name);
  const [editing, setEditing] = useState(false);
  useEffect(() => { if (!editing) setDraft(name); }, [name, editing]);
  const save = () => {
    setEditing(false);
    if (draft.trim() && draft.trim() !== name) onSave(draft.trim());
    else setDraft(name);
  };
  return html`<input type="text" value=${draft} disabled=${disabled}
    aria-label="Name of this set"
    placeholder="e.g. a client or case - it names the set's results folder"
    onFocus=${() => setEditing(true)}
    onInput=${(e) => setDraft(e.target.value)}
    onBlur=${save}
    onKeyDown=${(e) => { if (e.key === "Enter") e.target.blur(); }} />`;
}

function setStatus(info) {
  const read = info.reading || {};
  const run = info.run || {};
  switch (info.state) {
    case "empty": return "No documents yet - a set left empty is left out";
    case "reading": return read.total ? `Reading ${n(read.done)} of ${n(read.total)} scanned pages`
      : "Looking for scanned pages";
    case "preparing": return "Read - working out the name lists";
    case "ready": return "Ready";
    case "queued": return "Queued to run";
    case "running": return run.total ? `Running - page ${n(run.done)} of ${n(run.total)}` : "Running";
    case "done": return run.failed ? `Done - ${run.failed} file(s) refused` : "Done";
    case "failed": return "Run failed";
    default: return "";
  }
}

/* ------------------------------------------------------- the parties -- */

// The sheet filled in for each set before anything is scanned: who the
// parties are, and what identifies them. The overnight run that came back
// with thousands of review rows was guessing at all of this; the operator
// knows it from the file in a minute. Every field is optional and may hold
// several values; what is not one of the parties' is left as written.
const ACCOUNT_KINDS = ["checking", "savings", "credit card", "loan", "mortgage",
                       "retirement", "brokerage", "other"];

const PARTY_FIELDS = [
  { key: "names", label: "Names", many: true, core: true, placeholder: "Full name",
    hint: "Add every former or maiden name too - each is the same person." },
  { key: "dob", label: "Date of birth", core: true, placeholder: "e.g. 12/6/1986",
    hint: "Any date format." },
  { key: "ssn", label: "SSN", core: true, placeholder: "e.g. 528-41-9963",
    hint: "Any format - with or without dashes or spaces." },
  { key: "addresses", label: "Addresses", many: true, core: true,
    placeholder: "Street, city, state ZIP" },
  { key: "phones", label: "Phones", many: true, core: true, placeholder: "e.g. (801) 555-0143",
    hint: "Any format." },
  { key: "emails", label: "Emails", many: true, core: true, placeholder: "name@example.com" },
  { key: "tin", label: "TIN / EIN", placeholder: "Taxpayer number" },
  { key: "dl", label: "Driver's licence",
    parts: [{ k: "number", label: "Licence number" }, { k: "state", label: "State", narrow: true }] },
  { key: "passport", label: "Passport", placeholder: "Passport number" },
  { key: "employers", label: "Employers", many: true, placeholder: "Employer" },
  { key: "businesses", label: "Businesses", many: true, placeholder: "e.g. Smith Landscaping LLC" },
  { key: "accounts", label: "Accounts", many: true,
    hint: "The full number or only the last four.",
    parts: [{ k: "number", label: "Number or last four" }, { k: "institution", label: "Bank" },
            { k: "kind", label: "Kind", list: "account-kinds" }] },
  { key: "cards", label: "Cards", many: true,
    parts: [{ k: "number", label: "Last four" }, { k: "institution", label: "Issuer" }] },
  { key: "handles", label: "Handles", many: true, placeholder: "Venmo, Cash App, social",
    hint: "e.g. @janesmith86" },
  { key: "vehicles", label: "Vehicles", many: true,
    parts: [{ k: "vin", label: "VIN" }, { k: "plate", label: "Plate", narrow: true }] },
  { key: "case_numbers", label: "Case numbers", many: true, placeholder: "Court case number" },
  { key: "parcels", label: "Parcels", many: true, placeholder: "Property parcel number" },
  { key: "other", label: "Other identifiers", many: true,
    placeholder: "Anything else that identifies them",
    hint: "e.g. medical record, student, military, alien or voter number, bar or professional "
      + "licence, IP address, a website, place of birth - each replaced wherever it is written." },
];
const ROLE_ORDER = { petitioner: 0, respondent: 1, child: 2, other: 3 };

const blankValue = (def) => (def.parts ? Object.fromEntries(def.parts.map((p) => [p.k, ""])) : "");
const isBlank = (value) => (value && typeof value === "object"
  ? Object.values(value).every((v) => !String(v || "").trim())
  : !String(value || "").trim());
const trimmed = (value) => (value && typeof value === "object"
  ? Object.fromEntries(Object.entries(value).map(([k, v]) => [k, String(v || "").trim()]))
  : String(value || "").trim());

function blankParty(role) {
  return { role, ...Object.fromEntries(PARTY_FIELDS.map((def) => [def.key, []])) };
}

// The server answers with each value in its canonical form: a plain string,
// or the value with its canonical form beside it. Either way the field shows
// the canonical one - "8015550143" comes back "(801) 555-0143".
function plainText(value) {
  if (value === null || value === undefined) return "";
  if (typeof value !== "object") return String(value);
  return String(value.canonical ?? value.value ?? value.text ?? "");
}
function toDraftValue(def, value) {
  if (!def.parts) return plainText(value);
  if (!value || typeof value !== "object" || !def.parts.some((p) => p.k in value)) {
    return { ...blankValue(def), [def.parts[0].k]: plainText(value) };
  }
  return Object.fromEntries(def.parts.map((p) => [p.k, plainText(value[p.k])]));
}
function toDraftParty(party) {
  const out = blankParty(ROLE_ORDER[party.role] !== undefined ? party.role : "other");
  for (const def of PARTY_FIELDS) {
    const values = party[def.key];
    out[def.key] = (Array.isArray(values) ? values : values ? [values] : [])
      .map((v) => toDraftValue(def, v));
  }
  return out;
}

// petitioner and respondent always have a card; children and the rest follow
function ensureCore(parties) {
  const sorted = [...parties].sort((a, b) => ROLE_ORDER[a.role] - ROLE_ORDER[b.role]);
  if (!sorted.some((p) => p.role === "respondent")) {
    sorted.splice(sorted.filter((p) => p.role === "petitioner").length, 0, blankParty("respondent"));
  }
  if (!sorted.some((p) => p.role === "petitioner")) sorted.unshift(blankParty("petitioner"));
  return sorted;
}

function partyLabel(parties, index) {
  const party = parties[index];
  if (party.role === "petitioner") return "Petitioner";
  if (party.role === "respondent") return "Respondent";
  const nth = parties.slice(0, index + 1).filter((p) => p.role === party.role).length;
  return party.role === "child" ? `Child ${nth}` : `Another party ${nth}`;
}

const firstName = (party) => (party.names || []).find((v) => !isBlank(v)) || "";
const detailCount = (party) => PARTY_FIELDS.filter((def) => def.key !== "names")
  .reduce((sum, def) => sum + (party[def.key] || []).filter((v) => !isBlank(v)).length, 0);
// both parties named: the petitioner and the respondent each need one name
const hasPartyName = (parties) => ["petitioner", "respondent"].every((role) =>
  parties.some((p) => p.role === role && firstName(p)));

const parseSurnames = (text) => String(text || "").split(/[,;\n]/).map((s) => s.trim()).filter(Boolean);

// What goes to the server: the blanks left out, and each party sent with its
// place on the sheet remembered, so the canonical forms that come back land
// in the fields they were typed in. The family surnames go only once the
// operator has changed them; until then the server works them out.
function sheetBody(state) {
  const parties = [];
  const index = [];
  state.sheet.forEach((party, at) => {
    const out = { role: party.role };
    let any = false;
    for (const def of PARTY_FIELDS) {
      out[def.key] = (party[def.key] || []).filter((v) => !isBlank(v)).map(trimmed);
      if (out[def.key].length) any = true;
    }
    if (any) { parties.push(out); index.push(at); }
  });
  const body = { parties };
  if (state.surnamesEdited) body.family_surnames = parseSurnames(state.surnamesText);
  return { body, index };
}
const sheetKey = (state) => JSON.stringify(sheetBody(state).body);
const sheetDirty = (state) => state.sheetState === "ready" && sheetKey(state) !== state.sheetSaved;

// The normalised sheet folded back into what is on screen: each value typed
// takes its canonical form, and a blank row the operator has just added stays
// where it is. Where the answer does not line up with what was sent - the
// server dropped or merged something - the server's sheet is the one shown.
function mergeSheet(draft, index, answered) {
  if (!Array.isArray(answered)) return draft;
  if (answered.length !== index.length) return ensureCore(answered.map(toDraftParty));
  const out = draft.map((party) => ({ ...party }));
  answered.forEach((raw, k) => {
    const back = toDraftParty(raw);
    const party = out[index[k]];
    for (const def of PARTY_FIELDS) {
      const shown = party[def.key] || [];
      const filled = shown.map((v, i) => (isBlank(v) ? -1 : i)).filter((i) => i >= 0);
      const values = back[def.key];
      if (values.length === filled.length) {
        const next = [...shown];
        filled.forEach((at, j) => { next[at] = values[j]; });
        party[def.key] = next;
      } else {
        party[def.key] = [...values, ...shown.filter(isBlank)];
      }
    }
  });
  return out;
}

function sheetFromServer(payload, was, first) {
  const sheet = ensureCore((payload.parties || []).map(toDraftParty));
  const next = { sheet, surnamesText: (payload.family_surnames || []).join(", "),
                 surnamesEdited: false, sheetState: "ready", sheetError: null, sheetLoadError: "" };
  next.sheetSaved = sheetKey({ ...was, ...next });
  if (first) next.sheetOpen = !hasPartyName(sheet);
  return next;
}

// The line a set card collapses to: "Petitioner: Jane E. Smith - 7 details;
// Respondent: John M. Smith - 6 details; 2 children".
function partiesSummary(parties) {
  const parts = [];
  parties.forEach((party, at) => {
    if (!["petitioner", "respondent"].includes(party.role)) return;
    const name = firstName(party);
    const details = detailCount(party);
    if (name || details) {
      parts.push(`${partyLabel(parties, at)}: ${name || "no name"} - ${plural(details, "detail")}`);
    }
  });
  const filled = (role) => parties.filter((p) => p.role === role && (firstName(p) || detailCount(p))).length;
  const kids = filled("child");
  const others = filled("other");
  if (kids) parts.push(kids === 1 ? "1 child" : `${n(kids)} children`);
  if (others) parts.push(others === 1 ? "1 other party" : `${n(others)} other parties`);
  return parts.join("; ");
}

// Where a refused save points. The server names the field in its message
// ("Respondent SSN: not 9 digits"), or says it outright as an object; either
// way the message is put next to that field, and beside the Save button too.
const FIELD_WORDS = [
  ["ssn", /\bssn\b|social security/], ["tin", /\btin\b|\bein\b|taxpayer/],
  ["dob", /\bdob\b|birth/], ["phones", /phone/], ["emails", /e-?mail/],
  ["dl", /licen[cs]e|\bdl\b/], ["passport", /passport/], ["addresses", /address/],
  ["cards", /\bcards?\b/], ["accounts", /account/], ["handles", /handle/],
  ["vehicles", /vehicle|\bvin\b|plate/], ["case_numbers", /case/], ["parcels", /parcel/],
  ["employers", /employer/], ["businesses", /business/], ["dob", /\bdate\b/],
  ["names", /\bnames?\b/], ["other", /\bother\b/],
];
function fieldKey(word) {
  if (!word) return null;
  const said = String(word).toLowerCase();
  const plain = said.replace(/[\s-]+/g, "_");
  if (PARTY_FIELDS.some((d) => d.key === plain)) return plain;
  const many = PARTY_FIELDS.find((d) => d.key === `${plain}s` || d.key === `${plain}es`);
  if (many) return many.key;
  const found = FIELD_WORDS.find(([, pattern]) => pattern.test(said));
  return found ? found[0] : null;
}
function partyAt(parties, said) {
  if (said === null || said === undefined || said === "") return -1;
  const text = String(said).toLowerCase();
  const nth = (role, k) => {
    let seen = 0;
    return parties.findIndex((p) => p.role === role && ++seen === k);
  };
  let m = text.match(/child[\s_-]*(\d+)/);
  if (m) return nth("child", Number(m[1]));
  m = text.match(/(?:another|other)(?: party)?[\s_-]*(\d+)/);
  if (m) return nth("other", Number(m[1]));
  if (/petitioner/.test(text)) return parties.findIndex((p) => p.role === "petitioner");
  if (/respondent/.test(text)) return parties.findIndex((p) => p.role === "respondent");
  if (/\bchild\b/.test(text)) return nth("child", 1);
  return -1;
}
function locateRefusal(error, parties, index) {
  const detail = error.detail;
  const message = error.message;
  if (detail && typeof detail === "object" && !Array.isArray(detail)) {
    let at = -1;
    if (typeof detail.party === "number") at = index[detail.party] ?? -1;
    else at = partyAt(parties, detail.party ?? detail.role);
    if (at < 0 && typeof detail.index === "number") at = index[detail.index] ?? -1;
    return { message, party: at, field: fieldKey(detail.field) || fieldKey(message) };
  }
  return { message, party: partyAt(parties, message), field: fieldKey(message) };
}

function ValueInputs({ def, value, label, disabled, onChange }) {
  if (!def.parts) {
    return html`<input type="text" value=${value} disabled=${disabled}
      placeholder=${def.placeholder || ""} aria-label=${label}
      onInput=${(e) => onChange(e.target.value)} />`;
  }
  return def.parts.map((part) => html`<input key=${part.k} type="text" disabled=${disabled}
      class=${part.narrow ? "narrow" : ""} value=${value[part.k] || ""}
      placeholder=${part.label} aria-label=${`${label} - ${part.label}`}
      list=${part.list || null}
      onInput=${(e) => onChange({ ...value, [part.k]: e.target.value })} />`);
}

function PartyField({ def, values, label, error, locked, edit }) {
  const shown = values.length ? values : [blankValue(def)];
  return html`<div class=${"pfield" + (error ? " refused" : "")}>
    <label>${def.label}</label>
    <div class="pvalues">
      ${shown.map((value, i) => html`<div class="pvalue" key=${i}>
        <${ValueInputs} def=${def} value=${value} label=${`${label} - ${def.label}`}
            disabled=${locked} onChange=${(v) => edit.set(def.key, i, v)} />
        ${shown.length > 1 && !locked ? html`<button class="dropvalue" title="Take this one out"
            aria-label=${`Take this ${def.label.toLowerCase()} out`}
            onClick=${() => edit.drop(def.key, i)}>×</button>` : null}
      </div>`)}
      ${(def.many || shown.length > 1) && !locked ? html`<button class="addvalue"
          onClick=${() => edit.add(def.key)}>+ add</button>` : null}
      ${def.hint ? html`<span class="phint">${def.hint}</span>` : null}
      ${error ? html`<p class="fielderror" role="alert">${error}</p>` : null}
    </div>
  </div>`;
}

function PartyCard({ parties, index, refusal, locked, edit, onRemove }) {
  const party = parties[index];
  const label = partyLabel(parties, index);
  const mine = refusal && refusal.party === index ? refusal : null;
  const errorFor = (key) => (mine && mine.field === key ? mine.message : "");
  const extra = PARTY_FIELDS.filter((def) => !def.core);
  const fieldEdit = {
    set: (key, i, value) => edit.set(index, key, i, value),
    add: (key) => edit.add(index, key),
    drop: (key, i) => edit.drop(index, key, i),
  };
  const required = ["petitioner", "respondent"].includes(party.role);
  const field = (def) => html`<${PartyField} key=${def.key}
      def=${def.key === "names" && required ? { ...def, label: "Names (required)" } : def}
      values=${party[def.key] || []}
      label=${label} error=${errorFor(def.key)} locked=${locked} edit=${fieldEdit} />`;
  return html`<div class="party">
    <div class="partyhead">
      <strong>${label}</strong>
      ${onRemove && !locked ? html`<button onClick=${onRemove}>Remove</button>` : null}
    </div>
    ${mine && !mine.field ? html`<p class="fielderror" role="alert">${mine.message}</p>` : null}
    ${PARTY_FIELDS.filter((def) => def.core).map(field)}
    <p class="hint small moredetails-head">Optional - fill in anything you know; leave the rest
      empty. Each is replaced wherever it is written.</p>
    ${extra.map(field)}
  </div>`;
}

// The parties section of a set card: open until the parties are filled in,
// then one summary line, so eleven sets still fit on the screen.
function PartySheet({ state, actions, locked }) {
  if (state.sheetState === "unavailable") {
    return html`<div class="parties"><p class="hint"><strong>The parties</strong> -
      not available in this build. The names are worked from the name screen alone.</p></div>`;
  }
  if (state.sheetState === "failed") {
    return html`<div class="parties"><p class="warn"><strong>The parties</strong> could not
      be loaded: ${state.sheetLoadError}</p>
      <button onClick=${actions.loadSheet}>Try again</button></div>`;
  }
  if (state.sheetState !== "ready") {
    return html`<div class="parties"><p class="hint"><strong>The parties</strong> - loading…</p></div>`;
  }
  const parties = state.sheet;
  const open = state.sheetOpen !== false;
  const summary = partiesSummary(parties);
  const dirty = sheetDirty(state);
  const refusal = state.sheetError;
  let status;
  if (state.sheetSaving) status = html`<span class="hint">Saving…</span>`;
  else if (refusal) status = html`<span class="status-bad">Not saved - ${refusal.message}</span>`;
  else if (dirty) status = html`<span class="warn">Not saved yet</span>`;
  else if (summary) status = html`<span class="status-ok">Saved</span>`;
  else status = html`<span class="hint">Nothing entered yet</span>`;
  const edit = { set: actions.sheetSet, add: actions.sheetAdd, drop: actions.sheetDrop };
  // saved when the focus leaves the sheet, not on every field: each save
  // makes the set's name lists and review list stale on the server
  return html`<section class=${"parties" + (open ? " open" : "")}
      onBlur=${(e) => { if (!locked && !e.currentTarget.contains(e.relatedTarget)) actions.saveSheet(); }}>
    <div class="parties-head">
      <button class="parties-toggle" aria-expanded=${open}
              onClick=${() => actions.openSheet(!open)}>${open ? "▾" : "▸"} The parties</button>
      ${!open ? html`<span class="parties-summary">${summary
        || "Not filled in yet - open to enter the parties"}</span>` : null}
      ${!open && (dirty || refusal || state.sheetSaving) ? status : null}
    </div>
    ${open ? html`
      <p class="hint small">Who the case is about, and what identifies them. Everything
        written here is replaced in every written form found; everyone else - payees,
        landlords, the other attorneys - is left as written. Empty fields are fine.</p>
      ${parties.map((party, index) => html`<${PartyCard} key=${index} parties=${parties} index=${index}
          refusal=${refusal} locked=${locked} edit=${edit}
          onRemove=${["child", "other"].includes(party.role) ? () => actions.removeParty(index) : null} />`)}
      ${!locked ? html`<div class="bar tight">
        <button onClick=${() => actions.addParty("child")}>+ Add a child</button>
        <button onClick=${() => actions.addParty("other")}>+ Add another party</button>
      </div>` : null}
      <div class="pfield surnames">
        <label>Family surname(s)</label>
        <div class="pvalues">
          <input type="text" value=${state.surnamesText} disabled=${locked}
                 aria-label="Family surnames" placeholder="Worked out from the parties' names when saved"
                 onInput=${(e) => actions.surnames(e.target.value)} />
          <span class="phint">Replaced wherever it appears in this set - a relative, a
            business carrying it. Worked out from the parties' names when saved; change it
            if it is wrong. Separate several with commas.</span>
        </div>
      </div>
      ${!locked ? html`<div class="bar tight">
        <button class="primary" disabled=${state.sheetSaving}
                onClick=${() => actions.saveSheet({ collapse: true })}>Save parties</button>
        ${status}
      </div>` : null}` : null}
  </section>`;
}

/* ------------------------------------------------------- 1. documents -- */

// One set of documents on the Documents screen: its name, its own drop
// area and file list, and how far its reading has got.
function SetCard({ info, number, work, actions, canRemove }) {
  const [over, setOver] = useState(false);
  // a running set's documents are being read, and a finished set's are gone
  const locked = ["running", "done"].includes(info.state);
  const pick = () => document.getElementById(`filepick-${info.id}`).click();
  const onPicked = (list) => list.length && actions.upload(list);
  const asItems = (files) => [...files].map((file) => ({ file, path: file.webkitRelativePath || file.name }));
  const files = work.files;
  const folders = folderCount(files);
  const read = info.reading || {};
  return html`<div class=${"card setcard " + info.state}>
    <div class="sethead">
      <span class="setnum">${number}</span>
      <${SetName} name=${info.name} onSave=${actions.rename}
                  disabled=${["queued", "running", "done"].includes(info.state)} />
      ${canRemove ? html`<button title="Remove this set and its documents from the window"
                                 onClick=${actions.remove}>Remove set</button>` : null}
    </div>
    <p class=${"setstate " + info.state}>${setStatus(info)}</p>
    ${info.state === "reading" && read.total ? html`<span class="setbar"><span
       style=${{ width: `${Math.round(100 * read.done / read.total)}%` }}></span></span>` : null}
    ${work.adding ? html`<p class="hint">${work.adding}…</p>` : null}
    ${locked ? html`<p class="hint">${info.state === "running"
      ? "This set is running - its documents cannot be changed until it finishes."
      : "This set has been run. To run these documents again, make a new set."}</p>`
      : html`<div class=${"dropzone" + (over ? " over" : "")}
             onClick=${pick}
             onDragOver=${(e) => { e.preventDefault(); setOver(true); }}
             onDragLeave=${() => setOver(false)}
             onDrop=${(e) => { e.preventDefault(); setOver(false);
                               actions.dropped(takeDrop(e.dataTransfer)); }}>
          Drop this set's documents, photos of documents or whole folders here,
          or click to choose files. Folders keep their layout.
          <input id=${`filepick-${info.id}`} type="file" multiple hidden
                 accept=${[".docx", ".pdf", ...PHOTO_TYPES.map((t) => "." + t)].join(",")}
                 onChange=${(e) => { onPicked(asItems(e.target.files)); e.target.value = ""; }} />
          <input id=${`folderpick-${info.id}`} type="file" multiple hidden webkitdirectory=${true}
                 onChange=${(e) => { onPicked(asItems(e.target.files)); e.target.value = ""; }} />
        </div>
        <div class="bar">
          <button onClick=${actions.chooseFolder}>Choose folder…</button>
          ${files.length ? html`<button onClick=${actions.removeAll}>Remove all</button>` : null}
        </div>`}
    ${files.length ? html`<p class="hint">${plural(files.length, "document")}${
      folders > 1 || files.some((f) => f.name.includes("/"))
        ? ` in ${plural(folders, "folder")}` : ""}.</p>` : null}
    ${work.skipped.length ? html`<details class="skipped">
      <summary class="warn">${`${work.skipped.length} other file${work.skipped.length === 1 ? "" : "s"} will be left out - not documents or photos. They are listed in the run report and nothing from them is delivered.`}</summary>
      <ul>${work.skipped.map((name, i) => html`<li key=${i}>${name}</li>`)}</ul>
    </details>` : null}
    ${files.length ? html`<ul class="files">
      ${files.map((f, i) => html`<li key=${i}>
        <span class="kind">${(f.kind || "?").toUpperCase()}</span>
        <span class="name">${f.name}</span>
        ${locked ? null : html`<button onClick=${() => actions.removeFile(i)}>Remove</button>`}
      </li>`)}
    </ul>` : null}
    <${PartySheet} state=${work} actions=${actions}
        locked=${["queued", "running", "done"].includes(info.state)} />
  </div>`;
}

// The one set of options every set is run with.
function OptionsPanel({ options, setOption }) {
  return html`
    <div class="card">
      <h2>Settings for every set</h2>
      <p class="hint">Chosen once: every set in this window is read, scanned for
        names and run with these. A set already queued keeps the settings it was
        queued with.</p>
    </div>
    <div class="card">
      <h2>What to do with DOCX files</h2>
      <label class="check"><input type="radio" name="docx-mode"
        checked=${options.docx_mode === "anonymize"}
        onChange=${() => setOption("docx_mode", "anonymize")} /> Anonymize (realistic fake names)</label>
      <label class="check"><input type="radio" name="docx-mode"
        checked=${options.docx_mode === "redact"}
        onChange=${() => setOption("docx_mode", "redact")} /> Redact (black bars, nothing invented)</label>
      <p class=${options.docx_mode === "anonymize" ? "warn" : "hint"}>
        ${options.docx_mode === "anonymize" ? ANONYMIZE_NOTE : REDACT_NOTE}</p>
    </div>
    <div class="card">
      <h2>What to do with PDF files</h2>
      <label class="check"><input type="radio" name="pdf-mode"
        checked=${options.pdf_mode === "redact"}
        onChange=${() => setOption("pdf_mode", "redact")} /> Redact (black boxes, nothing invented)</label>
      <label class="check"><input type="radio" name="pdf-mode"
        checked=${options.pdf_mode === "anonymize"}
        onChange=${() => setOption("pdf_mode", "anonymize")} /> Anonymize (replacement text in place)</label>
      <p class=${options.pdf_mode === "anonymize" ? "warn" : "hint"}>
        ${options.pdf_mode === "anonymize" ? PDF_ANONYMIZE_NOTE : PDF_REDACT_NOTE}</p>
    </div>
    <div class="card">
      <h2>Also scrub</h2>
      ${OPTION_LABELS.map(([key, label]) => html`
        <label class="check" key=${key}>
          <input type="checkbox" checked=${options[key]}
                 onChange=${(e) => setOption(key, e.target.checked)} /> ${label}
        </label>`)}
    </div>
    <div class="card">
      <h2>Never change these terms (one per line)</h2>
      <textarea rows="3" value=${options.allowlist}
                onInput=${(e) => setOption("allowlist", e.target.value)}></textarea>
      <p class="hint">Courts, judges, commissioners, statutes, rules and
        reported citations are already protected automatically.</p>
    </div>
    <div class="card">
      <h2>Mapping-key passwords</h2>
      <p class="hint">Each set's original-to-replacement table is encrypted with
        a password of its own, made for it automatically. They are shown on the
        Run step, where each can be copied or changed before the sets are queued.</p>
    </div>`;
}

function ResultsLine({ sets, chooseResults }) {
  return html`<div class="resultsroot">
    Results go to: <strong>${sets.results_root || "not chosen yet"}</strong>
    <button onClick=${chooseResults}>${sets.results_root ? "Change…" : "Choose folder…"}</button>
    <span class="kevnote">Final check model (Kev): ${sets.kev || "not started"}</span>
  </div>`;
}

// Where the reading is, across every set: the sets are read one after
// another, in the order they were made, so one line says it.
function readingLine(filled) {
  const at = filled.findIndex((s) => s.state === "reading");
  if (at >= 0) {
    const read = filled[at].reading || {};
    const where = `Reading set ${at + 1} of ${filled.length} (${filled[at].name})`;
    return read.total
      ? { message: `${where} - page ${n(read.done)} of ${n(read.total)}`, fraction: read.done / read.total }
      : { message: `${where} - looking for scanned pages`, fraction: 0 };
  }
  const prep = filled.findIndex((s) => s.state === "preparing");
  if (prep >= 0) {
    return { message: `Working out the name lists for set ${prep + 1} of ${filled.length} (${filled[prep].name})`,
             fraction: 0 };
  }
  return null;
}

/* ----------------------------------------------------------- 2. names -- */

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

// Forms of the parties' names the scan found that the sheet does not hold -
// a misreading, a glued "JaneSmith", a nickname, a former name. Each is
// filed under its party; a form of the family surname under the surname.
const formId = (form) => `pf|${form.party || ""}|${form.kind || ""}|${form.text}`;
const isSurnameForm = (form) => form.kind === "family_surname" || /family surname/i.test(form.why || "");

// ticked to begin with when the match is exact or a known misreading: the
// server may say so outright; otherwise its reason says it
function pretick(form) {
  for (const flag of ["ticked", "preticked", "checked"]) {
    if (typeof form[flag] === "boolean") return form[flag];
  }
  if (form.exact === true) return true;
  return /misread|exact|former|family surname/i.test(form.why || "");
}

function formOwner(form, sheet) {
  if (isSurnameForm(form)) return { order: 98, label: "Family surname" };
  const at = partyAt(sheet, form.party);
  if (at >= 0) {
    const name = firstName(sheet[at]);
    return { order: at, label: `${partyLabel(sheet, at)}${name ? ` - ${name}` : ""}` };
  }
  if (/^parties$/i.test(form.party || "")) return { order: 97, label: "The parties" };
  return { order: 99, label: form.party ? String(form.party) : "Not filed under a party" };
}

function PartyForms({ state, actions, busy }) {
  const forms = (state.partyForms || []).map((form) => ({ ...form, id: formId(form) }));
  const groups = [];
  for (const form of forms) {
    const owner = formOwner(form, state.sheet || []);
    let group = groups.find((g) => g.label === owner.label);
    if (!group) groups.push(group = { ...owner, forms: [] });
    group.forms.push(form);
  }
  groups.sort((a, b) => a.order - b.order);
  const waiting = forms.filter((f) => state.suggChecked.has(f.id) && !state.formsAdded.has(f.id));
  const summary = partiesSummary(state.sheet || []);
  const surnames = parseSurnames(state.surnamesText);
  return html`<div class="card">
    <h2>Forms of the parties' names found in the documents</h2>
    <p class="hint">${summary ? `The parties: ${summary}.` : "No parties entered for this set."}
      ${" "}Family surname${surnames.length === 1 ? "" : "s"}: <strong>${surnames.join(", ") || "none"}</strong>${
      " - replaced wherever it appears in this set."} Both are changed on the Documents step.</p>
    ${forms.length ? html`<div class="tablewrap"><table>
      <thead><tr><th></th><th>Found as</th><th>Why</th><th>Times</th><th>Where</th></tr></thead>
      ${groups.map((group) => html`<tbody key=${group.label}>
        <tr class="partyrow"><td colspan="5">${group.label}</td></tr>
        ${group.forms.map((form) => html`<tr key=${form.id}>
          <td>${state.formsAdded.has(form.id)
            ? html`<span class="status-ok small" title="On the parties sheet">Added</span>`
            : html`<input type="checkbox" checked=${state.suggChecked.has(form.id)}
                     aria-label=${`Add ${form.text}`}
                     onChange=${() => actions.toggleSuggestion(form.id)} />`}</td>
          <td>${form.text}</td>
          <td class="hint">${form.why || ""}</td>
          <td class="num">${n(form.count)}</td>
          <td class="hint">${(form.documents || []).join("; ")}</td>
        </tr>`)}
      </tbody>`)}
    </table></div>` : html`<p class="hint">No other forms of the parties' names were found -
      every one found is already on the sheet.</p>`}
    ${waiting.length ? html`<p class="warn">${plural(waiting.length, "ticked form")} not on the
      sheet yet. They are added when you move on to the next set.</p>` : null}
    <div class="bar">
      <button disabled=${busy.active || !waiting.length}
              onClick=${() => actions.addForms(forms)}>Add ticked to the parties →</button>
      <button onClick=${() => actions.tickAllSuggestions(forms, true)}>Tick all</button>
      <button onClick=${() => actions.tickAllSuggestions(forms, false)}>Untick all</button>
      <span class="spacer"></span>
      <button disabled=${busy.active} title="Scan again with the parties as they are now"
              onClick=${actions.scanSuggestions}>Rescan documents</button>
    </div>
  </div>`;
}

// The parties-first build lists forms of the parties' names, and nobody
// else: a third party is left as written, so proposing one is noise. A build
// without the parties sheet keeps the suggestion table it always had.
function NamesStep(props) {
  return Array.isArray(props.state.partyForms)
    ? html`<${PartyNames} ...${props} />` : html`<${SuggestionNames} ...${props} />`;
}

function PartyNames({ state, actions, busy }) {
  return html`
    <div class="columns">
      <${PartyForms} state=${state} actions=${actions} busy=${busy} />
      <div class="card">
        <h2>Anyone else to replace - one full name per line</h2>
        <textarea rows="10" spellcheck="false" value=${state.namesText}
                  onInput=${(e) => actions.namesText(e.target.value)}></textarea>
        <p class="hint">Everyone who is not one of the parties is left as written. A
          name typed here is replaced as well, in all its written forms. Mark special
          types after a pipe: <code>Tommy Smith | minor</code>,
          <code>Zions Bank | organization</code>.</p>
      </div>
    </div>
    <${GuardPanel} state=${state} />`;
}

function SuggestionNames({ state, actions, meta, busy }) {
  const rows = useMemo(() => [
    ...state.captions.map((c, i) => ({
      id: `cap-${i}`, name: c.name, category: state.suggCats[`cap-${i}`] || c.category,
      role: c.role, extra: c.confidence, where: c.source,
    })),
    ...state.suggestions.map((s, i) => ({
      id: `ner-${i}`, name: s.text, category: state.suggCats[`ner-${i}`] || s.category,
      // a diminutive of a party already on the list belongs to that party
      role: s.nickname_for ? `Short for ${s.nickname_for}` : "",
      extra: `x${s.count}`, where: (s.documents || []).join("; "),
      likely: s.likely !== false,
    })),
  ], [state.captions, state.suggestions, state.suggCats]);
  // headings, merchants, places and payment rails the model called names:
  // listed apart and unticked, so "Tick all" does not send them through
  const likely = rows.filter((row) => row.likely !== false);
  const unlikely = rows.filter((row) => row.likely === false);

  const nameCategories = meta.name_categories || ["person", "minor", "organization", "location"];
  const labelFor = (key) =>
    (meta.categories.find((c) => c.key === key) || { label: key }).label;
  const suggestionRow = (row) => html`<tr key=${row.id}>
    <td><input type="checkbox" checked=${state.suggChecked.has(row.id)}
               onChange=${() => actions.toggleSuggestion(row.id)} /></td>
    <td>${row.name}</td>
    <td><select value=${row.category}
                onChange=${(e) => actions.retypeSuggestion(row, e.target.value)}>
      ${nameCategories.map((key) => html`<option key=${key} value=${key}>${labelFor(key)}</option>`)}
    </select></td>
    <td>${row.role || row.extra}</td>
    <td class="hint">${row.where}</td>
  </tr>`;

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
        ${state.sortedBy ? html`<p class="hint">${state.sortedBy}</p>` : null}
        <div class="tablewrap"><table>
          <thead><tr><th></th><th>Name</th><th>Type</th><th>Found as</th><th>Where</th></tr></thead>
          <tbody>
            ${likely.map((row) => suggestionRow(row))}
          </tbody>
        </table></div>
        ${unlikely.length ? html`<details class="unlikely">
          <summary>${unlikely.length} more the scan proposed that are probably not names -
            headings, merchants, places, payment services. Tick any that is a person.</summary>
          <div class="tablewrap"><table><tbody>
            ${unlikely.map((row) => suggestionRow(row))}
          </tbody></table></div>
          <div class="bar">
            <button onClick=${() => actions.tickAllSuggestions(unlikely, true)}>Tick all of these</button>
            <button onClick=${() => actions.tickAllSuggestions(unlikely, false)}>Untick all of these</button>
          </div>
        </details>` : null}
        <div class="bar">
          <button onClick=${() => actions.addTicked(rows)}>Add ticked to the name list →</button>
          <button onClick=${() => actions.tickAllSuggestions(likely, true)}>Tick all</button>
          <button onClick=${() => actions.tickAllSuggestions(rows, false)}>Untick all</button>
          <span class="spacer"></span>
          <button disabled=${busy.active} onClick=${actions.readCaptions}>Re-read captions</button>
          <button disabled=${busy.active} title="Scan again with the names now on the list"
                  onClick=${actions.scanSuggestions}>Rescan documents</button>
        </div>
      </div>
      <div class="card">
        <h2>Name list - one full name per line</h2>
        <textarea rows="18" spellcheck="false" value=${state.namesText}
                  onInput=${(e) => actions.namesText(e.target.value)}></textarea>
        <p class="hint">Mark special types after a pipe: <code>Tommy Smith | minor</code>,
          <code>Zions Bank | organization</code>, <code>Sandy | location</code>.</p>
      </div>
    </div>
    <${GuardPanel} state=${state} />`;
}

/* ---------------------------------------------------------- 3. review -- */

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

// Kinds that can be one thing - a person and a child, the account kinds -
// as merges._FAMILIES has them; any other kind only with itself.
const KIND_FAMILIES = [["person", "minor"],
  ["bank_account", "masked_account", "credit_card", "investment_account", "loan_number"],
  ["organization", "employer"], ["phone", "fax"], ["street_address"],
  // the party sheet's kinds (parties.kind_of)
  ["account", "card"]];
const sameKind = (a, b) => a === b || KIND_FAMILIES.some((f) => f.includes(a) && f.includes(b));

// "Merge into...": the rows this one could be another form of, most found
// first, narrowed by typing.
function MergePicker({ row, rows, onClose, onPick }) {
  const [query, setQuery] = useState("");
  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    return rows
      .filter((r) => r.key !== row.key && sameKind(r.category, row.category))
      .filter((r) => !q || r.canonical.toLowerCase().includes(q)
        || String(r.replacement || "").toLowerCase().includes(q))
      .sort((a, b) => b.occurrences - a.occurrences)
      .slice(0, 60);
  }, [rows, row, query]);
  return html`<div class="overlay" onClick=${(e) => e.target === e.currentTarget && onClose()}>
    <div class="modal">
      <h3>Merge into another row</h3>
      <div class="row"><span>This row</span><div class="found">${row.canonical}</div></div>
      <p class="hint">Choose the row it is another form of. It will take that row's replacement
        and be listed under it; "Split out" takes it back out.</p>
      <div class="row"><span>Find</span>
        <input type="text" autofocus value=${query} placeholder="Type part of the row"
               onInput=${(e) => setQuery(e.target.value)} /></div>
      <div class="tablewrap mergelist"><table>
        <thead><tr><th>Type</th><th>Found</th><th>Replaced with</th><th>Times</th><th></th></tr></thead>
        <tbody>${shown.map((r) => html`<tr key=${r.key}>
          <td>${r.label}</td><td>${r.canonical}</td><td>${r.replacement}</td>
          <td class="num">${r.occurrences}</td>
          <td><button class="primary" onClick=${() => onPick(r)}>Merge here</button></td>
        </tr>`)}</tbody>
      </table></div>
      ${shown.length ? null : html`<p class="hint">No row of the same kind matches.</p>`}
      <div class="bar"><span class="spacer"></span><button onClick=${onClose}>Cancel</button></div>
    </div>
  </div>`;
}

// A grouped row in the shape the merge picker lists
const groupAsRow = (g) => ({ key: g.key, category: g.kind, label: g.label,
  canonical: g.text, replacement: g.replacement, occurrences: g.count });

// A group's tick: every form ticked, none, or some - shown half-ticked.
function groupTicks(group) {
  const forms = group.forms || [];
  if (!forms.length) return { all: group.enabled !== false, some: group.enabled !== false };
  const on = forms.filter((f) => f.enabled).length;
  return { all: on === forms.length, some: on > 0 };
}

// The review rows of a set: one per group where the server groups them,
// otherwise one per entity, as a build without the parties sheet lists them.
const reviewRows = (state) => (Array.isArray(state.groups)
  ? state.groups.filter((g) => groupTicks(g).some).length : state.active);

// One row per party per kind - "Respondent - SSN: 6 forms, 41 places" - in
// the order the server gives (petitioner, respondent, children, the parties,
// anything else), its forms folded under it. The overnight run listed every
// form as a row of its own: thousands, for five sets.
function GroupTable({ groups, byKey, open, toggleOpen, actions, edit, merge }) {
  if (!groups.length) {
    return html`<p class="hint">Nothing of the parties' was found to replace.</p>`;
  }
  return html`<div class="tablewrap"><table class="groups">
    <thead><tr><th title="Ticked = will be changed">Change?</th><th>What</th><th>Found as</th>
      <th>Replaced with</th><th>Places</th><th>Forms</th><th></th></tr></thead>
    <tbody>
      ${groups.map((group) => {
        const forms = group.forms || [];
        const head = forms.find((f) => f.key === group.key) || forms[0];
        const ticks = groupTicks(group);
        const shown = open.has(group.key);
        const entity = byKey[group.key];
        return html`
          <tr key=${group.key} class="group-row">
            <td class="tick"><input type="checkbox" checked=${ticks.all}
                ref=${(el) => { if (el) el.indeterminate = ticks.some && !ticks.all; }}
                aria-label=${`Change every form of ${group.label}`}
                title=${ticks.all ? "Will be changed" : ticks.some ? "Some forms are left as written"
                  : "Left as written"}
                onChange=${(e) => actions.setGroup(group, e.target.checked)} /></td>
            <td><strong>${group.label}</strong></td>
            <td>${head ? head.text : ""}</td>
            <td>${group.replacement}</td>
            <td class="num">${n(group.count)}</td>
            <td>${forms.length > 1 ? html`<button class="formsbtn" aria-expanded=${shown}
                  title=${shown ? "Hide the forms" : "Show every form found"}
                  onClick=${() => toggleOpen(group.key)}>${shown ? "▾" : "▸"} ${plural(forms.length, "form")}</button>`
              : html`<span class="hint">1 form</span>`}</td>
            <td>${entity ? html`<button title="Edit type or replacement"
                onClick=${() => edit(entity)}>✎ Edit</button>` : null}
                <button title="The same thing as another row - list it under that row"
                  onClick=${() => merge(groupAsRow(group))}>Merge into…</button></td>
          </tr>
          ${shown ? forms.map((form) => html`
            <tr key=${`${group.key}/${form.key}`} class="form-row">
              <td class="tick"><input type="checkbox" checked=${form.enabled}
                  aria-label=${`Change ${form.text}`}
                  title=${form.enabled ? "Will be changed" : "Left as written"}
                  onChange=${(e) => actions.setEnabled(form, e.target.checked)} /></td>
              <td></td>
              <td>${form.text}</td>
              <td class="hint">${(byKey[form.key] || {}).replacement || ""}</td>
              <td class="num">${n(form.count)}</td>
              <td></td>
              <td>${form.key !== group.key ? html`<button
                  title="Not the same thing - give it its own row and replacement"
                  onClick=${() => actions.split(form)}>Split out</button>` : null}</td>
            </tr>`) : null}`;
      })}
    </tbody>
  </table></div>`;
}

// What was found but is not the parties' - a payee's account, a policy
// number - and so goes through untouched. Counted, not listed: listing it
// is how the review list ran to thousands.
function LeftAsWritten({ counts }) {
  const kinds = Object.entries(counts || {}).filter(([, count]) => count > 0);
  if (!kinds.length) return null;
  const total = kinds.reduce((sum, [, count]) => sum + count, 0);
  return html`<details class="leftalone">
    <summary>Found but left as written: ${n(total)} - not the parties', so not replaced</summary>
    <ul>${kinds.map(([kind, count]) => html`<li key=${kind}>${kind.replace(/_/g, " ")}: ${n(count)}</li>`)}</ul>
  </details>`;
}

function ReviewStep({ state, actions, meta, busy }) {
  const [sort, setSort] = useState({ key: null, desc: false });
  const [editing, setEditing] = useState(null);
  const [merging, setMerging] = useState(null);
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

  const [open, setOpen] = useState(() => new Set());
  const toggleOpen = (key) => setOpen((was) => {
    const next = new Set(was);
    if (next.has(key)) next.delete(key); else next.add(key);
    return next;
  });
  // every entity by key, its forms too: a group's row edits its head
  const byKey = useMemo(() => {
    const out = {};
    for (const row of state.entities || []) {
      out[row.key] = row;
      for (const form of row.forms || []) out[form.key] = form;
    }
    return out;
  }, [state.entities]);
  const grouped = Array.isArray(state.groups);

  const cells = (row, form) => html`
    <td>${row.label}</td>
    <td>${row.canonical}${!form && row.forms && row.forms.length ? html`
      <span class="hint"> (+${row.forms.length} other form${row.forms.length > 1 ? "s" : ""})</span>` : null}</td>
    <td>${row.replacement}</td>
    <td class="num">${row.occurrences}</td>
    <td class="hint">${row.source}</td>
    <td class="warn small">${row.note}</td>`;

  return html`
    <div class="card">
      <p class="instructions">${(state.instructions || "").split("\n").map((line, i) =>
        html`<span key=${i}>${line}<br /></span>`)}</p>
      ${state.risky.length ? html`<p class="warn">Common words used as names -
        check these did not over-match: ${state.risky.join(", ")}</p>` : null}
      ${state.unused ? html`<p class="warn">${`${state.unused} ${state.unused === 1 ? "entry" : "entries"} did not appear in any document.`}</p>` : null}
      ${state.llm && state.llm.note ? html`<p class="hint">${state.llm.note}</p>` : null}
      ${grouped ? html`<${GroupTable} groups=${state.groups} byKey=${byKey} open=${open}
          toggleOpen=${toggleOpen} actions=${actions} edit=${setEditing} merge=${setMerging} />
        <${LeftAsWritten} counts=${state.leftAsWritten} />` : html`
      <div class="tablewrap"><table>
        <thead><tr><th title="Ticked = will be changed">Change?</th>${header("label", "Type")}
          ${header("canonical", "Found")}
          ${header("replacement", "Replaced with")}${header("occurrences", "Times")}
          ${header("source", "How found")}<th>Check</th><th></th></tr></thead>
        <tbody>
          ${rows.map((row) => html`
            <tr key=${row.key}>
              <td class="tick">
                ${row.forms && row.forms.length ? html`<button class="expander"
                    aria-expanded=${open.has(row.key)}
                    title=${open.has(row.key) ? "Hide the other forms" : "Show every form found"}
                    onClick=${() => toggleOpen(row.key)}>${open.has(row.key) ? "▾" : "▸"}</button>`
                  : html`<span class="expander"></span>`}
                <input type="checkbox" checked=${row.enabled}
                       title=${row.enabled ? "Will be changed" : "Left as written"}
                       onChange=${(e) => actions.setEnabled(row, e.target.checked)} /></td>
              ${cells(row, false)}
              <td><button title="Edit type or replacement"
                          onClick=${() => setEditing(row)}>✎ Edit</button>
                  <button title="The same thing as another row - list it under that row"
                          onClick=${() => setMerging(row)}>Merge into…</button></td>
            </tr>
            ${open.has(row.key) ? (row.forms || []).map((form) => html`
              <tr key=${form.key} class="form-row">
                <td class="tick"><span class="expander"></span>
                  <input type="checkbox" checked=${form.enabled}
                         title=${form.enabled ? "Will be changed" : "Left as written"}
                         onChange=${(e) => actions.setEnabled(form, e.target.checked)} /></td>
                ${cells(form, true)}
                <td><button title="Not the same thing - give it its own row and replacement"
                            onClick=${() => actions.split(form)}>Split out</button>
                    <button title="Edit type or replacement"
                            onClick=${() => setEditing(form)}>✎</button></td>
              </tr>`) : null}`)}
        </tbody>
      </table></div>`}
      <div class="bar">
        <button onClick=${() => actions.setAll(true)}>Tick all</button>
        <button onClick=${() => actions.setAll(false)}>Untick all</button>
        <button disabled=${busy.active} onClick=${actions.rescan}>Rescan documents</button>
      </div>
    </div>
    ${editing ? html`<${RowEditor} row=${editing} meta=${meta}
        onClose=${() => setEditing(null)}
        onSave=${async (changes) => { if (await actions.saveEditor(editing, changes)) setEditing(null); }} />` : null}
    ${merging ? html`<${MergePicker} row=${merging}
        rows=${grouped ? state.groups.map(groupAsRow) : state.entities || []}
        onClose=${() => setMerging(null)}
        onPick=${async (into) => { await actions.merge(merging, into); setMerging(null); }} />` : null}`;
}

/* ------------------------------------------------------------- 4. run -- */

// The run is queued, not run here: the queue runs every queued set one after
// another, unattended, into the results folder - a folder per set.
function RunStatus({ info, download, unqueue }) {
  const run = info.run || {};
  const audit = run.audit || {};
  // pages still to look at
  const toLook = audit.still !== undefined ? audit.still : audit.flagged || 0;
  switch (info.state) {
    case "queued":
      return html`<div>Queued - it runs when every set before it has.
        ${run.message && run.message !== "Waiting to run" ? html`<br /><span class="hint">${run.message}</span>` : null}
        <div><button onClick=${unqueue}>Take out of the queue</button></div></div>`;
    case "running":
      return html`<div><strong>Running</strong>${run.total ? ` - page ${n(run.done)} of ${n(run.total)}` : ""}
        ${run.message ? html`<br /><span class="hint">${run.message}</span>` : null}
        ${run.total ? html`<span class="setbar"><span
           style=${{ width: `${Math.round(100 * run.done / run.total)}%` }}></span></span>` : null}</div>`;
    case "failed":
      return html`<div class="status-bad">Failed.${run.error ? html`<br /><span class="small">${run.error}</span>` : null}</div>`;
    case "done":
      return html`<div>
        <span class=${run.failed ? "status-bad" : "status-ok"}>${run.failed
          ? `${run.failed} of ${run.files} file(s) were refused or failed and are NOT in the archive - the report says why.`
          : `Done - ${run.files} file(s).`}</span>
        ${audit.error ? html`<br /><span class="status-bad">The final check did not finish (${audit.error}).
            Look the archive over yourself before it goes anywhere.</span>`
          : audit.file ? html`<br /><span class=${toLook ? "status-bad" : "status-ok"}>
            Final check: ${n(audit.pages)} page(s) read back by ${audit.kev}.
            ${audit.fixed ? ` ${n(audit.fixed)} page(s) fixed by the local model and rechecked.` : ""}
            ${audit.cleared ? ` ${n(audit.cleared)} page(s) cleared by the local model.` : ""}
            ${toLook ? ` ${n(toLook)} page(s) to look at - listed in final-check.txt.`
              : audit.flagged ? " Nothing is left to look at - final-check.txt lists what was fixed."
              : " No page was flagged."}</span>` : null}
        <br /><span class="hint">Saved in ${run.results}</span>
        <div class="downloads">
          <a href=${download("archive")} download><button>Archive</button></a>
          <a href=${download("key")} download><button>Mapping key</button></a>
          <a href=${download("report")} download><button>Report</button></a>
        </div></div>`;
    default:
      return html`<span class="hint">Not queued</span>`;
  }
}

function RunStage({ filled, todo, work, options, sets, meta, busy, actions, notice }) {
  const anyDone = filled.some((info) => info.state === "done");
  return html`
    <div class="card">
      <h2>Run every set</h2>
      <p>${plural(filled.length, "set")}. DOCX: ${options.docx_mode}. PDF: ${options.pdf_mode}.
        ${todo.length ? ` ${plural(todo.length, "set")} to queue.` : " Nothing left to queue."}</p>
      <${ResultsLine} sets=${sets} chooseResults=${actions.chooseResults} />
      <p class="hint">Run all queues every set below in order. The queue runs them one at
        a time, with no further input, each into a folder of its own in the results
        folder, named after the set.</p>
      <p class="warn">Keep the window open until every set is done. Quitting the app
        stops the queue: a set that was running is left unfinished and must be run
        again, and every set's documents are deleted with the app. While sets run the
        Mac is kept from idle sleep; the screen still sleeps.</p>
      <div class="runtable"><table>
        <thead><tr><th>Set</th><th>Documents</th><th>Items to replace</th>
          <th>Mapping-key password</th><th>Status</th></tr></thead>
        <tbody>
          ${filled.map((info) => {
            const w = work(info.id);
            // once queued the password has gone with it: changing it here
            // would show a password the mapping key was not made with
            const fixed = ["queued", "running", "done"].includes(info.state);
            return html`<tr key=${info.id}>
              <td><strong>${info.name}</strong></td>
              <td class="num">${n(info.files)}</td>
              <td class="num">${w.reviewState === "built" ? n(reviewRows(w)) : "-"}</td>
              <td>${fixed && !w.queued ? html`<span class="hint">Queued before this window
                was opened again - it is the password copied then.</span>` : html`<div class="pwrow">
                <input type=${w.showPassword ? "text" : "password"} value=${w.password}
                       aria-label=${`Mapping-key password for ${info.name}`}
                       disabled=${fixed} onInput=${(e) => actions.password(info.id, e.target.value)} />
                <button onClick=${() => actions.showPassword(info.id, !w.showPassword)}>
                  ${w.showPassword ? "Hide" : "Show"}</button>
                <button onClick=${() => actions.copyPassword(info)}>Copy</button>
                <button disabled=${fixed} title="Make a new password for this set"
                        onClick=${() => actions.password(info.id, generatePassword())}>New</button>
              </div>`}</td>
              <td><${RunStatus} info=${info} unqueue=${() => actions.unqueue(info)}
                    download=${(kind) => actions.download(info.id, kind)} /></td>
            </tr>`;
          })}
        </tbody>
      </table></div>
      <div class="bar">
        <button class="primary" disabled=${busy.active || !todo.length} onClick=${actions.runAll}>
          Run all${todo.length ? ` (${plural(todo.length, "set")})` : ""}</button>
        <button onClick=${actions.copyAll}>Copy all passwords</button>
        ${notice ? html`<span class="status-ok">${notice}</span>` : null}
      </div>
      <p class="hint">Copy the passwords somewhere safe before you leave: each set's
        mapping key is offered as its own download, never inside the archive, and
        without its password it cannot be opened.</p>
      ${anyDone ? html`<p class="warn">Keep the mapping keys, the reports and
        final-check.txt away from anything you deliver.</p>` : null}
    </div>`;
}

/* ------------------------------------------------------------------- app -- */

// What the window holds for one set beyond what the server says about it.
function blankWork() {
  return {
    files: [], skipped: [], adding: "",
    password: generatePassword(), showPassword: false, queued: false,  // queued from here
    namesText: "", captions: [], captionsRead: false, officials: [], guardNotes: [], overlaps: [],
    suggestions: [], scanned: false, namesTried: false, suggChecked: new Set(), suggCats: {},
    // "" not built | waiting | building | built | failed
    reviewState: "", entities: [], active: 0, instructions: "", risky: [], unused: 0,
    llm: null,                        // a note on merges at review
    groups: null, leftAsWritten: null,  // the review list by party and kind, where the server groups
    // the parties sheet: "" not asked | loading | ready | unavailable | failed
    sheetState: "", sheet: ensureCore([]), surnamesText: "", surnamesEdited: false,
    sheetSaved: "", sheetSaving: false, sheetError: null, sheetLoadError: "", sheetOpen: true,
    partyForms: null, formsAdded: new Set(),   // forms of the parties' names the scan found
  };
}
const NO_WORK = { ...blankWork(), password: "" };

// a review list built before a change to the names, the files or the
// options is not the one to run from: it is built again on the Review step
const restale = (state) => (["built", "failed"].includes(state) ? "" : state);

function App() {
  const [meta, setMeta] = useState(null);
  const [sets, setSets] = useState({ sets: [], selected: null, results_root: "" });
  const [options, setOptions] = useState(DEFAULT_OPTIONS);
  const [work, setWork] = useState({});
  const [stage, setStage] = useState(0);
  const [unlocked, setUnlocked] = useState(0);
  const [namesAt, setNamesAt] = useState(null);
  const [reviewAt, setReviewAt] = useState(null);
  const [busy, setBusy] = useState({ active: false, message: "", fraction: 0 });
  const [fault, setFault] = useState("");
  const [notice, setNotice] = useState("");
  const apis = useRef({});
  const optionsLoaded = useRef(false);
  // the latest of every set's work, for a save that finishes after more typing
  const workRef = useRef(work);
  workRef.current = work;
  const sheetFlight = useRef({});           // a parties save under way, per set
  const sheetAsked = useRef(new Set());     // sets whose sheet has been asked for

  const apiFor = (id) => apis.current[id] || (apis.current[id] = window.makeApi(id));
  const refresh = () => api.get("/api/sets").then(setSets).catch(() => {});

  useEffect(() => {
    // the launch token did its job at load; the session cookie carries auth
    // from here, so drop it from the URL rather than leave it on screen
    if (window.location.search.includes("token=")) {
      window.history.replaceState({}, "", window.location.pathname);
    }
    api.get("/api/state").then((state) => {
      setMeta(state); setSets(state);
      if (state.options) setOptions({ ...DEFAULT_OPTIONS, ...state.options });
      optionsLoaded.current = true;
      // a window opened again over sets already filled shows their documents
      for (const info of state.sets) {
        if (info.files) apiFor(info.id).get("/api/files")
          .then((payload) => patch(info.id, { files: payload.files, skipped: payload.skipped || [] }))
          .catch(() => {});
      }
    }).catch((e) => setFault(e.message));
    // the sets read and run in the background: their counters move on their own
    const timer = setInterval(refresh, 2000);
    return () => clearInterval(timer);
  }, []);

  // every set has its own password from the moment it appears
  const ids = sets.sets.map((s) => s.id).join(",");
  useEffect(() => {
    setWork((all) => {
      let changed = false;
      const next = { ...all };
      for (const info of sets.sets) {
        if (!next[info.id]) { next[info.id] = blankWork(); changed = true; }
      }
      return changed ? next : all;
    });
    // and its parties sheet, held by the server with the set
    for (const info of sets.sets) {
      if (sheetAsked.current.has(info.id)) continue;
      sheetAsked.current.add(info.id);
      loadSheet(info.id);
    }
  }, [ids]);

  // the options live on the server too: the name lists are worked out ahead
  // with them, before any step sends them
  useEffect(() => {
    if (!optionsLoaded.current) return undefined;
    const timer = setTimeout(() => {
      api.put("/api/options", options).catch((e) => setFault(e.message));
    }, 300);
    return () => clearTimeout(timer);
  }, [options]);

  const w = (id) => work[id] || NO_WORK;
  const patch = (id, change) => setWork((all) => {
    const was = all[id] || blankWork();
    return { ...all, [id]: { ...was, ...(typeof change === "function" ? change(was) : change) } };
  });
  const nameOf = (id) => (sets.sets.find((s) => s.id === id) || { name: "This set" }).name;

  // ------------------------------------------------- the parties sheet --
  // A build without the parties sheet answers 404: the card says so and the
  // window works as it did, names from the name screen alone.
  async function loadSheet(id) {
    patch(id, { sheetState: "loading", sheetLoadError: "" });
    try {
      const payload = await apiFor(id).get("/api/parties");
      patch(id, (was) => sheetFromServer(payload, was, true));
    } catch (error) {
      if (error.status === 404 && !/removed/i.test(error.message)) patch(id, { sheetState: "unavailable" });
      else patch(id, { sheetState: "failed", sheetLoadError: error.message });
    }
  }

  // One save of a set's sheet. A second asked for while one is under way
  // waits for it and then saves whatever the sheet holds by then. The
  // answer is folded in only when nothing was typed meanwhile; otherwise
  // what was typed is saved next. `rescan` makes the set's name screen and
  // review list again - the server has marked both stale.
  async function saveSheet(id, { collapse = false, rescan = true, current = null } = {}) {
    const flight = sheetFlight.current;
    if (flight[id]) {
      flight[id].again = true;
      flight[id].collapse = flight[id].collapse || collapse;
      return flight[id].done;
    }
    const state = current || workRef.current[id];
    if (!state || state.sheetState !== "ready") return undefined;
    const { body, index } = sheetBody(state);
    const sent = JSON.stringify(body);
    if (sent === state.sheetSaved && !state.sheetError) {
      if (collapse && hasPartyName(state.sheet)) patch(id, { sheetOpen: false });
      return true;
    }
    const mine = { again: false, collapse };
    flight[id] = mine;
    mine.done = (async () => {
      let ok = false;
      patch(id, { sheetSaving: true });
      try {
        const answer = await apiFor(id).put("/api/parties", body);
        patch(id, (was) => {
          const stale = rescan ? { scanned: false, namesTried: false } : {};
          const common = { sheetSaving: false, sheetError: null,
                           reviewState: restale(was.reviewState), ...stale };
          if (sheetKey(was) !== sent) return { ...common, sheetSaved: sent };
          const sheet = mergeSheet(was.sheet, index, answer.parties);
          const surnamesText = Array.isArray(answer.family_surnames)
            ? answer.family_surnames.join(", ") : was.surnamesText;
          const next = { ...was, ...common, sheet, surnamesText };
          next.sheetSaved = sheetKey(next);
          if (mine.collapse && hasPartyName(sheet)) next.sheetOpen = false;
          return next;
        });
        ok = true;
      } catch (error) {
        patch(id, (was) => ({
          sheetSaving: false,
          sheetError: error.status === 422 ? locateRefusal(error, was.sheet, index)
            : { message: error.message, party: -1, field: null },
          sheetOpen: true,
        }));
      } finally {
        delete flight[id];
      }
      if (mine.again) {
        // the state settles on the next render before it is read again
        await new Promise((resolve) => setTimeout(resolve, 0));
        const later = await saveSheet(id, { collapse: mine.collapse, rescan });
        if (later !== undefined) ok = later;
      }
      return ok;
    })();
    return mine.done;
  }

  // A set's sheet changed on screen: saved when the focus leaves the sheet.
  const editSheet = (id, change) => patch(id, (was) => ({
    sheet: change(was.sheet.map((party) => ({ ...party }))),
  }));

  const track = (prefix) => (message, fraction, pages) =>
    setBusy({ active: true, message: prefix ? `${prefix} - ${message}` : message, fraction, pages });
  const settle = () => setBusy({ active: false, message: "", fraction: 0 });

  async function guarded(task) {
    setFault("");
    try { return await task(); } catch (error) { setFault(error.message); return undefined; }
  }

  // One step on the server for one set, polled to the end. The server works
  // one step at a time, so the steps of a stage go one after another.
  async function runJob(id, path, body, prefix) {
    const one = apiFor(id);
    try {
      track(prefix)("Starting", 0);
      return await one.job(one.post(path, body), track(prefix));
    } finally {
      settle();
    }
  }

  // ------------------------------------------------------------ the sets --
  const filled = sets.sets.filter((s) => s.state !== "empty");
  // the sets still to name, review and queue: a set queued or run is past it
  const workable = filled.filter((s) => !["queued", "running", "done"].includes(s.state));

  // Every set read and its name lists worked out, or the reason not yet.
  function namesGate() {
    if (!filled.length) return "Add documents to at least one set first.";
    const adding = sets.sets.find((s) => w(s.id).adding);
    if (adding) return `Still adding documents to ${adding.name}.`;
    // the parties first: every other detail is matched to them, and the
    // name screen looks for the forms of their names. Said before the
    // reading, which needs no one - the sheet can be filled while it reads.
    for (const info of workable) {
      const state = w(info.id);
      if (state.sheetState === "unavailable") continue;
      if (["", "loading"].includes(state.sheetState)) return `Loading the parties for ${info.name}.`;
      if (state.sheetState === "failed") {
        return `The parties for ${info.name} could not be loaded - press Try again on its card.`;
      }
      if (!hasPartyName(state.sheet)) {
        return `Enter both the petitioner's and the respondent's names for ${info.name} under The ` +
          "parties - everything else is matched to the parties, and the name screen looks for " +
          "the forms of their names. Every other box is optional.";
      }
      if (state.sheetSaving) return `Saving the parties for ${info.name}.`;
      if (state.sheetError) return `The parties for ${info.name} were not saved - ${state.sheetError.message}`;
      if (sheetDirty(state)) return `The parties for ${info.name} have changes not saved yet - press Save parties.`;
    }
    const reading = filled.find((s) => s.state === "reading");
    if (reading) {
      const read = reading.reading || {};
      return read.total
        ? `Waiting for every set to be read - ${reading.name} is at page ${n(read.done)} of ${n(read.total)}.`
        : `Waiting for every set to be read - ${reading.name} is being looked over for scanned pages.`;
    }
    const preparing = filled.find((s) => s.state === "preparing");
    if (preparing) return `Waiting for ${preparing.name}'s name lists to be worked out.`;
    if (!workable.length) return "Every set with documents is already queued or run - add another set.";
    return "";
  }
  const gate = namesGate();
  const empties = sets.sets.filter((s) => s.state === "empty");

  // ------------------------------------------------- one set's actions --
  const forSet = (id) => {
    const one = apiFor(id);
    const state = w(id);
    const applyFiles = (payload) => patch(id, (was) => ({
      files: payload.files, skipped: payload.skipped || [],
      // new documents: the name screen and the review list are made again
      captionsRead: false, scanned: false, namesTried: false,
      reviewState: restale(was.reviewState),
    }));
    const absorbGuard = (result) => ({
      ...(result.officials ? { officials: result.officials } : {}),
      ...(result.guard_notes ? { guardNotes: result.guard_notes } : {}),
      ...(result.overlaps ? { overlaps: result.overlaps } : {}),
    });
    const reviewPayload = (payload) => ({
      entities: payload.entities, risky: payload.risky, unused: payload.unused,
      active: payload.active, instructions: payload.instructions,
      ...(payload.llm ? { llm: payload.llm } : {}),
      // a build without the parties sheet sends no groups: the table it had
      groups: Array.isArray(payload.groups) ? payload.groups : null,
      leftAsWritten: payload.left_as_written || null,
    });
    const named = (task) => async () => {
      try { return await task(); } catch (error) { throw new Error(`${nameOf(id)}: ${error.message}`); }
    };

    // the two halves of the name screen, each a step on the server
    const readCaptions = async () => {
      const result = await runJob(id, "/api/captions", undefined, `Names for ${nameOf(id)}`);
      patch(id, (was) => {
        const checked = new Set(was.suggChecked);
        result.captions.forEach((c, i) => {
          if (c.confidence === "high" && !was.suggChecked.has(`cap-${i}-seen`)) checked.add(`cap-${i}`);
          checked.add(`cap-${i}-seen`);
        });
        return { captions: result.captions, captionsRead: true, suggChecked: checked, ...absorbGuard(result) };
      });
    };
    const scanSuggestions = async () => {
      const body = { options, names: parseNameLines(state.namesText) };
      const result = await runJob(id, "/api/suggestions", body, `Names for ${nameOf(id)}`);
      const forms = Array.isArray(result.party_forms) ? result.party_forms : null;
      patch(id, (was) => {
        // a form is pre-ticked the first time it is shown, never again: an
        // untick survives a rescan
        const checked = new Set(was.suggChecked);
        for (const form of forms || []) {
          const key = formId(form);
          if (!checked.has(`${key}-seen`) && pretick(form)) checked.add(key);
          checked.add(`${key}-seen`);
        }
        return { scanned: true, suggestions: result.suggestions || [], sortedBy: result.sorted_by || "",
                 partyForms: forms, suggChecked: checked, ...absorbGuard(result) };
      });
    };

    // Ticked forms of the parties' names onto the sheet, each under its
    // party - a misreading of the petitioner's name is the petitioner, not a
    // person of its own with an invented name of its own - and a form of the
    // family surname onto the surnames. Saved at once.
    const addForms = async () => {
      const now = workRef.current[id] || state;
      const forms = (now.partyForms || []).filter((form) =>
        now.suggChecked.has(formId(form)) && !now.formsAdded.has(formId(form)));
      if (!forms.length) return;
      if (now.sheetState !== "ready") {
        throw new Error("The parties sheet is not loaded, so the forms cannot be added to it.");
      }
      const sheet = now.sheet.map((party) => ({ ...party }));
      const surnames = parseSurnames(now.surnamesText);
      const extra = [];
      const same = (list, text) => list.some((v) => String(v).trim().toLowerCase() === text.trim().toLowerCase());
      let surnamesEdited = now.surnamesEdited;
      for (const form of forms) {
        if (isSurnameForm(form)) {
          if (!same(surnames, form.text)) { surnames.push(form.text.trim()); surnamesEdited = true; }
          continue;
        }
        const at = partyAt(sheet, form.party);
        if (at < 0) { extra.push(form.text.trim()); continue; }
        const names = (sheet[at].names || []).filter((v) => !isBlank(v));
        if (!same(names, form.text)) sheet[at] = { ...sheet[at], names: [...names, form.text.trim()] };
      }
      const known = new Set(now.namesText.split("\n").map((l) => l.trim().toLowerCase()));
      const lines = extra.filter((t) => !known.has(t.toLowerCase()));
      const next = {
        ...now, sheet, surnamesEdited,
        surnamesText: surnames.join(", "),
        formsAdded: new Set([...now.formsAdded, ...forms.map(formId)]),
        namesText: lines.length
          ? (now.namesText.trim() ? now.namesText.replace(/\s+$/, "") + "\n" : "") + lines.join("\n")
          : now.namesText,
      };
      patch(id, { sheet: next.sheet, surnamesText: next.surnamesText, surnamesEdited,
                  formsAdded: next.formsAdded, namesText: next.namesText,
                  reviewState: restale(now.reviewState) });
      if ((await saveSheet(id, { rescan: false, current: next })) === false) {
        throw new Error("The forms are on the parties sheet but it was not saved - its card on the Documents step says why.");
      }
    };

    const actions = {
      // Documents go up a batch at a time - a case folder runs to hundreds -
      // each with its path inside the folder it was dropped in. The rest are
      // only named, so the report can list them; their contents never leave.
      upload: (items) => guarded(named(async () => {
        const quiet = (name) => name.startsWith(".") || name.startsWith("~$")
          || ["thumbs.db", "desktop.ini"].includes(name.toLowerCase());
        const leaf = (path) => path.slice(path.lastIndexOf("/") + 1);
        const kept = items.filter((item) => !quiet(leaf(item.path)));
        const docs = kept.filter((item) => READABLE.test(item.path));
        const others = kept.filter((item) => !READABLE.test(item.path));
        let result = null;
        const BATCH = 20;
        try {
          for (let start = 0; start < Math.max(docs.length, 1); start += BATCH) {
            const form = new FormData();
            for (const item of docs.slice(start, start + BATCH)) {
              form.append("files", item.file);
              form.append("paths", item.path);
            }
            if (start === 0) for (const item of others) form.append("skipped", item.path);
            patch(id, { adding: docs.length > BATCH
              ? `Adding documents ${Math.min(start + BATCH, docs.length)} of ${docs.length}`
              : "Adding documents" });
            result = await one.post("/api/files", form);
          }
          applyFiles(result);
        } finally {
          patch(id, { adding: "" });
          refresh();              // its state, at once: not a stale "empty"
        }
      })),

      // a folder that cannot be read says so, like every other failure
      dropped: (taken) => guarded(named(async () => {
        patch(id, { adding: "Reading the folder" });
        let items;
        try { items = await readDrop(taken); } finally { patch(id, { adding: "" }); }
        if (items.length) await actions.upload(items);
      })),

      chooseFolder: () => guarded(named(async () => {
        // the app window: a native folder panel, read straight from disk
        const bridge = window.pywebview && window.pywebview.api;
        if (bridge && bridge.choose_folder) {
          patch(id, { adding: "Reading the folder" });
          try {
            const result = await bridge.choose_folder(id);
            if (result) applyFiles(result);
          } finally {
            patch(id, { adding: "" });
            refresh();
          }
          return;
        }
        document.getElementById(`folderpick-${id}`).click();   // a browser's own
      })),

      removeFile: (index) => guarded(named(async () => {
        applyFiles(await one.del(`/api/files/${index}`));
        refresh();
      })),

      removeAll: () => guarded(named(async () => {
        applyFiles(await one.del("/api/files"));
        refresh();
      })),

      // the parties sheet
      loadSheet: () => loadSheet(id),
      openSheet: (open) => patch(id, { sheetOpen: open }),
      saveSheet: (how) => saveSheet(id, how || {}),
      sheetSet: (at, key, i, value) => editSheet(id, (sheet) => {
        const values = [...(sheet[at][key] || [])];
        while (values.length < i) values.push(blankValue(PARTY_FIELDS.find((d) => d.key === key)));
        values[i] = value;
        sheet[at] = { ...sheet[at], [key]: values };
        return sheet;
      }),
      sheetAdd: (at, key) => editSheet(id, (sheet) => {
        const def = PARTY_FIELDS.find((d) => d.key === key);
        const values = sheet[at][key] && sheet[at][key].length ? [...sheet[at][key]] : [blankValue(def)];
        sheet[at] = { ...sheet[at], [key]: [...values, blankValue(def)] };
        return sheet;
      }),
      sheetDrop: (at, key, i) => editSheet(id, (sheet) => {
        sheet[at] = { ...sheet[at], [key]: (sheet[at][key] || []).filter((_, k) => k !== i) };
        return sheet;
      }),
      addParty: (role) => editSheet(id, (sheet) => ensureCore([...sheet, blankParty(role)])),
      removeParty: (at) => {
        const party = state.sheet[at];
        if (party && (firstName(party) || detailCount(party))
            && !window.confirm(`Take ${partyLabel(state.sheet, at)} off the parties?`)) return;
        editSheet(id, (sheet) => sheet.filter((_, k) => k !== at));
        // nothing is focused once the card has gone: save now
        setTimeout(() => saveSheet(id), 0);
      },
      surnames: (text) => patch(id, { surnamesText: text, surnamesEdited: true }),
      addForms: () => guarded(named(addForms)),
      addPendingForms: named(addForms),

      rename: (name) => guarded(named(async () => {
        if (name.trim()) setSets(await api.patch(`/api/sets/${id}`, { name }));
      })),

      remove: () => guarded(named(async () => {
        if (!window.confirm(`Remove "${nameOf(id)}" and its documents from this window?`)) return;
        setSets(await api.del(`/api/sets/${id}`));
      })),

      // the whole name screen at once: the caption's names, then every
      // document scanned for the rest - worked out ahead, so it opens at once
      loadNames: named(async () => {
        patch(id, { namesTried: true });
        if (!state.captionsRead) await readCaptions();
        if (!state.scanned) await scanSuggestions();
      }),

      readCaptions: () => guarded(named(readCaptions)),
      scanSuggestions: () => guarded(named(scanSuggestions)),

      namesText: (text) => patch(id, (was) => ({ namesText: text, reviewState: restale(was.reviewState) })),

      toggleSuggestion: (rowId) => patch(id, (was) => {
        const next = new Set(was.suggChecked);
        next.has(rowId) ? next.delete(rowId) : next.add(rowId);
        return { suggChecked: next };
      }),

      tickAllSuggestions: (rows, on) => patch(id, (was) => {
        const next = new Set(was.suggChecked);
        rows.forEach((row) => (on ? next.add(row.id) : next.delete(row.id)));
        return { suggChecked: next };
      }),

      retypeSuggestion: (row, category) =>
        patch(id, (was) => ({ suggCats: { ...was.suggCats, [row.id]: category } })),

      addTicked: (rows) => patch(id, (was) => {
        const existing = new Set(was.namesText.split("\n").map((l) => l.trim().toLowerCase()));
        const added = [];
        for (const row of rows) {
          if (!was.suggChecked.has(row.id)) continue;
          const line = nameLine(row.name, row.category);
          if (!existing.has(line.toLowerCase())) added.push(line);
        }
        if (!added.length) return {};
        return { namesText: (was.namesText.trim() ? was.namesText.replace(/\s+$/, "") + "\n" : "")
                   + added.join("\n"),
                 reviewState: restale(was.reviewState) };
      }),

      // this set's review list, built with the names as they are now
      buildReview: (prefix) => named(async () => {
        patch(id, { reviewState: "building" });
        try {
          const body = { options, names: parseNameLines(state.namesText) };
          const result = await runJob(id, "/api/review", body,
                                      typeof prefix === "string" ? prefix : `Review list for ${nameOf(id)}`);
          patch(id, { ...absorbGuard(result), ...reviewPayload(result), reviewState: "built" });
        } catch (error) {
          patch(id, { reviewState: "failed" });
          throw error;
        }
      })(),

      rescan: () => guarded(() => actions.buildReview()),

      setEnabled: (row, enabled) => guarded(named(async () => {
        patch(id, reviewPayload(await one.patch("/api/entities", { key: row.key, enabled })));
      })),

      // A group's tick, for every form in it. The head carries the forms
      // filed under it; a form the server grouped from elsewhere is ticked
      // on its own, so the row means what its tick shows.
      setGroup: (group, enabled) => guarded(named(async () => {
        let payload = await one.patch("/api/entities", { key: group.key, enabled });
        const again = (payload.groups || []).find((g) => g.key === group.key);
        const stray = (again ? again.forms : group.forms.filter((f) => f.key !== group.key))
          .filter((f) => f.enabled !== enabled);
        for (const form of stray) payload = await one.patch("/api/entities", { key: form.key, enabled });
        patch(id, reviewPayload(payload));
      })),

      setAll: (enabled) => guarded(named(async () => {
        patch(id, reviewPayload(await one.post("/api/entities/all", { enabled })));
      })),

      split: (row) => guarded(named(async () => {
        patch(id, reviewPayload(await one.post("/api/entities/split", { key: row.key })));
      })),

      // one row put under another: the same thing written another way
      merge: (row, into) => guarded(named(async () => {
        patch(id, reviewPayload(await one.post("/api/entities/merge", { key: row.key, into: into.key })));
      })),

      // true once saved, so the editor closes
      saveEditor: (row, changes) => guarded(named(async () => {
        const noError = meta.error_types[0];
        if (!changes.replacement.trim()) throw new Error("The replacement cannot be empty.");
        patch(id, reviewPayload(await one.patch("/api/entities", {
          key: row.key,
          category: changes.category,
          replacement: changes.replacement !== row.replacement ? changes.replacement : null,
        })));
        if (changes.errorType !== noError) {
          const logged = await one.post("/api/feedback", {
            error_type: changes.errorType,
            text: row.canonical,
            predicted_category: row.category,
            corrected_category: changes.category,
            corrected_replacement:
              changes.replacement !== row.replacement ? changes.replacement : null,
            source: row.source,
            occurrences: row.occurrences,
            documents: state.files.map((f) => f.name),
            origin: "review",
          });
          const send = window.confirm(
            `The report was saved to\n${logged.logged_to}\n(stored unencrypted on ` +
            "this computer - it contains the flagged text).\n\nOpen an email to " +
            `${logged.address} with this report? You will see the draft before ` +
            "anything is sent.");
          if (send) window.location.href = logged.mailto;
        }
        return true;
      })),
    };
    return actions;
  };

  // ------------------------------------------------------ the stages --
  // Every set's review list, one after another - the server builds one at a
  // time. A set that fails says so and the rest are still built.
  const buildReviews = (list) => guarded(async () => {
    const todo = list.filter((info) => !["waiting", "building"].includes(w(info.id).reviewState));
    for (const info of todo) patch(info.id, { reviewState: "waiting" });
    const failed = [];
    for (const info of todo) {
      const at = workable.findIndex((s) => s.id === info.id) + 1;
      try {
        await forSet(info.id).buildReview(
          `Building the review list for set ${at} of ${workable.length} (${info.name})`);
      } catch (error) {
        failed.push(error.message);
      }
    }
    if (failed.length) throw new Error(`Could not build every review list. ${failed.join(" ")}`);
  });

  // the Review step builds what it has not got, however it was reached
  useEffect(() => {
    if (stage !== 2 || busy.active) return;
    const missing = workable.filter((info) => w(info.id).reviewState === "");
    if (missing.length) buildReviews(missing);
  }, [stage, busy.active, workable.map((s) => `${s.id}:${w(s.id).reviewState}`).join(",")]);

  // each set's name screen is opened as it is reached
  const namesList = workable;
  const namesSet = namesList.find((s) => s.id === namesAt) || namesList[0];
  useEffect(() => {
    if (stage !== 1 || !namesSet || busy.active) return;
    const state = w(namesSet.id);
    if (state.namesTried || (state.captionsRead && state.scanned)) return;
    guarded(forSet(namesSet.id).loadNames);
  }, [stage, namesSet && namesSet.id, busy.active, namesSet && w(namesSet.id).namesTried]);

  const reviewSet = workable.find((s) => s.id === reviewAt) || workable[0];
  const allBuilt = workable.length > 0 && workable.every((s) => w(s.id).reviewState === "built");

  // the Run step is open whenever a set has been queued: a window opened
  // again over a night's runs still shows them, and their downloads
  const queuedAny = sets.sets.some((s) => ["queued", "running", "done", "failed"].includes(s.state));
  const reachable = [true, !gate, !gate && unlocked >= 2, unlocked >= 3 || queuedAny];
  const go = (target) => {
    setFault("");
    setStage(target);
    setUnlocked((u) => Math.max(u, target));
    window.scrollTo(0, 0);
  };

  const flash = (message) => {
    setNotice(message);
    setTimeout(() => setNotice(""), 2500);
  };
  const copy = (text, message) => guarded(async () => {
    if (!(await copyText(text))) {
      throw new Error("Could not copy - press Show and copy the password by hand.");
    }
    flash(message);
  });

  const stageActions = {
    addSet: () => guarded(async () => setSets(await api.post("/api/sets", {}))),
    chooseResults: () => guarded(async () => {
      const bridge = window.pywebview && window.pywebview.api;
      if (bridge && bridge.choose_results_folder) {
        const result = await bridge.choose_results_folder();
        if (result) setSets(result);
        return;
      }
      const typed = window.prompt("Folder for the results (a full path):", sets.results_root || "");
      if (typed) setSets(await api.post("/api/results-folder", { path: typed }));
    }),
    setOption: (key, value) => {
      setOptions((o) => ({ ...o, [key]: value }));
      // what the scan proposes and what the review list holds both follow
      // the options: made again when next reached
      setWork((all) => Object.fromEntries(Object.entries(all).map(([id, one]) => [id, {
        ...one, reviewState: restale(one.reviewState),
        ...(key === "ner" ? { scanned: false, namesTried: false } : {}),
      }])));
    },
    password: (id, value) => patch(id, { password: value }),
    showPassword: (id, on) => patch(id, { showPassword: on }),
    copyPassword: (info) => copy(w(info.id).password, `Copied the password for ${info.name}.`),
    // the passwords this window knows: a set queued before the window was
    // opened again has a password only the operator kept
    copyAll: () => {
      const known = filled.filter((info) =>
        !["queued", "running", "done"].includes(info.state) || w(info.id).queued);
      return copy(known.map((info) => `${info.name}\t${w(info.id).password}`).join("\n"),
                  `Copied ${plural(known.length, "password")}.`);
    },
    download: (id, kind) => `/api/download/${kind}?set=${encodeURIComponent(id)}` +
      (meta && meta.download_token ? `&token=${encodeURIComponent(meta.download_token)}` : ""),
    unqueue: (info) => guarded(async () => {
      setSets(await apiFor(info.id).post("/api/queue/cancel"));
    }),
    // every set to the queue, in order, with its own password and the one
    // set of options; checked first, so a night's queue is not half made
    runAll: () => guarded(async () => {
      if (!sets.results_root) throw new Error("Choose the folder the results go to first.");
      for (const info of workable) {
        if (!w(info.id).password.trim()) throw new Error(`Give ${info.name} a mapping-key password, or press New.`);
        if (w(info.id).reviewState !== "built") {
          throw new Error(`${info.name} has no review list yet - open the Review step first.`);
        }
      }
      await api.put("/api/options", options);
      for (const info of workable) {
        try {
          setSets(await apiFor(info.id).post("/api/queue", { options, password: w(info.id).password }));
          patch(info.id, { queued: true });
        } catch (error) {
          throw new Error(`${info.name} was not queued: ${error.message}` +
            " The sets before it are queued.");
        }
      }
    }),
  };

  if (!meta) {
    return html`<div>
      <header class="topbar"><div class="topbar-inner">
        <h1>Document Redactions & Anonymization</h1>
      </div></header>
      <div class="shell"><div class="card">${fault ? html`<p class="error">${fault}</p>`
        : html`<p class="hint">Loading…</p>`}</div></div></div>`;
  }

  const position = (list, info) => `Set ${list.findIndex((s) => s.id === info.id) + 1} of ${list.length}`;
  const neighbours = (list, info) => {
    const at = list.findIndex((s) => s.id === info.id);
    return [list[at - 1] || null, list[at + 1] || null];
  };

  // Leaving a set's name screen puts its ticked forms on its sheet first: a
  // ticked form left behind would be ticked on screen and missing from the
  // run. If the sheet will not save, the screen stays put and says why.
  const leaveNames = (then) => guarded(async () => {
    if (stage === 1 && namesSet) await forSet(namesSet.id).addPendingForms();
    then();
  });

  let body = null;
  if (stage === 0) {
    const line = readingLine(filled);
    body = html`
      <div class="card">
        <p class="hint intro">One set per case. Each set's documents are read in the
          background as soon as they are added, set after set in the order the sets
          were made, and its name lists are worked out once it is read.${
          sets.sets.some((s) => w(s.id).sheetState === "ready") ? ` Fill in each set's
          parties while it reads: their names and what identifies them are what is
          replaced, and everyone else is left as written.` : ""} The names come when
          every set is read; everything stays on this computer.</p>
        <${ResultsLine} sets=${sets} chooseResults=${stageActions.chooseResults} />
      </div>
      <div class="columns">
        <div>
          ${sets.sets.map((info, index) => html`<${SetCard} key=${info.id} info=${info}
              number=${index + 1} work=${w(info.id)} actions=${forSet(info.id)}
              canRemove=${sets.sets.length > 1 && !["queued", "running"].includes(info.state)} />`)}
          <div class="bar"><button onClick=${stageActions.addSet}>+ Add another set</button></div>
        </div>
        <div><${OptionsPanel} options=${options} setOption=${stageActions.setOption} /></div>
      </div>
      ${line && !busy.active ? html`<${Progress} busy=${{ active: true, ...line }} />` : null}
      <div class="bar">
        <span class=${"spacer gate" + (gate ? " waiting" : "")}>${gate ||
          `${plural(workable.length, "set")} read and ready.`}${empties.length && filled.length
          ? ` ${empties.map((s) => s.name).join(", ")} ${empties.length === 1 ? "has" : "have"} no documents and ${empties.length === 1 ? "is" : "are"} left out.` : ""}</span>
        <button class="primary" disabled=${!!gate}
                onClick=${() => { setNamesAt(workable[0] && workable[0].id); go(1); }}>
          Continue to names →</button>
      </div>`;
  } else if (stage === 1) {
    if (!namesSet) {
      body = html`<div class="card"><p>Nothing to name - every set with documents is queued or run.</p>
        <div class="bar"><button onClick=${() => go(0)}>← Back to documents</button></div></div>`;
    } else {
      const state = w(namesSet.id);
      const [before, after] = neighbours(namesList, namesSet);
      const opened = state.captionsRead && state.scanned;
      body = html`
        <div class="card stagehead">
          <h2>Names - ${position(namesList, namesSet)}: ${namesSet.name}</h2>
          <p class="hint">Name every set in turn; what is typed and ticked here is kept
            when you move between sets. The review lists are built once the last set is named.</p>
        </div>
        ${!opened && !busy.active && state.namesTried ? html`<div class="card">
          <p class="warn">This set's name lists did not open.</p>
          <div class="bar"><button onClick=${() => guarded(forSet(namesSet.id).loadNames)}>Try again</button></div>
        </div>` : null}
        <${NamesStep} key=${namesSet.id} state=${state} actions=${forSet(namesSet.id)} meta=${meta} busy=${busy} />
        <div class="bar">
          ${before ? html`<button disabled=${busy.active}
                onClick=${() => leaveNames(() => { setNamesAt(before.id); window.scrollTo(0, 0); })}>
              ← Previous set</button>`
            : html`<button onClick=${() => leaveNames(() => go(0))}>← Back to documents</button>`}
          <span class="spacer"></span>
          ${after ? html`<button class="primary" disabled=${busy.active}
                onClick=${() => leaveNames(() => { setNamesAt(after.id); window.scrollTo(0, 0); })}>Next set →</button>`
            : html`<button class="primary" disabled=${busy.active}
                onClick=${() => leaveNames(() => { setReviewAt(workable[0] && workable[0].id); go(2); })}>
                Continue to review →</button>`}
        </div>`;
    }
  } else if (stage === 2) {
    if (!reviewSet) {
      body = html`<div class="card"><p>Nothing to review - every set with documents is queued or run.</p>
        <div class="bar"><button onClick=${() => go(3)}>Go to the run →</button></div></div>`;
    } else {
      const state = w(reviewSet.id);
      const [before, after] = neighbours(workable, reviewSet);
      const waiting = workable.filter((s) => w(s.id).reviewState !== "built");
      body = html`
        <div class="card stagehead">
          <h2>Review - ${position(workable, reviewSet)}: ${reviewSet.name}</h2>
          <ul class="buildlist">
            ${workable.map((info) => html`<li key=${info.id} class=${w(info.id).reviewState || "none"}>
              ${info.name}: ${{ waiting: "waiting its turn", building: "building the review list",
                               built: `${plural(reviewRows(w(info.id)), "item")} to replace`,
                               failed: "the review list could not be built" }[w(info.id).reviewState]
                              || "no review list yet"}</li>`)}
          </ul>
        </div>
        ${state.reviewState === "built"
          ? html`<${ReviewStep} key=${reviewSet.id} state=${state} actions=${forSet(reviewSet.id)}
                   meta=${meta} busy=${busy} />`
          : html`<div class="card">
              <p class=${state.reviewState === "failed" ? "warn" : "hint"}>${{
                waiting: "This set's review list is built after the ones before it - step through those meanwhile.",
                building: "This set's review list is being built now.",
                failed: "This set's review list could not be built - the message above says why.",
              }[state.reviewState] || "This set has no review list yet."}</p>
              ${["", "failed"].includes(state.reviewState) ? html`<div class="bar">
                <button disabled=${busy.active} onClick=${() => buildReviews([reviewSet])}>
                  Build its review list</button></div>` : null}
            </div>`}
        <div class="bar">
          ${before ? html`<button onClick=${() => { setReviewAt(before.id); window.scrollTo(0, 0); }}>← Previous set</button>`
            : html`<button onClick=${() => { setNamesAt(namesList[namesList.length - 1].id); go(1); }}>
                ← Back to names</button>`}
          <span class="spacer"></span>
          ${after ? html`<button class="primary" onClick=${() => { setReviewAt(after.id); window.scrollTo(0, 0); }}>
              Next set →</button>`
            : html`${!allBuilt ? html`<span class="gate waiting">Waiting for the review list of ${
                waiting.map((s) => s.name).join(", ")}.</span>` : null}
              <button class="primary" disabled=${busy.active || !allBuilt}
                onClick=${() => go(3)}>Continue to run →</button>`}
        </div>`;
    }
  } else {
    body = html`<${RunStage} filled=${filled} todo=${workable} work=${w} options=${options}
      sets=${sets} meta=${meta} busy=${busy} actions=${stageActions} notice=${notice} />
      <div class="bar"><button onClick=${() => go(2)}>← Back to review</button></div>`;
  }

  return html`<div>
    <header class="topbar"><div class="topbar-inner">
      <h1>Document Redactions & Anonymization</h1>
      <span class="version">${meta.version} · everything stays on this computer</span>
    </div></header>
    <div class="shell">
      <${Stepper} stage=${stage} reachable=${reachable} go=${(target) => leaveNames(() => go(target))} />
      <datalist id="account-kinds">${ACCOUNT_KINDS.map((kind) => html`<option key=${kind} value=${kind} />`)}</datalist>
      ${fault ? html`<div class="card"><p class="error">${fault}</p></div>` : null}
      ${body}
      <${Progress} busy=${busy} />
    </div>
  </div>`;
}

ReactDOM.createRoot(document.getElementById("root")).render(html`<${App} />`);
