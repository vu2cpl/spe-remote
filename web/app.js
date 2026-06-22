// SPE Remote Control - WebSocket Client
(function () {
  "use strict";

  // --- WebSocket ---
  let ws = null;
  let reconnectDelay = 1000;
  const MAX_RECONNECT = 16000;

  function connect() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(`${proto}//${location.host}/ws`);

    ws.onopen = () => {
      reconnectDelay = 1000;
      setConnected(true);
      // Pull the current radio config so the settings panel reflects it.
      ws.send("get_config");
    };

    ws.onclose = () => {
      setConnected(false);
      setTimeout(connect, reconnectDelay);
      reconnectDelay = Math.min(reconnectDelay * 2, MAX_RECONNECT);
    };

    ws.onerror = () => ws.close();

    ws.onmessage = (evt) => {
      try {
        const d = JSON.parse(evt.data);
        if (d.power_result) {
          handlePowerResult(d);
        } else if (d.config_event === "radio") {
          handleRadioConfig(d.radio);
        } else if (d.tune_event) {
          handleTuneEvent(d);
        } else if (d.heartbeat) {
          // presence heartbeat — no UI surface yet
        } else if (d.op_status) {
          updateUI(d);
        }
      } catch (e) {
        console.error("Parse error:", e);
      }
    };
  }

  function setConnected(ok) {
    document.getElementById("statusDot").classList.toggle("connected", ok);
    document.getElementById("statusText").textContent = ok ? "Connected" : "Disconnected";
  }

  // Expose for inline onclick
  window.sendCmd = function (cmd) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(cmd);
    }
  };

  // Power on/off with confirmation and busy state
  window.sendPower = function (cmd) {
    const label = cmd === "power_on" ? "POWER ON" : "POWER OFF";
    if (!confirm(`Are you sure you want to ${label} the amplifier?`)) return;

    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(cmd);
      // Set busy state on both buttons
      document.getElementById("btnPowerOn").classList.add("busy");
      document.getElementById("btnPowerOff").classList.add("busy");
      // Auto-clear busy after 5s safety timeout
      setTimeout(clearPowerBusy, 5000);
    }
  };

  function clearPowerBusy() {
    document.getElementById("btnPowerOn").classList.remove("busy");
    document.getElementById("btnPowerOff").classList.remove("busy");
  }

  // Live °C/°F toggle. Sends a "set_temp_unit:F" or "set_temp_unit:C"
  // text command; the server flips the in-memory unit, persists it back
  // to config.yaml, and rebroadcasts state to all clients.
  document.addEventListener("DOMContentLoaded", () => {
    const toggle = document.getElementById("unitToggle");
    if (!toggle) return;
    toggle.addEventListener("click", () => {
      const next = toggle.dataset.next || "F";
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(`set_temp_unit:${next}`);
      }
    });
  });

  function handlePowerResult(d) {
    clearPowerBusy();
    const alertBar = document.getElementById("alertBar");
    const action = d.power_result === "power_on" ? "POWER ON" : "POWER OFF";
    if (d.status === "ok") {
      alertBar.className = "alert-bar power-ok";
      alertBar.textContent = `${action} command sent successfully`;
    } else {
      alertBar.className = "alert-bar error";
      alertBar.textContent = `${action} failed — check serial connection`;
    }
    // Clear the alert after 4 seconds
    setTimeout(() => {
      alertBar.className = "alert-bar clear";
      alertBar.textContent = "";
    }, 4000);
  }

  // --- Model + power-level scaling ---
  // MODELS holds the per-model bits that don't depend on the selected
  // power level: display name and whether to surface the 2K-FA extra
  // temp readings (lower heatsink + combiner).
  const MODELS = {
    "13K": { name: "SPE 1.3K-FA", extraTemps: false },
    "15K": { name: "SPE 1.5K-FA", extraTemps: false },
    "20K": { name: "SPE 2K-FA",   extraTemps: true  },
  };
  const MODEL_DEFAULT = { name: "SPE Expert", extraTemps: false };

  // Power-bar full-scale + tick labels per (model, p_level). p_level is
  // L/M/H from the amp; values come from the SPE manuals for each model.
  const LEVELS = {
    "13K": {
      L: { maxW: 500,  ticks: ["0","100","200","300","400","450","500"] },
      M: { maxW: 800,  ticks: ["0","200","400","500","600","700","800"] },
      H: { maxW: 1300, ticks: ["0","250","500","750","1.0k","1.2k","1.3k"] },
    },
    "15K": {
      L: { maxW: 500,  ticks: ["0","100","200","300","400","450","500"] },
      M: { maxW: 1000, ticks: ["0","200","400","500","600","800","1.0k"] },
      H: { maxW: 1500, ticks: ["0","250","500","750","1.0k","1.3k","1.5k"] },
    },
    "20K": {
      L: { maxW: 700,  ticks: ["0","150","300","400","500","600","700"] },
      M: { maxW: 1400, ticks: ["0","250","500","750","1.0k","1.2k","1.4k"] },
      H: { maxW: 2000, ticks: ["0","400","800","1.0k","1.3k","1.6k","2.0k"] },
    },
  };
  // Fallback scale when neither model nor level has been heard from yet.
  const SCALE_DEFAULT = { maxW: 1500, ticks: ["0","250","500","750","1.0k","1.3k","1.5k"] };

  let currentModelKey = "";
  let currentLevelKey = "";
  let currentModel = MODEL_DEFAULT;
  let currentScale = SCALE_DEFAULT;

  function applyModel(modelCode) {
    if (modelCode === currentModelKey) return;
    currentModelKey = modelCode;
    currentModel = MODELS[modelCode] || MODEL_DEFAULT;

    document.getElementById("modelLabel").textContent = currentModel.name;

    const extraEl = document.getElementById("extraTemps");
    if (extraEl) extraEl.style.display = currentModel.extraTemps ? "" : "none";

    // Scale depends on (model, level); reapply with the current level
    // because the model just changed underneath it.
    applyScale(currentLevelKey, true);
  }

  // Pick the power-bar scale for the current (model, level). p_level is
  // L/M/H once parsed; the dataclass default of "0" means we haven't seen
  // a real frame yet, so fall back to H for the current model. forceRebuild
  // is used by applyModel to redraw ticks when only the model changed.
  function applyScale(levelCode, forceRebuild) {
    const sameLevel = levelCode === currentLevelKey;
    currentLevelKey = levelCode || "";
    const levelTable = LEVELS[currentModelKey];
    const next = (levelTable && levelTable[currentLevelKey])
              || (levelTable && levelTable.H)
              || SCALE_DEFAULT;
    if (next === currentScale && sameLevel && !forceRebuild) return;
    currentScale = next;

    const ticksEl = document.getElementById("powerTicks");
    if (ticksEl) {
      ticksEl.innerHTML = currentScale.ticks.map(t => `<span>${t}</span>`).join("");
    }
  }

  // --- UI Update ---
  function updateUI(d) {
    // Model auto-detection (header + extra temps); scale is applied
    // separately so it can react to the user changing the L/M/H level
    // on the amp's front panel without the model itself changing.
    if (d.model_id !== undefined) applyModel(d.model_id || "");
    if (d.p_level !== undefined) applyScale(d.p_level || "");

    // Power
    const pOut = parseInt(d.p_out, 10) || 0;
    document.getElementById("powerValue").textContent = pOut;
    const pct = Math.min((pOut / currentScale.maxW) * 100, 100);
    document.getElementById("powerBar").style.width = pct + "%";

    // SWR
    const swr = parseFloat(d.swr) || 1;
    const swrDisplay = swr > 0 ? `SWR: 1:${d.swr}` : "SWR: ---";
    document.getElementById("swrDisplay").textContent = swrDisplay;
    document.getElementById("valSWR").textContent = `1:${d.swr}`;

    // Drain, temp, voltage. The server stamps temperature_unit ("C"/"F")
    // onto every state — the amp itself doesn't tell us which unit it
    // reports in, so this comes from config.yaml on the Pi (or from the
    // runtime UI toggle, which writes back to config.yaml).
    const unit = d.temperature_unit === "F" ? "F" : "C";
    document.getElementById("valDrain").textContent = d.drain;
    document.getElementById("valTemp").innerHTML = `${d.pa_temp}&deg;${unit}`;
    document.getElementById("valVolt").textContent = d.voltage;

    // Update the °C/°F toggle label so it shows the *other* unit you'd
    // switch to. Stash the current unit on the element so the click
    // handler knows what to flip to.
    const toggle = document.getElementById("unitToggle");
    if (toggle) {
      const other = unit === "C" ? "F" : "C";
      toggle.textContent = `→ °${other}`;
      toggle.dataset.next = other;
    }

    // 2K-FA extra temps (only rendered if applyModel revealed the row)
    if (currentModel.extraTemps) {
      const lwr = document.getElementById("valTempLower");
      const cmb = document.getElementById("valTempCombiner");
      if (lwr && d.pa_temp_lower !== undefined) lwr.textContent = d.pa_temp_lower;
      if (cmb && d.pa_temp_combiner !== undefined) cmb.textContent = d.pa_temp_combiner;
      // Update the °C / °F suffix on both extra-temp chips.
      document.querySelectorAll(".tempUnitSuffix").forEach(el => el.textContent = unit);
    }

    // Info chips
    const txEl = document.getElementById("txStatus");
    const chipTX = document.getElementById("chipTX");
    txEl.textContent = d.tx_status;
    chipTX.classList.toggle("tx-on", d.tx_status === "TX");
    chipTX.classList.toggle("rx-on", d.tx_status === "RX");

    document.getElementById("bandDisplay").textContent = d.band;
    document.getElementById("antDisplay").textContent = d.tx_antenna;
    document.getElementById("inputDisplay").textContent = d.input;
    document.getElementById("levelDisplay").textContent = d.p_level;

    // Operate button highlight
    document.getElementById("btnOper").classList.toggle("active", d.op_status === "Oper");

    // Alerts
    const alertBar = document.getElementById("alertBar");
    if (d.error && d.error.trim()) {
      alertBar.className = "alert-bar error";
      alertBar.textContent = d.error;
    } else if (d.warnings && d.warnings.trim()) {
      alertBar.className = "alert-bar warning";
      alertBar.textContent = d.warnings;
    } else {
      alertBar.className = "alert-bar clear";
      alertBar.textContent = "";
    }

    // Draw gauges
    drawGauge("gaugeSWR", parseFloat(d.swr) || 1, 1, 3.5, swrColors);
    drawGauge("gaugeDrain", parseFloat(d.drain) || 0, 0, 60, drainColors);
    // Scale the temperature gauge by unit. 80°C ≈ 176°F so the Fahrenheit
    // dial gets a 0–180 range; warning thresholds in tempColors are
    // proportional, so they convert automatically.
    const tempMax = unit === "F" ? 180 : 80;
    drawGauge("gaugeTemp", parseFloat(d.pa_temp) || 0, 0, tempMax, tempColors);
    drawGauge("gaugeVolt", parseFloat(d.voltage) || 0, 40, 60, voltColors);
  }

  // --- Gauge Drawing ---
  const swrColors = [
    [0, "#4caf50"], [0.4, "#4caf50"], [0.6, "#ff9800"], [0.8, "#f44336"], [1, "#f44336"]
  ];
  const drainColors = [
    [0, "#00bcd4"], [0.7, "#00bcd4"], [0.85, "#ff9800"], [1, "#f44336"]
  ];
  const tempColors = [
    [0, "#00bcd4"], [0.5, "#4caf50"], [0.75, "#ff9800"], [1, "#f44336"]
  ];
  const voltColors = [
    [0, "#f44336"], [0.2, "#ff9800"], [0.3, "#4caf50"], [0.7, "#4caf50"], [0.85, "#ff9800"], [1, "#f44336"]
  ];

  function drawGauge(canvasId, value, min, max, colorStops) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const w = canvas.width;
    const h = canvas.height;
    const cx = w / 2;
    const cy = h - 5;
    const r = Math.min(cx, cy) - 8;

    ctx.clearRect(0, 0, w, h);

    const startAngle = Math.PI;
    const endAngle = 2 * Math.PI;
    const range = max - min;
    const norm = Math.max(0, Math.min(1, (value - min) / range));
    const valueAngle = startAngle + norm * Math.PI;

    // Background arc
    ctx.beginPath();
    ctx.arc(cx, cy, r, startAngle, endAngle);
    ctx.lineWidth = 10;
    ctx.strokeStyle = "#1a1a2e";
    ctx.stroke();

    // Colored arc
    const grad = ctx.createLinearGradient(cx - r, cy, cx + r, cy);
    for (const [stop, color] of colorStops) {
      grad.addColorStop(stop, color);
    }
    ctx.beginPath();
    ctx.arc(cx, cy, r, startAngle, valueAngle);
    ctx.lineWidth = 10;
    ctx.lineCap = "round";
    ctx.strokeStyle = grad;
    ctx.stroke();

    // Needle
    const nx = cx + (r - 5) * Math.cos(valueAngle);
    const ny = cy + (r - 5) * Math.sin(valueAngle);
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.lineTo(nx, ny);
    ctx.lineWidth = 2;
    ctx.strokeStyle = "#e0e0e0";
    ctx.stroke();

    // Center dot
    ctx.beginPath();
    ctx.arc(cx, cy, 3, 0, 2 * Math.PI);
    ctx.fillStyle = "#e0e0e0";
    ctx.fill();
  }

  // ─────────────────────────────────────────────────────────────
  //  ATU band sweep — panel state + tune_event handling
  // ─────────────────────────────────────────────────────────────
  //
  // Sends `tune_band:<band>` and `tune_stop` over WS; reacts to the
  // server's `tune_event` JSON broadcasts to drive the panel UI.
  // Phases the server emits (see spe/tune_orchestrator.py PHASES):
  //   STARTED PREFLIGHT_OK VFO_SAVED FREQ_SET TUNE_SENT LED_ON
  //   CARRIER_ON LED_OFF CARRIER_OFF VFO_RESTORED SUCCESS FAIL ABORT
  //   SWEEP_STARTED SWEEP_STEP SWEEP_DONE
  //
  // Terminal phases that release the Start button: SUCCESS FAIL
  // ABORT SWEEP_DONE.

  const SWEEP_BANDS = ["160m", "80m", "60m", "40m", "30m",
                       "20m", "17m", "15m", "12m", "10m", "6m"];
  let selectedBand = "20m";
  let isSweeping = false;

  function renderBandButtons() {
    const wrap = document.getElementById("sweepBands");
    if (!wrap) return;
    wrap.innerHTML = "";
    SWEEP_BANDS.forEach((band) => {
      const b = document.createElement("button");
      b.textContent = band;
      b.dataset.band = band;
      if (band === selectedBand) b.classList.add("selected");
      b.onclick = () => {
        if (isSweeping) return;
        selectedBand = band;
        wrap.querySelectorAll("button").forEach((x) =>
          x.classList.toggle("selected", x.dataset.band === band)
        );
        document.getElementById("btnSweepStart").disabled = !ws ||
          ws.readyState !== WebSocket.OPEN;
      };
      wrap.appendChild(b);
    });
    document.getElementById("btnSweepStart").disabled = !ws ||
      ws.readyState !== WebSocket.OPEN;
  }

  // Radio connection is on-demand: opening the Sweep menu pre-warms it,
  // closing it (while idle) drops it. The server also connects lazily at
  // tune start and disconnects when the cycle is over, so this is purely
  // a head-start, not a hard requirement.
  function radioConnect() {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send("radio_connect");
  }
  function radioDisconnect() {
    // Never drop the radio mid-sweep; the server ignores this while a
    // cycle is running, but guard here too to avoid the needless message.
    if (!isSweeping && ws && ws.readyState === WebSocket.OPEN) {
      ws.send("radio_disconnect");
    }
  }

  // ---- Radio settings panel (client-selected radio) -----------------
  // The server sends {config_event:"radio", radio:{kind, flex:{}, tci:{}}}
  // on connect and after any change; we mirror it into the form. Applying
  // sends set_radio_config:<json> which the server persists + applies live.
  function val(id) { const el = document.getElementById(id); return el ? el.value : ""; }
  function setVal(id, v) { const el = document.getElementById(id); if (el && v !== undefined && v !== null) el.value = v; }

  function showRadioFields(kind) {
    const flex = document.getElementById("flexFields");
    const tci = document.getElementById("tciFields");
    if (flex) flex.hidden = kind !== "flex";
    if (tci) tci.hidden = kind !== "tci";
  }

  function handleRadioConfig(radio) {
    if (!radio) return;
    setVal("radioKind", radio.kind);
    const f = radio.flex || {}, t = radio.tci || {};
    setVal("flexHost", f.host); setVal("flexPort", f.port);
    setVal("flexSlice", f.slice_rx); setVal("flexPower", f.tune_power_watts);
    setVal("tciHost", t.host); setVal("tciPort", t.port);
    setVal("tciTrx", t.trx); setVal("tciMode", t.mode); setVal("tciDrive", t.tune_drive);
    showRadioFields(radio.kind);
  }

  window.onRadioKindChange = function () { showRadioFields(val("radioKind")); };

  window.toggleRadioPanel = function () {
    const panel = document.getElementById("radioPanel");
    if (!panel) return;
    panel.hidden = !panel.hidden;
    if (!panel.hidden && ws && ws.readyState === WebSocket.OPEN) ws.send("get_config");
  };

  window.applyRadioConfig = function () {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    const kind = val("radioKind");
    const payload = { kind };
    if (kind === "flex") {
      payload.flex = {
        host: val("flexHost"), port: Number(val("flexPort")) || 4992,
        slice_rx: Number(val("flexSlice")) || 0,
        tune_power_watts: Number(val("flexPower")) || 10,
      };
    } else if (kind === "tci") {
      payload.tci = {
        host: val("tciHost"), port: Number(val("tciPort")) || 50001,
        trx: Number(val("tciTrx")) || 0, mode: val("tciMode") || "CW",
        tune_drive: Number(val("tciDrive")) || 0,
      };
    }
    ws.send("set_radio_config:" + JSON.stringify(payload));
  };

  window.toggleSweepPanel = function () {
    const panel = document.getElementById("sweepPanel");
    if (!panel) return;
    panel.hidden = !panel.hidden;
    if (!panel.hidden) {
      renderBandButtons();
      radioConnect();
    } else {
      radioDisconnect();
    }
  };

  window.startSelectedSweep = function () {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    if (isSweeping) return;
    if (!confirm(
      `Start ATU sweep on ${selectedBand}? ` +
      `Amp must be in STBY and the antenna for ${selectedBand} selected.`
    )) return;
    isSweeping = true;
    setSweepUI({ phase: "STARTED", message: `${selectedBand} requested` });
    ws.send(`tune_band:${selectedBand}`);
  };

  function setSweepUI(state) {
    const wrap = document.getElementById("sweepStatus");
    const phaseEl = document.getElementById("sweepPhase");
    const msgEl = document.getElementById("sweepMessage");
    const startBtn = document.getElementById("btnSweepStart");
    const stopBtn = document.getElementById("btnSweepStop");
    if (!wrap) return;

    const phase = state.phase || "";
    phaseEl.textContent = phase.replace(/_/g, " ") || "Ready";
    msgEl.textContent = state.message || "";

    const terminal = ["SUCCESS", "FAIL", "ABORT", "SWEEP_DONE"]
      .includes(phase);
    const failure = phase === "FAIL" || phase === "ABORT";
    const success = phase === "SUCCESS" || phase === "SWEEP_DONE";

    wrap.classList.remove("running", "success", "fail");
    if (terminal) {
      if (failure) wrap.classList.add("fail");
      else if (success) wrap.classList.add("success");
      isSweeping = false;
    } else if (phase) {
      wrap.classList.add("running");
    }

    startBtn.disabled = isSweeping || !ws || ws.readyState !== WebSocket.OPEN;
    stopBtn.disabled = !isSweeping;

    // Disable band buttons while running
    document.querySelectorAll("#sweepBands button").forEach((b) => {
      b.disabled = isSweeping;
    });
  }

  function handleTuneEvent(d) {
    const phase = d.tune_event;
    const message = d.tune_message || "";

    // Radio connection-lifecycle events. Show pre-tune feedback
    // (connecting / connected / error) but never flip sweeping state or
    // clobber a finished sweep's terminal status with the housekeeping
    // RADIO_DISCONNECTED that follows it. RADIO_CONFIG_UPDATED is handled
    // via the config_event message, so ignore it here. (FLEX_ kept for
    // tolerance with an older server.)
    if (phase && (phase.indexOf("RADIO_") === 0 || phase.indexOf("FLEX_") === 0)) {
      if (phase === "RADIO_CONFIG_UPDATED") return;
      if (phase !== "RADIO_DISCONNECTED" && phase !== "FLEX_DISCONNECTED") {
        setSweepUI({ phase, message });
      }
      return;
    }

    if (phase === "STARTED" || phase === "SWEEP_STARTED") {
      isSweeping = true;
    }

    setSweepUI({ phase, message });

    // Progress bar from SWEEP_STEP "N/M: freq MHz" messages
    if (phase === "SWEEP_STEP") {
      const m = message.match(/^(\d+)\s*\/\s*(\d+)/);
      if (m) {
        const pct = (parseInt(m[1], 10) - 1) / parseInt(m[2], 10) * 100;
        const wrap = document.getElementById("sweepProgressWrap");
        const bar = document.getElementById("sweepProgressBar");
        if (wrap && bar) {
          wrap.hidden = false;
          bar.style.width = pct + "%";
        }
      }
    } else if (phase === "SWEEP_DONE") {
      const bar = document.getElementById("sweepProgressBar");
      if (bar) bar.style.width = "100%";
    }
  }

  // Render band buttons once on first page load so reopening the
  // panel is instant.
  document.addEventListener("DOMContentLoaded", renderBandButtons);

  // --- Init ---
  connect();
})();
