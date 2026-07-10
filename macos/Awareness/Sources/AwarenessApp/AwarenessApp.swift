import SwiftUI
import AppKit
import AwarenessCore

@main
struct AwarenessApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @State private var manager = APIServerManager()

    var body: some Scene {
        WindowGroup("Awareness") {
            RootView(manager: manager)
                .frame(minWidth: 1100, minHeight: 700)
                .onAppear {
                    appDelegate.manager = manager
                }
        }
        .defaultSize(width: 1280, height: 840)
        .commands {
            CommandGroup(replacing: .newItem) {}
        }
    }
}

/// Tears down the owned API process on Quit / last window close.
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    var manager: APIServerManager?

    func applicationWillTerminate(_ notification: Notification) {
        manager?.stop()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }
}
