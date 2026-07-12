const bridge = window.AstrBotPluginPage;

const CITY_OPTIONS = [
  ["宜蘭縣", "宜蘭縣"],
  ["花蓮縣", "花蓮縣"],
  ["臺東縣", "臺東縣"],
  ["澎湖縣", "澎湖縣"],
  ["金門縣", "金門縣"],
  ["連江縣", "連江縣"],
  ["臺北市", "臺北市"],
  ["新北市", "新北市"],
  ["桃園市", "桃園市"],
  ["臺中市", "臺中市"],
  ["臺南市", "臺南市"],
  ["高雄市", "高雄市"],
  ["基隆市", "基隆市"],
  ["新竹市", "新竹市"],
  ["嘉義市", "嘉義市"],
  ["新竹縣", "新竹縣"],
  ["苗栗縣", "苗栗縣"],
  ["彰化縣", "彰化縣"],
  ["南投縣", "南投縣"],
  ["雲林縣", "雲林縣"],
  ["嘉義縣", "嘉義縣"],
  ["屏東縣", "屏東縣"],
];

const ALERT_OPTIONS = [
  ["significant_earthquake", "顯著有感地震"],
  ["local_earthquake", "小區域有感地震"],
  ["tsunami", "海嘯"],
  ["typhoon", "颱風"],
  ["rain", "豪雨／大雨"],
  ["wind", "陸上強風"],
  ["fog", "濃霧"],
  ["heat", "高溫"],
  ["cold", "低溫"],
  ["other", "其他天氣警報"],
];

const SOURCE_LABELS = {
  auto: "自動",
  api: "開放資料 API",
  rss: "官方 RSS",
  current: "目前設定",
  unavailable: "不可用",
  unknown: "未知",
};

const dom = {
  connectionState: document.getElementById("connection-state"),
  refreshButton: document.getElementById("refresh-button"),
  saveButton: document.getElementById("save-button"),
  pageError: document.getElementById("page-error"),
  serviceState: document.getElementById("service-state"),
  activeSource: document.getElementById("active-source"),
  targetCount: document.getElementById("target-count"),
  subscriptionCount: document.getElementById("subscription-count"),
  sourceSummary: document.getElementById("source-summary"),
  revisionLabel: document.getElementById("revision-label"),
  enabledInput: document.getElementById("enabled-input"),
  sourceModeInput: document.getElementById("source-mode-input"),
  pollIntervalInput: document.getElementById("poll-interval-input"),
  apiKeyInput: document.getElementById("api-key-input"),
  clearApiKeyInput: document.getElementById("clear-api-key-input"),
  apiKeyState: document.getElementById("api-key-state"),
  addRuleButton: document.getElementById("add-rule-button"),
  rulesContainer: document.getElementById("rules-container"),
  rulesSummary: document.getElementById("rules-summary"),
  sourceTestInput: document.getElementById("source-test-input"),
  sourceTestButton: document.getElementById("source-test-button"),
  sourceDetail: document.getElementById("source-detail"),
  sourceTestResult: document.getElementById("source-test-result"),
  targetsContainer: document.getElementById("targets-container"),
  targetsSummary: document.getElementById("targets-summary"),
  deliveryBody: document.getElementById("delivery-body"),
  deliverySummary: document.getElementById("delivery-summary"),
  confirmDialog: document.getElementById("confirm-dialog"),
  confirmTitle: document.getElementById("confirm-title"),
  confirmMessage: document.getElementById("confirm-message"),
  confirmAccept: document.getElementById("confirm-accept"),
  previewDialog: document.getElementById("preview-dialog"),
  previewTitle: document.getElementById("preview-title"),
  previewMeta: document.getElementById("preview-meta"),
  previewContent: document.getElementById("preview-content"),
  previewClose: document.getElementById("preview-close"),
  toast: document.getElementById("toast"),
};

const state = {
  revision: 0,
  settings: null,
  targets: [],
  sourceState: {},
  deliveryStatus: [],
  cityOptions: CITY_OPTIONS,
  alertOptions: ALERT_OPTIONS,
  apiKeyFromEnvironment: false,
  dirty: false,
  busy: false,
  toastTimer: null,
};

