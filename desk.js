// The filter chain as a mixing desk: one channel strip per filter line, knobs for its settings and a
// fader for its main one. The text in #filters stays the source of truth; the desk reads it and writes it.
"use strict";

const Desk = (() => {
  const db = (v) => (v > 0 ? (20 * Math.log10(v)).toFixed(1) + " dB" : "−∞");
  const hz = (v) => (v >= 1000 ? +(v / 1000).toFixed(v >= 10000 ? 1 : 2) + "k" : Math.round(v)) + " Hz";
  const ms = (v) => (v >= 1000 ? +(v / 1000).toFixed(2) + " s" : +v.toPrecision(3) + " ms");
  const P = (keys, label, min, max, reset, o = {}) => ({ keys, label, min, max, reset, ...o });

  // Each filter's settings: ffmpeg option names (first is written), range, the value a double-click restores.
  const SPECS = {
    pan: { title: "Mono", what: "Stereo into one channel", params: [] },
    highpass: { title: "Rumble cut", what: "Removes sound below", order: ["f"],
      params: [P(["f", "frequency"], "Below", 20, 500, 90, { log: true, show: hz, fader: true })] },
    lowpass: { title: "Hiss cut", what: "Removes sound above", order: ["f"],
      params: [P(["f", "frequency"], "Above", 2000, 20000, 8000, { log: true, show: hz, fader: true })] },
    afftdn: { title: "Noise reduction", what: "Steady background noise",
      params: [P(["nf", "noise_floor"], "Floor", -80, -20, -50, { unit: " dB" }),
               P(["tn", "track_noise"], "Track", 0, 1, 0, { toggle: true }),
               P(["nr", "noise_reduction"], "Amount", 1, 60, 12, { unit: " dB", fader: true })] },
    anlmdn: { title: "Deep denoise", what: "Strong, slow noise reduction",
      params: [P(["s", "strength"], "Strength", 0.01, 50, 1, { log: true, fader: true })] },
    agate: { title: "Gate", what: "Quietens the gaps",
      params: [P(["ratio"], "Ratio", 1, 20, 2, { log: true, unit: ":1" }),
               P(["attack"], "Attack", 1, 200, 20, { log: true, show: ms }),
               P(["release"], "Release", 10, 2000, 250, { log: true, show: ms }),
               P(["threshold"], "Threshold", 0.001, 0.5, 0.125, { log: true, show: db, fader: true })] },
    equalizer: { title: "EQ band", what: "Lifts or cuts one band",
      params: [P(["f", "frequency"], "Freq", 40, 16000, 1000, { log: true, show: hz }),
               P(["w", "width"], "Width", 0.1, 5, 1, { log: true }),
               P(["g", "gain"], "Gain", -15, 15, 0, { unit: " dB", fader: true, step: 0.5 })] },
    deesser: { title: "De-esser", what: "Softens s sounds",
      params: [P(["f"], "Freq", 0, 1, 0.5), P(["m"], "Max", 0, 1, 0.5),
               P(["i"], "Amount", 0, 1, 0, { fader: true })] },
    acompressor: { title: "Compressor", what: "Holds down peaks",
      params: [P(["ratio"], "Ratio", 1, 20, 2, { log: true, unit: ":1" }),
               P(["attack"], "Attack", 1, 200, 20, { log: true, show: ms }),
               P(["release"], "Release", 10, 2000, 250, { log: true, show: ms }),
               P(["makeup"], "Make-up", 1, 8, 1, { log: true, show: db }),
               P(["threshold"], "Threshold", 0.001, 1, 0.125, { log: true, show: db, fader: true })] },
    dynaudnorm: { title: "Leveller", what: "Evens loud and quiet passages",
      params: [P(["f", "framelen"], "Window", 10, 8000, 500, { log: true, show: ms }),
               P(["g", "gausssize"], "Smooth", 3, 301, 31, { step: 2, odd: true }),
               P(["p", "peak"], "Peak", 0, 1, 0.95, { step: 0.01 }),
               P(["m", "maxgain"], "Max lift", 1, 100, 10, { log: true, unit: "×", fader: true })] },
    speechnorm: { title: "Speech leveller", what: "Levels phrase by phrase",
      params: [P(["p", "peak"], "Peak", 0, 1, 0.95, { step: 0.01 }),
               P(["r", "raise"], "Raise", 0.00001, 0.1, 0.001, { log: true }),
               P(["e", "expansion"], "Expansion", 1, 50, 2, { log: true, unit: "×", fader: true })] },
    adeclick: { title: "De-click", what: "Removes clicks and pops", params: [] },
    adeclip: { title: "De-clip", what: "Repairs distortion", params: [] },
    volume: { title: "Volume", what: "Fixed level change", order: ["volume"],
      params: [P(["volume"], "Level", -20, 20, 0, { unit: " dB", fader: true, step: 0.5,
        read: (s) => /db$/i.test(s) ? parseFloat(s) : 20 * Math.log10(parseFloat(s) || 1),
        write: (v) => v + "dB" })] },
    loudnorm: { title: "Loudness", what: "Overall loudness target",
      params: [P(["TP", "tp"], "Peak", -9, -0.5, -2, { unit: " dB", step: 0.5 }),
               P(["I", "i"], "Target", -36, -10, -14, { unit: " LUFS", fader: true, step: 0.5 })] },
  };
  const ADDABLE = ["highpass", "lowpass", "afftdn", "anlmdn", "agate", "equalizer", "deesser", "acompressor",
                   "dynaudnorm", "speechnorm", "adeclick", "adeclip", "volume", "loudnorm", "pan"];
  const NEW_LINE = {
    pan: "pan=mono|c0=0.5*c0+0.5*c1", highpass: "highpass=f=90", lowpass: "lowpass=f=8000", afftdn: "afftdn=nf=-25",
    anlmdn: "anlmdn=s=7", agate: "agate=threshold=0.02:ratio=3:attack=10:release=250",
    equalizer: "equalizer=f=3000:t=q:w=1:g=3", deesser: "deesser=i=0.4",
    acompressor: "acompressor=threshold=0.1:ratio=3:attack=20:release=250",
    dynaudnorm: "dynaudnorm=f=250:g=15:p=0.9:m=8", speechnorm: "speechnorm=e=6:r=0.0001:l=1",
    adeclick: "adeclick", adeclip: "adeclip", volume: "volume=3dB", loudnorm: "loudnorm=I=-14:TP=-2",
  };

  let ta, root, onChange;
  let lines = [];          // every line of the text, parsed; strips are the ones that are filters

  // ---------------------------------------------------------------- reading and writing lines

  function parse(raw) {
    let body = raw.trim(), on = true;
    const c = body.match(/^#\s*(.*)$/);
    if (c) { on = false; body = c[1].trim(); }
    body = body.replace(/,$/, "");
    const m = body.match(/^([A-Za-z0-9_]+)(?:=(.*))?$/);
    if (!m || (!on && !SPECS[m[1]])) return { raw, filter: false };   // a note, or a blank line
    const name = m[1], spec = SPECS[name];
    const args = (m[2] || "").split(/(?<!\\):/).filter((a) => a !== "").map((a, i) => {
      const eq = a.indexOf("=");
      if (eq > 0 && name !== "pan") return [a.slice(0, eq), a.slice(eq + 1)];
      return [spec && spec.order && spec.order[i] || null, a];
    });
    return { raw, filter: true, on, name, spec, args };
  }

  function serialise(l) {
    const args = l.args.map(([k, v]) => (k ? `${k}=${v}` : v)).join(":");
    return (l.on ? "" : "# ") + l.name + (args ? "=" + args : "");
  }

  function find(l, p) { return l.args.find(([k]) => k && p.keys.includes(k)); }

  function get(l, p) {
    const a = find(l, p);
    if (!a) return p.reset;
    const v = p.read ? p.read(a[1]) : parseFloat(a[1]);
    return isNaN(v) ? p.reset : v;
  }

  function fmtNum(v, p) {
    if (p.toggle) return v ? "1" : "0";
    if (p.write) return p.write(round(v, p));
    return String(round(v, p));
  }

  function round(v, p) {
    if (p.odd) { v = Math.round(v); return v % 2 ? v : v + 1; }
    if (p.step) return +(Math.round(v / p.step) * p.step).toFixed(3);
    const a = Math.abs(v);
    if (a >= 100) return Math.round(v);
    if (a >= 1) return +v.toFixed(1);
    return +v.toPrecision(2);
  }

  function set(l, p, v) {
    v = Math.min(p.max, Math.max(p.min, v));
    const a = find(l, p);
    if (a) a[1] = fmtNum(v, p);
    else l.args.push([p.keys[0], fmtNum(v, p)]);
    l.raw = serialise(l);
    commit();
  }

  function show(v, p) {
    if (p.toggle) return v ? "On" : "Off";
    v = round(v, p);
    return p.show ? p.show(v) : v + (p.unit || "");
  }

  const pos = (v, p) => p.log ? Math.log(v / p.min) / Math.log(p.max / p.min) : (v - p.min) / (p.max - p.min);
  const val = (t, p) => p.log ? p.min * Math.pow(p.max / p.min, t) : p.min + t * (p.max - p.min);

  function commit(rebuild) {
    ta.value = lines.map((l) => l.raw).join("\n");
    onChange();
    if (rebuild) render();
  }

  // ---------------------------------------------------------------- controls

  function knob(l, p) {
    const el = document.createElement("div");
    el.className = "knob";
    el.tabIndex = 0;
    el.title = `${p.label}. Drag up or down (Shift for fine), scroll, or use the arrow keys. Double-click resets.`;
    el.innerHTML = `<div class="kl">${p.label}</div><svg viewBox="0 0 48 48" aria-hidden="true">
      <circle cx="24" cy="24" r="17" class="kbody"/><path class="ktrack"/><path class="karc"/><line x1="24" y1="24" x2="24" y2="11" class="kline"/></svg>
      <div class="kv"></div>`;
    const arc = (t) => {
      const a0 = -135, a = a0 + 270 * t, r = 21;
      const pt = (deg) => [24 + r * Math.sin(deg * Math.PI / 180), 24 - r * Math.cos(deg * Math.PI / 180)];
      const [x0, y0] = pt(a0), [x1, y1] = pt(a);
      return `M${x0.toFixed(2)} ${y0.toFixed(2)} A${r} ${r} 0 ${a - a0 > 180 ? 1 : 0} 1 ${x1.toFixed(2)} ${y1.toFixed(2)}`;
    };
    el.querySelector(".ktrack").setAttribute("d", arc(1));
    const paint = () => {
      const v = get(l, p), t = Math.min(1, Math.max(0, pos(v, p)));
      el.querySelector(".karc").setAttribute("d", arc(Math.max(t, 0.001)));
      el.querySelector(".kline").setAttribute("transform", `rotate(${-135 + 270 * t} 24 24)`);
      el.querySelector(".kv").textContent = show(v, p);
      el.setAttribute("role", "slider");
      el.setAttribute("aria-label", p.label);
      el.setAttribute("aria-valuetext", show(v, p));
    };
    const nudge = (dt) => { set(l, p, val(Math.min(1, Math.max(0, pos(get(l, p), p) + dt)), p)); paint(); };
    el.addEventListener("pointerdown", (e) => {
      el.setPointerCapture(e.pointerId);
      let y = e.clientY;
      const move = (ev) => { nudge((y - ev.clientY) / (ev.shiftKey ? 600 : 150)); y = ev.clientY; };
      const up = () => { el.removeEventListener("pointermove", move); el.removeEventListener("pointerup", up); };
      el.addEventListener("pointermove", move);
      el.addEventListener("pointerup", up);
    });
    el.addEventListener("wheel", (e) => { e.preventDefault(); nudge(-e.deltaY / (e.shiftKey ? 3000 : 800)); }, { passive: false });
    el.addEventListener("dblclick", () => { set(l, p, p.reset); paint(); });
    el.addEventListener("keydown", (e) => {
      const d = { ArrowUp: 1, ArrowRight: 1, ArrowDown: -1, ArrowLeft: -1 }[e.key];
      if (d) { e.preventDefault(); nudge(d * (e.shiftKey ? 0.005 : 0.025)); }
    });
    paint();
    return el;
  }

  function toggle(l, p) {
    const b = document.createElement("button");
    b.className = "tg";
    const paint = () => { b.setAttribute("aria-pressed", get(l, p) ? "true" : "false"); b.innerHTML = `<i></i>${p.label}`; };
    b.addEventListener("click", () => { set(l, p, get(l, p) ? 0 : 1); paint(); });
    b.title = p.label === "Track" ? "Follow background noise that changes over time" : p.label;
    paint();
    return b;
  }

  function fader(l, p) {
    const el = document.createElement("div");
    el.className = "fader";
    el.innerHTML = `<div class="fv"></div><div class="fwell"><input type="range" min="0" max="1000" step="1" aria-label="${p.label}"></div><div class="fl">${p.label}</div>`;
    const input = el.querySelector("input");
    const paint = () => {
      const v = get(l, p);
      input.value = Math.round(Math.min(1, Math.max(0, pos(v, p))) * 1000);
      el.querySelector(".fv").textContent = show(v, p);
    };
    input.addEventListener("input", () => { set(l, p, val(input.value / 1000, p)); el.querySelector(".fv").textContent = show(get(l, p), p); });
    input.addEventListener("dblclick", () => { set(l, p, p.reset); paint(); });
    input.title = "Double-click resets";
    paint();
    return el;
  }

  function strip(l, i, n) {
    const s = document.createElement("div");
    s.className = "strip" + (l.on ? "" : " off");
    const spec = l.spec;
    s.innerHTML = `<div class="sh"><span class="sn">${n}</span><button class="led" aria-pressed="${l.on}" title="${l.on ? "On: click to bypass" : "Bypassed: click to switch on"}"></button></div>
      <div class="st">${spec ? spec.title : l.name}</div><div class="sw">${spec ? spec.what : "ffmpeg filter"}</div><div class="sk"></div>`;
    s.querySelector(".led").addEventListener("click", () => { l.on = !l.on; l.raw = serialise(l); commit(true); });
    const knobs = s.querySelector(".sk");
    if (spec) {
      for (const p of spec.params.filter((p) => !p.fader)) knobs.appendChild(p.toggle ? toggle(l, p) : knob(l, p));
      const f = spec.params.find((p) => p.fader);
      if (f) s.appendChild(fader(l, f));
      else s.appendChild(Object.assign(document.createElement("div"), { className: "nofader", textContent: spec.params.length ? "" : "on / off" }));
    } else {
      const inp = document.createElement("input");
      inp.type = "text";
      inp.className = "rawargs";
      inp.spellcheck = false;
      inp.value = l.args.map(([k, v]) => (k ? `${k}=${v}` : v)).join(":");
      inp.title = "This filter's options, as ffmpeg takes them";
      inp.addEventListener("change", () => { lines[i] = parse((l.on ? "" : "# ") + l.name + (inp.value ? "=" + inp.value : "")); commit(true); });
      knobs.appendChild(inp);
      s.appendChild(Object.assign(document.createElement("div"), { className: "nofader" }));
    }
    const ft = document.createElement("div");
    ft.className = "sf";
    ft.innerHTML = `<button title="Move earlier" data-m="-1">◀</button><button title="Remove" data-x>✕</button><button title="Move later" data-m="1">▶</button>`;
    ft.addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      const idx = lines.filter((x) => x.filter).map((x) => lines.indexOf(x));
      const me = idx.indexOf(i);
      if (b.dataset.x !== undefined) lines.splice(i, 1);
      else {
        const other = idx[me + +b.dataset.m];
        if (other === undefined) return;
        [lines[i], lines[other]] = [lines[other], lines[i]];
      }
      commit(true);
    });
    s.appendChild(ft);
    return s;
  }

  function addMenu(select) {
    select.innerHTML = `<option value="">Add a filter…</option>${ADDABLE.map((n) => `<option value="${n}">${SPECS[n].title}</option>`).join("")}`;
    select.addEventListener("change", () => { if (select.value) insert(NEW_LINE[select.value]); select.value = ""; });
  }

  function insert(line) {
    // before a final loudness stage, so the overall level is still set last
    while (lines.length && !lines[lines.length - 1].raw.trim()) lines.pop();
    const fl = lines.filter((x) => x.filter);
    const last = fl[fl.length - 1];
    const at = last && last.name === "loudnorm" && !line.startsWith("loudnorm") ? lines.indexOf(last) : lines.length;
    lines.splice(at, 0, parse(line));
    commit(true);
  }

  function render() {
    lines = ta.value.split("\n").map(parse);
    root.innerHTML = "";
    const io = (t) => Object.assign(document.createElement("div"), { className: "io", textContent: t });
    root.appendChild(io("IN"));
    let n = 0;
    lines.forEach((l, i) => { if (l.filter) root.appendChild(strip(l, i, ++n)); });
    root.appendChild(io("OUT"));
  }

  return {
    init(textarea, container, select, changed) { ta = textarea; root = container; onChange = changed; addMenu(select); render(); },
    render,
    insert,
    titles: Object.fromEntries(Object.entries(SPECS).map(([k, s]) => [k, s.title])),
  };
})();
