const state = {
  status: null,
  saving: false
};

const elements = {
  activeCount: document.querySelector("#active-count"),
  addressSelect: document.querySelector("#address-select"),
  jobCountNav: document.querySelector("#job-count-nav"),
  jobs: document.querySelector("#jobs"),
  jobsEmpty: document.querySelector("#jobs-empty"),
  lanEnabled: document.querySelector("#lan-enabled"),
  networkDot: document.querySelector("#network-dot"),
  portalNote: document.querySelector("#portal-note"),
  portalStatus: document.querySelector("#portal-status"),
  portalUrl: document.querySelector("#portal-url"),
  portInput: document.querySelector("#port-input"),
  printerSelect: document.querySelector("#printer-select"),
  printerCount: document.querySelector("#printer-count"),
  qrCode: document.querySelector("#qr-code"),
  qrWrap: document.querySelector("#qr-wrap"),
  refreshButton: document.querySelector("#refresh-button"),
  directPrint: document.querySelector("#direct-print"),
  saveNetwork: document.querySelector("#save-network"),
  serverError: document.querySelector("#server-error"),
  serviceWarning: document.querySelector("#service-warning"),
  settingsMessage: document.querySelector("#settings-message"),
  updatedAt: document.querySelector("#updated-at"),
  version: document.querySelector("#version"),
  openPortalButton: document.querySelector("#open-portal-button")
};

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatBytes(value) {
  const bytes = Number(value || 0);
  if (bytes < 1024) return `${bytes} Б`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} КБ`;
  return `${(bytes / 1024 / 1024).toFixed(1)} МБ`;
}

function statusLabel(status) {
  const value = String(status || "").toLowerCase();
  if (value === "draft") return "Настройка";
  if (value === "pending_approval") return "Ждёт подтверждения";
  if (value === "queued") return "В очереди";
  if (value === "printing") return "Печатается";
  if (value === "completed") return "Готово";
  if (value === "failed") return "Ошибка";
  if (value === "interrupted") return "Остановлено";
  if (value === "cancelled") return "Отклонено";
  if (value.includes("print")) return "Печатается";
  if (value.includes("pause")) return "На паузе";
  if (value.includes("error")) return "Ошибка";
  if (value.includes("delete")) return "Удаляется";
  return status || "В очереди";
}

function jobMarkup(job) {
  const total = Math.max(0, Number(job.totalPages || 0));
  const printed = Math.max(0, Number(job.pagesPrinted || 0));
  const progress = Number.isFinite(Number(job.progress))
    ? Math.max(0, Math.min(100, Number(job.progress)))
    : total > 0
      ? Math.min(100, Math.round((printed / total) * 100))
      : 8;
  const pageText = job.message || (total > 0 ? `${printed} из ${total} стр.` : "Готов к настройке");
  return `
    <article class="job-card">
      <div class="job-title">
        <strong title="${escapeHtml(job.documentName)}">${escapeHtml(job.documentName)}</strong>
        <small>${escapeHtml(job.printerName)} · ${formatBytes(job.size)}</small>
      </div>
      <div class="job-progress">
        <div class="progress-track"><i style="width:${progress}%"></i></div>
        <span>${pageText}</span>
      </div>
      <div class="job-actions">
        <span class="status-pill">${escapeHtml(statusLabel(job.status))}</span>
        ${
          job.awaitingApproval
            ? `<div class="approval-actions">
                <button class="approve-job" type="button" data-job-id="${escapeHtml(job.id)}">Печатать</button>
                <button class="reject-job" type="button" data-job-id="${escapeHtml(job.id)}">Отклонить</button>
              </div>`
            : ""
        }
        ${job.canOpen ? `<button class="job-open" type="button">Открыть в портале</button>` : ""}
      </div>
    </article>
  `;
}

function renderAddressOptions(status) {
  const selected = status.settings.preferredAddress || "auto";
  const options = [
    '<option value="auto">Автоматически</option>',
    ...status.network.addresses.map(
      (item) =>
        `<option value="${escapeHtml(item.address)}">${escapeHtml(item.label)}</option>`
    )
  ];
  elements.addressSelect.innerHTML = options.join("");
  elements.addressSelect.value = selected;
  if (!elements.addressSelect.value) elements.addressSelect.value = "auto";
}

function renderPrinterOptions(status) {
  const selected = status.settings.printerName || "";
  elements.printerSelect.innerHTML = [
    '<option value="">Автоматически / из старых настроек</option>',
    ...(status.printers || []).map(
      (printer) =>
        `<option value="${escapeHtml(printer.name)}">${escapeHtml(printer.name)}</option>`
    )
  ].join("");
  elements.printerSelect.value = selected;
  if (!elements.printerSelect.value) elements.printerSelect.value = "";
}

function render(status) {
  state.status = status;
  const jobs = status.jobs || [];
  const portalOnline = status.network.running && !status.network.error;
  elements.activeCount.textContent = jobs.length;
  elements.jobCountNav.textContent = jobs.length;
  elements.printerCount.textContent = status.printers?.length || 0;
  elements.jobs.innerHTML = jobs.map(jobMarkup).join("");
  elements.jobsEmpty.classList.toggle("hidden", jobs.length > 0);
  elements.version.textContent = `Версия ${status.version}`;

  elements.portalStatus.textContent = portalOnline ? "Работает" : "Выключен";
  elements.portalNote.textContent = status.settings.lanEnabled
    ? "доступен в локальной сети"
    : "доступен только на компьютере";
  elements.networkDot.classList.toggle("online", portalOnline);

  const updated = status.lastUpdatedAt ? new Date(status.lastUpdatedAt) : null;
  elements.updatedAt.textContent = updated
    ? `Обновлено ${updated.toLocaleTimeString("ru-RU", {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit"
      })}`
    : "Состояние ещё не обновлено";

  elements.serviceWarning.classList.toggle("hidden", status.printerService.available);
  elements.serviceWarning.textContent = status.printerService.message || "";

  elements.lanEnabled.checked = status.settings.lanEnabled;
  elements.directPrint.checked = !status.settings.requireDesktopApproval;
  elements.portInput.value = status.settings.port;
  renderAddressOptions(status);
  renderPrinterOptions(status);
  elements.portalUrl.textContent = status.network.url;
  elements.qrWrap.classList.toggle("disabled", !portalOnline);
  if (status.network.qrDataUrl) elements.qrCode.src = status.network.qrDataUrl;

  elements.serverError.classList.toggle("hidden", !status.network.error);
  elements.serverError.textContent = status.network.error || "";
}

async function refresh() {
  elements.refreshButton.disabled = true;
  try {
    render(await window.printLantern.refresh());
  } finally {
    elements.refreshButton.disabled = false;
  }
}

async function saveNetwork() {
  elements.saveNetwork.disabled = true;
  elements.settingsMessage.textContent = "Перезапускаем локальный портал…";
  try {
    const status = await window.printLantern.updateSettings({
      lanEnabled: elements.lanEnabled.checked,
      preferredAddress: elements.addressSelect.value,
      port: Number(elements.portInput.value),
      printerName: elements.printerSelect.value,
      requireDesktopApproval: !elements.directPrint.checked
    });
    render(status);
    elements.settingsMessage.textContent = status.network.error
      ? "Проверьте настройки и попробуйте другой порт."
      : "Настройки применены.";
  } catch (error) {
    elements.settingsMessage.textContent = error.message || "Не удалось сохранить настройки.";
  } finally {
    elements.saveNetwork.disabled = false;
  }
}

document.querySelectorAll(".nav-item").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".nav-item").forEach((item) => item.classList.remove("active"));
    document.querySelectorAll(".view").forEach((view) => view.classList.remove("active"));
    button.classList.add("active");
    document.querySelector(`#${button.dataset.target}`).classList.add("active");
  });
});

