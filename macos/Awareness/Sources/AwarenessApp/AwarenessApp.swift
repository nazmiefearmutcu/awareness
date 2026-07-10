import SwiftUI
import AwarenessCore

@main
struct AwarenessApp: App {
    @State private var manager = APIServerManager()

    var body: some Scene {
        WindowGroup("Awareness") {
            RootView(manager: manager)
                .frame(minWidth: 1100, minHeight: 700)
        }
        .defaultSize(width: 1280, height: 840)
        .commands {
            CommandGroup(replacing: .newItem) {}
        }
    }
}
