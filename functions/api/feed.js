import { XMLParser } from "fast-xml-parser";

const parser = new XMLParser({
  ignoreAttributes: false,
  attributeNamePrefix: "@",
  textNodeName: "#text",
  cdataPropName: "#cdata",
  trimValues: true,
  parseTagValue: false,
});

const imageExtensionPattern = /\.(avif|gif|jpe?g|png|webp)(?:$|[?#])/i;
const defaultBlockedKeywords = [
  "미성년",
  "중딩",
  "고딩",
  "학생",
  "몰카",
  "도촬",
  "리벤지",
  "강간",
  "child",
  "kid",
  "teen",
  "underage",
  "minor",
  "lolita",
];

export async function onRequestGet(context) {
  const requestUrl = new URL(context.request.url);
  const config = await loadConfig(context);
  const refresh = requestUrl.searchParams.has("refresh");
  const limit = clamp(toNumber(requestUrl.searchParams.get("limit"), 80), 1, 200);
  const selectedSource = requestUrl.searchParams.get("source") || "all";
  const cacheSeconds = clamp(toNumber(config.settings.cacheSeconds, 300), 0, 3600);

  if (!refresh && cacheSeconds > 0) {
    const cached = await caches.default.match(context.request);
    if (cached) return cached;
  }

  const kvPayload = await readKvPayload(context, selectedSource, limit);
  if (kvPayload) {
    const response = jsonResponse(kvPayload, {
      "Cache-Control": `public, max-age=${cacheSeconds}`,
    });
    if (!refresh && cacheSeconds > 0) {
      context.waitUntil(caches.default.put(context.request, response.clone()));
    }
    return response;
  }

  const maxSources = clamp(toNumber(config.settings.maxSources, 10), 1, 50);
  const enabledSources = normalizeSources(config.sources)
    .filter((source) => source.enabled !== false)
    .sort((left, right) => left.rank - right.rank)
    .slice(0, maxSources);
  const activeSources =
    selectedSource === "all"
      ? enabledSources
      : enabledSources.filter((source) => source.id === selectedSource);

  if (!activeSources.length) {
    return jsonResponse({
      generatedAt: new Date().toISOString(),
      message: "활성화된 소스가 없습니다.",
      sources: enabledSources.map(sourceSummary),
      items: [],
    });
  }

  const settled = await Promise.allSettled(activeSources.map((source) => readSource(source, config)));
  const sourceResults = [];
  const items = [];

  for (let index = 0; index < settled.length; index += 1) {
    const source = activeSources[index];
    const result = settled[index];

    if (result.status === "fulfilled") {
      sourceResults.push({
        ...sourceSummary(source),
        count: result.value.items.length,
      });
      items.push(...result.value.items);
    } else {
      sourceResults.push({
        ...sourceSummary(source),
        count: 0,
        error: result.reason?.message || "소스를 불러오지 못했습니다.",
      });
    }
  }

  const payload = {
    generatedAt: new Date().toISOString(),
    sources: sourceResults,
    items: dedupeItems(items)
      .sort((left, right) => dateScore(right.publishedAt) - dateScore(left.publishedAt))
      .slice(0, limit),
  };

  const response = jsonResponse(payload, {
    "Cache-Control": `public, max-age=${cacheSeconds}`,
  });

  if (!refresh && cacheSeconds > 0) {
    context.waitUntil(caches.default.put(context.request, response.clone()));
  }

  return response;
}

async function loadConfig(context) {
  let raw = context.env.SOURCES_JSON;

  if (!raw && context.env.ASSETS) {
    const configUrl = new URL("/sources.json", context.request.url);
    const response = await context.env.ASSETS.fetch(new Request(configUrl.toString(), context.request));
    if (response.ok) raw = await response.text();
  }

  const parsed = raw ? JSON.parse(raw) : {};
  return {
    settings: {
      maxSources: 10,
      maxItemsPerSource: 24,
      requestTimeoutMs: 8000,
      cacheSeconds: 300,
      requireHttps: true,
      allowUnknownImageTypes: true,
      ...(parsed.settings || {}),
    },
    safety: {
      blockedKeywords: defaultBlockedKeywords,
      allowedImageDomains: [],
      allowedLinkDomains: [],
      ...(parsed.safety || {}),
    },
    sources: Array.isArray(parsed) ? parsed : parsed.sources || [],
  };
}

async function readKvPayload(context, selectedSource, limit) {
  const namespace = context.env.FEED_KV;
  if (!namespace) return null;

  const key = context.env.FEED_KV_KEY || context.env.KV_KEY || "feed:latest";
  const raw = await namespace.get(key);
  if (!raw) return null;

  const payload = JSON.parse(raw);
  const items = asArray(payload.items)
    .filter((item) => selectedSource === "all" || item.sourceId === selectedSource)
    .sort((left, right) => dateScore(right.publishedAt) - dateScore(left.publishedAt))
    .slice(0, limit);
  const sources = asArray(payload.sources).length ? asArray(payload.sources) : summarizeSourcesFromItems(payload.items);

  return {
    generatedAt: payload.generatedAt || new Date().toISOString(),
    fromKv: true,
    sources,
    items,
  };
}

function summarizeSourcesFromItems(items) {
  const sources = new Map();
  for (const item of asArray(items)) {
    if (!item.sourceId) continue;
    if (!sources.has(item.sourceId)) {
      sources.set(item.sourceId, {
        id: item.sourceId,
        name: item.sourceName || item.sourceId,
        enabled: true,
        count: 0,
      });
    }
    sources.get(item.sourceId).count += 1;
  }
  return Array.from(sources.values());
}

async function readSource(source, config) {
  const sourceUrl = new URL(source.url);
  if (config.settings.requireHttps && sourceUrl.protocol !== "https:") {
    throw new Error("HTTPS 소스만 허용됩니다.");
  }

  const timeoutMs = clamp(toNumber(source.timeoutMs, config.settings.requestTimeoutMs), 1000, 20000);
  const response = await fetchWithTimeout(sourceUrl.toString(), timeoutMs);
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }

  const contentType = response.headers.get("content-type") || "";
  const body = await response.text();
  const type = source.type || guessSourceType(contentType, body);
  const rawItems = parseItems(type, body, source);
  const maxItems = clamp(toNumber(source.maxItems, config.settings.maxItemsPerSource), 1, 100);

  const items = rawItems
    .map((item) => normalizeItem(item, source, config))
    .filter(Boolean)
    .slice(0, maxItems);

  return { items };
}

function parseItems(type, body, source) {
  if (type === "jsonfeed") return parseJsonFeed(body, source);
  if (type === "reddit") return parseRedditFeed(body, source);
  return parseXmlFeed(body, source);
}

function parseXmlFeed(body, source) {
  const document = parser.parse(body);
  const channel = document.rss?.channel || document.feed || document["rdf:RDF"] || {};
  const rawItems = asArray(channel.item || channel.entry || document.feed?.entry || document["rdf:RDF"]?.item);

  return rawItems.map((item) => {
    const html = [item.description, item["content:encoded"], item.summary, item.content]
      .map(textValue)
      .filter(Boolean)
      .join(" ");

    return {
      title: textValue(item.title),
      link: extractXmlLink(item.link, source.homepage || source.url),
      imageUrl: extractMediaImage(item, source.homepage || source.url) || extractImageFromHtml(html, source.homepage || source.url),
      publishedAt: textValue(item.pubDate || item.published || item.updated || item["dc:date"]),
    };
  });
}

function parseJsonFeed(body, source) {
  const document = JSON.parse(body);
  const items = Array.isArray(document.items) ? document.items : [];

  return items.map((item) => {
    const html = [item.content_html, item.summary, item.content_text].filter(Boolean).join(" ");
    const attachmentImage = asArray(item.attachments).find((attachment) =>
      String(attachment?.mime_type || "").startsWith("image/")
    );

    return {
      title: item.title || item.summary || item.url,
      link: item.external_url || item.url,
      imageUrl: item.image || item.banner_image || attachmentImage?.url || extractImageFromHtml(html, source.homepage || source.url),
      publishedAt: item.date_published || item.date_modified,
    };
  });
}

function parseRedditFeed(body, source) {
  const document = JSON.parse(body);
  const posts = asArray(document?.data?.children).map((child) => child?.data).filter(Boolean);

  return posts.map((post) => {
    const preview = post.preview?.images?.[0]?.source?.url;
    const thumbnail = /^https?:\/\//i.test(post.thumbnail || "") ? post.thumbnail : "";
    const destination = post.url_overridden_by_dest || post.url || post.permalink;

    return {
      title: post.title,
      link: makeAbsoluteUrl(post.permalink || destination, source.homepage || "https://www.reddit.com"),
      imageUrl: decodeEntities(preview || thumbnail || destination),
      publishedAt: post.created_utc ? new Date(post.created_utc * 1000).toISOString() : "",
    };
  });
}

function normalizeItem(item, source, config) {
  const baseUrl = source.homepage || source.url;
  const title = stripTags(decodeEntities(item.title || "")).trim();
  const link = makeAbsoluteUrl(item.link, baseUrl);
  const imageUrl = makeAbsoluteUrl(decodeEntities(item.imageUrl || ""), baseUrl);

  if (!title || !link || !imageUrl) return null;
  if (!passesProtocol(link, config) || !passesProtocol(imageUrl, config)) return null;
  if (!passesDomain(link, [...config.safety.allowedLinkDomains, ...(source.allowedLinkDomains || [])])) return null;
  if (!passesDomain(imageUrl, [...config.safety.allowedImageDomains, ...(source.allowedImageDomains || [])])) return null;
  if (!passesImageCheck(imageUrl, source, config)) return null;
  if (hasBlockedKeyword(`${title} ${link}`, config.safety.blockedKeywords)) return null;

  return {
    id: `${source.id}:${hashString(link + imageUrl)}`,
    sourceId: source.id,
    sourceName: source.name || source.id,
    title,
    link,
    imageUrl,
    publishedAt: normalizeDate(item.publishedAt),
  };
}

function extractXmlLink(link, baseUrl) {
  const links = asArray(link);
  const alternate = links.find((entry) => typeof entry === "object" && (!entry["@rel"] || entry["@rel"] === "alternate"));
  return makeAbsoluteUrl(textValue(alternate?.["@href"] || alternate || links[0]), baseUrl);
}

function extractMediaImage(item, baseUrl) {
  const candidates = [
    item["media:thumbnail"],
    item["media:content"],
    item.enclosure,
    item.image,
    item["itunes:image"],
  ];

  for (const candidate of candidates.flatMap(asArray)) {
    const url = textValue(candidate?.["@url"] || candidate?.["@href"] || candidate?.url || candidate);
    if (url) return makeAbsoluteUrl(url, baseUrl);
  }

  return "";
}

function extractImageFromHtml(html, baseUrl) {
  const imageMatch = String(html || "").match(/<img\b[^>]*\bsrc=["']([^"']+)["'][^>]*>/i);
  if (imageMatch) return makeAbsoluteUrl(decodeEntities(imageMatch[1]), baseUrl);

  const anchorMatch = String(html || "").match(/href=["']([^"']+\.(?:avif|gif|jpe?g|png|webp)(?:\?[^"']*)?)["']/i);
  if (anchorMatch) return makeAbsoluteUrl(decodeEntities(anchorMatch[1]), baseUrl);

  return "";
}

async function fetchWithTimeout(url, timeoutMs) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort("timeout"), timeoutMs);

  try {
    return await fetch(url, {
      signal: controller.signal,
      headers: {
        accept: "application/rss+xml, application/atom+xml, application/feed+json, application/json, text/xml;q=0.9, */*;q=0.7",
      },
    });
  } finally {
    clearTimeout(timeout);
  }
}