function createElement(tagName, className = "", text = "") {
  const node = document.createElement(tagName);
  if (className) node.className = className;
  if (text !== "") node.textContent = String(text);
  return node;
}

function makeId() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  return `subscription-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function asArray(value) {
  return Array.isArray(value) ? value : [];
}

function unwrapPayload(value) {
  if (value && value.status === "ok" && Object.hasOwn(value, "data")) {
    return value.data;
  }
  return value ?? {};
}

function sourceLabel(value) {
  return SOURCE_LABELS[value] || String(value || SOURCE_LABELS.unknown);
}

function targetLabel(target) {
  return String(
    target?.display_name ||
      target?.group_name ||
      target?.group_id ||
      target?.target_umo ||
      "未命名群組",
  );
}

function targetId(target) {
  return String(target?.id || target?.target_id || "");
}

function findTargetByUmo(umo) {
  return state.targets.find((target) => target.target_umo === umo) || null;
}

function formatTime(value) {
  if (!value) return "--";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-TW", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
}

function setStatusPill(node, text, kind = "neutral") {
  node.textContent = text;
  node.className = `status-pill status-${kind}`;
}

function setDirty(value = true) {
  state.dirty = value;
  dom.saveButton.textContent = value ? "儲存設定 *" : "儲存設定";
}

function setBusy(value) {
  state.busy = value;
  dom.refreshButton.disabled = value;
  dom.saveButton.disabled = value;
  dom.addRuleButton.disabled = value || state.targets.length === 0;
}

function showError(message) {
  dom.pageError.textContent = String(message || "發生未知錯誤");
  dom.pageError.hidden = false;
  setStatusPill(dom.connectionState, "載入失敗", "danger");
}

function clearError() {
  dom.pageError.textContent = "";
  dom.pageError.hidden = true;
}

function showToast(message) {
  if (state.toastTimer) window.clearTimeout(state.toastTimer);
  dom.toast.textContent = String(message || "完成");
  dom.toast.hidden = false;
  state.toastTimer = window.setTimeout(() => {
    dom.toast.hidden = true;
  }, 3800);
}

function requestConfirmation(title, message, confirmLabel = "確認") {
  dom.confirmTitle.textContent = title;
  dom.confirmMessage.textContent = message;
  dom.confirmAccept.textContent = confirmLabel;
  dom.confirmDialog.returnValue = "";
  dom.confirmDialog.showModal();
  return new Promise((resolve) => {
    dom.confirmDialog.addEventListener(
      "close",
      () => resolve(dom.confirmDialog.returnValue === "confirm"),
      { once: true },
    );
  });
}

function emptyState(text) {
  return createElement("div", "empty-state", text);
}

function normalizeSettings(raw) {
  const settings = raw && typeof raw === "object" ? raw : {};
  return {
    revision: Number(settings.revision || state.revision || 0),
    enabled: settings.enabled === true,
    source_mode: ["auto", "api", "rss"].includes(settings.source_mode)
      ? settings.source_mode
      : "auto",
    poll_interval_seconds: Math.min(
      600,
      Math.max(60, Number(settings.poll_interval_seconds || 120)),
    ),
    subscriptions: asArray(settings.subscriptions).map((subscription) => ({
      id: String(subscription?.id || makeId()),
      target_umo: String(subscription?.target_umo || ""),
      enabled: subscription?.enabled !== false,
      cities: asArray(subscription?.cities).map(String),
      forecast_enabled: subscription?.forecast_enabled === true,
      forecast_times: asArray(subscription?.forecast_times)
        .map(String)
        .slice(0, 4),
      alert_types: asArray(subscription?.alert_types).map(String),
    })),
  };
}

function defaultSubscription(target = null) {
  return {
    id: makeId(),
    target_umo: String(target?.target_umo || ""),
    enabled: true,
    cities: ["臺北市"],
    forecast_enabled: true,
    forecast_times: ["07:30", "18:30"],
    alert_types: state.alertOptions.map(([value]) => value),
  };
}

async function apiGet(endpoint, params = undefined) {
  return unwrapPayload(await bridge.apiGet(endpoint, params));
}

async function apiPost(endpoint, body = {}) {
  return unwrapPayload(await bridge.apiPost(endpoint, body));
}

function applyState(payload) {
  state.revision = Number(payload?.revision || payload?.settings?.revision || 0);
  state.settings = normalizeSettings(payload?.settings);
  state.settings.revision = state.revision;
  state.targets = asArray(payload?.targets).map((target) => ({ ...target }));
  state.sourceState = payload?.source_state || {};
  state.deliveryStatus = asArray(payload?.delivery_status);
  state.cityOptions = asArray(payload?.cities).length
    ? payload.cities.map((city) => [String(city), String(city)])
    : CITY_OPTIONS;
  state.alertOptions =
    payload?.alert_types && typeof payload.alert_types === "object"
      ? Object.entries(payload.alert_types).map(([value, label]) => [
          String(value),
          String(label),
        ])
      : ALERT_OPTIONS;

  dom.enabledInput.checked = state.settings.enabled;
  dom.sourceModeInput.value = state.settings.source_mode;
  dom.pollIntervalInput.value = String(state.settings.poll_interval_seconds);
  dom.apiKeyInput.value = "";
  dom.clearApiKeyInput.checked = false;
  const keyConfigured =
    payload?.api_key_configured ?? payload?.settings?.api_key_configured ?? false;
  const keySuffix = payload?.api_key_suffix || payload?.settings?.api_key_suffix || "****";
  const keyFromEnvironment =
    payload?.api_key_from_environment ??
    payload?.settings?.api_key_from_environment ??
    false;
  state.apiKeyFromEnvironment = Boolean(keyFromEnvironment);
  dom.clearApiKeyInput.disabled = state.apiKeyFromEnvironment;
  dom.apiKeyState.textContent = keyConfigured
    ? `已設定（尾碼 ${keySuffix}${keyFromEnvironment ? "，來自環境變數" : ""}）`
    : "未設定；自動模式將使用 RSS";

  renderStatus();
  renderSourceDetail();
  renderTargets();
  renderRules();
  renderDelivery();
  setDirty(false);
}

function renderStatus() {
  const enabledRules = state.settings.subscriptions.filter(
    (subscription) => subscription.enabled,
  ).length;
  const activeSource = state.sourceState.active_source || "unknown";
  const activeSourceLabel =
    state.sourceState.status_label ||
    (activeSource === "rss"
      ? state.sourceState.is_fallback
        ? "RSS 降級中"
        : "RSS 模式"
      : sourceLabel(activeSource));

  dom.serviceState.textContent = state.settings.enabled ? "運作中" : "已停用";
  dom.activeSource.textContent = activeSourceLabel;
  dom.targetCount.textContent = String(state.targets.length);
  dom.subscriptionCount.textContent = String(enabledRules);
  dom.revisionLabel.textContent = `版本 ${state.revision}`;

  const apiError = state.sourceState.last_api_error;
  const rssError = state.sourceState.last_rss_error;
  if (apiError && rssError) {
    dom.sourceSummary.textContent = "API 與 RSS 最近皆有錯誤";
  } else if (state.sourceState.is_fallback) {
    dom.sourceSummary.textContent = "API 異常，已切換 RSS";
  } else {
    dom.sourceSummary.textContent = `使用 ${sourceLabel(activeSource)}`;
  }

  setStatusPill(
    dom.connectionState,
    state.dirty ? "尚未儲存" : "已同步",
    state.dirty ? "warning" : "success",
  );
}

function appendDetail(label, value) {
  dom.sourceDetail.append(
    createElement("dt", "", label),
    createElement("dd", "", value),
  );
}

function renderSourceDetail() {
  const source = state.sourceState;
  dom.sourceDetail.replaceChildren();
  appendDetail("要求模式", sourceLabel(source.requested_mode || state.settings.source_mode));
  appendDetail(
    "API 可用",
    source.api_available == null ? "--" : source.api_available ? "是" : "否",
  );
  appendDetail(
    "RSS 可用",
    source.rss_available == null ? "--" : source.rss_available ? "是" : "否",
  );
  appendDetail("API 連續失敗", String(source.api_consecutive_failures || 0));
  appendDetail("API 最近成功", formatTime(source.last_api_success_at));
  appendDetail("RSS 最近成功", formatTime(source.last_rss_success_at));
  appendDetail("最近切換", formatTime(source.last_switch_at));
  if (source.last_api_error) appendDetail("API 錯誤", String(source.last_api_error));
  if (source.last_rss_error) appendDetail("RSS 錯誤", String(source.last_rss_error));
}

function createTargetOption(target, selectedUmo) {
  const option = document.createElement("option");
  option.value = String(target.target_umo || "");
  const availability = target.available === false ? " · 目前不可用" : "";
  option.textContent = `${targetLabel(target)} · ${target.platform_type || target.platform_id || "平台未知"}${availability}`;
  option.selected = option.value === selectedUmo;
  option.disabled = target.available === false && !option.selected;
  return option;
}

function renderTargets() {
  dom.targetsContainer.replaceChildren();
  dom.targetsSummary.textContent = `${state.targets.length} 個`;
  dom.addRuleButton.disabled = state.busy || state.targets.length === 0;

  if (state.targets.length === 0) {
    dom.targetsContainer.append(emptyState("尚無已登記群組"));
    return;
  }

  for (const target of state.targets) {
    const item = createElement("div", "target-item");
    const info = createElement("div");
    info.append(createElement("div", "target-name", targetLabel(target)));
    const meta = createElement("div", "target-meta");
    meta.append(
      createElement("span", "tag", target.platform_type || target.platform_id || "未知平台"),
      createElement("span", "tag", target.group_id || "群組 ID 未知"),
    );
    const availability = createElement(
      "span",
      `status-pill ${target.available === false ? "status-danger" : "status-success"}`,
      target.available === false ? "目前不可用" : "可主動發送",
    );
    availability.title = String(target.availability_message || "");
    meta.append(availability);
    if (target.registered_at) {
      meta.append(createElement("span", "tag", `登記 ${formatTime(target.registered_at)}`));
    }
    info.append(meta);

    const remove = createElement("button", "button button-quiet-danger button-small", "移除");
    remove.type = "button";
    remove.disabled = !targetId(target);
    remove.addEventListener("click", () => deleteTarget(target));
    item.append(info, remove);
    dom.targetsContainer.append(item);
  }
}

function createCheckboxGrid(options, selectedValues, onChange) {
  const selected = new Set(selectedValues);
  const grid = createElement("div", "checkbox-grid");
  for (const [value, label] of options) {
    const wrapper = createElement("label", "checkbox-option");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.value = value;
    checkbox.checked = selected.has(value);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) selected.add(value);
      else selected.delete(value);
      onChange(Array.from(selected));
      markChanged();
    });
    wrapper.append(checkbox, createElement("span", "", label));
    grid.append(wrapper);
  }
  return grid;
}

function createTimeList(subscription) {
  const container = createElement("div", "time-list");

  subscription.forecast_times.forEach((time, timeIndex) => {
    const item = createElement("div", "time-item");
    const input = document.createElement("input");
    input.type = "time";
    input.value = time;
    input.disabled = !subscription.forecast_enabled;
    input.addEventListener("input", () => {
      subscription.forecast_times[timeIndex] = input.value;
      markChanged();
    });
    const remove = createElement("button", "time-remove", "×");
    remove.type = "button";
    remove.title = "移除時刻";
    remove.disabled = !subscription.forecast_enabled;
    remove.addEventListener("click", () => {
      subscription.forecast_times.splice(timeIndex, 1);
      markChanged(true);
    });
    item.append(input, remove);
    container.append(item);
  });

  if (subscription.forecast_times.length === 0) {
    container.append(createElement("span", "metadata-text", "未設定播報時刻"));
  }
  return container;
}

function renderRule(subscription, index) {
  const item = createElement("article", "rule-item");
  item.dataset.subscriptionId = subscription.id;

  const header = createElement("div", "rule-header");
  header.append(createElement("span", "rule-index", `#${index + 1}`));

  const targetField = createElement("label", "field");
  targetField.append(createElement("span", "", "發送群組"));
  const targetSelect = document.createElement("select");
  const emptyOption = document.createElement("option");
  emptyOption.value = "";
  emptyOption.textContent = "選擇已登記群組";
  targetSelect.append(emptyOption);
  if (subscription.target_umo && !findTargetByUmo(subscription.target_umo)) {
    const retainedOption = document.createElement("option");
    retainedOption.value = subscription.target_umo;
    retainedOption.textContent = "已取消登記（規則保留）";
    targetSelect.append(retainedOption);
  }
  state.targets.forEach((target) => {
    targetSelect.append(createTargetOption(target, subscription.target_umo));
  });
  targetSelect.value = subscription.target_umo;
  targetSelect.addEventListener("change", () => {
    subscription.target_umo = targetSelect.value;
    markChanged();
  });
  targetField.append(targetSelect);
  header.append(targetField);

  const enabledField = createElement("label", "field field-inline");
  enabledField.append(createElement("span", "", "啟用"));
  const enabledInput = document.createElement("input");
  enabledInput.type = "checkbox";
  enabledInput.checked = subscription.enabled;
  enabledInput.role = "switch";
  enabledInput.addEventListener("change", () => {
    subscription.enabled = enabledInput.checked;
    markChanged();
  });
  enabledField.append(enabledInput);
  header.append(enabledField);

  const removeButton = createElement("button", "button button-quiet-danger button-small", "刪除規則");
  removeButton.type = "button";
  removeButton.addEventListener("click", async () => {
    const accepted = await requestConfirmation(
      "刪除播報規則",
      `確定刪除「${targetLabel(findTargetByUmo(subscription.target_umo))}」的播報規則？`,
      "刪除",
    );
    if (!accepted) return;
    state.settings.subscriptions.splice(index, 1);
    markChanged(true);
  });
  header.append(removeButton);

  const body = createElement("div", "rule-body");

  const citiesSection = createElement("section", "rule-section");
  const citiesHeading = createElement("div", "rule-section-heading");
  citiesHeading.append(
    createElement("span", "", "天氣縣市"),
    createElement("span", "", `${subscription.cities.length} 個`),
  );
  citiesSection.append(citiesHeading);
  citiesSection.append(
    createCheckboxGrid(state.cityOptions, subscription.cities, (values) => {
      subscription.cities = values;
    }),
  );
  body.append(citiesSection);

  const alertsSection = createElement("section", "rule-section");
  const alertsHeading = createElement("div", "rule-section-heading");
  alertsHeading.append(
    createElement("span", "", "災害與警特報"),
    createElement("span", "", `${subscription.alert_types.length} 種`),
  );
  alertsSection.append(alertsHeading);
  alertsSection.append(
    createCheckboxGrid(state.alertOptions, subscription.alert_types, (values) => {
      subscription.alert_types = values;
    }),
  );
  body.append(alertsSection);

  const forecastSection = createElement("section", "rule-section");
  const forecastHeading = createElement("div", "rule-section-heading");
  const forecastToggle = createElement("label", "checkbox-option");
  const forecastInput = document.createElement("input");
  forecastInput.type = "checkbox";
  forecastInput.checked = subscription.forecast_enabled;
  forecastToggle.append(forecastInput, createElement("span", "", "定時天氣"));
  const addTimeButton = createElement("button", "button button-secondary button-small", "新增時刻");
  addTimeButton.type = "button";
  addTimeButton.disabled = !subscription.forecast_enabled || subscription.forecast_times.length >= 4;
  addTimeButton.addEventListener("click", () => {
    if (subscription.forecast_times.length >= 4) return;
    subscription.forecast_times.push("07:30");
    markChanged(true);
  });
  forecastHeading.append(forecastToggle, addTimeButton);
  forecastSection.append(forecastHeading, createTimeList(subscription));
  forecastInput.addEventListener("change", () => {
    subscription.forecast_enabled = forecastInput.checked;
    if (subscription.forecast_enabled && subscription.forecast_times.length === 0) {
      subscription.forecast_times.push("07:30");
    }
    markChanged(true);
  });
  body.append(forecastSection);

  const actions = createElement("div", "rule-actions");
  const weatherPreview = createElement("button", "button button-secondary button-small", "預覽天氣");
  weatherPreview.type = "button";
  weatherPreview.addEventListener("click", () => previewSubscription(subscription, "weather"));
  const alertsPreview = createElement("button", "button button-secondary button-small", "預覽警報");
  alertsPreview.type = "button";
  alertsPreview.addEventListener("click", () => previewSubscription(subscription, "alerts"));
  const testSend = createElement("button", "button button-primary button-small", "測試發送");
  testSend.type = "button";
  const selectedTarget = findTargetByUmo(subscription.target_umo);
  testSend.disabled =
    !selectedTarget || !targetId(selectedTarget) || selectedTarget.available === false;
  testSend.addEventListener("click", () => sendTest(subscription));
  actions.append(weatherPreview, alertsPreview, testSend);
  body.append(actions);

  item.append(header, body);
  return item;
}

