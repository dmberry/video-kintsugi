// Video Kintsugi page: the recordings list, the preset and filter editor, the preview and the job.
"use strict";

const $ = (id) => document.getElementById(id);
const STORE = "video-kintsugi.v1";
const FILTERS = [
  ["pan", "Mix stereo down to mono (one voice into both ears).", "pan=mono|c0=0.5*c0+0.5*c1"],
  ["highpass", "Cut rumble, traffic and handling noise below a frequency.", "highpass=f=90"],
  ["lowpass", "Cut hiss above a frequency. Speech needs little above 8 kHz.", "lowpass=f=8000"],
  ["afftdn", "Reduce steady background noise. nf is the noise floor in dB; nr the reduction; tn=1 follows changing noise.", "afftdn=nf=-25"],
  ["anlmdn", "Stronger, slower noise reduction for very noisy recordings.", "anlmdn=s=7"],
  ["agate", "Silence the gaps between phrases when the room sound is distracting.", "agate=threshold=0.02:ratio=3:attack=10:release=250"],
  ["equalizer", "Lift or cut one band. Around 3 kHz brings speech forward; 200–300 Hz cut reduces boominess.", "equalizer=f=3000:t=q:w=1:g=3"],
  ["deesser", "Soften harsh s and sh sounds.", "deesser=i=0.4"],
  ["acompressor", "Compress loud peaks.", "acompressor=threshold=0.1:ratio=3:attack=20:release=250"],
  ["dynaudnorm", "Even out quiet and loud passages over a few seconds. Higher g and m lift quiet stretches more.", "dynaudnorm=f=250:g=15:p=0.9:m=8"],
  ["speechnorm", "Faster levelling made for speech, phrase by phrase.", "speechnorm=e=6:r=0.0001:l=1"],
  ["adeclick", "Remove clicks and pops.", "adeclick"],
  ["adeclip", "Repair speech that was recorded too loud and clipped.", "adeclip"],
  ["volume", "Change the level by a fixed amount.", "volume=3dB"],
  ["loudnorm", "Set the overall loudness, measured over the whole recording first. I=-14 matches YouTube and suits phones; -16 suits podcasts; -23 is broadcast. TP is the peak ceiling.", "loudnorm=I=-16:TP=-2"],
];

let files = [];
let presets = [];
let builtin = [];
let current = null;       // the selected preset object
let outdir = "";          // "" means beside the first recording
let nameEdited = false;
let lastJob = null;
let preview = null;
let planTimer = 0;
let planSeq = 0;
let lastPlan = null;
let previewTimer = 0;
let previewBusy = false;
const prefs = loadPrefs();
// inside the Mac app (mac/VideoKintsugi.swift) files, folders and the clipboard go through the window
const NATIVE = !!(window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.kintsugi);
const native = (msg) => window.webkit.messageHandlers.kintsugi.postMessage(msg);
if (NATIVE) document.documentElement.classList.add("native");

async function copyText(text) {
  if (NATIVE) native({ cmd: "copy", text });
  else await navigator.clipboard.writeText(text);
}

function loadPrefs() {
  try { return JSON.parse(localStorage.getItem(STORE)) || {}; } catch { return {}; }
}
function savePrefs() {
  prefs.preset = current && current.id;
  prefs.video = $("video").value;
  prefs.outdir = outdir;
  prefs.pvlen = $("pvlen").value;
  prefs.pvauto = $("pvauto").checked;
  try { localStorage.setItem(STORE, JSON.stringify(prefs)); } catch { /* private window */ }
}

async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json", "X-Kintsugi": "1" }, body: JSON.stringify(body) });
  const data = await r.json().catch(() => ({ error: "Video Kintsugi isn't responding." }));
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}

