const categories = ["company", "commodity", "country", "macro"];
const directions = ["bullish", "bearish", "unclear"];
const marketCapStops = [
  1e9, 2e9, 5e9, 1e10, 2.5e10,
  5e10, 1e11, 2.5e11, 5e11, 1e12,
];
const modes = {
  vavi: [
    ["immediate", "Immediate", "Each matching event as it happens"],
    ["digest", "Daily digest", "One grouped email at your saved time"],
  ],
  sentinel: [
    ["immediate", "Immediate", "Immediate tier only; digest tier suppressed"],
    ["smart", "Smart", "Immediate alerts plus lower-tier daily digest"],
    ["digest", "Daily digest", "All eligible events in one daily email"],
  ],
};

const root = document.querySelector("#settings-root");
const timezone = document.querySelector("#timezone");
const toast = document.querySelector("#toast");
let toastTimer;

// The recipient currently being edited. Equals your own address for a regular
// user; an admin can point it at any recipient via the switcher. Every mutating
// request carries it so the server scopes the write to the same recipient.
let viewing = null;
let isAdmin = false;

function withUser(path) {
  if (!viewing) return path;
  return `${path}${path.includes("?") ? "&" : "?"}user=${encodeURIComponent(viewing)}`;
}

function showToast(message, error = false) {
  toast.textContent = message;
  toast.className = `toast show${error ? " error" : ""}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.className = "toast"; }, 3500);
}

function titled(value) { return value.charAt(0).toUpperCase() + value.slice(1); }

function formatMarketCap(value) {
  if (value >= 1e12) return `$${value / 1e12}T`;
  return `$${value / 1e9}B`;
}

function updateMarketCapLabel(card) {
  const slider = card.querySelector('[name="minimum_market_cap_index"]');
  if (!slider) return;
  card.querySelector("#sentinel-market-cap-label").textContent =
    formatMarketCap(marketCapStops[Number(slider.value)]);
}

function buildControls(card, monitor) {
  const makeChoices = (holder, name, values) => {
    holder.innerHTML = values.map(value => `
      <label><input type="checkbox" name="${name}" value="${value}"><span>${titled(value)}</span></label>
    `).join("");
  };
  makeChoices(card.querySelector(".categories"), "categories", categories);
  makeChoices(card.querySelector(".directions"), "directions", directions);
  card.querySelector(".mode-grid").innerHTML = modes[monitor].map(([value, label, help]) => `
    <label><input type="radio" name="delivery_mode" value="${value}"><span><b>${label}</b><small>${help}</small></span></label>
  `).join("");
}

document.querySelectorAll(".monitor-card").forEach(card => {
  buildControls(card, card.dataset.monitor);
  card.addEventListener("change", () => updateConditional(card));
  card.querySelector('[name="minimum_market_cap_index"]')?.addEventListener(
    "input", () => updateMarketCapLabel(card));
  card.querySelector("form").addEventListener("submit", event => saveMonitor(event, card));
  card.querySelector(".test-email-button").addEventListener("click", () => sendTestEmail(card));
});

function setChecks(card, name, values) {
  card.querySelectorAll(`input[name="${name}"]`).forEach(input => {
    input.checked = values.includes(input.value);
  });
}

function renderPreference(monitor, pref) {
  const card = document.querySelector(`[data-monitor="${monitor}"]`);
  card.querySelector('input[name="enabled"]').checked = pref.enabled;
  card.querySelector(`input[name="delivery_mode"][value="${pref.delivery_mode}"]`).checked = true;
  card.querySelector('[name="minimum_significance"]').value = pref.minimum_significance;
  card.querySelector('[name="quiet_hours_enabled"]').checked = pref.quiet_hours_enabled;
  card.querySelector('[name="quiet_start_local"]').value = pref.quiet_start_local;
  card.querySelector('[name="quiet_end_local"]').value = pref.quiet_end_local;
  card.querySelector('[name="digest_time_local"]').value = pref.digest_time_local;
  setChecks(card, "categories", pref.categories);
  setChecks(card, "directions", pref.directions);
  const special = monitor === "vavi" ? "kalshi_enabled" : "update_emails_enabled";
  card.querySelector(`[name="${special}"]`).checked = pref[special];
  if (monitor === "sentinel") {
    const slider = card.querySelector('[name="minimum_market_cap_index"]');
    slider.value = marketCapStops.reduce((best, value, index) =>
      Math.abs(value - pref.minimum_market_cap_usd)
        < Math.abs(marketCapStops[best] - pref.minimum_market_cap_usd) ? index : best, 0);
    updateMarketCapLabel(card);
  }
  updateConditional(card);
}

function updateConditional(card) {
  const quiet = card.querySelector('[name="quiet_hours_enabled"]').checked;
  card.querySelector(".quiet-controls").classList.toggle("is-hidden", !quiet);
  card.querySelector(".quiet-note").classList.toggle("is-hidden", !quiet);
  const mode = card.querySelector('[name="delivery_mode"]:checked')?.value;
  const showDigest = quiet || mode === "digest" || mode === "smart";
  card.querySelector(".digest-control").classList.toggle("is-hidden", !showDigest);
  if (card.dataset.monitor === "vavi") {
    const kalshiOn = card.querySelector('[name="kalshi_enabled"]').checked;
    card.querySelector(".kalshi-preview").classList.toggle("is-hidden", !kalshiOn);
  } else {
    const updatesOn = card.querySelector('[name="update_emails_enabled"]').checked;
    card.querySelector(".update-preview").classList.toggle("is-hidden", !updatesOn);
  }
}

function selected(card, name) {
  return [...card.querySelectorAll(`input[name="${name}"]:checked`)].map(input => input.value);
}

function payloadFor(card) {
  const monitor = card.dataset.monitor;
  const payload = {
    enabled: card.querySelector('[name="enabled"]').checked,
    delivery_mode: card.querySelector('[name="delivery_mode"]:checked')?.value,
    minimum_significance: card.querySelector('[name="minimum_significance"]').value,
    quiet_hours_enabled: card.querySelector('[name="quiet_hours_enabled"]').checked,
    quiet_start_local: card.querySelector('[name="quiet_start_local"]').value,
    quiet_end_local: card.querySelector('[name="quiet_end_local"]').value,
    digest_time_local: card.querySelector('[name="digest_time_local"]').value,
    categories: selected(card, "categories"),
    directions: selected(card, "directions"),
    timezone: timezone.value.trim(),
  };
  payload[monitor === "vavi" ? "kalshi_enabled" : "update_emails_enabled"] =
    card.querySelector(`[name="${monitor === "vavi" ? "kalshi_enabled" : "update_emails_enabled"}"]`).checked;
  if (monitor === "sentinel") {
    const index = Number(card.querySelector('[name="minimum_market_cap_index"]').value);
    payload.minimum_market_cap_usd = marketCapStops[index];
  }
  return payload;
}

function clearErrors(card) {
  card.querySelectorAll("[data-error]").forEach(el => { el.textContent = ""; });
  document.querySelectorAll("[data-global-error]").forEach(el => { el.textContent = ""; });
}

function renderErrors(card, fields = {}) {
  Object.entries(fields).forEach(([field, message]) => {
    const local = card.querySelector(`[data-error="${field}"]`);
    const global = document.querySelector(`[data-global-error="${field}"]`);
    if (local) local.textContent = message;
    else if (global) global.textContent = message;
  });
}

async function saveMonitor(event, card) {
  event.preventDefault();
  clearErrors(card);
  const button = card.querySelector('button[type="submit"]');
  const status = card.querySelector(".save-status");
  button.disabled = true;
  status.textContent = "Saving…";
  try {
    const response = await fetch(withUser(`/api/settings/${card.dataset.monitor}`), {
      method: "PUT",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payloadFor(card)),
    });
    const data = await response.json();
    if (!response.ok) {
      renderErrors(card, data.fields);
      throw new Error("Review the highlighted settings.");
    }
    renderPreference(card.dataset.monitor, data.preference);
    status.textContent = "Saved ✓";
    showToast(`${titled(card.dataset.monitor)} preferences saved. Future events will use them.`);
  } catch (error) {
    status.textContent = "Not saved";
    showToast(error.message || "Could not save settings.", true);
  } finally {
    button.disabled = false;
  }
}

function renderAdmin(data) {
  const bar = document.querySelector("#admin-bar");
  if (!data.is_admin) { bar.classList.add("is-hidden"); return; }
  document.querySelector("#admin-identity").textContent = data.identity || "";
  const switcher = document.querySelector("#user-switcher");
  switcher.innerHTML = (data.users || []).map(user =>
    `<option value="${user.email}">${user.email}${user.status !== "active" ? ` · ${user.status}` : ""}</option>`
  ).join("");
  const current = data.viewing || (data.user && data.user.email) || "";
  if (current) switcher.value = current;
  bar.classList.remove("is-hidden");
}

function showAccessNotice(data) {
  const notice = document.querySelector("#not-recipient");
  const grid = document.querySelector("#settings-root");
  const subscription = document.querySelector("#subscription");
  if (!data) {  // recipient resolved — restore the normal panels
    notice.classList.add("is-hidden");
    grid.classList.remove("is-hidden");
    subscription.classList.remove("is-hidden");
    return;
  }
  if (data.message) document.querySelector("#not-recipient-detail").textContent = data.message;
  notice.classList.remove("is-hidden");
  grid.classList.add("is-hidden");
  subscription.classList.add("is-hidden");
}

async function loadSettings(userEmail) {
  document.body.classList.add("loading");
  try {
    const response = await fetch(
      userEmail ? `/api/settings?user=${encodeURIComponent(userEmail)}` : "/api/settings",
      {headers: {"Accept": "application/json"}});
    const data = await response.json();
    if (!response.ok) throw new Error(data.message || "Could not load settings.");
    isAdmin = !!data.is_admin;
    renderAdmin(data);
    if (!data.user) {  // authenticated, but no recipient row to edit
      viewing = null;
      document.querySelector("#user-email").textContent = data.identity || "—";
      showAccessNotice(data);
      return;
    }
    showAccessNotice(null);
    viewing = data.viewing || data.user.email;
    document.querySelector("#user-email").textContent = data.user.email;
    timezone.value = data.user.timezone;
    document.querySelector("#subscription-status").textContent = titled(data.user.status);
    // Only show monitors this recipient is actually seeded for.
    document.querySelectorAll(".monitor-card").forEach(card => {
      const pref = data.preferences[card.dataset.monitor];
      card.classList.toggle("is-hidden", !pref);
      if (pref) renderPreference(card.dataset.monitor, pref);
    });
    if (data.user.status !== "active") {
      showToast("This address is unsubscribed. Monitor preferences remain saved.", true);
    }
  } catch (error) {
    showToast(error.message, true);
  } finally {
    document.body.classList.remove("loading");
  }
}

async function sendTestEmail(card) {
  const monitor = card.dataset.monitor;
  const button = card.querySelector(".test-email-button");
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Sending…";
  try {
    const response = await fetch("/api/settings/test-email", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({monitor, user: viewing}),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.message || "Could not send test email.");
    showToast(`Test ${titled(monitor)} email sent to ${data.to}.`);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

document.querySelector("#user-switcher").addEventListener("change", event => {
  loadSettings(event.target.value);
});

document.querySelector("#unsubscribe").addEventListener("click", async () => {
  const email = document.querySelector("#user-email").textContent;
  if (!window.confirm(`Disable all future notification delivery to ${email}?`)) return;
  try {
    const response = await fetch(withUser("/api/unsubscribe"), {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: "{}",
    });
    if (!response.ok) throw new Error("Could not unsubscribe this address.");
    document.querySelector("#subscription-status").textContent = "Unsubscribed";
    showToast("This address is now unsubscribed from all delivery.");
  } catch (error) {
    showToast(error.message, true);
  }
});

if (new URLSearchParams(location.search).get("action") === "unsubscribe") {
  window.addEventListener("load", () => document.querySelector("#unsubscribe").focus());
}

loadSettings();
