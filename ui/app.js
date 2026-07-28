const TILE_PLACEHOLDER_SVG = `<svg class="tile-placeholder" viewBox="0 0 100 100">
  <path d="M50 50 C40 30, 20 25, 12 10 C28 15, 42 28, 50 50 Z"/>
  <path d="M50 50 C65 32, 85 30, 96 16 C85 28, 68 32, 50 50 Z"/>
  <path d="M50 50 C62 62, 82 68, 92 84 C76 78, 62 68, 50 50 Z"/>
  <path d="M50 50 C36 65, 18 68, 6 86 C18 72, 34 66, 50 50 Z"/>
  <path d="M50 50 C55 30, 48 12, 54 2 C58 16, 58 34, 50 50 Z"/>
</svg>`;

let STATE = { apps: [], categories: [], app_types: {} };
let activeFilter = "Todas";
let selectedId = null;
let pollTimer = null;
let lastStateKey = null;
let searchQuery = "";
let sortBy = "name";
const imageCache = {}; // path -> data URI

window.addEventListener("pywebviewready", async () => {
  await refreshState(true);
  pollTimer = setInterval(refreshState, 4000);
  checkForUpdatesQuiet();
  initDiscordCorner();
  initFpstation();
  initDownloadToasts();
});

window.addEventListener("backstage-update", () => refreshState(true));
window.addEventListener("backstage-error", (e) => alert(e.detail));

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("rail-add").onclick = openAddModal;
  document.getElementById("rail-logs").onclick = openLogs;
  document.getElementById("strip-add").onclick = openAddModal;
  document.getElementById("emptyAddBtn").onclick = openAddModal;
  document.getElementById("strip-prev").onclick = () => scrollStrip(-1);
  document.getElementById("strip-next").onclick = () => scrollStrip(1);
  document.getElementById("favBtn").onclick = () => selectedId && toggleFavorite(selectedId);
  document.getElementById("delBtn").onclick = () => selectedId && deleteApp(selectedId);
  document.getElementById("playBtn").onclick = () => selectedId && launchApp(selectedId);
  document.getElementById("closeBtn").onclick = (e) => selectedId && closeApp(selectedId, e.currentTarget);
  document.getElementById("repairBtn").onclick = (e) => selectedId && repairApp(selectedId, e.currentTarget);

  document.getElementById("rail-menu").onclick = (e) => { e.stopPropagation(); toggleRailMenu(); };
  document.getElementById("link-github").onclick = () => openExternalLink("https://github.com/kivppy");
  document.getElementById("link-telegram").onclick = () => openExternalLink("https://t.me/ashiganai");
  document.getElementById("link-twitter").onclick = () => openExternalLink("https://x.com/Nau_webp");
  document.getElementById("link-discord-community").onclick = () => openExternalLink("https://discord.gg/JbGe6T8QFC");
  document.addEventListener("click", (e) => {
    const menu = document.getElementById("railMenu");
    if (!menu.classList.contains("hidden") && !menu.contains(e.target) && e.target.id !== "rail-menu") {
      closeRailMenu();
    }
  });

  document.getElementById("pickExeBtn").onclick = pickExeOrId;
  document.getElementById("editPathBtn").onclick = editPathManually;
  document.getElementById("advancedToggle").onclick = toggleAdvanced;
  document.getElementById("f-exe").addEventListener("input", (e) => {
    updateSubmitState();
    if (currentType === "flatpak") {
      const val = e.target.value.trim();
      if (val.includes(".")) autoFillName(val);
    }
  });
  document.getElementById("f-name").addEventListener("input", (e) => {
    nameWasAutoFilled = false; // el usuario tomó el control del nombre
    document.getElementById("nameAutoBadge").classList.add("hidden");
    updateSubmitState();
    const val = e.target.value.trim();
    if (val.length >= 2) triggerCoverSearch(val);
    else resetCoverSection();
  });
  document.getElementById("coverClearBtn").onclick = clearSelectedCover;

  document.getElementById("searchInput").addEventListener("input", (e) => onSearchInput(e.target.value));
  document.getElementById("searchClearBtn").onclick = clearSearch;
  document.getElementById("sortSelect").addEventListener("change", (e) => onSortChange(e.target.value));
});

function toggleRailMenu() {
  const menu = document.getElementById("railMenu");
  menu.classList.toggle("hidden");
  document.getElementById("rail-menu").classList.toggle("active", !menu.classList.contains("hidden"));
}

function closeRailMenu() {
  document.getElementById("railMenu").classList.add("hidden");
  document.getElementById("rail-menu").classList.remove("active");
}

async function openExternalLink(url) {
  closeRailMenu();
  const res = await pywebview.api.open_link(url);
  if (res && res.error) alert(res.error);
}

function scrollStrip(dir) {
  document.getElementById("appStrip").scrollBy({ left: dir * 260, behavior: "smooth" });
}

async function refreshState(force = false) {
  const newState = await pywebview.api.get_state();
  const key = JSON.stringify(newState);
  if (!force && key === lastStateKey) return; // nada cambió, no tocar el DOM
  lastStateKey = key;
  STATE = newState;

  // si la app seleccionada ya no existe (o no pasa el filtro), elegir otra
  if (!STATE.apps.find(a => a.id === selectedId)) selectedId = null;

  if (STATE.sort_by && STATE.sort_by !== sortBy) {
    sortBy = STATE.sort_by;
    const sel = document.getElementById("sortSelect");
    if (sel) sel.value = sortBy;
  }

  maybeShowUpdateBadge();
  renderFilters();
  renderStrip();
  renderHero();
}

function applySort(apps) {
  const sorted = [...apps];
  switch (sortBy) {
    case "favorite":
      sorted.sort((a, b) => (b.favorite === true) - (a.favorite === true) || a.name.localeCompare(b.name));
      break;
    case "playtime":
      sorted.sort((a, b) => (b.playtime || 0) - (a.playtime || 0));
      break;
    case "last_played":
      sorted.sort((a, b) => new Date(b.last_played || 0) - new Date(a.last_played || 0));
      break;
    case "added":
      sorted.sort((a, b) => new Date(b.added_at || 0) - new Date(a.added_at || 0));
      break;
    case "name":
    default:
      sorted.sort((a, b) => a.name.localeCompare(b.name));
      break;
  }
  return sorted;
}

function filteredApps() {
  let apps = STATE.apps;
  if (activeFilter === "⭐ Favoritas") apps = apps.filter(a => a.favorite);
  else if (activeFilter !== "Todas") apps = apps.filter(a => (a.category || "General") === activeFilter);

  const q = searchQuery.trim().toLowerCase();
  if (q) apps = apps.filter(a => a.name.toLowerCase().includes(q));

  return applySort(apps);
}

async function onSortChange(value) {
  sortBy = value;
  await pywebview.api.set_sort(value);
  renderStrip();
}

function onSearchInput(value) {
  searchQuery = value;
  document.getElementById("searchClearBtn").classList.toggle("hidden", !value);
  renderStrip();
}

function clearSearch() {
  searchQuery = "";
  document.getElementById("searchInput").value = "";
  document.getElementById("searchClearBtn").classList.add("hidden");
  renderStrip();
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str ?? "";
  return div.innerHTML;
}