function renderRules() {
  dom.rulesContainer.replaceChildren();
  const subscriptions = state.settings?.subscriptions || [];
  dom.rulesSummary.textContent = `${subscriptions.length} 筆`;
  dom.subscriptionCount.textContent = String(
    subscriptions.filter((subscription) => subscription.enabled).length,
  );

  if (subscriptions.length === 0) {
    dom.rulesContainer.append(emptyState("尚無播報規則"));
    return;
  }

  subscriptions.forEach((subscription, index) => {
    dom.rulesContainer.append(renderRule(subscription, index));
  });
}

function renderDelivery() {
  dom.deliveryBody.replaceChildren();
  const records = state.deliveryStatus.slice(0, 50);
  dom.deliverySummary.textContent = records.length ? `${records.length} 筆` : "尚無紀錄";

  if (records.length === 0) {
    const row = document.createElement("tr");
    const cell = createElement("td", "empty-state", "尚無投遞紀錄");
    cell.colSpan = 6;
    row.append(cell);
    dom.deliveryBody.append(row);
    return;
  }

  records.forEach((record) => {
    const row = document.createElement("tr");
    const target = findTargetByUmo(record.target_umo);
    const status = record.status === "success" ? "成功" : "失敗";
    const statusClass = record.status === "success" ? "status-success" : "status-danger";
    const statusCell = document.createElement("td");
    statusCell.append(createElement("span", `status-pill ${statusClass}`, status));
    row.append(
      createElement("td", "", formatTime(record.delivered_at || record.last_attempt_at)),
      createElement("td", "", target ? targetLabel(target) : record.target_umo || "--"),
      createElement("td", "", record.part === "image" ? "圖片" : "文字"),
      statusCell,
      createElement("td", "", String(record.attempts || 0)),
      createElement("td", "cell-error", record.error || ""),
    );
    dom.deliveryBody.append(row);
  });
}

