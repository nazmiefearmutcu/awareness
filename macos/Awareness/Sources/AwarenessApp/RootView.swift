import SwiftUI
import AppKit
import Observation
import AwarenessCore

struct RootView: View {
    @Bindable var manager: APIServerManager
    @State private var reloadToken = 0

    var body: some View {
        Group {
            switch manager.state {
            case .stopped, .starting:
                VStack(spacing: 16) {
                    ProgressView()
                        .controlSize(.large)
                    Text("Starting Awareness API…")
                        .foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)

            case .ready(let port, let owned):
                DashboardWebView(
                    url: AppConfig.fromEnvironment().baseURL(port: port),
                    reloadToken: reloadToken
                )
                .overlay(alignment: .topTrailing) {
                    if !owned {
                        Text("attached")
                            .font(.caption2.monospaced())
                            .padding(.horizontal, 8)
                            .padding(.vertical, 4)
                            .background(.thinMaterial, in: Capsule())
                            .padding(10)
                    }
                }

            case .unhealthy(_, let detail):
                statusPanel(
                    title: "API unreachable",
                    message: detail,
                    systemImage: "exclamationmark.triangle"
                )

            case .failed(let msg):
                statusPanel(
                    title: "API failed to start",
                    message: msg,
                    systemImage: "xmark.octagon"
                )
            }
        }
        .task {
            await manager.start()
        }
        .onReceive(NotificationCenter.default.publisher(for: .awarenessReloadWebView)) { _ in
            reloadToken += 1
        }
        .focusable()
        .onAppear {
            NSApp.activate(ignoringOtherApps: true)
        }
    }

    @ViewBuilder
    private func statusPanel(title: String, message: String, systemImage: String) -> some View {
        VStack(spacing: 14) {
            Image(systemName: systemImage)
                .font(.system(size: 36))
                .foregroundStyle(.secondary)
            Text(title)
                .font(.title2.weight(.semibold))
            Text(message)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 480)
            HStack(spacing: 12) {
                Button("Retry") {
                    Task { await manager.restart() }
                }
                .keyboardShortcut("r", modifiers: [.command])
                Button("Open API log") {
                    openAPILog()
                }
            }
            .buttonStyle(.borderedProminent)
        }
        .padding(32)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func openAPILog() {
        let url = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/Awareness/api.log")
        NSWorkspace.shared.activateFileViewerSelecting([url])
    }

    /// Called from app menus.
    func reloadWebView() {
        reloadToken += 1
    }
}
