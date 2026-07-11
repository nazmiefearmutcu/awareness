"use strict";

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("awareness", {
  retry: () => ipcRenderer.invoke("awareness:retry"),
  openLog: () => ipcRenderer.invoke("awareness:open-log"),
  onState: (cb) => {
    if (typeof cb !== "function") return () => {};
    const handler = (_event, state) => {
      try {
        cb(state);
      } catch {
        /* ignore renderer callback errors */
      }
    };
    ipcRenderer.on("awareness:state", handler);
    return () => ipcRenderer.removeListener("awareness:state", handler);
  },
});