function markChanged(rerender = false) {
  setDirty(true);
  renderStatus();
  if (rerender) renderRules();
}

function buildSettingsPayload() {
  const pollInterval = Number(dom.pollIntervalInput.value);
  const subscriptions = state.settings.subscriptions.map((subscription) => ({
    id: String(subscription.id),
    target_umo: String(subscription.target_umo).trim(),
    enabled: Boolean(subscription.enabled),
    cities: Array.from(new Set(subscription.cities)),
    forecast_enabled: Boolean(subscription.forecast_enabled),
    forecast_times: Array.from(
      new Set(subscription.forecast_times.filter((value) => /^\d{2}:\d{2}$/.test(value))),
    ).slice(0, 4),
    alert_types: Array.from(new Set(subscription.alert_types)),
  }));

  const payload = {
    revision: state.revision,
    enabled: dom.enabledInput.checked,
    source_mode: dom.sourceModeInput.value,
    poll_interval_seconds: pollInterval,
    subscriptions,
  };

  const apiKey = dom.apiKeyInput.value.trim();
  if (apiKey) payload.api_key = apiKey;
  else if (dom.clearApiKeyInput.checked) payload.api_key = "";
  return payload;
}

function validateSettings(payload) {
  if (!Number.isInteger(payload.poll_interval_seconds)) {
    return "輪詢間隔必須是整數。";
  }
  if (payload.poll_interval_seconds < 60 || payload.poll_interval_seconds > 600) {
    return "輪詢間隔必須介於 60 至 600 秒。";
  }

  const registeredUmos = new Set(state.targets.map((target) => target.target_umo));
  const selectedUmos = new Set();
  for (const [index, subscription] of payload.subscriptions.entries()) {
    if (!subscription.target_umo || !registeredUmos.has(subscription.target_umo)) {
      return `第 ${index + 1} 筆規則尚未選擇有效的已登記群組。`;
    }
    if (selectedUmos.has(subscription.target_umo)) {
      return `「${targetLabel(findTargetByUmo(subscription.target_umo))}」有重複規則。`;
    }
    selectedUmos.add(subscription.target_umo);
    if (subscription.forecast_enabled && subscription.cities.length === 0) {
      return `第 ${index + 1} 筆規則已啟用定時天氣，但未選擇縣市。`;
    }
    if (subscription.forecast_enabled && subscription.forecast_times.length === 0) {
      return `第 ${index + 1} 筆規則已啟用定時天氣，但未設定播報時刻。`;
    }
    if (subscription.alert_types.length === 0 && !subscription.forecast_enabled) {
      return `第 ${index + 1} 筆規則沒有可播報的內容。`;
    }
  }
  return "";
}

