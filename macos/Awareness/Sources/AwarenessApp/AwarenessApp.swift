import SwiftUI
import AppKit
import AwarenessCore

@main
struct AwarenessApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @State private var manager = APIServerManager()

    var body: some Scene {
        WindowGroup {
            RootView(manager: manager)
                .frame(minWidth: 1100, minHeight: 700)
                .onAppear {
                    appDelegate.manager = manager
                }
        }
        .defaultSize(width: 1280, height: 840)
        .commands {
            CommandGroup(replacing: .newItem) {}
            CommandGroup(after: .toolbar) {
                Button("Reload Dashboard") {
                    NotificationCenter.default.post(name: .awarenessReloadWebView, object: nil)
                }
                .keyboardShortcut("r", modifiers: [.command])

                Button("Restart API") {
                    Task { @MainActor in
                        await appDelegate.manager?.restart()
                    }
                }
                .keyboardShortcut("r", modifiers: [.command, .shift])

                Button("Open API Log") {
                    let url = FileManager.default.homeDirectoryForCurrentUser
                        .appendingPathComponent("Library/Logs/Awareness/api.log")
                    NSWorkspace.shared.activateFileViewerSelecting([url])
                }
            }
        }
    }
}

extension Notification.Name {
    static let awarenessReloadWebView = Notification.Name("awareness.reloadWebView")
}

/// Single-instance activation + API teardown on quit.
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    var manager: APIServerManager?
    private var lockFD: Int32 = -1

    func applicationWillFinishLaunching(_ notification: Notification) {
        // Prefer a single running instance: if another Awareness is alive, activate it and exit.
        if !acquireSingleInstanceLock() {
            activateExistingInstance()
            NSApp.terminate(nil)
            return
        }
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationWillTerminate(_ notification: Notification) {
        manager?.stop()
        releaseSingleInstanceLock()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag {
            // Create a window if none (SwiftUI usually keeps WindowGroup alive).
            NSApp.windows.first?.makeKeyAndOrderFront(nil)
        }
        NSApp.activate(ignoringOtherApps: true)
        return true
    }

    // MARK: - Single instance via flock

    private func lockFileURL() -> URL {
        let support = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/Awareness", isDirectory: true)
        try? FileManager.default.createDirectory(at: support, withIntermediateDirectories: true)
        return support.appendingPathComponent("instance.lock")
    }

    private func acquireSingleInstanceLock() -> Bool {
        let path = lockFileURL().path
        let fd = open(path, O_RDWR | O_CREAT, 0o644)
        guard fd >= 0 else { return true } // if we can't lock, don't block launch
        if flock(fd, LOCK_EX | LOCK_NB) != 0 {
            close(fd)
            return false
        }
        lockFD = fd
        // Write pid for debugging.
        let pid = "\(ProcessInfo.processInfo.processIdentifier)\n"
        pid.withCString { ptr in
            _ = ftruncate(fd, 0)
            _ = write(fd, ptr, strlen(ptr))
        }
        return true
    }

    private func releaseSingleInstanceLock() {
        if lockFD >= 0 {
            flock(lockFD, LOCK_UN)
            close(lockFD)
            lockFD = -1
        }
    }

    private func activateExistingInstance() {
        let bundleID = Bundle.main.bundleIdentifier ?? "dev.awareness.app"
        let apps = NSRunningApplication.runningApplications(withBundleIdentifier: bundleID)
        for app in apps where app.processIdentifier != ProcessInfo.processInfo.processIdentifier {
            app.activate(options: [.activateAllWindows])
            return
        }
        // Fallback: match by localized name.
        for app in NSWorkspace.shared.runningApplications
        where app.localizedName == "Awareness"
            && app.processIdentifier != ProcessInfo.processInfo.processIdentifier {
            app.activate(options: [.activateAllWindows])
            return
        }
    }
}