function normalizeSources(sources) {
  return asArray(sources)
    .filter((source) => source && source.id && source.url)
    .map((source, index) => ({
      ...source,
      rank: toNumber(source.rank, index + 1),
    }));
}

function sourceSummary(source) {
  return {
    id: source.id,
    name: source.name || source.id,
    rank: source.rank,
    enabled: source.enabled !== false,
  };
}

function guessSourceType(contentType, body) {
  if (/json/i.test(contentType) || /^\s*[{[]/.test(body)) return "jsonfeed";
  return "rss";
}

function passesProtocol(value, config) {
  try {
    const url = new URL(value);
    return !config.settings.requireHttps || url.protocol === "https:";
  } catch {
    return false;
  }
}

function passesDomain(value, allowedDomains) {
  if (!allowedDomains.length) return true;
  const hostname = new URL(value).hostname.toLowerCase();
  return allowedDomains.some((domain) => {
    const normalized = String(domain).toLowerCase();
    return hostname === normalized || hostname.endsWith(`.${normalized}`);
  });
}

function passesImageCheck(value, source, config) {
  const allowUnknown =
    source.allowUnknownImageTypes ?? config.settings.allowUnknownImageTypes ?? true;
  if (allowUnknown) return true;
  return imageExtensionPattern.test(new URL(value).pathname);
}

function hasBlockedKeyword(value, keywords) {
  const haystack = String(value || "").toLowerCase();
  return asArray(keywords).some((keyword) => keyword && haystack.includes(String(keyword).toLowerCase()));
}

function dedupeItems(items) {
  const seen = new Set();
  return items.filter((item) => {
    const key = item.link || item.imageUrl;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function textValue(value) {
  if (!value) return "";
  if (typeof value === "string" || typeof value === "number") return String(value);
  if (Array.isArray(value)) return textValue(value[0]);
  return textValue(value["#cdata"] || value["#text"] || value["@href"] || value["@url"] || value.url || value.link);
}

function stripTags(value) {
  return String(value || "").replace(/<[^>]*>/g, " ");
}

function decodeEntities(value) {
  return String(value || "")
    .replace(/&#(\d+);/g, (_, code) => String.fromCodePoint(Number(code)))
    .replace(/&#x([0-9a-f]+);/gi, (_, code) => String.fromCodePoint(Number.parseInt(code, 16)))
    .replaceAll("&amp;", "&")
    .replaceAll("&quot;", '"')
    .replaceAll("&#39;", "'")
    .replaceAll("&lt;", "<")
    .replaceAll("&gt;", ">");
}

function makeAbsoluteUrl(value, baseUrl) {
  if (!value) return "";
  try {
    return new URL(String(value).trim(), baseUrl).toString();
  } catch {
    return "";
  }
}

function normalizeDate(value) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : date.toISOString();
}

function dateScore(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? 0 : date.getTime();
}

function hashString(value) {
  let hash = 5381;
  for (let index = 0; index < value.length; index += 1) {
    hash = (hash * 33) ^ value.charCodeAt(index);
  }
  return (hash >>> 0).toString(36);
}

function asArray(value) {
  if (!value) return [];
  return Array.isArray(value) ? value : [value];
}

function toNumber(value, fallback) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function clamp(value, min, max) {
  return Math.min(Math.max(value, min), max);
}

function jsonResponse(body, headers = {}) {
  return new Response(JSON.stringify(body, null, 2), {
    headers: {
      "content-type": "application/json; charset=utf-8",
      ...headers,
    },
  });
}