async function loadState(force = false) {
  if (state.busy) return;
  if (state.dirty && !force) {
    const accepted = await requestConfirmation(
      "捨棄未儲存變更",
      "重新整理將捨棄目前尚未儲存的設定。",
      "捨棄並重新整理",
    );
    if (!accepted) return;
  }

  setBusy(true);
  clearError();
  setStatusPill(dom.connectionState, "同步中", "neutral");
  try {
    applyState(await apiGet("state"));
    setStatusPill(dom.connectionState, "已同步", "success");
  } catch (error) {
    showError(error?.message || error);
  } finally {
    setBusy(false);
  }
}

async function saveSettings() {
  if (state.busy) return;
  const payload = buildSettingsPayload();
  const validationError = validateSettings(payload);
  if (validationError) {
    showToast(validationError);
    return;
  }

  setBusy(true);
  clearError();
  setStatusPill(dom.connectionState, "儲存中", "neutral");
  try {
    const result = await apiPost("settings", payload);
    if (result?.ok === false) throw new Error(result.message || "儲存失敗");
    state.revision = Number(result?.revision || state.revision + 1);
    setDirty(false);
    showToast(result?.message || "設定已儲存");
    setBusy(false);
    await loadState(true);
  } catch (error) {
    showError(error?.message || error);
  } finally {
    setBusy(false);
  }
}

