const DEFAULT_OWNER = "piranxpg";
const DEFAULT_REPO = "private-board-watch";
const DEFAULT_WORKFLOW_ID = "refresh-feed.yml";
const DEFAULT_REF = "main";

function envValue(env, key, fallback) {
  return env[key] || fallback;
}

function writeLog(level, event, fields = {}) {
  const record = {
    service: "private-board-watch-scheduler",
    event,
    at: new Date().toISOString(),
    ...fields,
  };
  console[level](JSON.stringify(record));
}

function shortBody(body) {
  if (!body) {
    return "";
  }

  return body.length > 1000 ? `${body.slice(0, 1000)}...` : body;
}

async function dispatchWorkflow(env, reason, scheduledTime) {
  const startedAt = Date.now();
  const token = env.GITHUB_ACTIONS_TOKEN;
  const owner = envValue(env, "GITHUB_OWNER", DEFAULT_OWNER);
  const repo = envValue(env, "GITHUB_REPO", DEFAULT_REPO);
  const workflowId = envValue(env, "GITHUB_WORKFLOW_ID", DEFAULT_WORKFLOW_ID);
  const ref = envValue(env, "GITHUB_REF", DEFAULT_REF);
  const endpoint = `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${workflowId}/dispatches`;
  const context = {
    reason,
    scheduledTime: scheduledTime ? new Date(scheduledTime).toISOString() : undefined,
    owner,
    repo,
    workflowId,
    ref,
  };

  if (!token) {
    writeLog("error", "github_dispatch_missing_secret", context);
    throw new Error("Missing GITHUB_ACTIONS_TOKEN secret");
  }

  writeLog("log", "github_dispatch_start", context);

  let response;
  try {
    response = await fetch(endpoint, {
      method: "POST",
      headers: {
        Accept: "application/vnd.github+json",
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
        "User-Agent": "private-board-watch-scheduler",
        "X-GitHub-Api-Version": "2022-11-28",
      },
      body: JSON.stringify({ ref }),
    });
  } catch (error) {
    writeLog("error", "github_dispatch_network_error", {
      ...context,
      elapsedMs: Date.now() - startedAt,
      message: error instanceof Error ? error.message : String(error),
    });
    throw error;
  }

  const body = await response.text();
  if (!response.ok) {
    writeLog("error", "github_dispatch_failed", {
      ...context,
      elapsedMs: Date.now() - startedAt,
      status: response.status,
      statusText: response.statusText,
      body: shortBody(body),
    });
    throw new Error(`GitHub workflow dispatch failed: ${response.status} ${body}`);
  }

  writeLog("log", "github_dispatch_ok", {
    ...context,
    elapsedMs: Date.now() - startedAt,
    status: response.status,
  });
}

export default {
  async scheduled(controller, env, ctx) {
    ctx.waitUntil(dispatchWorkflow(env, `cron ${controller.cron}`, controller.scheduledTime));
  },

  async fetch() {
    return Response.json({
      ok: true,
      service: "private-board-watch-scheduler",
    });
  },
};
