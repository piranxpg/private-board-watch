const apiUrl = "/api/feed?limit=500";
const state = {
  allItems: [],
  sources: [],
  activeSource: "all",
  query: "",
  loading: false,
  error: "",
};

const grid = document.querySelector("#grid");
const statusEl = document.querySelector("#status");
const tabsEl = document.querySelector("#sourceTabs");
const template = document.querySelector("#cardTemplate");
const refreshButton = document.querySelector("#refreshButton");
const blurToggle = document.querySelector("#blurToggle");
const searchInput = document.querySelector("#searchInput");
const ageGate = document.querySelector("#ageGate");
const acceptGate = document.querySelector("#acceptGate");

const gateAcceptedKey = "private-board-watch:adult-confirmed";
const blurKey = "private-board-watch:blur";

init();

function init() {
  const accepted = localStorage.getItem(gateAcceptedKey) === "yes";
  const shouldBlur = localStorage.getItem(blurKey) !== "off";

  blurToggle.checked = shouldBlur;
  document.body.classList.toggle("blurred", shouldBlur);

  if (!accepted) {
    ageGate.hidden = false;
  } else {
    loadFeed();
  }

  acceptGate.addEventListener("click", () => {
    localStorage.setItem(gateAcceptedKey, "yes");
    ageGate.hidden = true;
    loadFeed();
  });

  refreshButton.addEventListener("click", () => loadFeed(true));

  blurToggle.addEventListener("change", () => {
    const shouldBlurNow = blurToggle.checked;
    document.body.classList.toggle("blurred", shouldBlurNow);
    localStorage.setItem(blurKey, shouldBlurNow ? "on" : "off");
  });

  searchInput.addEventListener("input", (event) => {
    state.query = event.target.value.trim().toLowerCase();
    render();
  });
}

async function loadFeed(forceRefresh = false) {
  if (state.loading) return;

  state.loading = true;
  state.error = "";
  statusEl.textContent = "불러오는 중...";
  refreshButton.disabled = true;

  try {
    const response = await fetch(forceRefresh ? `${apiUrl}&refresh=1` : apiUrl, {
      headers: { accept: "application/json" },
    });

    if (!response.ok) {
      throw new Error(`API 오류 ${response.status}`);
    }

    const payload = await response.json();
    state.allItems = Array.isArray(payload.items) ? payload.items : [];
    state.sources = Array.isArray(payload.sources) ? payload.sources : [];
    state.error = payload.message || "";
  } catch (error) {
    state.error = error.message || "알 수 없는 오류";
    state.allItems = [];
    state.sources = [];
  } finally {
    state.loading = false;
    refreshButton.disabled = false;
    render();
  }
}

function render() {
  renderTabs();

  const visibleItems = state.allItems.filter((item) => {
    const sourceOk = state.activeSource === "all" || item.sourceId === state.activeSource;
    const queryOk =
      !state.query ||
      [item.title, item.sourceName].some((value) => String(value || "").toLowerCase().includes(state.query));
    return sourceOk && queryOk;
  });

  const failedSources = state.sources.filter((source) => source.error);
  const loadedSources = state.sources.filter((source) => !source.error && source.enabled !== false);

  if (state.error) {
    statusEl.innerHTML = `<strong>${escapeHtml(state.error)}</strong>`;
  } else if (!state.sources.length) {
    statusEl.textContent = "활성화된 소스가 없습니다. sources.json에서 enabled 값을 확인하세요.";
  } else {
    const loadedText = loadedSources.length ? `${loadedSources.length}개 소스` : "소스 없음";
    const failedText = failedSources.length ? `, 실패 ${failedSources.length}개` : "";
    statusEl.textContent = `${loadedText}${failedText}, ${visibleItems.length}개 표시`;
  }

  grid.innerHTML = "";

  if (!visibleItems.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = state.loading ? "불러오는 중..." : "표시할 썸네일이 없습니다.";
    grid.append(empty);
    return;
  }

  const fragment = document.createDocumentFragment();
  for (const item of visibleItems) {
    fragment.append(createCard(item));
  }
  grid.append(fragment);
}

function renderTabs() {
  const counts = new Map();
  for (const item of state.allItems) {
    counts.set(item.sourceId, (counts.get(item.sourceId) || 0) + 1);
  }

  const tabs = [
    {
      id: "all",
      name: "전체",
      count: state.allItems.length,
    },
    ...state.sources.map((source) => ({
      id: source.id,
      name: source.name || source.id,
      count: counts.get(source.id) || 0,
    })),
  ];

  tabsEl.innerHTML = "";
  for (const tab of tabs) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "tab";
    button.setAttribute("aria-pressed", String(state.activeSource === tab.id));
    button.innerHTML = `${escapeHtml(tab.name)} <span class="count">${tab.count}</span>`;
    button.addEventListener("click", () => {
      state.activeSource = tab.id;
      render();
    });
    tabsEl.append(button);
  }
}

function createCard(item) {
  const node = template.content.firstElementChild.cloneNode(true);
  const image = node.querySelector("img");
  const title = node.querySelector("strong");
  const source = node.querySelector(".source-name");
  const time = node.querySelector("time");

  node.href = item.link;
  image.src = item.imageUrl;
  image.alt = item.title || "";
  image.addEventListener("error", () => {
    image.removeAttribute("src");
    image.alt = "이미지를 불러오지 못했습니다.";
  });
  title.textContent = item.title || "제목 없음";
  source.textContent = item.sourceName || item.sourceId || "source";

  if (item.publishedAt) {
    time.dateTime = item.publishedAt;
    time.textContent = formatDate(item.publishedAt);
  } else {
    time.textContent = "";
  }

  return node;
}

function formatDate(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("ko-KR", {
    timeZone: "Asia/Seoul",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}