async function testSource() {
  if (state.busy) return;
  const requestedSource = dom.sourceTestInput.value;
  const testingApi =
    requestedSource === "api" ||
    (requestedSource === "current" &&
      (state.sourceState.active_source === "api" ||
        state.sourceState.requested_mode === "api"));
  const hasPendingKeyChange =
    dom.apiKeyInput.value.trim() !== "" || dom.clearApiKeyInput.checked;
  if (testingApi && hasPendingKeyChange) {
    showToast("授權碼變更尚未儲存。請先儲存設定，再測試 API。");
    return;
  }
  dom.sourceTestButton.disabled = true;
  dom.sourceTestResult.hidden = false;
  dom.sourceTestResult.textContent = "測試中…";
  try {
    const result = await apiPost("source/test", {
      source: requestedSource,
    });
    const details = [
      result?.ok === false ? "失敗" : "成功",
      sourceLabel(result?.source),
      result?.latency_ms != null ? `${result.latency_ms} ms` : "",
      result?.item_count != null ? `${result.item_count} 筆` : "",
      result?.message || "",
    ].filter(Boolean);
    dom.sourceTestResult.textContent = details.join(" · ");
  } catch (error) {
    dom.sourceTestResult.textContent = `失敗 · ${error?.message || error}`;
  } finally {
    dom.sourceTestButton.disabled = false;
  }
}

