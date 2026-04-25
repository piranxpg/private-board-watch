const DEFAULT_OWNER = "piranxpg";
const DEFAULT_REPO = "private-board-watch";
const DEFAULT_WORKFLOW_ID = "refresh-feed.yml";
const DEFAULT_REF = "main";

function envValue(env, key, fallback) {
  return env[key] || fallback;
}

async function dispatchWorkflow(env, reason) {
  const token = env.GITHUB_ACTIONS_TOKEN;
  if (!token) {
    throw new Error("Missing GITHUB_ACTIONS_TOKEN secret.");
  }

  const owner = envValue(env, "GITHUB_OWNER", DEFAULT_OWNER);
  const repo = envValue(env, "GITHUB_REPO", DEFAULT_REPO);
  const workflowId = envValue(env, "GITHUB_WORKFLOW_ID", DEFAULT_WORKFLOW_ID);
  const ref = envValue(env, "GITHUB_REF", DEFAULT_REF);
  const endpoint = `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${workflowId}/dispatches`;

  const response = await fetch(endpoint, {
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

  const body = await response.text();
  if (!response.ok) {
    throw new Error(`GitHub workflow dispatch failed: ${response.status} ${body}`);
  }

  console.log(`Dispatched ${owner}/${repo}/${workflowId} on ${ref}: ${reason}`);
}

export default {
  async scheduled(controller, env, ctx) {
    ctx.waitUntil(dispatchWorkflow(env, `cron ${controller.cron}`));
  },

  async fetch() {
    return Response.json({
      ok: true,
      service: "private-board-watch-scheduler",
    });
  },
};
