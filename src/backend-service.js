const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const { spawn } = require("node:child_process");

class BackendService {
  constructor({ executablePath, dataDir, onLog = () => {} }) {
    this.executablePath = executablePath;
    this.dataDir = dataDir;
    this.onLog = onLog;
    this.process = null;
    this.url = null;
    this.lastError = null;
    this.desktopToken = crypto.randomBytes(32).toString("hex");
  }

  async start({
    bindHost,
    publicAddress,
    port,
    printerName = "",
    requireDesktopApproval = false
  }) {
    await this.stop();
    this.lastError = null;
    this.url = `http://${publicAddress}:${port}`;
    fs.mkdirSync(this.dataDir, { recursive: true });

    if (!fs.existsSync(this.executablePath)) {
      throw new Error(`Backend PrintLantern не найден: ${this.executablePath}`);
    }

    const childEnv = {
      ...process.env,
      PRINTLANTERN_DATA_DIR: this.dataDir,
      PRINTLANTERN_HOST: bindHost,
      PRINTLANTERN_PORT: String(port),
      PRINTLANTERN_REQUIRE_DESKTOP_APPROVAL: requireDesktopApproval ? "1" : "0",
      PRINTLANTERN_DESKTOP_TOKEN: this.desktopToken
    };
    if (printerName) childEnv.PRINTLANTERN_PRINTER_NAME = printerName;

    this.process = spawn(this.executablePath, [], {
      cwd: path.dirname(this.executablePath),
      windowsHide: true,
      env: childEnv,
      stdio: ["ignore", "pipe", "pipe"]
    });

    this.process.stdout.on("data", (chunk) => this.onLog(chunk.toString().trim()));
    this.process.stderr.on("data", (chunk) => this.onLog(chunk.toString().trim()));
    this.process.once("exit", (code) => {
      if (code && !this.lastError) {
        this.lastError = `Backend завершился с кодом ${code}.`;
      }
      this.process = null;
    });

    try {
      await this.waitUntilReady(port);
    } catch (error) {
      this.lastError = error.message;
      await this.stop();
      throw error;
    }
    return this.url;
  }

  async waitUntilReady(port) {
    const healthUrl = `http://127.0.0.1:${port}/api/health`;
    const deadline = Date.now() + 20000;
    while (Date.now() < deadline) {
      if (!this.process) {
        throw new Error(this.lastError || "Backend PrintLantern остановился при запуске.");
      }
      try {
        const response = await fetch(healthUrl, { cache: "no-store" });
        if (response.ok) return;
      } catch {
        // The backend may still be extracting or binding its HTTP socket.
      }
      await new Promise((resolve) => setTimeout(resolve, 300));
    }
    throw new Error("Backend PrintLantern не запустился за 20 секунд.");
  }

  async getJobs(port) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/api/jobs`, {
        cache: "no-store"
      });
      if (!response.ok) return [];
      const payload = await response.json();
      return Array.isArray(payload.jobs) ? payload.jobs : [];
    } catch {
      return [];
    }
  }

  async decideJob(port, jobId, decision) {
    if (!["approve", "reject"].includes(decision)) {
      throw new Error("Неизвестное решение по заданию.");
    }
    const response = await fetch(
      `http://127.0.0.1:${port}/api/desktop/${decision}`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-PrintLantern-Desktop-Token": this.desktopToken
        },
        body: JSON.stringify({ job_id: jobId })
      }
    );
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(payload.error || "Не удалось изменить состояние задания.");
    }
    return payload.job;
  }

  async stop() {
    const current = this.process;
    this.process = null;
    this.url = null;
    if (!current || current.exitCode != null) return;

    current.kill();
    await new Promise((resolve) => {
      const timeout = setTimeout(resolve, 2500);
      current.once("exit", () => {
        clearTimeout(timeout);
        resolve();
      });
    });
  }
}

module.exports = { BackendService };
