"use strict";

const path = require("node:path");
const fs = require("node:fs");
const { app, BrowserWindow, Menu, shell, ipcMain, dialog } = require("electron");
const { APIManager } = require("./api-manager.cjs");

/** @type {BrowserWindow | null} */
let mainWindow = null;
/** @type {APIManager | null} */
let apiManager = null;
let quitting = false;
let dashboardLoaded = false;

const BOOT_HTML = path.join(__dirname, "boot.html");

function isLoopbackUrl(urlString) {
  try {
    const u = new URL(urlString);
    if (u.protocol !== "http:" && u.protocol !== "https:") return false;
    const host = u.hostname.toLowerCase();
    return host === "127.0.0.1" || host === "localhost" || host === "[::1]" || host === "::1";
  } catch {
    return false;
  }
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1180,
    height: 820,
    minWidth: 900,
    minHeight: 640,
    title: "Awareness",
    backgroundColor: "#0c0c0e",
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  mainWindow.once("ready-to-show", () => {
    if (mainWindow) mainWindow.show();
  });

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (isLoopbackUrl(url)) {
      return { action: "allow" };
    }
    if (/^https?:/i.test(url)) {
      shell.openExternal(url);
    }
    return { action: "deny" };
  });

  mainWindow.webContents.on("will-navigate", (event, url) => {
    // Allow boot file and loopback dashboard; open external in OS browser.
    if (url.startsWith("file://")) return;
    if (isLoopbackUrl(url)) return;
    event.preventDefault();
    if (/^https?:/i.test(url)) {
      shell.openExternal(url);
    }
  });

  mainWindow.loadFile(BOOT_HTML).catch((err) => {
    console.error("Failed to load boot.html", err);
  });

  mainWindow.on("closed", () => {
    mainWindow = null;
  });
}

function broadcastState(state) {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send("awareness:state", state);
  }
}

function buildMenu() {
  const template = [
    {
      label: "Awareness",
      submenu: [
        {
          label: "Restart API",
          accelerator: "CmdOrCtrl+Shift+R",
          click: async () => {
            if (!apiManager) return;
            dashboardLoaded = false;
            if (mainWindow && !mainWindow.isDestroyed()) {
              await mainWindow.loadFile(BOOT_HTML);
            }
            await apiManager.restart();
          },
        },
        {
          label: "Open API log",
          click: () => openLog(),
        },
        { type: "separator" },
        {
          label: "Reload",
          accelerator: "CmdOrCtrl+R",
          click: () => {
            if (mainWindow && !mainWindow.isDestroyed()) {
              mainWindow.webContents.reload();
            }
          },
        },
        { type: "separator" },
        {
          label: "Quit",
          accelerator: process.platform === "darwin" ? "Cmd+Q" : "Alt+F4",
          click: () => {
            app.quit();
          },
        },
      ],
    },
    {
      label: "Edit",
      submenu: [
        { role: "undo" },
        { role: "redo" },
        { type: "separator" },
        { role: "cut" },
        { role: "copy" },
        { role: "paste" },
        { role: "selectAll" },
      ],
    },
    {
      label: "View",
      submenu: [
        { role: "reload" },
        { role: "forceReload" },
        { role: "toggleDevTools" },
        { type: "separator" },
        { role: "resetZoom" },
        { role: "zoomIn" },
        { role: "zoomOut" },
        { type: "separator" },
        { role: "togglefullscreen" },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

function openLog() {
  if (!apiManager) return;
  const logPath = apiManager.logPath;
  try {
    fs.mkdirSync(path.dirname(logPath), { recursive: true });
    if (!fs.existsSync(logPath)) {
      fs.writeFileSync(logPath, "");
    }
  } catch {
    /* ignore */
  }
  shell.showItemInFolder(logPath);
}

async function onApiState(state) {
  broadcastState(state);
  if (!mainWindow || mainWindow.isDestroyed()) return;

  if (state.status === "ready" && state.port && !dashboardLoaded) {
    const host = process.env.AW_API_HOST || "127.0.0.1";
    const url = `http://${host}:${state.port}/`;
    try {
      await mainWindow.loadURL(url);
      dashboardLoaded = true;
      mainWindow.setTitle("Awareness");
    } catch (err) {
      console.error("Failed to load dashboard", err);
      dashboardLoaded = false;
      await mainWindow.loadFile(BOOT_HTML);
      broadcastState({
        status: "failed",
        detail: `Dashboard load failed: ${err && err.message ? err.message : err}`,
      });
    }
  }

  if (
    (state.status === "failed" || state.status === "unhealthy" || state.status === "starting") &&
    dashboardLoaded
  ) {
    // Drop back to boot UI for failures after we had a dashboard.
    if (state.status === "failed" || state.status === "unhealthy") {
      dashboardLoaded = false;
      try {
        await mainWindow.loadFile(BOOT_HTML);
        broadcastState(state);
      } catch {
        /* ignore */
      }
    }
  }
}

async function startApi() {
  apiManager = new APIManager();
  apiManager.on("state", (state) => {
    onApiState(state).catch((err) => console.error(err));
  });
  await apiManager.start();
}

function registerIpc() {
  ipcMain.handle("awareness:retry", async () => {
    if (!apiManager) return;
    dashboardLoaded = false;
    if (mainWindow && !mainWindow.isDestroyed()) {
      try {
        await mainWindow.loadFile(BOOT_HTML);
      } catch {
        /* ignore */
      }
    }
    await apiManager.restart();
  });
  ipcMain.handle("awareness:open-log", async () => {
    openLog();
  });
}

app.whenReady().then(async () => {
  registerIpc();
  buildMenu();
  createWindow();
  await startApi();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
      if (apiManager && apiManager.state.status === "stopped") {
        startApi().catch(console.error);
      } else if (apiManager) {
        broadcastState(apiManager.state);
        if (apiManager.state.status === "ready" && apiManager.state.port) {
          dashboardLoaded = false;
          onApiState(apiManager.state).catch(console.error);
        }
      }
    }
  });
});

app.on("window-all-closed", () => {
  // Quit on all platforms for this multi-platform shell (including darwin).
  if (!quitting) {
    app.quit();
  }
});

app.on("before-quit", (event) => {
  if (quitting) return;
  if (apiManager && apiManager.state.status !== "stopped") {
    event.preventDefault();
    quitting = true;
    apiManager
      .stop()
      .catch(() => {})
      .finally(() => {
        app.exit(0);
      });
  } else {
    quitting = true;
  }
});