function fmtPlaytime(sec) {
  sec = Math.floor(sec || 0);
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m`;
}

// ---------- Filtros ----------
function renderFilters() {
  const tabs = ["Todas", "⭐ Favoritas", ...STATE.categories];
  const el = document.getElementById("filters");
  el.innerHTML = "";
  tabs.forEach(t => {
    const btn = document.createElement("button");
    btn.className = "filter-chip" + (t === activeFilter ? " active" : "");
    btn.textContent = t;
    btn.onclick = () => { activeFilter = t; renderFilters(); renderStrip(); };
    el.appendChild(btn);
  });
}

// ---------- Strip de apps ----------
function renderStrip() {
  const strip = document.getElementById("appStrip");
  const apps = filteredApps();
  strip.innerHTML = "";

  if (apps.length === 0) {
    selectedId = null;
    renderHero();
    return;
  }

  if (!selectedId || !apps.find(a => a.id === selectedId)) {
    selectedId = apps[0].id;
  }

  apps.forEach(app => {
    const tile = document.createElement("div");
    tile.className = "app-tile" + (app.id === selectedId ? " active" : "");
    tile.title = app.name;
    tile.id = `tile-${app.id}`;

    tile.insertAdjacentHTML("afterbegin", TILE_PLACEHOLDER_SVG);

    const isRunning = (STATE.running || []).includes(app.id);
    if (app.favorite) {
      const fav = document.createElement("span");
      fav.className = "tile-fav";
      fav.innerHTML = `<svg viewBox="0 0 24 24"><path d="M12 3.5 14.9 9.4l6.5.9-4.7 4.6 1.1 6.5-5.8-3-5.8 3 1.1-6.5-4.7-4.6 6.5-.9Z"/></svg>`;
      tile.appendChild(fav);
    }
    if (isRunning) {
      const run = document.createElement("span");
      run.className = "tile-running";
      tile.appendChild(run);
    }

    tile.onclick = () => { selectedId = app.id; renderStrip(); renderHero(); };
    strip.appendChild(tile);
    loadTileImage(app);
  });

  renderHero();
}

function loadTileImage(app) {
  if (!app.image) return;
  const paint = (dataUrl) => {
    const el = document.getElementById(`tile-${app.id}`);
    if (!el || !dataUrl) return;
    const placeholder = el.querySelector(".tile-placeholder");
    if (placeholder) placeholder.remove();
    const img = document.createElement("img");
    img.src = dataUrl;
    el.prepend(img);
  };
  if (imageCache[app.image]) { paint(imageCache[app.image]); return; }
  pywebview.api.get_image_data(app.image).then(dataUrl => {
    if (dataUrl) imageCache[app.image] = dataUrl;
    paint(dataUrl);
  });
}

// ---------- Hero ----------
function renderHero() {
  const hero = document.getElementById("hero");
  const empty = document.getElementById("heroEmpty");
  const content = document.querySelector(".hero-content");
  const bg = document.getElementById("heroBg");

  const app = STATE.apps.find(a => a.id === selectedId);

  if (!app) {
    empty.classList.remove("hidden");
    content.classList.add("hidden");
    bg.className = "hero-bg placeholder";
    bg.style.backgroundImage = "";
    return;
  }

  empty.classList.add("hidden");
  content.classList.remove("hidden");

  document.getElementById("heroLogo").textContent = app.name;

  const typeLabel = STATE.app_types[app.type] || app.type;
  document.getElementById("statType").textContent = typeLabel;
  document.getElementById("statPlaytime").textContent = fmtPlaytime(app.playtime);
  document.getElementById("statCategory").textContent = app.category || "General";

  const warnWrap = document.getElementById("statWarnWrap");
  if (app.missing_dlls && app.missing_dlls.length) {
    warnWrap.style.display = "flex";
    document.getElementById("statWarn").textContent = app.missing_dlls.join(", ");
  } else {
    warnWrap.style.display = "none";
  }

  const favBtn = document.getElementById("favBtn");
  favBtn.classList.toggle("fav-active", !!app.favorite);

  const repairBtn = document.getElementById("repairBtn");
  const isWineRunner = app.type === "exe" && (app.runner || "system_wine") !== "dosbox";
  repairBtn.classList.toggle("hidden", !isWineRunner);

  const runnerWrap = document.getElementById("statRunnerWrap");
  if (app.type === "exe") {
    runnerWrap.style.display = "flex";
    const runnerLabel = (STATE.runners && STATE.runners[app.runner || "system_wine"] || {}).label || "Wine del sistema";
    const presetLabel = app.compat_preset && app.compat_preset !== "none"
      ? (STATE.compat_presets && STATE.compat_presets[app.compat_preset] || {}).label
      : null;
    document.getElementById("statRunner").textContent = presetLabel ? `${runnerLabel} · ${presetLabel}` : runnerLabel;
  } else {
    runnerWrap.style.display = "none";
  }

  const isRunning = (STATE.running || []).includes(app.id);
  const playBtn = document.getElementById("playBtn");
  const closeBtn = document.getElementById("closeBtn");
  playBtn.disabled = isRunning;
  document.getElementById("playBtnLabel").textContent = isRunning ? "CORRIENDO..." : "JUGAR";
  closeBtn.classList.toggle("hidden", !isRunning);
  closeBtn.disabled = false;
  closeBtn.textContent = "Cerrar";

  // fondo: imagen de la app si existe, si no un placeholder con gradiente
  bg.className = "hero-bg";
  if (app.image) {
    if (imageCache[app.image]) {
      bg.style.backgroundImage = `url(${imageCache[app.image]})`;
    } else {
      bg.style.backgroundImage = "";
      bg.classList.add("placeholder");
      pywebview.api.get_image_data(app.image).then(dataUrl => {
        if (dataUrl) {
          imageCache[app.image] = dataUrl;
          if (selectedId === app.id) {
            bg.classList.remove("placeholder");
            bg.style.backgroundImage = `url(${dataUrl})`;
          }
        }
      });
    }
  } else {
    bg.style.backgroundImage = "";
    bg.classList.add("placeholder");
  }
}

async function toggleFavorite(id) {
  STATE = await pywebview.api.toggle_favorite(id);
  lastStateKey = JSON.stringify(STATE);
  renderStrip();
}

async function deleteApp(id) {
  if (!confirm("¿Eliminar esta app de la lista?")) return;
  STATE = await pywebview.api.delete_app(id);
  lastStateKey = JSON.stringify(STATE);
  selectedId = null;
  renderFilters();
  renderStrip();
}

async function launchApp(id) {
  const res = await pywebview.api.launch(id);
  if (res.error) { alert(res.error); return; }
  await refreshState(true);
}

async function closeApp(id, btn) {
  btn.disabled = true;
  btn.textContent = "Cerrando...";
  const res = await pywebview.api.close_app(id);
  if (res.error) alert(res.error);
  await refreshState(true);
}

async function repairApp(id, btn) {
  const original = btn.innerHTML;
  btn.classList.add("repairing");
  const res = await pywebview.api.repair(id);
  if (res.error) alert(res.error);
  setTimeout(() => { btn.innerHTML = original; btn.classList.remove("repairing"); refreshState(); }, 2500);
}

// ---------- Modal agregar ----------
const TYPE_ICONS = {
  exe: "WIN", flatpak: "FP", appimage: "AI",
  native: "LNX", script: "SH", jar: "JAR",
};
const TYPE_SHORT_LABELS = {
  exe: "Windows", flatpak: "Flatpak", appimage: "AppImage",
  native: "Nativo", script: "Script", jar: "Java",
};

let currentType = "exe";
let nameWasAutoFilled = false;
let selectedCoverUrl = null;
let coverSearchToken = 0;
let coverSearchDebounce = null;

function openAddModal() {
  currentType = Object.keys(STATE.app_types)[0] || "exe";
  nameWasAutoFilled = false;

  renderTypeGrid();
  populateCategoryList();
  updateExeLabel();
  resetPickedPath();
  resetCoverSection();

  document.getElementById("f-name").value = "";
  document.getElementById("nameAutoBadge").classList.add("hidden");
  document.getElementById("f-category").value = "";
  document.getElementById("f-image").value = "";
  document.getElementById("f-wineprefix").value = "";
  document.getElementById("advancedFields").classList.add("hidden");
  document.getElementById("advancedToggle").classList.remove("open");
  resetCompatFields();
  updateSubmitState();

  document.getElementById("addModal").classList.remove("hidden");
}

function renderTypeGrid() {
  const grid = document.getElementById("typeGrid");
  grid.innerHTML = "";
  Object.keys(STATE.app_types).forEach(val => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "type-chip" + (val === currentType ? " active" : "");
    chip.innerHTML = `<span class="type-chip-icon">${TYPE_ICONS[val] || "APP"}</span><span>${TYPE_SHORT_LABELS[val] || val}</span>`;
    chip.onclick = () => selectType(val);
    grid.appendChild(chip);
  });
}

function selectType(val) {
  currentType = val;
  resetPickedPath();
  resetCoverSection();
  document.getElementById("f-name").value = "";
  document.getElementById("nameAutoBadge").classList.add("hidden");
  nameWasAutoFilled = false;
  renderTypeGrid();
  updateExeLabel();
  updateSubmitState();
}

function updateExeLabel() {
  const isFlatpak = currentType === "flatpak";
  document.getElementById("lbl-exe").textContent = isFlatpak ? "Application ID" : "Archivo";
  document.getElementById("pickExeLabel").textContent = isFlatpak
    ? "Escribir Application ID…"
    : "Elegir archivo…";
  document.getElementById("f-exe").placeholder = isFlatpak
    ? "ej: org.videolan.VLC"
    : "/ruta/al/archivo";
  document.getElementById("wineprefix-field").style.display = currentType === "exe" ? "block" : "none";
  document.getElementById("compat-field").style.display = currentType === "exe" ? "block" : "none";
}

function resetPickedPath() {
  document.getElementById("f-exe").value = "";
  document.getElementById("pickedPathWrap").classList.add("hidden");
  document.getElementById("pickExeBtn").classList.remove("hidden");
}

function showPickedPath(path) {
  document.getElementById("f-exe").value = path;
  document.getElementById("pickedPathText").textContent = path;
  document.getElementById("pickedPathWrap").classList.remove("hidden");
  document.getElementById("pickExeBtn").classList.add("hidden");
}

function closeAddModal() {
  document.getElementById("addModal").classList.add("hidden");
  ["f-exe", "f-name", "f-category", "f-image", "f-wineprefix", "f-custom-wine"].forEach(id => document.getElementById(id).value = "");
}

function resetCompatFields() {
  const select = document.getElementById("f-compat-preset");
  select.innerHTML = "";
  Object.entries(STATE.compat_presets || {}).forEach(([id, preset]) => {
    const opt = document.createElement("option");
    opt.value = id;
    opt.textContent = preset.label;
    select.appendChild(opt);
  });
  select.value = "none";
  document.getElementById("f-custom-wine").value = "";
  document.getElementById("compatWineCheck").textContent = "";
  document.getElementById("compatCustomWineField").classList.add("hidden");
  document.getElementById("compatReqList").innerHTML = "";
  onCompatPresetChange();
}

async function onCompatPresetChange() {
  const presetId = document.getElementById("f-compat-preset").value;
  const preset = (STATE.compat_presets || {})[presetId];
  document.getElementById("compatPresetDesc").textContent = preset ? preset.desc : "";
  document.getElementById("compatCustomWineField").classList.toggle("hidden", !preset || preset.runner !== "custom_wine");

  const list = document.getElementById("compatReqList");
  list.innerHTML = "";
  if (!preset || presetId === "none") return;

  const res = await pywebview.api.check_compat_preset(presetId);
  (res.items || []).forEach(item => {
    const row = document.createElement("div");
    row.className = "compat-req-item";
    const status = document.createElement("span");
    status.className = "sakura-req-status " + (item.status || "info");
    const info = document.createElement("div");
    info.className = "compat-req-info";
    info.innerHTML = `<span class="compat-req-name">${item.label}</span><span class="compat-req-desc">${item.desc}</span>`;
    row.appendChild(status);
    row.appendChild(info);
    list.appendChild(row);
  });
}

async function browseCustomWine() {
  const path = await pywebview.api.browse_file("wine");
  if (!path) return;
  document.getElementById("f-custom-wine").value = path;
  await validateCustomWineField();
}

async function validateCustomWineField() {
  const path = document.getElementById("f-custom-wine").value.trim();
  const out = document.getElementById("compatWineCheck");
  if (!path) { out.textContent = ""; return; }
  const res = await pywebview.api.validate_custom_wine(path);
  if (res.error) {
    out.textContent = res.error;
    out.classList.add("error");
  } else {
    out.textContent = "Detectado: " + res.version;
    out.classList.remove("error");
  }
}

// ---------- Temas / editor de CSS ----------
function reloadStylesheet() {
  const link = document.getElementById("mainStylesheet");
  link.href = "style.css?t=" + Date.now();
}

function appendThemeLog(message, level = "info") {
  const box = document.getElementById("themeLog");
  const line = document.createElement("div");
  line.className = "sakura-log-line " + level;
  line.textContent = message;
  box.appendChild(line);
  box.scrollTop = box.scrollHeight;
}

function renderThemeList(themes) {
  const list = document.getElementById("themeList");
  list.innerHTML = "";
  (themes || []).forEach(theme => {
    const item = document.createElement("div");
    item.className = "theme-item";
    item.innerHTML = `
      <div class="theme-item-info">
        <span class="theme-item-name">${theme.name}</span>
        <span class="theme-item-path">${theme.path}</span>
      </div>
      <div class="theme-item-actions">
        <button class="apply-theme">Aplicar</button>
        <button class="remove">Quitar</button>
      </div>`;
    item.querySelector(".apply-theme").onclick = () => applySavedTheme(theme.id);
    item.querySelector(".remove").onclick = () => removeSavedTheme(theme.id);
    list.appendChild(item);
  });
}

async function openThemesModal() {
  document.getElementById("themesModal").classList.remove("hidden");
  document.getElementById("themeLog").innerHTML = "";
  const state = await pywebview.api.get_theme_state();
  document.getElementById("cssEditorText").value = state.css || "";
  document.getElementById("themeUndoBtn").disabled = !state.has_backup;
  renderThemeList(state.themes);
}

function closeThemesModal() {
  document.getElementById("themesModal").classList.add("hidden");
}

async function applyCssEdits() {
  const css = document.getElementById("cssEditorText").value;
  const res = await pywebview.api.save_css(css);
  if (res.error) {
    appendThemeLog(res.error, "error");
  } else {
    appendThemeLog("Cambios aplicados", "ok");
    document.getElementById("themeUndoBtn").disabled = false;
    reloadStylesheet();
  }
}

async function undoCssChange() {
  if (!confirm("Esto va a reemplazar el CSS actual por el último respaldo. ¿Seguir?")) return;
  const res = await pywebview.api.restore_backup_css();
  if (res.error) {
    appendThemeLog(res.error, "error");
  } else {
    document.getElementById("cssEditorText").value = res.css;
    appendThemeLog("Se restauró el último respaldo", "ok");
    reloadStylesheet();
  }
}

async function resetCssToDefault() {
  if (!confirm("Esto va a restaurar el CSS predeterminado de fábrica, perdiendo los cambios actuales. ¿Seguir?")) return;
  const res = await pywebview.api.restore_default_css();
  if (res.error) {
    appendThemeLog(res.error, "error");
  } else {
    document.getElementById("cssEditorText").value = res.css;
    document.getElementById("themeUndoBtn").disabled = false;
    appendThemeLog("CSS restaurado al predeterminado", "ok");
    reloadStylesheet();
  }
}

async function importCssTheme() {
  const res = await pywebview.api.import_css_file();
  if (res.status === "cancelled") return;
  if (res.error) { appendThemeLog(res.error, "error"); return; }
  document.getElementById("cssEditorText").value = res.css;
  appendThemeLog("CSS importado desde " + res.path + " (revisá y aplicá los cambios)", "ok");
}

async function exportCssTheme() {
  const css = document.getElementById("cssEditorText").value;
  const res = await pywebview.api.export_css_file(css, "style.css");
  if (res.status === "cancelled") return;
  if (res.error) { appendThemeLog(res.error, "error"); return; }
  appendThemeLog("CSS exportado a " + res.path, "ok");
}

async function saveCssAsTheme() {
  const name = prompt("Nombre para este tema:");
  if (!name) return;
  const css = document.getElementById("cssEditorText").value;
  const res = await pywebview.api.save_theme(name, css);
  if (res.status === "cancelled") return;
  if (res.error) { appendThemeLog(res.error, "error"); return; }
  renderThemeList(res.themes);
  appendThemeLog("Tema guardado: " + name, "ok");
}

async function applySavedTheme(themeId) {
  if (!confirm("Esto va a reemplazar el CSS actual del launcher por el del tema elegido. ¿Seguir?")) return;
  const res = await pywebview.api.apply_theme(themeId);
  if (res.error) { appendThemeLog(res.error, "error"); return; }
  document.getElementById("cssEditorText").value = res.css;
  document.getElementById("themeUndoBtn").disabled = false;
  appendThemeLog("Tema aplicado", "ok");
  reloadStylesheet();
}

async function removeSavedTheme(themeId) {
  const res = await pywebview.api.remove_theme(themeId);
  renderThemeList(res.themes);
}

// ---------- Conexión con Discord ----------
function renderDiscordConnected(user) {
  document.getElementById("discordConnectBtn").classList.add("hidden");
  const card = document.getElementById("discordCard");
  card.classList.remove("hidden");
  document.getElementById("discordAvatar").src = user.avatar_url;
  document.getElementById("discordUsername").textContent = user.global_name || user.username;
}

function renderDiscordDisconnected() {
  document.getElementById("discordCard").classList.add("hidden");
  const btn = document.getElementById("discordConnectBtn");
  btn.classList.remove("hidden");
  btn.disabled = false;
  btn.querySelector("span").textContent = "Conectar con Discord";
}

async function initDiscordCorner() {
  try {
    const state = await pywebview.api.get_discord_state();
    if (state.connected && state.user) {
      renderDiscordConnected(state.user);
    } else {
      renderDiscordDisconnected();
    }
    const box = document.getElementById("discordRedirectBox");
    if (box) box.textContent = state.redirect_uri || "";
  } catch (err) {
    console.error("No se pudo leer el estado de Discord:", err);
    renderDiscordDisconnected();
  }
}

async function onDiscordConnectClick() {
  const state = await pywebview.api.get_discord_state();
  if (!state.configured) {
    openDiscordConfigModal(state.redirect_uri);
    return;
  }
  await launchDiscordLogin();
}

async function launchDiscordLogin() {
  const btn = document.getElementById("discordConnectBtn");
  btn.disabled = true;
  btn.querySelector("span").textContent = "Esperando el navegador...";
  const res = await pywebview.api.start_discord_login();
  if (res.error) {
    alert(res.error);
    btn.disabled = false;
    btn.querySelector("span").textContent = "Conectar con Discord";
  }
}

function onDiscordAuthEvent(e) {
  const { ok, message, user } = e.detail;
  if (ok && user) {
    renderDiscordConnected(user);
  } else {
    renderDiscordDisconnected();
    if (message) alert("No se pudo conectar con Discord: " + message);
  }
}

async function onDiscordDisconnectClick() {
  if (!confirm("¿Desconectar tu cuenta de Discord de Sakura Launcher?")) return;
  await pywebview.api.disconnect_discord();
  renderDiscordDisconnected();
}

function openDiscordConfigModal(redirectUri) {
  document.getElementById("discordRedirectBox").textContent = redirectUri || "";
  document.getElementById("f-discord-client-id").value = "";
  document.getElementById("f-discord-client-secret").value = "";
  document.getElementById("discordConfigModal").classList.remove("hidden");
}

function closeDiscordConfigModal() {
  document.getElementById("discordConfigModal").classList.add("hidden");
}

async function saveDiscordConfigAndConnect() {
  const clientId = document.getElementById("f-discord-client-id").value.trim();
  const clientSecret = document.getElementById("f-discord-client-secret").value.trim();
  if (!clientId || !clientSecret) {
    alert("Completá el Client ID y el Client Secret");
    return;
  }
  await pywebview.api.set_discord_credentials(clientId, clientSecret);
  closeDiscordConfigModal();
  await launchDiscordLogin();
}

async function pickExeOrId() {
  if (currentType === "flatpak") {
    // Application ID: se escribe a mano, no hay diálogo de archivo que tenga sentido acá
    document.getElementById("f-exe").classList.remove("hidden");
    document.getElementById("pickExeBtn").classList.add("hidden");
    document.getElementById("f-exe").focus();
    return;
  }
  const path = await pywebview.api.browse_file("exe");
  if (!path) return;
  showPickedPath(path);
  await autoFillName(path);
  updateSubmitState();
}

function editPathManually() {
  document.getElementById("pickedPathWrap").classList.add("hidden");
  document.getElementById("f-exe").classList.remove("hidden");
  document.getElementById("f-exe").focus();
}

async function autoFillName(path) {
  try {
    const guessed = await pywebview.api.guess_name(path, currentType);
    const nameInput = document.getElementById("f-name");
    // solo pisamos el nombre si el usuario no escribió algo propio a mano
    if (guessed && (!nameInput.value || nameWasAutoFilled)) {
      nameInput.value = guessed;
      nameWasAutoFilled = true;
      document.getElementById("nameAutoBadge").classList.remove("hidden");
      triggerCoverSearch(guessed);
    }
  } catch (e) { /* si falla la detección, el usuario igual puede escribir el nombre a mano */ }
}

function populateCategoryList() {
  const dl = document.getElementById("category-list");
  dl.innerHTML = "";
  (STATE.categories || []).forEach(cat => {
    const opt = document.createElement("option");
    opt.value = cat;
    dl.appendChild(opt);
  });
}

// ---------- Búsqueda automática de portada ----------
function resetCoverSection() {
  coverSearchToken++; // invalida cualquier búsqueda en vuelo
  selectedCoverUrl = null;
  clearTimeout(coverSearchDebounce);
  document.getElementById("coverSection").classList.add("hidden");
  document.getElementById("coverLoading").classList.add("hidden");
  document.getElementById("coverOptions").classList.add("hidden");
  document.getElementById("coverOptions").innerHTML = "";
  document.getElementById("coverEmptyMsg").classList.add("hidden");
  document.getElementById("coverSelectedWrap").classList.add("hidden");
}

function triggerCoverSearch(name) {
  clearTimeout(coverSearchDebounce);
  coverSearchDebounce = setTimeout(() => runCoverSearch(name), 400);
}

async function runCoverSearch(name) {
  name = (name || "").trim();
  if (!name) { resetCoverSection(); return; }

  const myToken = ++coverSearchToken;
  selectedCoverUrl = null;

  const section = document.getElementById("coverSection");
  const loading = document.getElementById("coverLoading");
  const options = document.getElementById("coverOptions");
  const empty = document.getElementById("coverEmptyMsg");
  const selectedWrap = document.getElementById("coverSelectedWrap");

  section.classList.remove("hidden");
  selectedWrap.classList.add("hidden");
  empty.classList.add("hidden");
  options.classList.add("hidden");
  options.innerHTML = "";
  loading.classList.remove("hidden");
  document.getElementById("coverLoadingName").textContent = name;

  let res;
  try {
    res = await pywebview.api.search_covers(name);
  } catch (e) {
    res = { results: [] };
  }

  // si el usuario ya cambió el nombre/tipo mientras esperábamos, descartamos esta respuesta
  if (myToken !== coverSearchToken) return;

  loading.classList.add("hidden");
  const results = (res && res.results) || [];

  if (results.length === 0) {
    empty.classList.remove("hidden");
    return;
  }

  options.classList.remove("hidden");
  results.forEach((item, i) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "cover-option";
    btn.title = item.source ? `Fuente: ${item.source}` : "";
    const img = document.createElement("img");
    img.src = item.url;
    img.loading = "lazy";
    img.onerror = () => btn.remove(); // si la imagen no carga, la sacamos de las opciones
    btn.appendChild(img);
    btn.onclick = () => selectCoverOption(btn, item.url, name);
    options.appendChild(btn);
  });
}

async function selectCoverOption(btn, url, name) {
  document.querySelectorAll(".cover-option").forEach(el => el.classList.remove("active"));
  btn.classList.add("active");
  btn.classList.add("loading-tile");

  const res = await pywebview.api.download_cover(url, name);
  btn.classList.remove("loading-tile");

  if (!res || res.error) {
    alert((res && res.error) || "No se pudo descargar esa portada");
    btn.classList.remove("active");
    return;
  }

  selectedCoverUrl = url;
  document.getElementById("f-image").value = res.path;

  const selectedWrap = document.getElementById("coverSelectedWrap");
  document.getElementById("coverSelectedImg").src = url;
  selectedWrap.classList.remove("hidden");
}

function clearSelectedCover() {
  selectedCoverUrl = null;
  document.getElementById("f-image").value = "";
  document.getElementById("coverSelectedWrap").classList.add("hidden");
  document.querySelectorAll(".cover-option").forEach(el => el.classList.remove("active"));
}

function toggleAdvanced() {
  const fields = document.getElementById("advancedFields");
  const toggle = document.getElementById("advancedToggle");
  fields.classList.toggle("hidden");
  toggle.classList.toggle("open");
}

function updateSubmitState() {
  const exe = document.getElementById("f-exe").value.trim();
  const name = document.getElementById("f-name").value.trim();
  document.getElementById("submitAddBtn").disabled = !(exe && name);
}

async function browse(kind) {
  const path = await pywebview.api.browse_file(kind);
  if (!path) return;
  if (kind === "exe") {
    showPickedPath(path);
    await autoFillName(path);
    updateSubmitState();
  } else {
    document.getElementById("f-image").value = path;
    selectedCoverUrl = null;
    document.querySelectorAll(".cover-option").forEach(el => el.classList.remove("active"));
    document.getElementById("coverSelectedWrap").classList.add("hidden");
  }
}

async function browseFolder() {
  const path = await pywebview.api.browse_folder();
  if (path) document.getElementById("f-wineprefix").value = path;
}

async function submitAdd() {
  const presetId = document.getElementById("f-compat-preset").value || "none";
  const preset = (STATE.compat_presets || {})[presetId];
  const data = {
    type: currentType,
    exe: document.getElementById("f-exe").value,
    name: document.getElementById("f-name").value,
    category: document.getElementById("f-category").value,
    image: document.getElementById("f-image").value,
    wineprefix: document.getElementById("f-wineprefix").value,
    compat_preset: presetId,
    runner: preset ? preset.runner : "system_wine",
    custom_wine_path: document.getElementById("f-custom-wine").value,
  };
  const res = await pywebview.api.add_app(data);
  if (res.error) { alert(res.error); return; }
  STATE = res;
  lastStateKey = JSON.stringify(STATE);
  selectedId = STATE.apps[STATE.apps.length - 1]?.id || null; // seleccionar la recién creada
  closeAddModal();
  renderFilters();
  renderStrip();
}

// ---------- Toasts temporales de descarga/instalación ----------
let downloadToastActive = false;

function initDownloadToasts() {
  window.addEventListener("sakura-progress", (e) => {
    if (!downloadToastActive) {
      downloadToastActive = true;
      showToast(e.detail.status ? `Se está descargando: ${e.detail.status}` : "Se está descargando…");
    }
  });
  window.addEventListener("sakura-done", (e) => {
    downloadToastActive = false;
    showToast(e.detail.message || (e.detail.ok ? "Descarga completada" : "Descarga fallida"), e.detail.ok ? "ok" : "error");
  });
}

function showToast(message, kind = "info") {
  const stack = document.getElementById("toastStack");
  const card = document.createElement("div");
  card.className = "toast-card" + (kind === "ok" ? " ok" : kind === "error" ? " error" : "");
  card.innerHTML = `
    <svg class="toast-icon" viewBox="0 0 24 24">${
      kind === "ok" ? '<path d="M20 6 9 17l-5-5"/>' :
      kind === "error" ? '<path d="M6 6l12 12M18 6 6 18"/>' :
      '<path d="M12 3v10m0 0 4-4m-4 4-4-4M5 15v3a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-3"/>'
    }</svg>
    <span class="toast-text"></span>
  `;
  card.querySelector(".toast-text").textContent = message;
  stack.appendChild(card);
  setTimeout(() => {
    card.classList.add("leaving");
    setTimeout(() => card.remove(), 220);
  }, 8000);
}
// ---------- Modal logs ----------
async function openLogs() {
  document.getElementById("logsModal").classList.remove("hidden");
  await refreshLogs();
}
function closeLogs() { document.getElementById("logsModal").classList.add("hidden"); }
async function refreshLogs() {
  const logs = await pywebview.api.get_logs();
  const box = document.getElementById("logsBox");
  box.textContent = logs || "(sin errores registrados)";
  box.scrollTop = box.scrollHeight;
}
async function clearLogs() {
  await pywebview.api.clear_logs();
  refreshLogs();
}

// ---------- Wine Setup (sakura) ----------
let sakuraRequirements = [];
let sakuraInstalling = false;

window.addEventListener("sakura-log", (e) => appendSakuraLog(e.detail.message, e.detail.level));
window.addEventListener("sakura-progress", (e) => updateSakuraProgress(e.detail.percent, e.detail.status));
window.addEventListener("sakura-done", (e) => onSakuraDone(e.detail.ok, e.detail.message));

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("rail-wine-setup").onclick = openSakuraModal;
  document.getElementById("rail-wine-config").onclick = openWineConfigModal;
  document.getElementById("wcBrowsePrefixBtn").onclick = browseWineConfigPrefix;
  document.getElementById("wcApplyVersionBtn").onclick = applyWinVersion;
  document.getElementById("wcOpenWinecfg").onclick = () => runWineConfigAction("open_winecfg", "Abriendo winecfg…");
  document.getElementById("wcOpenWinetricks").onclick = () => runWineConfigAction("open_winetricks_gui", "Abriendo Winetricks…");
  document.getElementById("wcOpenFolder").onclick = () => runWineConfigAction("open_prefix_folder", "Abriendo carpeta del prefix…");
  document.getElementById("wcResetPrefix").onclick = () => runWineConfigAction("reset_wine_prefix", "Reiniciando el prefix…");
  document.getElementById("wcCleanCache").onclick = () => runWineConfigAction("clean_wine_cache", "Limpiando caché temporal…");
  document.getElementById("wcSaveBtn").onclick = saveWineConfigSettings;
  document.getElementById("f-compat-preset").onchange = onCompatPresetChange;
  document.getElementById("compatBrowseWineBtn").onclick = browseCustomWine;
  document.getElementById("f-custom-wine").addEventListener("blur", validateCustomWineField);
  document.getElementById("rail-themes").onclick = openThemesModal;
  document.getElementById("themeApplyBtn").onclick = applyCssEdits;
  document.getElementById("themeUndoBtn").onclick = undoCssChange;
  document.getElementById("themeResetBtn").onclick = resetCssToDefault;
  document.getElementById("themeImportBtn").onclick = importCssTheme;
  document.getElementById("themeExportBtn").onclick = exportCssTheme;
  document.getElementById("themeSaveAsBtn").onclick = saveCssAsTheme;
  document.getElementById("discordConnectBtn").onclick = onDiscordConnectClick;
  document.getElementById("discordDisconnectBtn").onclick = onDiscordDisconnectClick;
  document.getElementById("discordConfigSaveBtn").onclick = saveDiscordConfigAndConnect;
  window.addEventListener("discord-auth", onDiscordAuthEvent);
});

async function openSakuraModal() {
  document.getElementById("sakuraModal").classList.remove("hidden");
  resetSakuraProgressUI();
  document.getElementById("sakuraReqList").innerHTML =
    `<div class="sakura-req-item"><span class="sakura-req-desc">Revisando pétalos caídos…</span></div>`;

  const state = await pywebview.api.check_wine_setup();
  renderSakuraDistro(state);
  renderSakuraRequirements(state);
}

function closeSakuraModal() {
  if (sakuraInstalling) return; // no cerrar a mitad de una instalación en curso
  document.getElementById("sakuraModal").classList.add("hidden");
}

function renderSakuraDistro(state) {
  const el = document.getElementById("sakuraDistro");
  if (!state.supported) {
    el.textContent = `Distro "${state.os_id}" no reconocida automáticamente`;
  } else {
    el.textContent = `Distro detectada: ${state.os_id} (${state.family})`;
  }
}

function renderSakuraRequirements(state) {
  sakuraRequirements = state.requirements || [];
  const list = document.getElementById("sakuraReqList");
  list.innerHTML = "";

  if (!state.supported) {
    list.innerHTML = `<div class="sakura-unsupported">No pudimos reconocer tu gestor de paquetes.<br>Instalá Wine y winetricks manualmente para esta distro.</div>`;
    document.getElementById("sakuraInstallBtn").disabled = true;
    return;
  }

  sakuraRequirements.forEach(req => {
    const item = document.createElement("div");
    item.className = "sakura-req-item";

    const statusIcon = document.createElement("span");
    statusIcon.className = "sakura-req-status " + (req.installed ? "ok" : "pending");

    const info = document.createElement("div");
    info.className = "sakura-req-info";
    info.innerHTML = `<div class="sakura-req-name">${escapeHtml(req.label)}</div>
                       <div class="sakura-req-desc">${escapeHtml(req.desc)}</div>`;

    const check = document.createElement("input");
    check.type = "checkbox";
    check.className = "sakura-req-check";
    check.id = `sakura-check-${req.id}`;
    check.checked = !req.installed && req.installable;
    check.disabled = req.installed || !req.installable;
    check.onchange = updateSakuraInstallButton;

    item.appendChild(statusIcon);
    item.appendChild(info);
    item.appendChild(check);
    list.appendChild(item);
  });

  updateSakuraInstallButton();
}

function updateSakuraInstallButton() {
  const anyChecked = sakuraRequirements.some(
    req => !req.installed && document.getElementById(`sakura-check-${req.id}`)?.checked
  );
  document.getElementById("sakuraInstallBtn").disabled = !anyChecked || sakuraInstalling;
}

function resetSakuraProgressUI() {
  document.getElementById("sakuraProgressWrap").classList.add("hidden");
  document.getElementById("sakuraLog").classList.add("hidden");
  document.getElementById("sakuraLog").innerHTML = "";
  updateSakuraProgress(0, "Preparando…");
  sakuraInstalling = false;
}

async function startWineInstall() {
  const ids = sakuraRequirements
    .filter(req => !req.installed && document.getElementById(`sakura-check-${req.id}`)?.checked)
    .map(req => req.id);
  if (ids.length === 0) return;

  sakuraInstalling = true;
  document.getElementById("sakuraInstallBtn").disabled = true;
  document.getElementById("sakuraCloseBtn").disabled = true;
  document.getElementById("sakuraProgressWrap").classList.remove("hidden");
  document.getElementById("sakuraLog").classList.remove("hidden");
  document.getElementById("sakuraLog").innerHTML = "";
  updateSakuraProgress(0, "Pidiendo permisos…");

  const res = await pywebview.api.install_wine_requirements(ids);
  if (res && res.error) {
    appendSakuraLog(res.error, "error");
    sakuraInstalling = false;
    document.getElementById("sakuraCloseBtn").disabled = false;
    updateSakuraInstallButton();
  }
}

function updateSakuraProgress(percent, label) {
  percent = Math.max(0, Math.min(100, percent || 0));
  document.getElementById("sakuraProgressPct").textContent = `${percent}%`;
  if (label) document.getElementById("sakuraProgressLabel").textContent = label;

  // el pétalo se "colorea" de abajo hacia arriba: el rect clippeado ocupa
  // el (100 - percent)% superior en y, y percent% de alto
  const fill = document.getElementById("sakuraFill");
  const height = percent; // viewBox es 100x100, 1:1 con el porcentaje
  fill.setAttribute("y", 100 - height);
  fill.setAttribute("height", height);
}

function appendSakuraLog(message, level = "info") {
  const box = document.getElementById("sakuraLog");
  box.classList.remove("hidden");
  const line = document.createElement("div");
  line.className = "sakura-log-line " + (level || "info");
  line.textContent = message;
  box.appendChild(line);
  box.scrollTop = box.scrollHeight;
}

async function onSakuraDone(ok, message) {
  sakuraInstalling = false;
  document.getElementById("sakuraCloseBtn").disabled = false;
  updateSakuraProgress(ok ? 100 : undefined, message);
  appendSakuraLog(message, ok ? "ok" : "error");

  // refrescamos el checklist para reflejar lo que efectivamente quedó instalado
  const state = await pywebview.api.check_wine_setup();
  renderSakuraDistro(state);
  renderSakuraRequirements(state);
}

// ---------- Configurar Wine (optimización y mantenimiento) ----------
async function openWineConfigModal() {
  document.getElementById("wineConfigModal").classList.remove("hidden");
  document.getElementById("wcLog").innerHTML = "";

  const cfg = await pywebview.api.get_wine_config();

  document.getElementById("wcPrefixInput").value = cfg.default_prefix || "";
  document.getElementById("wcEsync").checked = !!cfg.esync;
  document.getElementById("wcFsync").checked = !!cfg.fsync;
  document.getElementById("wcDebugOff").checked = !!cfg.debug_off;

  const versionSelect = document.getElementById("wcWinVersion");
  versionSelect.innerHTML = "";
  Object.entries(cfg.win_versions || {}).forEach(([id, label]) => {
    const opt = document.createElement("option");
    opt.value = id;
    opt.textContent = label;
    if (id === cfg.win_version) opt.selected = true;
    versionSelect.appendChild(opt);
  });
}

function closeWineConfigModal() {
  document.getElementById("wineConfigModal").classList.add("hidden");
}

async function browseWineConfigPrefix() {
  const path = await pywebview.api.browse_folder();
  if (path) document.getElementById("wcPrefixInput").value = path;
}

function currentWinePrefix() {
  return document.getElementById("wcPrefixInput").value.trim() || null;
}

function appendWcLog(message, level = "info") {
  const box = document.getElementById("wcLog");
  const line = document.createElement("div");
  line.className = "sakura-log-line " + level;
  line.textContent = message;
  box.appendChild(line);
  box.scrollTop = box.scrollHeight;
}

async function runWineConfigAction(method, startMessage) {
  appendWcLog(startMessage);
  const res = await pywebview.api[method](currentWinePrefix());
  if (res && res.error) {
    appendWcLog(res.error, "error");
  } else {
    appendWcLog("Listo", "ok");
  }
}

async function applyWinVersion() {
  const version = document.getElementById("wcWinVersion").value;
  appendWcLog("Aplicando versión de Windows…");
  const res = await pywebview.api.set_windows_version(currentWinePrefix(), version);
  if (res && res.error) appendWcLog(res.error, "error");
  else appendWcLog("Versión aplicada", "ok");
}

async function saveWineConfigSettings() {
  const data = {
    esync: document.getElementById("wcEsync").checked,
    fsync: document.getElementById("wcFsync").checked,
    debug_off: document.getElementById("wcDebugOff").checked,
    win_version: document.getElementById("wcWinVersion").value,
  };
  const btn = document.getElementById("wcSaveBtn");
  btn.disabled = true;
  await pywebview.api.save_wine_config_settings(data);
  appendWcLog("Optimización guardada, se aplica en el próximo lanzamiento", "ok");
  btn.disabled = false;
}

// ---------- FPStation (overlay de rendimiento en tiempo real) ----------
let fpstationEnabled = false;

async function initFpstation() {
  const state = await pywebview.api.get_fpstation_state();
  fpstationEnabled = !!state.enabled;
  updateFpstationButton();
}

function updateFpstationButton() {
  const btn = document.getElementById("rail-fpstation");
  btn.classList.toggle("active", fpstationEnabled);
  btn.title = fpstationEnabled
    ? "FPStation: activado (click para desactivar)"
    : "FPStation: desactivado (click para activar)";
}

async function toggleFpstation() {
  const res = await pywebview.api.toggle_fpstation();
  fpstationEnabled = !!res.enabled;
  updateFpstationButton();
}

async function openFpstationModal() {
  const state = await pywebview.api.get_fpstation_state();
  fpstationEnabled = !!state.enabled;

  document.getElementById("fpEnabledCheck").checked = fpstationEnabled;
  document.getElementById("fpMangohudWarning").classList.toggle("hidden", !!state.mangohud_available);

  const metrics = state.metrics || {};
  document.getElementById("fpMetricFps").checked = metrics.fps !== false;
  document.getElementById("fpMetricCpu").checked = metrics.cpu !== false;
  document.getElementById("fpMetricRam").checked = metrics.ram !== false;
  document.getElementById("fpMetricBattery").checked = metrics.battery !== false;
  document.getElementById("fpMetricDisk").checked = !!metrics.disk;
  document.getElementById("fpMetricNet").checked = !!metrics.net;

  document.getElementById("fpPositionSelect").value = state.position || "top-right";
  document.getElementById("fpRefreshSelect").value = String(state.refresh_ms || 1000);

  document.getElementById("fpstationModal").classList.remove("hidden");
}

function closeFpstationModal() {
  document.getElementById("fpstationModal").classList.add("hidden");
}

async function saveFpstationSettings() {
  const btn = document.getElementById("fpSaveBtn");
  btn.disabled = true;

  // el toggle maestro del modal también controla el enabled real, así se
  // puede prender/apagar desde acá o desde el botón del rail indistintamente
  const wantsEnabled = document.getElementById("fpEnabledCheck").checked;
  if (wantsEnabled !== fpstationEnabled) {
    await toggleFpstation();
  }

  await pywebview.api.save_fpstation_settings({
    position: document.getElementById("fpPositionSelect").value,
    refresh_ms: parseInt(document.getElementById("fpRefreshSelect").value, 10),
    metrics: {
      fps: document.getElementById("fpMetricFps").checked,
      cpu: document.getElementById("fpMetricCpu").checked,
      ram: document.getElementById("fpMetricRam").checked,
      battery: document.getElementById("fpMetricBattery").checked,
      disk: document.getElementById("fpMetricDisk").checked,
      net: document.getElementById("fpMetricNet").checked,
    },
  });

  btn.disabled = false;
  closeFpstationModal();
}

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("rail-fpstation").onclick = toggleFpstation;
  document.getElementById("rail-fpstation-config").onclick = openFpstationModal;
  document.getElementById("fpSaveBtn").onclick = saveFpstationSettings;
});

// ---------- Detección automática ----------
let detectSuggestions = [];

async function openDetectModal() {
  document.getElementById("detectModal").classList.remove("hidden");
  document.getElementById("detectAddBtn").disabled = true;
  await runDetection();
}

function closeDetectModal() {
  document.getElementById("detectModal").classList.add("hidden");
}

async function runDetection() {
  const sub = document.getElementById("detectSub");
  const list = document.getElementById("detectList");
  sub.textContent = "Buscando en Steam, Lutris y prefixes de Wine…";
  list.innerHTML = "";
  document.getElementById("detectRescanBtn").disabled = true;

  const res = await pywebview.api.scan_installed_apps();
  document.getElementById("detectRescanBtn").disabled = false;
  detectSuggestions = res.suggestions || [];

  if (res.error) {
    sub.textContent = "Ocurrió un error al escanear";
  } else if (detectSuggestions.length === 0) {
    sub.textContent = "No encontramos apps nuevas para agregar";
    list.innerHTML = `<div class="detect-empty">Tu biblioteca ya tiene todo lo que detectamos, o no hay Steam/Lutris/Wine instalados.</div>`;
  } else {
    sub.textContent = `Encontramos ${detectSuggestions.length} app(s) que todavía no están en tu biblioteca`;
  }

  renderDetectList();
}

function renderDetectList() {
  const list = document.getElementById("detectList");
  list.innerHTML = "";
  detectSuggestions.forEach((item, i) => {
    const row = document.createElement("div");
    row.className = "detect-item";

    const check = document.createElement("input");
    check.type = "checkbox";
    check.className = "detect-item-check";
    check.id = `detect-check-${i}`;
    check.checked = true;
    check.onchange = updateDetectAddButton;

    const info = document.createElement("div");
    info.className = "detect-item-info";
    info.innerHTML = `<div class="detect-item-name">${escapeHtml(item.name)}</div>
                       <div class="detect-item-path">${escapeHtml(item.exe)}</div>`;

    const source = document.createElement("span");
    source.className = "detect-item-source " + item.source;
    source.textContent = item.source;

    row.appendChild(check);
    row.appendChild(info);
    row.appendChild(source);
    list.appendChild(row);
  });
  updateDetectAddButton();
}

function updateDetectAddButton() {
  const anyChecked = detectSuggestions.some((_, i) => document.getElementById(`detect-check-${i}`)?.checked);
  document.getElementById("detectAddBtn").disabled = !anyChecked;
}

async function addDetectedApps() {
  const chosen = detectSuggestions.filter((_, i) => document.getElementById(`detect-check-${i}`)?.checked);
  if (chosen.length === 0) return;

  const btn = document.getElementById("detectAddBtn");
  btn.disabled = true;
  btn.textContent = "Agregando…";

  STATE = await pywebview.api.add_detected_apps(chosen);
  lastStateKey = JSON.stringify(STATE);
  btn.textContent = "Agregar seleccionadas";
  renderFilters();
  renderStrip();
  closeDetectModal();
}

// ---------- Diagnóstico ----------
const DIAG_ICON = { ok: "🟢", optional: "🟡", missing: "🔴" };

async function openDiagnosticsModal() {
  document.getElementById("diagnosticsModal").classList.remove("hidden");
  document.getElementById("diagLog").innerHTML = "";
  await refreshDiagnostics();
}

function closeDiagnosticsModal() {
  document.getElementById("diagnosticsModal").classList.add("hidden");
}

async function refreshDiagnostics() {
  const list = document.getElementById("diagList");
  list.innerHTML = `<div class="detect-empty">Revisando el entorno…</div>`;
  const res = await pywebview.api.run_diagnostics();
  renderDiagnostics(res.items || []);
}

function renderDiagnostics(items) {
  const list = document.getElementById("diagList");
  list.innerHTML = "";
  items.forEach(item => {
    const row = document.createElement("div");
    row.className = "diag-item";

    const dot = document.createElement("span");
    dot.className = "diag-item-dot";
    dot.textContent = DIAG_ICON[item.status] || "🟡";

    const info = document.createElement("div");
    info.className = "diag-item-info";
    info.innerHTML = `<div class="diag-item-name">${escapeHtml(item.label)}</div>
                       <div class="diag-item-detail">${escapeHtml(item.detail || "")}</div>`;

    row.appendChild(dot);
    row.appendChild(info);

    if (item.repairable) {
      const btn = document.createElement("button");
      btn.className = "diag-item-repair";
      btn.textContent = "Reparar";
      btn.onclick = () => repairDiagnosticItem(item.id, btn);
      row.appendChild(btn);
    }

    list.appendChild(row);
  });
}

async function repairDiagnosticItem(itemId, btn) {
  document.querySelectorAll(".diag-item-repair").forEach(b => b.disabled = true);
  btn.textContent = "Reparando…";
  document.getElementById("diagLog").innerHTML = "";

  const res = await pywebview.api.repair_diagnostic_item(itemId);
  if (res && res.error) {
    appendDiagLog(res.error, "error");
    document.querySelectorAll(".diag-item-repair").forEach(b => b.disabled = false);
    btn.textContent = "Reparar";
  }
  // el resultado final llega por los mismos eventos sakura-log/sakura-done
}

function appendDiagLog(message, level = "info") {
  const box = document.getElementById("diagLog");
  const line = document.createElement("div");
  line.className = "sakura-log-line " + level;
  line.textContent = message;
  box.appendChild(line);
  box.scrollTop = box.scrollHeight;
}

// reutilizamos los eventos sakura-* (ya usados por Wine Setup) también acá,
// mostrando el log en el modal de Diagnóstico si está abierto
window.addEventListener("sakura-log", (e) => {
  if (!document.getElementById("diagnosticsModal").classList.contains("hidden")) {
    appendDiagLog(e.detail.message, e.detail.level);
  }
});
window.addEventListener("sakura-done", async (e) => {
  if (!document.getElementById("diagnosticsModal").classList.contains("hidden")) {
    appendDiagLog(e.detail.message, e.detail.ok ? "ok" : "error");
    await refreshDiagnostics();
  }
});

// ---------- Preparar aplicación ----------
let prepareAppId = null;
let prepareRunning = false;

async function openPrepareModal(appId) {
  prepareAppId = appId;
  prepareRunning = false;
  document.getElementById("prepareModal").classList.remove("hidden");
  document.getElementById("prepareProgressWrap").classList.add("hidden");
  document.getElementById("prepareLog").classList.add("hidden");
  document.getElementById("prepareLog").innerHTML = "";
  updatePrepareProgress(0, "Preparando…");
  document.getElementById("prepareStartBtn").disabled = true;

  const sub = document.getElementById("prepareSub");
  const list = document.getElementById("prepareList");
  sub.textContent = "Analizando requisitos…";
  list.innerHTML = "";

  const res = await pywebview.api.analyze_app_requirements(appId);
  if (res.error) {
    sub.textContent = res.error;
    return;
  }

  sub.textContent = res.pending_count > 0
    ? `Faltan ${res.pending_count} requisito(s) para "${res.app}"`
    : `"${res.app}" ya tiene todo lo necesario`;

  if (res.items.length === 0) {
    list.innerHTML = `<div class="prepare-empty">No hay requisitos específicos para este tipo de app.</div>`;
  } else {
    res.items.forEach(item => {
      const row = document.createElement("div");
      row.className = "sakura-req-item";
      const status = document.createElement("span");
      status.className = "sakura-req-status " + (item.installed === false ? "pending" : item.installed === true ? "ok" : "info");
      const info = document.createElement("div");
      info.className = "sakura-req-info";
      info.innerHTML = `<div class="sakura-req-name">${escapeHtml(item.label)}</div>`;
      row.appendChild(status);
      row.appendChild(info);
      list.appendChild(row);
    });
  }

  document.getElementById("prepareStartBtn").disabled = res.pending_count === 0;
}

function closePrepareModal() {
  if (prepareRunning) return; // no cerrar a mitad de una preparación en curso
  document.getElementById("prepareModal").classList.add("hidden");
  prepareAppId = null;
}

async function startPrepareApp() {
  if (!prepareAppId) return;
  prepareRunning = true;
  document.getElementById("prepareStartBtn").disabled = true;
  document.getElementById("prepareCloseBtn").disabled = true;
  document.getElementById("prepareProgressWrap").classList.remove("hidden");
  document.getElementById("prepareLog").classList.remove("hidden");
  document.getElementById("prepareLog").innerHTML = "";
  updatePrepareProgress(0, "Instalando requisitos…");

  const res = await pywebview.api.prepare_app(prepareAppId);
  if (res && res.error) {
    appendPrepareLog(res.error, "error");
    prepareRunning = false;
    document.getElementById("prepareCloseBtn").disabled = false;
  }
}

function updatePrepareProgress(percent, label) {
  percent = Math.max(0, Math.min(100, percent || 0));
  document.getElementById("prepareProgressPct").textContent = `${percent}%`;
  if (label) document.getElementById("prepareProgressLabel").textContent = label;
  const fill = document.getElementById("prepareFill");
  fill.setAttribute("y", 100 - percent);
  fill.setAttribute("height", percent);
}

function appendPrepareLog(message, level = "info") {
  const box = document.getElementById("prepareLog");
  box.classList.remove("hidden");
  const line = document.createElement("div");
  line.className = "sakura-log-line " + (level || "info");
  line.textContent = message;
  box.appendChild(line);
  box.scrollTop = box.scrollHeight;
}

window.addEventListener("sakura-progress", (e) => {
  if (!document.getElementById("prepareModal").classList.contains("hidden") && prepareRunning) {
    updatePrepareProgress(e.detail.percent, e.detail.status);
  }
});
window.addEventListener("sakura-log", (e) => {
  if (!document.getElementById("prepareModal").classList.contains("hidden") && prepareRunning) {
    appendPrepareLog(e.detail.message, e.detail.level);
  }
});
window.addEventListener("sakura-done", (e) => {
  if (!document.getElementById("prepareModal").classList.contains("hidden") && prepareRunning) {
    prepareRunning = false;
    document.getElementById("prepareCloseBtn").disabled = false;
    updatePrepareProgress(e.detail.ok ? 100 : undefined, e.detail.message);
    appendPrepareLog(e.detail.message, e.detail.ok ? "ok" : "error");
  }
});

// ---------- Backup ----------
function openBackupModal() {
  document.getElementById("backupModal").classList.remove("hidden");
  document.getElementById("backupLog").innerHTML = "";
}
function closeBackupModal() {
  document.getElementById("backupModal").classList.add("hidden");
}
function appendBackupLog(message, level = "info") {
  const box = document.getElementById("backupLog");
  const line = document.createElement("div");
  line.className = "sakura-log-line " + level;
  line.textContent = message;
  box.appendChild(line);
  box.scrollTop = box.scrollHeight;
}
async function exportBackup() {
  appendBackupLog("Exportando biblioteca…");
  const res = await pywebview.api.export_backup();
  if (res.status === "cancelled") return;
  if (res.error) appendBackupLog(res.error, "error");
  else appendBackupLog("Backup exportado a " + res.path, "ok");
}
async function importBackup() {
  if (!confirm("Esto va a reemplazar tu biblioteca actual (apps, favoritos, config y temas) por el contenido del backup. ¿Seguir?")) return;
  appendBackupLog("Importando backup…");
  const res = await pywebview.api.import_backup();
  if (res.status === "cancelled") return;
  if (res.error) { appendBackupLog(res.error, "error"); return; }
  appendBackupLog(`Backup importado: ${res.apps_count} app(s) restauradas`, "ok");
  await refreshState(true);
  reloadStylesheet();
}

// ---------- Actualizador ----------
async function checkForUpdatesQuiet() {
  try {
    const res = await pywebview.api.check_for_updates();
    if (res && res.has_update) {
      window._pendingUpdate = res;
      maybeShowUpdateBadge();
    }
  } catch (e) { /* sin conexión u otro error: no molestamos al usuario */ }
}

function maybeShowUpdateBadge() {
  const badge = document.getElementById("rail-update");
  if (window._pendingUpdate && window._pendingUpdate.has_update) {
    badge.classList.remove("hidden");
  }
}

function openUpdateModal() {
  const info = window._pendingUpdate;
  if (!info) return;
  document.getElementById("updateText").textContent =
    `Hay una nueva versión disponible: ${info.latest_version} (tenés ${info.current_version}). El launcher no se actualiza solo: podés revisarla y descargarla vos.`;
  document.getElementById("updateNotes").textContent = info.notes || "";
  document.getElementById("updateModal").classList.remove("hidden");
}
function closeUpdateModal() {
  document.getElementById("updateModal").classList.add("hidden");
}
async function dismissUpdate() {
  const info = window._pendingUpdate;
  if (info) await pywebview.api.dismiss_update(info.latest_version);
  window._pendingUpdate = null;
  document.getElementById("rail-update").classList.add("hidden");
  closeUpdateModal();
}
async function openUpdateDownload() {
  const info = window._pendingUpdate;
  if (info && info.url) await pywebview.api.open_update_page(info.url);
}

// ---------- Atajos de teclado ----------
function anyModalOpen() {
  return Array.from(document.querySelectorAll(".modal-overlay"))
    .some(m => !m.classList.contains("hidden"));
}
function closeTopmostModal() {
  const overlays = Array.from(document.querySelectorAll(".modal-overlay"))
    .filter(m => !m.classList.contains("hidden"));
  if (overlays.length === 0) return false;
  const last = overlays[overlays.length - 1];
  last.classList.add("hidden");
  return true;
}

document.addEventListener("keydown", (e) => {
  const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName);

  if (e.ctrlKey && e.key.toLowerCase() === "k") {
    e.preventDefault();
    document.getElementById("searchInput").focus();
    return;
  }
  if (e.ctrlKey && e.key.toLowerCase() === "n") {
    e.preventDefault();
    openAddModal();
    return;
  }
  if (e.ctrlKey && e.key.toLowerCase() === "q") {
    e.preventDefault();
    pywebview.api.quit_app();
    return;
  }
  if (e.key === "Escape") {
    if (typing && document.activeElement.id === "searchInput") {
      clearSearch();
      document.activeElement.blur();
      return;
    }
    closeTopmostModal();
  }
});

// ---------- Enganche de eventos: nuevos botones del rail y hero ----------
document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("rail-detect").onclick = openDetectModal;
  document.getElementById("detectRescanBtn").onclick = runDetection;
  document.getElementById("detectAddBtn").onclick = addDetectedApps;

  document.getElementById("rail-diagnostics").onclick = openDiagnosticsModal;
  document.getElementById("diagRefreshBtn").onclick = refreshDiagnostics;

  document.getElementById("prepareBtn").onclick = () => selectedId && openPrepareModal(selectedId);
  document.getElementById("prepareStartBtn").onclick = startPrepareApp;

  document.getElementById("rail-backup").onclick = openBackupModal;
  document.getElementById("backupExportBtn").onclick = exportBackup;
  document.getElementById("backupImportBtn").onclick = importBackup;

  document.getElementById("rail-update").onclick = openUpdateModal;
  document.getElementById("updateDismissBtn").onclick = dismissUpdate;
  document.getElementById("updateOpenBtn").onclick = openUpdateDownload;
});