async function previewSubscription(subscription, kind) {
  const city = subscription.cities[0] || undefined;
  dom.previewTitle.textContent = kind === "weather" ? "天氣訊息預覽" : "災害警報預覽";
  dom.previewMeta.textContent = "產生中…";
  dom.previewContent.replaceChildren();
  dom.previewDialog.showModal();
  try {
    const result = await apiPost("preview", {
      kind,
      city,
      source: "current",
    });
    if (result?.ok === false) throw new Error(result.message || "無法產生預覽");
    dom.previewMeta.textContent = `${sourceLabel(result?.source)} · ${formatTime(result?.generated_at)}`;
    const messages = asArray(result?.messages);
    if (messages.length === 0) {
      dom.previewContent.append(emptyState("目前沒有可預覽內容"));
      return;
    }
    messages.forEach((message) => {
      dom.previewContent.append(createElement("pre", "preview-message", message));
    });
  } catch (error) {
    dom.previewMeta.textContent = "預覽失敗";
    dom.previewContent.append(emptyState(error?.message || String(error)));
  }
}

async function sendTest(subscription) {
  const target = findTargetByUmo(subscription.target_umo);
  if (!target) {
    showToast("請先選擇有效的已登記群組");
    return;
  }
  const accepted = await requestConfirmation(
    "發送測試訊息",
    `測試訊息將立即傳送至「${targetLabel(target)}」。`,
    "確認發送",
  );
  if (!accepted) return;

  try {
    const result = await apiPost("test-send", { target_id: targetId(target) });
    if (result?.ok === false) throw new Error(result.message || "發送失敗");
    showToast(`${result?.message || "測試訊息已發送"} · ${formatTime(result?.sent_at)}`);
    await loadState(true);
  } catch (error) {
    showToast(`測試發送失敗：${error?.message || error}`);
  }
}

