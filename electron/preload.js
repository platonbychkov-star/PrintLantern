const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("printLantern", {
  getStatus: () => ipcRenderer.invoke("app:get-status"),
  refresh: () => ipcRenderer.invoke("app:refresh"),
  updateSettings: (settings) => ipcRenderer.invoke("settings:update", settings),
  decideJob: (jobId, decision) => ipcRenderer.invoke("job:decide", jobId, decision),
  openExternal: (url) => ipcRenderer.invoke("shell:open-external", url),
  onStatus: (callback) => {
    const listener = (_event, status) => callback(status);
    ipcRenderer.on("status:changed", listener);
    return () => ipcRenderer.removeListener("status:changed", listener);
  }
});
