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
  groupSearch: document.getElementById("group-search"),
  groupFilter: document.getElementById("group-filter"),
  rulesContainer: document.getElementById("rules-container"),
  rulesSummary: document.getElementById("rules-summary"),
  sourceTestInput: document.getElementById("source-test-input"),
  sourceTestButton: document.getElementById("source-test-button"),
  sourceDetail: document.getElementById("source-detail"),
  sourceTestResult: document.getElementById("source-test-result"),
  deliveryGroup: document.getElementById("delivery-group"),
  deliveryFilter: document.getElementById("delivery-filter"),
  deliveryKind: document.getElementById("delivery-kind"),
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
  mediaOptions: [],
  groupPage: 0,
  deliveryPage: 0,
  openGroup: "",
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
  document.querySelector(".operations-main").inert = value;
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
      media_types: asArray(subscription?.media_types).map(String),
    })),
  };
}

function defaultSubscription(target) {
  return {
    id: makeId(),
    target_umo: String(target?.target_umo || ""),
    enabled: false,
    cities: [],
    forecast_enabled: false,
    forecast_times: ["07:30", "18:30"],
    alert_types: [],
    media_types: [],
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
  state.mediaOptions = Object.entries(payload?.media_types || {});
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
  renderRules();
  const selectedGroup = dom.deliveryGroup.value;
  dom.deliveryGroup.replaceChildren(new Option("所有群組", ""));
  for (const umo of new Set([
    ...state.targets.map((target) => target.target_umo),
    ...state.deliveryStatus.map((record) => record.target_umo),
  ])) {
    if (!umo) continue;
    const target = findTargetByUmo(umo);
    dom.deliveryGroup.add(new Option(target ? targetLabel(target) : umo, umo));
  }
  if (Array.from(dom.deliveryGroup.options).some((option) => option.value === selectedGroup)) {
    dom.deliveryGroup.value = selectedGroup;
  }
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

function renderRule(subscription, target, onChange) {
  const body = createElement("div", "rule-body");
  const header = createElement("div", "rule-toolbar");
  const enabledField = createElement("label", "field field-inline");
  enabledField.append(createElement("span", "", "啟用本群組播報"));
  const enabledInput = document.createElement("input");
  enabledInput.type = "checkbox";
  enabledInput.checked = subscription.enabled;
  enabledInput.role = "switch";
  enabledInput.addEventListener("change", () => {
    subscription.enabled = enabledInput.checked;
    onChange();
  });
  enabledField.append(enabledInput);
  header.append(enabledField);

  const removeButton = createElement("button", "button button-quiet-danger button-small", "移除群組");
  removeButton.type = "button";
  removeButton.addEventListener("click", () => deleteTarget(target));
  header.append(removeButton);

  body.append(header);

  const citiesSection = createElement("section", "rule-section");
  const citiesHeading = createElement("div", "rule-section-heading");
  citiesHeading.append(
    createElement("span", "", "縣市（定時天氣與地方警報）"),
  );
  citiesSection.append(citiesHeading);
  citiesSection.append(
    createCheckboxGrid(state.cityOptions, subscription.cities, (values) => {
      subscription.cities = values;
      onChange();
    }),
  );
  body.append(citiesSection);

  const alertsSection = createElement("section", "rule-section");
  const alertsHeading = createElement("div", "rule-section-heading");
  alertsHeading.append(
    createElement("span", "", "災害與警特報"),
  );
  alertsSection.append(alertsHeading);
  alertsSection.append(
    createCheckboxGrid(state.alertOptions, subscription.alert_types, (values) => {
      subscription.alert_types = values;
      onChange();
    }),
  );
  body.append(alertsSection);

  const mediaSection = createElement("section", "rule-section");
  mediaSection.append(
    createElement("div", "rule-section-heading", "官方圖資（隨對應的災害警報傳送）"),
    createCheckboxGrid(state.mediaOptions, subscription.media_types, (values) => {
      subscription.media_types = values;
      onChange();
    }),
  );
  mediaSection.classList.add("media-options");
  body.append(mediaSection);

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
    onChange(true);
  });
  forecastHeading.append(forecastToggle, addTimeButton);
  forecastSection.append(forecastHeading, createTimeList(subscription));
  forecastInput.addEventListener("change", () => {
    subscription.forecast_enabled = forecastInput.checked;
    if (subscription.forecast_enabled && subscription.forecast_times.length === 0) {
      subscription.forecast_times.push("07:30");
    }
    onChange(true);
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
  testSend.disabled = !targetId(target) || target.available === false;
  testSend.addEventListener("click", () => sendTest(subscription));
  actions.append(weatherPreview, alertsPreview, testSend);
  body.append(actions);

  return body;
}

function renderRules() {
  dom.rulesContainer.replaceChildren();
  const subscriptions = state.settings?.subscriptions || [];
  const rules = new Map(subscriptions.map((rule) => [rule.target_umo, rule]));
  const search = dom.groupSearch.value.trim().toLowerCase();
  const filter = dom.groupFilter.value;
  const matches = state.targets.filter((target) => {
    const enabled = rules.get(target.target_umo)?.enabled;
    return (!filter || (filter === "enabled") === Boolean(enabled)) &&
      [targetLabel(target), target.group_id, target.platform_type, target.platform_id]
        .some((value) => String(value || "").toLowerCase().includes(search));
  });
  dom.rulesSummary.textContent = `${matches.length}／${state.targets.length} 個群組`;
  const page = listPage("group", matches, 12);
  if (page.length === 0) {
    dom.rulesContainer.append(emptyState(
      state.targets.length ? "找不到符合條件的群組" : "請先在群組內輸入 /cwa_register",
    ));
    return;
  }

  for (const target of page) {
    const umo = target.target_umo;
    const rule = rules.get(umo) || defaultSubscription(target);
    const item = createElement("details", "rule-item");
    item.name = "broadcast-groups";
    const summary = createElement("summary", "group-summary");
    const info = createElement("div", "group-info");
    const meta = createElement("div", "metadata-text");
    info.append(createElement("div", "target-name", targetLabel(target)), meta);
    const badge = createElement("span", "status-pill");
    badge.title = String(target.availability_message || "");
    const updateSummary = () => {
      meta.textContent = [
        target.platform_type || target.platform_id,
        target.group_id,
        ...rule.cities.slice(0, 2),
        rule.cities.length > 2 ? `等 ${rule.cities.length} 縣市` : "",
        rule.alert_types.length ? `${rule.alert_types.length} 種警報` : "",
        rule.media_types.length ? `${rule.media_types.length} 種圖資` : "",
      ].filter(Boolean).join(" · ");
      if (target.available === false) {
        setStatusPill(badge, "平台不可用", "warning");
      } else {
        setStatusPill(badge, rule.enabled ? "已啟用" : "未啟用", rule.enabled ? "success" : "neutral");
      }
    };
    updateSummary();
    summary.append(info, badge);
    item.append(summary);
    const showEditor = () => {
      if (item.children.length > 1) return;
      item.append(renderRule(rule, target, (rerender = false) => {
        if (!rules.has(umo)) {
          rules.set(umo, rule);
          state.settings.subscriptions.push(rule);
        }
        updateSummary();
        markChanged(rerender);
      }));
    };
    if (state.openGroup === umo) {
      item.open = true;
      showEditor();
    }
    item.addEventListener("toggle", () => {
      if (!item.isConnected) return;
      if (item.open) {
        state.openGroup = umo;
        showEditor();
        for (const other of dom.rulesContainer.querySelectorAll("details[open]")) {
          if (other !== item) other.open = false;
        }
      } else if (state.openGroup === umo) {
        state.openGroup = "";
      }
    });
    dom.rulesContainer.append(item);
  }
}

function listPage(name, items, size) {
  const key = `${name}Page`;
  const pages = Math.max(1, Math.ceil(items.length / size));
  state[key] = Math.max(0, Math.min(state[key], pages - 1));
  const index = state[key];
  document.getElementById(`${name}-pagination`).hidden = pages === 1;
  document.getElementById(`${name}-page-label`).textContent = `${index + 1}／${pages} 頁`;
  document.getElementById(`${name}-previous`).disabled = index === 0;
  document.getElementById(`${name}-next`).disabled = index >= pages - 1;
  return items.slice(index * size, (index + 1) * size);
}

function renderDelivery() {
  dom.deliveryBody.replaceChildren();
  const kind = dom.deliveryKind.value;
  const matches = state.deliveryStatus.filter((record) => {
    const media = record.part !== "text";
    const type = media ? "media" : record.kind || (
      record.event_fingerprint?.startsWith("forecast:") ? "weather" : "alerts"
    );
    const groupMatches = !dom.deliveryGroup.value || record.target_umo === dom.deliveryGroup.value;
    const statusMatches = !dom.deliveryFilter.value || record.status === dom.deliveryFilter.value;
    const kindMatches = !kind || type === kind ||
      (kind === "alerts" && !["media", "weather", "test"].includes(type));
    return groupMatches && statusMatches && kindMatches;
  });
  const records = listPage("delivery", matches, 12);
  dom.deliverySummary.textContent = `${matches.length}／${state.deliveryStatus.length} 筆`;

  if (records.length === 0) {
    const row = document.createElement("tr");
    const cell = createElement(
      "td", "empty-state", state.deliveryStatus.length ? "沒有符合條件的傳送紀錄" : "尚無傳送紀錄",
    );
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
    const details = createElement("td");
    const open = createElement("button", "button button-secondary button-small", "查看");
    open.type = "button";
    open.addEventListener("click", () => viewDelivery(record, target));
    details.append(open);
    const label = record.title ||
      (record.part === "image" ? "颱風警報單" : record.part === "text" ? "文字播報" : "官方圖資");
    row.append(
      createElement("td", "", formatTime(record.last_attempt_at || record.delivered_at)),
      createElement("td", "", target ? targetLabel(target) : record.target_umo || "--"),
      createElement("td", "delivery-title", label),
      statusCell,
      createElement("td", "", String(record.attempts || 0)),
      details,
    );
    dom.deliveryBody.append(row);
  });
}

async function viewDelivery(record, target) {
  dom.previewTitle.textContent = record.title || "傳送紀錄";
  dom.previewMeta.textContent = [
    target ? targetLabel(target) : record.target_umo, formatTime(record.last_attempt_at),
  ].join(" · ");
  dom.previewContent.replaceChildren(emptyState("載入中…"));
  dom.previewDialog.showModal();
  try {
    const result = await apiPost("delivery/detail", { id: record.id });
    if (!result?.record) throw new Error(result?.message || "無法讀取傳送紀錄");
    const detail = result.record;
    const snapshot = detail.content;
    dom.previewContent.replaceChildren();
    if (detail.error) {
      dom.previewContent.append(createElement("p", "cell-error", detail.error));
    }
    if (!snapshot) {
      dom.previewContent.append(emptyState("此紀錄尚未保存訊息內容。"));
      return;
    }
    const chunks = asArray(snapshot.chunks);
    const accepted = detail.status === "success" ? chunks.length : (snapshot.accepted || 0);
    if (snapshot.media_name) {
      const label = detail.status === "success" ? "已交付附件" : "嘗試傳送的附件";
      dom.previewContent.append(createElement("p", "", `${label}：${snapshot.media_name}`));
    }
    if (!chunks.length && !snapshot.media_name) {
      dom.previewContent.append(emptyState("沒有可預覽的傳送內容。"));
    }
    chunks.forEach((chunk, index) => {
      if (detail.status !== "success" || chunks.length > 1) {
        const status = index < accepted ? "已交付平台" : "尚未交付";
        dom.previewContent.append(createElement("p", "metadata-text", `第 ${index + 1} 段 · ${status}`));
      }
      dom.previewContent.append(createElement("pre", "preview-message", chunk));
    });
  } catch (error) {
    dom.previewContent.replaceChildren(emptyState(error?.message || String(error)));
  }
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
    media_types: Array.from(new Set(subscription.media_types)),
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
  for (const subscription of payload.subscriptions) {
    const label = targetLabel(findTargetByUmo(subscription.target_umo));
    if (!subscription.target_umo || (subscription.enabled && !registeredUmos.has(subscription.target_umo))) {
      return `「${label}」尚未登記。`;
    }
    if (selectedUmos.has(subscription.target_umo)) {
      return `「${targetLabel(findTargetByUmo(subscription.target_umo))}」有重複規則。`;
    }
    selectedUmos.add(subscription.target_umo);
    if (subscription.forecast_enabled && subscription.cities.length === 0) {
      return `請為「${label}」選擇定時天氣的縣市。`;
    }
    if (subscription.forecast_enabled && subscription.forecast_times.length === 0) {
      return `請為「${label}」設定定時天氣的時刻。`;
    }
    if (subscription.enabled && subscription.alert_types.length === 0 && !subscription.forecast_enabled) {
      return `請為「${label}」選擇播報內容。`;
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
    if (result?.ok === false) {
      if (!state.dirty) await loadState(true);
      throw new Error(result.message || "發送失敗");
    }
    showToast(`${result?.message || "測試訊息已發送"} · ${formatTime(result?.sent_at)}`);
    if (!state.dirty) await loadState(true);
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

function bindEvents() {
  dom.refreshButton.addEventListener("click", () => loadState(false));
  dom.saveButton.addEventListener("click", saveSettings);
  dom.sourceTestButton.addEventListener("click", testSource);
  dom.previewClose.addEventListener("click", () => dom.previewDialog.close());
  dom.groupSearch.addEventListener("input", () => {
    state.groupPage = 0;
    renderRules();
  });
  dom.groupFilter.addEventListener("change", () => {
    state.groupPage = 0;
    renderRules();
  });
  for (const filter of [dom.deliveryGroup, dom.deliveryFilter, dom.deliveryKind]) {
    filter.addEventListener("change", () => {
      state.deliveryPage = 0;
      renderDelivery();
    });
  }
  for (const [name, render] of [["group", renderRules], ["delivery", renderDelivery]]) {
    document.getElementById(`${name}-previous`).addEventListener("click", () => {
      state[`${name}Page`] -= 1;
      render();
    });
    document.getElementById(`${name}-next`).addEventListener("click", () => {
      state[`${name}Page`] += 1;
      render();
    });
  }

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
