import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import fs from "node:fs/promises";
import path from "node:path";

const backendPath = path.resolve(
  "backend",
  "dist",
  "printlantern-backend",
  "printlantern-backend.exe"
);
const pngPage =
  "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=";

async function waitForHealth(port) {
  const deadline = Date.now() + 15000;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/api/health`);
      if (response.ok) return response.json();
    } catch {
      // The packaged backend may still be starting.
    }
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  throw new Error(`Backend on port ${port} did not start`);
}

async function runScenario({ port, requireApproval }) {
  const token = `desktop-token-${port}`;
  const dataDir = path.resolve("work", `approval-test-${port}`);
  await fs.mkdir(dataDir, { recursive: true });

  const child = spawn(backendPath, [], {
    windowsHide: true,
    env: {
      ...process.env,
      PRINTLANTERN_DATA_DIR: dataDir,
      PRINTLANTERN_HOST: "127.0.0.1",
      PRINTLANTERN_PORT: String(port),
      PRINTLANTERN_REQUIRE_DESKTOP_APPROVAL: requireApproval ? "1" : "0",
      PRINTLANTERN_DESKTOP_TOKEN: token,
      PRINTLANTERN_TEST_NO_PRINT: "1"
    },
    stdio: "ignore"
  });

  try {
    const health = await waitForHealth(port);
    assert.equal(health.require_desktop_approval, requireApproval);

    const form = new FormData();
    form.append(
      "file",
      new Blob(["PrintLantern approval test"], { type: "text/plain" }),
      `approval-${port}.txt`
    );
    const uploadResponse = await fetch(`http://127.0.0.1:${port}/upload`, {
      method: "POST",
      body: form
    });
    assert.equal(uploadResponse.status, 201);
    const uploaded = await uploadResponse.json();

    const printResponse = await fetch(`http://127.0.0.1:${port}/print`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        job_id: uploaded.job.id,
        copies: 1,
        pages: [pngPage]
      })
    });
    assert.equal(printResponse.status, 200);
    const submitted = await printResponse.json();

    if (requireApproval) {
      assert.equal(submitted.job.status, "pending_approval");

      const forbidden = await fetch(
        `http://127.0.0.1:${port}/api/desktop/approve`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-PrintLantern-Desktop-Token": "wrong-token"
          },
          body: JSON.stringify({ job_id: uploaded.job.id })
        }
      );
      assert.equal(forbidden.status, 403);

      const rejected = await fetch(
        `http://127.0.0.1:${port}/api/desktop/reject`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-PrintLantern-Desktop-Token": token
          },
          body: JSON.stringify({ job_id: uploaded.job.id })
        }
      );
      assert.equal(rejected.status, 200);
      assert.equal((await rejected.json()).job.status, "cancelled");
      return;
    }

    assert.ok(["queued", "printing", "completed"].includes(submitted.job.status));
    const deadline = Date.now() + 5000;
    while (Date.now() < deadline) {
      const jobs = await (
        await fetch(`http://127.0.0.1:${port}/api/jobs`)
      ).json();
      const job = jobs.jobs.find((item) => item.id === uploaded.job.id);
      if (job?.status === "completed") return;
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    throw new Error("Direct-print test job did not complete");
  } finally {
    child.kill();
  }
}

await runScenario({ port: 4881, requireApproval: true });
await runScenario({ port: 4882, requireApproval: false });
console.log("Backend approval and direct-print modes passed.");
