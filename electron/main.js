const fs = require("node:fs");
const path = require("node:path");
const { app, BrowserWindow, ipcMain, shell } = require("electron");
const QRCode = require("qrcode");
const { listLanAddresses, normalizePort, resolvePublicAddress } = require("../src/network");
const { readPrinterSnapshot } = require("../src/printer-service");
const { BackendService } = require("../src/backend-service");

const DEFAULT_SETTINGS = {
  lanEnabled: true,
  preferredAddress: "auto",
  port: 4876,
  printerName: "",
  requireDesktopApproval: false
};

let mainWindow;
let backend;
let settings = { ...DEFAULT_SETTINGS };
let lastSnapshot = {
  printers: [],
  jobs: [],
  available: true,
  message: null
};
let portalJobs = [];
let lastUpdatedAt = null;
let serverError = null;
let refreshTimer;

function settingsPath() {
  return path.join(app.getPath("userData"), "settings.json");
}

function loadSettings() {
  try {
    const stored = JSON.parse(fs.readFileSync(settingsPath(), "utf8"));
    settings = {
      ...DEFAULT_SETTINGS,
      ...stored,
      port: normalizePort(stored.port ?? DEFAULT_SETTINGS.port)
    };
  } catch {
    settings = { ...DEFAULT_SETTINGS };
  }
}

function saveSettings() {
  fs.mkdirSync(path.dirname(settingsPath()), { recursive: true });
  fs.writeFileSync(settingsPath(), JSON.stringify(settings, null, 2));
}

function networkState() {
  const addresses = listLanAddresses();
  const publicAddress = settings.lanEnabled
    ? resolvePublicAddress(settings.preferredAddress, addresses)
    : "127.0.0.1";
  const bindHost = settings.lanEnabled ? "0.0.0.0" : "127.0.0.1";
  const url = backend?.url || `http://${publicAddress}:${settings.port}`;
  return { addresses, publicAddress, bindHost, url };
}

function resolveBackendExecutable() {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, "backend", "printlantern-backend.exe");
  }
  return path.join(__dirname, "..", "backend", "dist", "printlantern-backend", "printlantern-backend.exe");
}

function resolvePortalDataDir() {
  const legacyDir = path.join(
    app.getPath("documents"),
    "Mnecraft PlatonTehnology",
    "printer_portal",
    "data"
  );
  if (fs.existsSync(path.join(legacyDir, "config.json"))) return legacyDir;
  return path.join(app.getPath("userData"), "portal-data");
}

function mapPortalJob(job) {
  return {
    id: job.id,
    printerName: settings.printerName || "Принтер Windows",
    documentName: job.original_name || "Без названия",
    status: job.status || "draft",
    submittedAt: job.created_at || null,
    updatedAt: job.updated_at || null,
    totalPages: Number(job.prepared_count || 0),
    pagesPrinted: job.status === "completed" ? Number(job.prepared_count || 0) : 0,
    progress: Number(job.progress || 0),
    size: Number(job.size || 0),
    message: job.message || "",
    previewMode: job.preview_mode || null,
    canOpen: ["draft", "failed", "interrupted", "cancelled"].includes(job.status),
    awaitingApproval: job.status === "pending_approval"
  };
}

async function getStatus({ includeQr = false } = {}) {
  const network = networkState();
  const status = {
    appName: "PrintLantern",
    version: app.getVersion(),
    settings,
    network: {
      addresses: network.addresses,
      url: network.url,
      running: Boolean(backend?.process),
      error: serverError
    },
    printers: lastSnapshot.printers,
    jobs: portalJobs.map(mapPortalJob),
    systemJobs: lastSnapshot.jobs,
    printerService: {
      available: lastSnapshot.available,
      message: lastSnapshot.message
    },
    lastUpdatedAt
  };

  if (includeQr && backend?.process) {
    status.network.qrDataUrl = await QRCode.toDataURL(network.url, {
      width: 320,
      margin: 1,
      color: { dark: "#10211b", light: "#ffffff" }
    });
  }
  return status;
}

async function readAndBroadcast() {
  [lastSnapshot, portalJobs] = await Promise.all([
    readPrinterSnapshot(),
    backend?.getJobs(settings.port) || []
  ]);
  lastUpdatedAt = new Date().toISOString();
  const status = await getStatus({ includeQr: true });
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send("status:changed", status);
  }
  return status;
}

async function restartPortal() {
  serverError = null;
  const network = networkState();
  try {
    await backend.start({
      bindHost: network.bindHost,
      publicAddress: network.publicAddress,
      port: settings.port,
      printerName: settings.printerName,
      requireDesktopApproval: settings.requireDesktopApproval
    });
  } catch (error) {
    serverError =
      error.code === "EADDRINUSE"
        ? `Порт ${settings.port} уже занят другой программой.`
        : "Локальный портал не удалось запустить.";
  }
}

async function updateSettings(patch) {
  const next = { ...settings };
  if (Object.hasOwn(patch, "lanEnabled")) next.lanEnabled = Boolean(patch.lanEnabled);
  if (Object.hasOwn(patch, "preferredAddress")) {
    next.preferredAddress = String(patch.preferredAddress || "auto");
  }
  if (Object.hasOwn(patch, "port")) next.port = normalizePort(patch.port);
  if (Object.hasOwn(patch, "printerName")) next.printerName = String(patch.printerName || "");
  if (Object.hasOwn(patch, "requireDesktopApproval")) {
    next.requireDesktopApproval = Boolean(patch.requireDesktopApproval);
  }
  settings = next;
  saveSettings();
  await restartPortal();
  return getStatus({ includeQr: true });
}

async function decidePortalJob(jobId, decision) {
  await backend.decideJob(settings.port, String(jobId || ""), decision);
  return readAndBroadcast();
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1180,
    height: 780,
    minWidth: 900,
    minHeight: 640,
    backgroundColor: "#f5f2e9",
    title: "PrintLantern",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true
    }
  });
  mainWindow.setMenuBarVisibility(false);
  mainWindow.loadFile(path.join(__dirname, "..", "renderer", "index.html"));
}

app.whenReady().then(async () => {
  loadSettings();
  backend = new BackendService({
    executablePath: resolveBackendExecutable(),
    dataDir: resolvePortalDataDir(),
    onLog: (message) => {
      if (message) console.log(`[portal] ${message}`);
    }
  });

  ipcMain.handle("app:get-status", () => getStatus({ includeQr: true }));
  ipcMain.handle("app:refresh", () => readAndBroadcast());
  ipcMain.handle("settings:update", (_event, patch) => updateSettings(patch || {}));
  ipcMain.handle("job:decide", (_event, jobId, decision) =>
    decidePortalJob(jobId, decision)
  );
  ipcMain.handle("shell:open-external", (_event, url) => {
    if (typeof url === "string" && /^https?:\/\//.test(url)) return shell.openExternal(url);
    throw new Error("Недопустимая ссылка.");
  });

  await restartPortal();
  await readAndBroadcast();
  createWindow();
  refreshTimer = setInterval(readAndBroadcast, 5000);

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("before-quit", () => {
  clearInterval(refreshTimer);
  backend?.stop();
});
