const { execFile } = require("node:child_process");
const { promisify } = require("node:util");

const execFileAsync = promisify(execFile);

const POWERSHELL_SCRIPT = `
$ErrorActionPreference = 'Stop'
$printers = @(
  Get-Printer | Select-Object Name, DriverName, PortName, PrinterStatus, WorkOffline, Shared
)
$jobs = @(
  foreach ($printer in $printers) {
    try {
      Get-PrintJob -PrinterName $printer.Name -ErrorAction Stop |
        Select-Object @{Name='PrinterName';Expression={$printer.Name}},
          ID, DocumentName, JobStatus, SubmittedTime, TotalPages, PagesPrinted, Size, UserName
    } catch {}
  }
)
@{ printers = $printers; jobs = $jobs } | ConvertTo-Json -Depth 5 -Compress
`;

function toArray(value) {
  if (value == null) return [];
  return Array.isArray(value) ? value : [value];
}

function normalizeStatus(value) {
  if (Array.isArray(value)) return value.join(", ");
  if (value == null || value === "") return "Unknown";
  return String(value);
}

function normalizeSnapshot(payload = {}) {
  const printers = toArray(payload.printers).map((printer) => ({
    name: String(printer.Name || "Неизвестный принтер"),
    driverName: String(printer.DriverName || ""),
    portName: String(printer.PortName || ""),
    status: normalizeStatus(printer.PrinterStatus),
    offline: Boolean(printer.WorkOffline),
    shared: Boolean(printer.Shared)
  }));

  const jobs = toArray(payload.jobs)
    .map((job) => ({
      id: Number(job.ID || 0),
      printerName: String(job.PrinterName || "Неизвестный принтер"),
      documentName: String(job.DocumentName || "Без названия"),
      status: normalizeStatus(job.JobStatus),
      submittedAt: job.SubmittedTime ? new Date(job.SubmittedTime).toISOString() : null,
      totalPages: Number(job.TotalPages || 0),
      pagesPrinted: Number(job.PagesPrinted || 0),
      size: Number(job.Size || 0),
      userName: String(job.UserName || "")
    }))
    .sort((a, b) => (b.submittedAt || "").localeCompare(a.submittedAt || ""));

  return { printers, jobs };
}

async function readPrinterSnapshot() {
  if (process.platform !== "win32") {
    return {
      printers: [],
      jobs: [],
      available: false,
      message: "Чтение системной очереди поддерживается в Windows."
    };
  }

  try {
    const { stdout } = await execFileAsync(
      "powershell.exe",
      ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", POWERSHELL_SCRIPT],
      { windowsHide: true, timeout: 12000, maxBuffer: 1024 * 1024 }
    );
    const snapshot = normalizeSnapshot(JSON.parse(stdout.trim() || "{}"));
    return { ...snapshot, available: true, message: null };
  } catch (error) {
    return {
      printers: [],
      jobs: [],
      available: false,
      message: "Не удалось прочитать очередь печати Windows.",
      technicalMessage: error.message
    };
  }
}

module.exports = {
  normalizeSnapshot,
  readPrinterSnapshot,
  toArray
};
