// Settings page: loads current config + secrets state, saves config,
// manages keychain secrets via /api/secrets, probes providers via
// /api/providers/test.

(() => {
  const form = document.getElementById("settings-form");
  const saveStatus = document.getElementById("save-status");
  const secretsList = document.getElementById("secrets-list");

  function setByPath(obj, path, value) {
    const parts = path.split(".");
    let cur = obj;
    for (let i = 0; i < parts.length - 1; i++) {
      cur[parts[i]] = cur[parts[i]] ?? {};
      cur = cur[parts[i]];
    }
    cur[parts[parts.length - 1]] = value;
  }

  function getByPath(obj, path) {
    return path.split(".").reduce((o, k) => (o == null ? undefined : o[k]), obj);
  }

  function elValue(el) {
    if (el.type === "checkbox") return el.checked;
    if (el.type === "number") return Number(el.value);
    return el.value;
  }

  function setElValue(el, v) {
    if (v === undefined || v === null) return;
    if (el.type === "checkbox") el.checked = !!v;
    else el.value = v;
  }

  async function loadConfig() {
    const cfg = await fetch("/api/config").then(r => r.json());
    for (const el of form.elements) {
      if (!el.name) continue;
      setElValue(el, getByPath(cfg, el.name));
    }
    return cfg;
  }

  async function saveConfig() {
    const cfg = await fetch("/api/config").then(r => r.json());
    for (const el of form.elements) {
      if (!el.name) continue;
      setByPath(cfg, el.name, elValue(el));
    }
    const res = await fetch("/api/config", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(cfg),
    });
    return res.ok;
  }

  async function loadSecrets() {
    const { known, present } = await fetch("/api/secrets").then(r => r.json());
    secretsList.innerHTML = "";
    for (const name of known) {
      const row = document.createElement("div");
      row.className = "secret-row";
      row.innerHTML = `
        <label>${name} <span class="secret-state">${present[name] ? "✓ set" : "— empty"}</span></label>
        <div class="actions">
          <input type="password" placeholder="paste key, then Save" data-secret-input="${name}" autocomplete="off" />
          <button type="button" data-secret-save="${name}">Save</button>
          <button type="button" data-secret-clear="${name}">Clear</button>
          <span data-secret-status="${name}"></span>
        </div>
      `;
      secretsList.appendChild(row);
    }
  }

  async function handleSecretSave(name) {
    const input = secretsList.querySelector(`[data-secret-input="${name}"]`);
    const status = secretsList.querySelector(`[data-secret-status="${name}"]`);
    const value = input.value;
    if (!value) { status.textContent = "nothing to save"; return; }
    status.textContent = "saving…";
    const res = await fetch("/api/secrets", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ name, value }),
    });
    if (res.ok) {
      input.value = "";
      status.textContent = "saved ✓";
      await loadSecrets();
    } else {
      const err = await res.json().catch(() => ({ detail: "error" }));
      status.textContent = err.detail || "error";
    }
  }

  async function handleSecretClear(name) {
    const status = secretsList.querySelector(`[data-secret-status="${name}"]`);
    status.textContent = "clearing…";
    await fetch(`/api/secrets/${encodeURIComponent(name)}`, { method: "DELETE" });
    status.textContent = "cleared";
    await loadSecrets();
  }

  async function handleProviderTest(kind) {
    const statusEl = document.querySelector(`[data-test-status="${kind}"]`);
    statusEl.textContent = "testing…";
    // Persist the currently-entered config first so the test uses what the user
    // sees, not an older saved state.
    await saveConfig();
    const cfg = await fetch("/api/config").then(r => r.json());
    let name, options;
    if (kind === "llm") {
      name = cfg.commentator.provider.name;
      options = cfg.commentator.provider.options || {};
    } else {
      name = cfg[kind].name;
      options = cfg[kind].options || {};
    }
    const res = await fetch("/api/providers/test", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ kind, name, options }),
    });
    const out = await res.json();
    if (out.ok) {
      const detail = out.preview ? `"${out.preview}"` : out.bytes ? `${out.bytes} bytes` : out.note || "ok";
      statusEl.textContent = `✓ ${detail}`;
    } else {
      statusEl.textContent = `✗ ${out.where || ""}: ${out.error || "failed"}`;
    }
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    saveStatus.textContent = "saving…";
    const ok = await saveConfig();
    saveStatus.textContent = ok ? "saved ✓" : "error";
    setTimeout(() => { saveStatus.textContent = ""; }, 2000);
  });

  secretsList.addEventListener("click", (e) => {
    const saveName = e.target?.dataset?.secretSave;
    const clearName = e.target?.dataset?.secretClear;
    if (saveName) handleSecretSave(saveName);
    else if (clearName) handleSecretClear(clearName);
  });

  document.querySelectorAll("[data-test-provider]").forEach(btn => {
    btn.addEventListener("click", () => handleProviderTest(btn.dataset.testProvider));
  });

  loadConfig().then(loadSecrets);
})();