function hms(s) {
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60, x = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(x).padStart(2, "0")}` : `${m}:${String(x).padStart(2, "0")}`;
}
function parseTime(t) {
  const parts = String(t).trim().split(":").map(Number);
  if (parts.some(isNaN)) return 0;
  return parts.reduce((a, b) => a * 60 + b, 0);
}
function size(b) {
  return b >= 1e9 ? (b / 1e9).toFixed(2) + " GB" : b >= 1e6 ? (b / 1e6).toFixed(1) + " MB" : Math.round(b / 1e3) + " KB";
}
function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
}
const natural = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });

// ---------------------------------------------------------------- recordings

function addFiles(list, errors) {
  const fresh = list.filter((f) => !files.some((g) => g.path === f.path));
  fresh.sort((a, b) => natural.compare(a.name, b.name));
  files = files.concat(fresh);
  $("fileerr").textContent = (errors || []).join("\n");
  renderFiles();
}

function describe(f) {
  const bits = [hms(f.duration)];
  const v = f.video, a = f.audio;
  if (v) bits.push(`${v.width}×${v.height} ${String(v.codec).toUpperCase()} ${+v.fps.toFixed(2)} fps`);
  bits.push(a ? `${String(a.codec).toUpperCase()} ${a.channels === 1 ? "mono" : a.channels === 2 ? "stereo" : a.channels + " ch"} ${a.rate / 1000} kHz` : "no sound");
  bits.push(size(f.size));
  return bits.join(" · ");
}

function renderFiles() {
  const ul = $("files");
  ul.innerHTML = "";
  const key = (f) => f.video && [f.video.codec, f.video.width, f.video.height, f.video.fps].join();
  files.forEach((f, i) => {
    const li = document.createElement("li");
    li.draggable = true;
    li.dataset.i = i;
    const odd = files.length > 1 && i > 0 && key(f) !== key(files[0]);
    li.innerHTML = `<span class="grip" title="Drag to reorder">⋮⋮</span><span class="num">${i + 1}</span>
      <div class="meta"><div class="name">${esc(f.name)}</div><div class="info${odd ? " odd" : ""}">${esc(describe(f))}${odd ? " · differs from the first" : ""}</div>
      <div class="info">${esc(f.dir)}</div></div>
      <div class="btns"><button class="b icon" data-mv="-1" title="Move up" ${i ? "" : "disabled"}>↑</button>
      <button class="b icon" data-mv="1" title="Move down" ${i < files.length - 1 ? "" : "disabled"}>↓</button>
      <button class="b icon" data-rm title="Remove">✕</button></div>`;
    ul.appendChild(li);
  });
  const total = files.reduce((a, f) => a + f.duration, 0);
  $("total").textContent = files.length ? `${files.length} file${files.length > 1 ? "s" : ""} · ${hms(total)}` : "";
  $("drop").classList.toggle("compact", files.length > 0);
  $("drop").querySelector(".big").textContent = files.length ? "Add more recordings…" : "Drop recordings here, or click to choose";
  $("pvmake").disabled = !files.length;
  if (files.length && parseTime($("pvat").value) > total) $("pvat").value = hms(total / 2);
  if (!nameEdited) $("outname").value = defaultName();
  refreshStartLabel();
  schedulePlan();
}

$("files").addEventListener("click", (e) => {
  const li = e.target.closest("li");
  if (!li) return;
  const i = +li.dataset.i;
  const mv = e.target.closest("[data-mv]");
  if (mv) {
    const j = i + +mv.dataset.mv;
    [files[i], files[j]] = [files[j], files[i]];
  } else if (e.target.closest("[data-rm]")) {
    files.splice(i, 1);
  } else return;
  renderFiles();
});

let dragFrom = -1;
$("files").addEventListener("dragstart", (e) => {
  const li = e.target.closest("li");
  dragFrom = +li.dataset.i;
  li.classList.add("dragging");
  e.dataTransfer.effectAllowed = "move";
  e.dataTransfer.setData("text/x-kintsugi-row", String(dragFrom));
});
$("files").addEventListener("dragend", () => { dragFrom = -1; renderFiles(); });
$("files").addEventListener("dragover", (e) => {
  if (dragFrom < 0) return;
  e.preventDefault();
  document.querySelectorAll("#files li.target").forEach((x) => x.classList.remove("target"));
  const li = e.target.closest("li");
  if (li) li.classList.add("target");
});
$("files").addEventListener("drop", (e) => {
  if (dragFrom < 0) return;
  e.preventDefault();
  e.stopPropagation();
  const li = e.target.closest("li");
  if (!li) return;
  const to = +li.dataset.i;
  const [f] = files.splice(dragFrom, 1);
  files.splice(to, 0, f);
  dragFrom = -1;
  renderFiles();
});

async function chooseFiles() {
  if (NATIVE) return native({ cmd: "choose-files", start: files.length ? files[files.length - 1].dir : undefined });
  try {
    const r = await api("/api/choose-files", {});
    addFiles(r.files, r.errors);
  } catch (e) { $("fileerr").textContent = e.message; }
}

async function addPaths(paths) {
  try {
    const r = await api("/api/probe", { paths });
    addFiles(r.files, r.errors);
  } catch (e) { $("fileerr").textContent = e.message; }
}

const drop = $("drop");
drop.addEventListener("click", chooseFiles);
drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); chooseFiles(); } });
for (const ev of ["dragenter", "dragover"]) {
  document.addEventListener(ev, (e) => {
    if (dragFrom >= 0 || !e.dataTransfer.types.includes("Files")) return;
    e.preventDefault();
    drop.classList.add("over");
  });
}
document.addEventListener("dragleave", (e) => { if (!e.relatedTarget) drop.classList.remove("over"); });
document.addEventListener("drop", async (e) => {
  if (dragFrom >= 0 || !e.dataTransfer.files.length) return;
  e.preventDefault();
  drop.classList.remove("over");
  const found = [], errors = [];
  for (const f of e.dataTransfer.files) {
    try {
      const r = await api("/api/resolve", { name: f.name, size: f.size, modified: f.lastModified });
      if (r.files.length) found.push(...r.files);
      else errors.push(`Couldn't find where ${f.name} is on disk. Use Choose, or paste its path.`);
      errors.push(...r.errors);
    } catch (err) { errors.push(err.message); }
  }
  addFiles(found, errors);
});

