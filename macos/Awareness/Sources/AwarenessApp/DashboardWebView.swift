import SwiftUI
import WebKit
import AppKit
import AwarenessCore

struct DashboardWebView: NSViewRepresentable {
    let url: URL
    var reloadToken: Int = 0

    func makeCoordinator() -> Coordinator {
        Coordinator()
    }

    func makeNSView(context: Context) -> WKWebView {
        let cfg = WKWebViewConfiguration()
        let wv = WKWebView(frame: .zero, configuration: cfg)
        wv.navigationDelegate = context.coordinator
        wv.allowsBackForwardNavigationGestures = true
        wv.allowsMagnification = true
        #if DEBUG
        if #available(macOS 13.3, *) {
            wv.isInspectable = true
        }
        #endif
        return wv
    }

    func updateNSView(_ webView: WKWebView, context: Context) {
        let needsLoad =
            context.coordinator.loadedURL != url
            || context.coordinator.reloadToken != reloadToken
        if needsLoad {
            context.coordinator.loadedURL = url
            context.coordinator.reloadToken = reloadToken
            webView.load(URLRequest(url: url))
        }
    }

    final class Coordinator: NSObject, WKNavigationDelegate {
        var loadedURL: URL?
        var reloadToken: Int = -1

        func webView(
            _ webView: WKWebView,
            decidePolicyFor navigationAction: WKNavigationAction,
            decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
        ) {
            guard let url = navigationAction.request.url else {
                decisionHandler(.cancel)
                return
            }
            // Allow about:blank and data URLs used by SPA.
            if url.scheme == "about" || url.absoluteString == "about:blank" {
                decisionHandler(.allow)
                return
            }
            if LoopbackPolicy.isAllowedNavigation(url) {
                decisionHandler(.allow)
                return
            }
            if LoopbackPolicy.isOutboundHttp(url) {
                NSWorkspace.shared.open(url)
            }
            decisionHandler(.cancel)
        }

        func webView(
            _ webView: WKWebView,
            didFailProvisionalNavigation navigation: WKNavigation!,
            withError error: Error
        ) {
            // Soft fail — RootView health monitor will surface offline state.
            NSLog("Awareness WebView provisional fail: \(error.localizedDescription)")
        }

        func webView(
            _ webView: WKWebView,
            didFail navigation: WKNavigation!,
            withError error: Error
        ) {
            NSLog("Awareness WebView fail: \(error.localizedDescription)")
        }
    }
}