elements.refreshButton.addEventListener("click", refresh);
elements.saveNetwork.addEventListener("click", saveNetwork);
elements.lanEnabled.addEventListener("change", saveNetwork);
elements.directPrint.addEventListener("change", saveNetwork);
elements.openPortalButton.addEventListener("click", () => {
  if (state.status?.network.running) window.printLantern.openExternal(state.status.network.url);
});
elements.portalUrl.addEventListener("click", () => {
  if (state.status?.network.running) {
    window.printLantern.openExternal(state.status.network.url);
  }
});
elements.jobs.addEventListener("click", async (event) => {
  const approveButton = event.target.closest(".approve-job");
  const rejectButton = event.target.closest(".reject-job");
  if (approveButton || rejectButton) {
    const button = approveButton || rejectButton;
    button.disabled = true;
    try {
      render(
        await window.printLantern.decideJob(
          button.dataset.jobId,
          approveButton ? "approve" : "reject"
        )
      );
    } catch (error) {
      elements.serviceWarning.textContent =
        error.message || "Не удалось обработать задание.";
      elements.serviceWarning.classList.remove("hidden");
    }
    return;
  }
  if (event.target.closest(".job-open") && state.status?.network.running) {
    window.printLantern.openExternal(state.status.network.url);
  }
});

window.printLantern.onStatus(render);
window.printLantern.getStatus().then(render);