async function deleteTarget(target) {
  if (state.dirty) {
    showToast("請先儲存或重新整理尚未儲存的設定，再移除群組。");
    return;
  }
  const subscriptions = state.settings.subscriptions.filter(
    (subscription) => subscription.target_umo === target.target_umo,
  );
  const suffix = subscriptions.length
    ? `\n同時會移除 ${subscriptions.length} 筆關聯播報規則。`
    : "";
  const accepted = await requestConfirmation(
    "移除已登記群組",
    `確定移除「${targetLabel(target)}」？${suffix}`,
    "移除",
  );
  if (!accepted) return;

  try {
    const result = await apiPost("targets/delete", {
      target_id: targetId(target),
      revision: state.revision,
    });
    if (result?.ok === false) throw new Error(result.message || "移除失敗");
    state.settings.subscriptions = state.settings.subscriptions.filter(
      (subscription) => subscription.target_umo !== target.target_umo,
    );
    showToast(result?.message || "群組已移除");
    await loadState(true);
  } catch (error) {
    showToast(`移除失敗：${error?.message || error}`);
  }
}

function addRule() {
  const used = new Set(
    state.settings.subscriptions.map((subscription) => subscription.target_umo),
  );
  const target = state.targets.find((candidate) => !used.has(candidate.target_umo));
  if (!target) {
    showToast("所有已登記群組都已有播報規則");
    return;
  }
  state.settings.subscriptions.push(defaultSubscription(target));
  markChanged(true);
}

function bindEvents() {
  dom.refreshButton.addEventListener("click", () => loadState(false));
  dom.saveButton.addEventListener("click", saveSettings);
  dom.addRuleButton.addEventListener("click", addRule);
  dom.sourceTestButton.addEventListener("click", testSource);
  dom.previewClose.addEventListener("click", () => dom.previewDialog.close());

  [
    dom.enabledInput,
    dom.sourceModeInput,
    dom.pollIntervalInput,
    dom.apiKeyInput,
    dom.clearApiKeyInput,
  ].forEach((input) => input.addEventListener("input", () => markChanged()));

  window.addEventListener("beforeunload", (event) => {
    if (!state.dirty) return;
    event.preventDefault();
    event.returnValue = "";
  });
}

async function start() {
  bindEvents();
  if (!bridge) {
    showError("無法連接 AstrBot Plugin Page bridge");
    return;
  }
  try {
    await bridge.ready();
    await loadState(true);
  } catch (error) {
    showError(error?.message || error);
  }
}

start();
