import SwiftUI
import AppKit
import Observation
import AwarenessCore

struct RootView: View {
    @Bindable var manager: APIServerManager

    var body: some View {
        Group {
            switch manager.state {
            case .stopped, .starting:
                ProgressView("Starting Awareness API…")
                    .controlSize(.large)
            case .ready(let port, _):
                DashboardWebView(url: AppConfig.fromEnvironment().baseURL(port: port))
            case .failed(let msg):
                VStack(spacing: 12) {
                    Text("API failed to start")
                        .font(.title2)
                    Text(msg)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                    Button("Retry") {
                        Task { await manager.start() }
                    }
                }
                .padding()
            }
        }
        .task {
            await manager.start()
        }
    }
}