$("pathin").addEventListener("keydown", (e) => { if (e.key === "Enter") $("pathadd").click(); });
$("pathadd").addEventListener("click", () => {
  const raw = $("pathin").value.trim();
  if (!raw) return;
  // several paths pasted at once: one per line, or shell-quoted as Finder's "Copy as Pathname" gives them
  const paths = raw.includes("\n") ? raw.split("\n") : (raw.match(/'[^']+'|"[^"]+"|\S+(?:\\ \S*)*/g) || []).map((p) => p.replace(/\\ /g, " "));
  $("pathin").value = "";
  addPaths(paths.map((p) => p.trim().replace(/^['"]|['"]$/g, "")).filter(Boolean));
});

// ---------------------------------------------------------------- presets and filters

function editorLines() { return $("filters").value.replace(/\s+$/, "").split("\n"); }
function isEdited() {
  return current && (editorLines().join("\n") !== current.filters.join("\n") || $("bitrate").value !== current.bitrate);
}

function renderPresets() {
  $("preset").innerHTML = presets.map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join("");
  $("preset").value = current.id;
  $("m-presets").innerHTML = presets.map((p) =>
    `<button data-preset="${esc(p.id)}" aria-checked="${p.id === current.id}">${esc(p.name)}</button>`).join("");
}

function selectPreset(id) {
  current = presets.find((p) => p.id === id) || presets[0];
  $("filters").value = current.filters.join("\n");
  $("bitrate").value = current.bitrate;
  $("about").textContent = current.about || "";
  renderPresets();
  Desk.render();
  markEdited();
  savePrefs();
  refreshStartLabel();
  schedulePlan();
}

function markEdited() {
  const ed = isEdited();
  $("edited").classList.toggle("hidden", !ed);
  $("save").disabled = !ed;
  $("revert").disabled = !ed;
}

async function storePresets(list) {
  const r = await api("/api/presets", { presets: list });
  presets = r.presets;
}

async function saveToPreset() {
  if (!isEdited()) return;
  current.filters = editorLines();
  current.bitrate = $("bitrate").value;
  await storePresets(presets);
  selectPreset(current.id);
}

function openSaveAs() {
  $("sa-name").value = current.name + " (mine)";
  $("sa-about").value = current.about;
  $("dlg-saveas").showModal();
  $("sa-name").select();
}
$("sa-ok").addEventListener("click", async () => {
  const p = { id: "p" + Date.now(), name: $("sa-name").value.trim() || "My preset",
              about: $("sa-about").value.trim(), bitrate: $("bitrate").value, filters: editorLines() };
  const old = presets.find((x) => x.id === current.id);
  if (old) { const i = presets.indexOf(old); presets.splice(i + 1, 0, p); } else presets.push(p);
  // the preset we came from keeps its saved lines
  try {
    await storePresets(presets);
    $("dlg-saveas").close();
    selectPreset(p.id);
  } catch (e) { alert(e.message); }
});

function confirmBox(title, text, ok) {
  $("cf-title").textContent = title;
  $("cf-text").textContent = text;
  $("cf-ok").onclick = async () => { $("dlg-confirm").close(); await ok(); };
  $("dlg-confirm").showModal();
}

function deletePreset() {
  if (presets.length < 2) return;
  confirmBox(`Delete “${current.name}”?`, builtin.includes(current.id)
    ? "This is a built-in preset; Restore Built-in Presets brings it back." : "This can't be undone.", async () => {
    const i = presets.indexOf(current);
    presets.splice(i, 1);
    await storePresets(presets);
    selectPreset(presets[Math.max(0, i - 1)].id);
  });
}

function restoreBuiltins() {
  confirmBox("Restore built-in presets?", "The built-in presets go back to how they came. Presets you made are kept.", async () => {
    const r = await api("/api/presets");
    await storePresets(r.defaults.concat(presets.filter((p) => !r.builtin.includes(p.id))));
    selectPreset(current.id);
  });
}

$("preset").addEventListener("change", () => {
  const id = $("preset").value;
  if (isEdited()) {
    confirmBox("Discard your changes?", `The filter chain for “${current.name}” has changes that aren't saved.`, () => selectPreset(id));
    $("preset").value = current.id;
  } else selectPreset(id);
});
$("filters").addEventListener("input", chainChanged);
$("filters").addEventListener("change", () => Desk.render());

function chainChanged() {
  markEdited();
  schedulePlan();
  if (preview && $("pvauto").checked) {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(() => makePreview(true), 900);
  }
}

function setView(v) {
  $("view-desk").setAttribute("aria-pressed", v === "desk");
  $("view-text").setAttribute("aria-pressed", v === "text");
  $("desk").classList.toggle("hidden", v !== "desk");
  $("textview").classList.toggle("hidden", v !== "text");
  if (v === "desk") Desk.render();
  prefs.view = v;
  savePrefs();
}
$("view-desk").addEventListener("click", () => setView("desk"));
$("view-text").addEventListener("click", () => setView("text"));
$("bitrate").addEventListener("change", () => { markEdited(); schedulePlan(); });
$("revert").addEventListener("click", () => selectPreset(current.id));
$("save").addEventListener("click", () => saveToPreset().catch((e) => alert(e.message)));
$("saveas").addEventListener("click", openSaveAs);

$("ref").innerHTML = FILTERS.map(([n, what, ex], i) =>
  `<tr><td>${n}</td><td>${esc(what)}<br><code>${esc(ex)}</code></td><td><button class="b icon" data-add="${i}">Add</button></td></tr>`).join("") +
  `<tr><td></td><td class="muted">Filters that change timing (atempo, silenceremove) would put the sound out of step with the picture. The full list is in the <a href="https://ffmpeg.org/ffmpeg-filters.html#Audio-Filters" target="_blank" rel="noopener">ffmpeg filter documentation</a>.</td><td></td></tr>`;
$("ref").addEventListener("click", (e) => {
  const b = e.target.closest("[data-add]");
  if (!b) return;
  Desk.insert(FILTERS[+b.dataset.add][2]);
});

// ---------------------------------------------------------------- name, folder, plan

function defaultName() {
  return files.length ? files[0].name.replace(/\.[^.]+$/, "") + " (kintsugi)" : "";
}
$("outname").addEventListener("input", () => { nameEdited = $("outname").value.trim() !== ""; schedulePlan(); });

function showDir() {
  $("outdir").textContent = outdir || "Same folder as the first recording";
  $("resetdir").classList.toggle("hidden", !outdir);
}
function setOutdir(path) { outdir = path; showDir(); savePrefs(); schedulePlan(); }
$("choosedir").addEventListener("click", async () => {
  const start = outdir || (files[0] && files[0].dir);
  if (NATIVE) return native({ cmd: "choose-folder", start });
  const r = await api("/api/choose-folder", { start });
  if (r.path) setOutdir(r.path);
});
$("resetdir").addEventListener("click", () => { outdir = ""; showDir(); savePrefs(); schedulePlan(); });
$("video").addEventListener("change", () => { savePrefs(); schedulePlan(); });

function opts() {
  return { paths: files.map((f) => f.path), filters: $("filters").value, bitrate: $("bitrate").value,
           video: $("video").value, outname: $("outname").value, outdir };
}

function refreshStartLabel() {
  const af = $("filters").value.split("\n").some((l) => l.split("#")[0].trim());
  $("start").textContent = files.length > 1 ? (af ? "Join and mend" : "Join") : "Mend";
}

function schedulePlan() {
  clearTimeout(planTimer);
  planTimer = setTimeout(runPlan, 350);
  refreshStartLabel();
}

async function runPlan() {
  const seq = ++planSeq;
  if (!files.length) {
    lastPlan = null;
    $("cmd").textContent = "Add recordings to see the command.";
    $("notes").innerHTML = "";
    $("chainstate").textContent = "";
    $("copycmd").disabled = true;
    updateStart();
    return;
  }
  try {
    const p = await api("/api/plan", opts());
    if (seq !== planSeq) return;
    lastPlan = p;
    $("cmd").textContent = p.shown;
    $("notes").innerHTML = p.notes.map((n) => `<li>${esc(n)}</li>`).join("") +
      `<li>Saves as ${esc(p.out.split("/").pop())}${p.out.split("/").pop() !== $("outname").value + p.out.slice(p.out.lastIndexOf(".")) ? " (that name was taken)" : ""}.</li>`;
    $("chainstate").className = p.chain_error ? "chainbad" : "chainok";
    $("chainstate").textContent = p.chain_error ? "✗ " + p.chain_error : ($("filters").value.trim() ? "✓ ffmpeg accepts this chain" : "No filters: the sound is left as it is");
    $("copycmd").disabled = false;
  } catch (e) {
    if (seq !== planSeq) return;
    lastPlan = null;
    $("cmd").textContent = e.message;
    $("notes").innerHTML = "";
    $("copycmd").disabled = true;
  }
  updateStart();
}

$("copycmd").addEventListener("click", async () => {
  if (!lastPlan) return;
  await copyText(lastPlan.shown);
  $("copycmd").textContent = "Copied";
  setTimeout(() => { $("copycmd").textContent = "Copy command"; }, 1400);
});

// ---------------------------------------------------------------- preview

async function makePreview(auto) {
  if (previewBusy) { if (auto) previewTimer = setTimeout(() => makePreview(true), 400); return; }
  previewBusy = true;
  const a = $("pvaudio");
  const keep = auto === true && { side: $("pvbefore").getAttribute("aria-pressed") === "true" ? "before" : "after",
                                  t: a.currentTime, playing: !a.paused };
  $("pvmake").disabled = true;
  $("pvmake").textContent = auto === true ? "Updating…" : "Making…";
  if (auto !== true) $("pvinfo").textContent = "";
  try {
    preview = await api("/api/preview", { paths: files.map((f) => f.path), filters: $("filters").value,
                                         at: parseTime($("pvat").value), length: +$("pvlen").value || 20 });
    $("ab").classList.remove("hidden");
    $("pvplay").classList.remove("hidden");
    const l = (x) => x.lufs == null ? "?" : x.lufs.toFixed(1);
    $("pvinfo").textContent = `${preview.file}, ${hms(preview.at)} to ${hms(preview.at + preview.length)}. ` +
      `Loudness ${l(preview.before)} → ${l(preview.after)} LUFS; loudness range ${preview.before.lra ?? "?"} → ${preview.after.lra ?? "?"} LU.`;
    if (keep) setSide(keep.side, keep.playing, keep.t);
    else setSide("after", true);
    savePrefs();
  } catch (e) {
    $("pvinfo").textContent = e.message;
  }
  previewBusy = false;
  $("pvmake").disabled = !files.length;
  $("pvmake").textContent = "Make preview";
}

function setSide(which, play, at) {
  const a = $("pvaudio");
  const t = at ?? a.currentTime, wasPlaying = !a.paused;
  $("pvbefore").setAttribute("aria-pressed", which === "before");
  $("pvafter").setAttribute("aria-pressed", which === "after");
  a.src = `/api/preview-audio?which=${which}&token=${preview.token}`;
  a.addEventListener("loadedmetadata", () => {
    if (!play || at !== undefined) a.currentTime = Math.min(t, a.duration || t);
    if (play || wasPlaying) a.play();
  }, { once: true });
}
$("pvmake").addEventListener("click", () => makePreview());
$("pvbefore").addEventListener("click", () => setSide("before"));
$("pvafter").addEventListener("click", () => setSide("after"));
$("pvplay").addEventListener("click", () => { const a = $("pvaudio"); if (a.paused) a.play(); else a.pause(); });
$("pvaudio").addEventListener("play", () => { $("pvplay").textContent = "❚❚ Pause"; });
$("pvaudio").addEventListener("pause", () => { $("pvplay").textContent = "▶︎ Play"; });

// ---------------------------------------------------------------- the job

function updateStart() {
  const running = lastJob && lastJob.state === "running";
  $("start").disabled = !lastPlan || !!lastPlan.chain_error || running;
  $("cancel").classList.toggle("hidden", !running);
  $("startnote").textContent = lastPlan && !running ? `${hms(lastPlan.total)} of recording` +
    (lastPlan.reencode ? "; re-encoding the video takes a while" : "") : "";
}

// ---------------------------------------------------------------- watching: loudness lanes

let lanes = { before: [], after: [], gen: -1 };
let laneJob = null;

function drawLanes(j) {
  const c = $("lanes");
  const w = c.clientWidth, h = c.clientHeight, dpr = window.devicePixelRatio || 1;
  if (!w) return;
  c.width = w * dpr;
  c.height = h * dpr;
  const g = c.getContext("2d");
  g.scale(dpr, dpr);
  const total = j.total || 1, top = -6, bottom = -60;
  const x = (t) => (t / total) * w;
  const y = (v) => h - ((Math.max(bottom, Math.min(top, v)) - bottom) / (top - bottom)) * h;
  g.font = "10px ui-monospace, Menlo, monospace";
  g.fillStyle = "#6f7480";
  for (const v of [-50, -40, -30, -20, -10]) {
    g.fillRect(0, y(v), w, 1 / dpr);
    g.fillText(v + "", 4, y(v) - 3);
  }
  const area = (vals, fill, stroke) => {
    if (!vals.length) return;
    g.beginPath();
    g.moveTo(x(0), h);
    vals.forEach((v, t) => g.lineTo(x(t + 0.5), y(v)));
    g.lineTo(x(vals.length), h);
    g.closePath();
    g.fillStyle = fill;
    g.fill();
    if (stroke) {
      g.beginPath();
      vals.forEach((v, t) => (t ? g.lineTo : g.moveTo).call(g, x(t + 0.5), y(v)));
      g.strokeStyle = stroke;
      g.lineWidth = 1.2;
      g.stroke();
    }
  };
  area(lanes.after, "rgba(217,170,63,.22)");
  area(lanes.before, "rgba(111,116,128,.7)");
  area(lanes.after, "rgba(0,0,0,0)", "#d9aa3f");
  if (j.target != null) {
    g.setLineDash([5, 4]);
    g.strokeStyle = "#e9eaee";
    g.lineWidth = 1;
    g.beginPath();
    g.moveTo(0, y(j.target));
    g.lineTo(w, y(j.target));
    g.stroke();
    g.setLineDash([]);
    g.fillStyle = "#e9eaee";
    g.fillText(`${j.target} LUFS`, w - 62, y(j.target) - 4);
  }
  if (j.state === "running" && j.stage === "Writing") {
    g.fillStyle = "#e9eaee";
    g.fillRect(x(j.done), 0, 1.5, h);
  }
}

function showSteps(j) {
  const order = ["Measuring", "Levelling", "Writing", "Checking"];
  const now = j.state === "done" ? order.length : order.indexOf(j.stage);
  $("steps").innerHTML = order.map((s, i) =>
    `<li class="${i < now ? "done" : i === now ? "now" : ""}">${s}${s === "Levelling" && j.passes > 3 ? ` (${j.passes - 2} passes)` : ""}</li>`).join("");
}

async function start() {
  if ($("start").disabled) return;
  $("joberr").textContent = "";
  $("result").classList.add("hidden");
  try {
    await api("/api/run", opts());
    lastJob = { state: "running" };
    lanes = { before: [], after: [], gen: -1 };
    $("progress").classList.remove("hidden");
    $("barfill").style.width = "0";
    updateStart();
    poll();
  } catch (e) { $("joberr").textContent = e.message; }
}
$("start").addEventListener("click", start);
$("cancel").addEventListener("click", () => api("/api/cancel", {}));

function showJob(j) {
  lastJob = j;
  if (!j) return;
  if (j.lanes) {
    if (laneJob !== j.out) {   // another job: start the lanes again from its first reading
      lanes = { before: [], after: [], gen: -1 };
      laneJob = j.out;
    } else {
      lanes.before = lanes.before.concat(j.lanes.before);
      lanes.after = j.gen !== lanes.gen ? j.lanes.after : lanes.after.concat(j.lanes.after);
      lanes.gen = j.gen;
    }
  }
  const watching = j.stage != null || (j.state === "done" && lanes.before.length > 0);
  $("watch").classList.toggle("hidden", !watching);
  if (watching) { showSteps(j); drawLanes(j); }
  $("barfill").style.width = (j.fraction * 100).toFixed(1) + "%";
  if (j.state === "running") {
    $("progress").classList.remove("hidden");
    $("state").textContent = (j.stage ? `${j.stage} · ` : "") + (j.stage === "Checking" ? "" : `${hms(j.done)} of ${hms(j.total)}`) + (j.speed ? ` · ${Math.round(j.speed)}× real time` : "") +
      (j.left != null ? ` · about ${j.left < 60 ? Math.max(1, Math.round(j.left)) + " s" : Math.round(j.left / 60) + " min"} left` : "");
  } else {
    $("progress").classList.add("hidden");
    if (j.state === "done") {
      $("result").classList.remove("hidden");
      $("donename").textContent = "Saved " + j.out.split("/").pop();
      $("donemeta").textContent = `· ${size(j.size)} · took ${hms(j.elapsed)}` +
        (j.result && j.result.lufs != null ? ` · loudness ${j.measured != null ? j.measured.toFixed(1) + " → " : ""}${j.result.lufs.toFixed(1)} LUFS, peaks ${j.result.peak != null ? j.result.peak.toFixed(1) : "?"} dB` : "");
      $("m-reveal").disabled = false;
    } else {
      $("joberr").textContent = j.state === "cancelled" ? "Cancelled. Nothing was saved." : j.error;
    }
    schedulePlan();   // the next run needs a fresh name
  }
  updateStart();
}

let hadFfmpeg = true;
function applySetup(s) {
  const ok = !!s.ffmpeg;
  document.body.classList.toggle("noffmpeg", !ok);
  $("setup").classList.toggle("hidden", ok);
  $("setup-brew").classList.toggle("hidden", !s.brew);
  $("setup-nobrew").classList.toggle("hidden", s.brew);
  document.querySelectorAll(".brewcmd").forEach((p) => { p.textContent = `${s.brew_path} install ffmpeg`; });
  if (ok && !hadFfmpeg) { $("fileerr").textContent = ""; schedulePlan(); }
  hadFfmpeg = ok;
}
$("installff").addEventListener("click", () => api("/api/install-ffmpeg", {}).catch((e) => alert(e.message)));
document.querySelectorAll("[data-copy]").forEach((b) => b.addEventListener("click", async () => {
  await copyText(document.querySelector(b.dataset.copy).textContent);
  const t = b.textContent;
  b.textContent = "Copied";
  setTimeout(() => { b.textContent = t; }, 1400);
}));

let polling = false;
async function poll() {
  if (polling) return;
  polling = true;
  try {
    while (true) {
      const s = await api(`/api/status?page=1&lb=${lanes.before.length}&la=${lanes.after.length}&gen=${lanes.gen}`);
      applySetup(s);
      showJob(s.job);
      if (s.queued) {
        const r = await api("/api/take-queue", {});
        addFiles(r.files, r.errors);
      }
      await new Promise((r) => setTimeout(r, s.job && s.job.state === "running" ? 1000 : 3000));
    }
  } catch {
    $("joberr").textContent = "Video Kintsugi has stopped. Open it again from Applications.";
  } finally { polling = false; }
}

$("reveal").addEventListener("click", () => api("/api/open", { reveal: true }));
$("openout").addEventListener("click", () => api("/api/open", {}));

// ---------------------------------------------------------------- menus and keys

const menus = [...document.querySelectorAll(".menubar .top")];
function closeMenus() {
  menus.forEach((m) => { m.setAttribute("aria-expanded", "false"); $("menu-" + m.dataset.menu).classList.add("hidden"); });
}
menus.forEach((m) => {
  m.addEventListener("click", (e) => {
    e.stopPropagation();
    const open = m.getAttribute("aria-expanded") === "true";
    closeMenus();
    if (!open) { m.setAttribute("aria-expanded", "true"); $("menu-" + m.dataset.menu).classList.remove("hidden"); }
  });
  m.addEventListener("mouseenter", () => { if (menus.some((x) => x.getAttribute("aria-expanded") === "true") && m.getAttribute("aria-expanded") !== "true") m.click(); });
});
document.addEventListener("click", closeMenus);
document.querySelectorAll(".menu").forEach((menu) => menu.addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b || b.disabled) return;
  closeMenus();
  if (b.dataset.preset) {
    $("preset").value = b.dataset.preset;
    $("preset").dispatchEvent(new Event("change"));
    return;
  }
  ({
    add: chooseFiles,
    clear: () => { files = []; nameEdited = false; renderFiles(); },
    start,
    reveal: () => api("/api/open", { reveal: true }),
    quit: () => confirmBox("Quit Video Kintsugi?", lastJob && lastJob.state === "running" ? "The job that is running will be cancelled." : "Open it again from Applications.",
                           async () => { await api("/api/quit", {}); document.body.innerHTML = "<main><p class='muted'>Video Kintsugi has stopped.</p></main>"; }),
    save: () => saveToPreset().catch((err) => alert(err.message)),
    saveas: openSaveAs,
    delete: deletePreset,
    restore: restoreBuiltins,
    howto: () => $("dlg-howto").showModal(),
    about: () => $("dlg-about").showModal(),
  })[b.dataset.act]?.();
}));
document.querySelectorAll("dialog [data-close]").forEach((b) => b.addEventListener("click", () => b.closest("dialog").close()));

document.addEventListener("keydown", (e) => {
  if (!e.metaKey) return;
  if (e.key === "o") { e.preventDefault(); chooseFiles(); }
  if (e.key === "Enter") { e.preventDefault(); start(); }
});

// ---------------------------------------------------------------- start up

(async () => {
  const s = await api("/api/status?page=1");
  $("ver").textContent = s.version;
  $("aboutver").textContent = s.version;
  applySetup(s);
  const r = await api("/api/presets");
  presets = r.presets;
  builtin = r.builtin;
  $("video").value = prefs.video || "auto";
  $("pvlen").value = prefs.pvlen || 20;
  $("pvauto").checked = prefs.pvauto !== false;
  outdir = prefs.outdir || "";
  showDir();
  Desk.init($("filters"), $("desk"), $("addfilter"), chainChanged);
  selectPreset(prefs.preset);
  setView(prefs.view || "desk");
  renderFiles();
  showJob(s.job);
  poll();
  if (NATIVE) native({ cmd: "ready" });
})();

// what the Mac app's menus call
window.kintsugi = {
  addPaths, setOutdir, start, about: () => $("dlg-about").showModal(), howto: () => $("dlg-howto").showModal(),
  clear: () => { files = []; nameEdited = false; renderFiles(); },
  cancel: () => api("/api/cancel", {}),
  reveal: () => { if (lastJob && lastJob.state === "done") api("/api/open", { reveal: true }); },
  view: (v) => setView(v),
  save: () => saveToPreset().catch((err) => alert(err.message)),
  saveAs: openSaveAs, deletePreset, restore: restoreBuiltins,
  jobRunning: () => !!(lastJob && lastJob.state === "running"),
  dragOver: (on) => $("drop").classList.toggle("over", on),
};
