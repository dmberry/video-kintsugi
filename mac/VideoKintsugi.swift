// Video Kintsugi for macOS: a native window, menus and Finder drag and drop around the page.
// The page and the FFmpeg work are the same as in the browser; the app starts kintsugi.py
// (copied into Contents/Resources/web) and shows it in a WKWebView.
// Built by make-app.sh with: xcrun swiftc -swift-version 5 -O VideoKintsugi.swift -framework Cocoa -framework WebKit

import Cocoa
import UniformTypeIdentifiers
import WebKit

let appName = "Video Kintsugi"
let preferredPort = 8767
let logPath = NSHomeDirectory() + "/Library/Logs/Video Kintsugi.log"
let repoURL = URL(string: "https://github.com/dmberry/video-kintsugi")!

// ---------------------------------------------------------------- the server

final class Server {
    var process: Process?
    var port = preferredPort
    var owned = false          // false when another copy (e.g. the browser launcher) already runs one
    var version = ""
    let web = Bundle.main.resourceURL!.appendingPathComponent("web")

    var url: URL { URL(string: "http://127.0.0.1:\(port)/")! }

    func python() -> String? {
        for p in ["/opt/homebrew/bin/python3", "/usr/local/bin/python3"] where FileManager.default.isExecutableFile(atPath: p) {
            return p
        }
        // /usr/bin/python3 is only a stub until Apple's command line tools are installed
        let check = Process()
        check.executableURL = URL(fileURLWithPath: "/usr/bin/xcode-select")
        check.arguments = ["-p"]
        check.standardOutput = FileHandle.nullDevice
        check.standardError = FileHandle.nullDevice
        try? check.run()
        check.waitUntilExit()
        return check.terminationStatus == 0 ? "/usr/bin/python3" : nil
    }

    /// The running server's version on a port, if a Video Kintsugi server answers there.
    func status(_ port: Int) -> [String: Any]? {
        var req = URLRequest(url: URL(string: "http://127.0.0.1:\(port)/api/status")!)
        req.timeoutInterval = 0.5
        var out: [String: Any]?
        let done = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: req) { data, _, _ in
            if let data = data { out = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] }
            done.signal()
        }.resume()
        done.wait()
        return out
    }

    func post(_ path: String) {
        var req = URLRequest(url: url.appendingPathComponent(path))
        req.httpMethod = "POST"
        req.setValue("1", forHTTPHeaderField: "X-Kintsugi")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = Data("{}".utf8)
        req.timeoutInterval = 1
        let done = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: req) { _, _, _ in done.signal() }.resume()
        _ = done.wait(timeout: .now() + 1.5)
    }

    func portFree(_ p: Int) -> Bool {
        let s = socket(AF_INET, SOCK_STREAM, 0)
        defer { close(s) }
        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = in_port_t(UInt16(p).bigEndian)
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
        return withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(s, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) == 0 }
        }
    }

    /// Start (or reuse) a server; calls back on the main queue with nil or an error message.
    func start(onExit: @escaping (Int32) -> Void, ready: @escaping (String?) -> Void) {
        version = (try? String(contentsOf: web.appendingPathComponent("VERSION"), encoding: .utf8))?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        DispatchQueue.global().async {
            if let s = self.status(preferredPort) {
                if (s["version"] as? String) == self.version {
                    DispatchQueue.main.async { ready(nil) }   // same version, already running: share it
                    return
                }
                self.post("api/quit")                         // an older copy: replace it
                for _ in 0..<20 where !self.portFree(preferredPort) { usleep(100_000) }
            }
            self.port = preferredPort
            while !self.portFree(self.port) && self.port < preferredPort + 20 { self.port += 1 }
            guard let py = self.python() else {
                DispatchQueue.main.async { ready("python") }
                return
            }
            let p = Process()
            p.executableURL = URL(fileURLWithPath: py)
            p.arguments = ["-B", self.web.appendingPathComponent("kintsugi.py").path]
            var env = ProcessInfo.processInfo.environment
            env["KINTSUGI_PORT"] = String(self.port)
            env["KINTSUGI_PARENT"] = String(getpid())
            env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/opt/local/bin:" + (env["PATH"] ?? "/usr/bin:/bin")
            p.environment = env
            if !FileManager.default.fileExists(atPath: logPath) {
                FileManager.default.createFile(atPath: logPath, contents: nil)
            }
            if let log = FileHandle(forWritingAtPath: logPath) {
                log.seekToEndOfFile()
                p.standardOutput = log
                p.standardError = log
            }
            p.terminationHandler = { proc in DispatchQueue.main.async { onExit(proc.terminationStatus) } }
            do { try p.run() } catch {
                DispatchQueue.main.async { ready(error.localizedDescription) }
                return
            }
            self.process = p
            self.owned = true
            for _ in 0..<100 {
                if self.status(self.port) != nil {
                    DispatchQueue.main.async { ready(nil) }
                    return
                }
                if !p.isRunning { break }
                usleep(100_000)
            }
            DispatchQueue.main.async { ready("The server didn't start. Details are in ~/Library/Logs/Video Kintsugi.log.") }
        }
    }

    func stop() {
        guard owned, let p = process, p.isRunning else { return }
        p.terminate()   // the server cancels a running job and removes its partial file
        for _ in 0..<40 where p.isRunning { usleep(100_000) }
    }
}

// ---------------------------------------------------------------- the web view

/// Takes files dragged from Finder itself, so the page gets their real paths. Other drags
/// (reordering rows inside the page) go to WebKit as usual.
final class DropWebView: WKWebView {
    var onFiles: (([String]) -> Void)?
    var onHover: ((Bool) -> Void)?
    private var fileDrag = false

    private func paths(_ info: NSDraggingInfo) -> [String] {
        let urls = info.draggingPasteboard.readObjects(forClasses: [NSURL.self],
                                                       options: [.urlReadingFileURLsOnly: true]) as? [URL]
        return (urls ?? []).map { $0.path }
    }

    override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation {
        fileDrag = !paths(sender).isEmpty
        if fileDrag { onHover?(true); return .copy }
        return super.draggingEntered(sender)
    }

    override func draggingUpdated(_ sender: NSDraggingInfo) -> NSDragOperation {
        fileDrag ? .copy : super.draggingUpdated(sender)
    }

    override func draggingExited(_ sender: NSDraggingInfo?) {
        if fileDrag { onHover?(false); fileDrag = false } else { super.draggingExited(sender) }
    }

    override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        guard fileDrag else { return super.performDragOperation(sender) }
        fileDrag = false
        onHover?(false)
        onFiles?(paths(sender))
        return true
    }
}

// ---------------------------------------------------------------- the app

final class AppDelegate: NSObject, NSApplicationDelegate, WKScriptMessageHandler, WKUIDelegate, WKNavigationDelegate {
    let server = Server()
    var window: NSWindow!
    var web: DropWebView!
    var pageReady = false
    var pending: [String] = []     // files opened before the page was ready
    var quitting = false

    func applicationDidFinishLaunching(_ note: Notification) {
        buildMenu()
        let config = WKWebViewConfiguration()
        config.userContentController.add(self, name: "kintsugi")
        config.mediaTypesRequiringUserActionForPlayback = []
        web = DropWebView(frame: .zero, configuration: config)
        web.uiDelegate = self
        web.navigationDelegate = self
        web.onFiles = { [weak self] paths in self?.addPaths(paths) }
        web.onHover = { [weak self] on in self?.js("kintsugi.dragOver(\(on))") }

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1060, height: 900),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = appName
        window.minSize = NSSize(width: 640, height: 520)
        window.contentView = web
        window.center()
        window.setFrameAutosaveName("main")
        window.tabbingMode = .disallowed
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        server.start(onExit: { [weak self] code in self?.serverExited(code) }) { [weak self] err in
            guard let self = self else { return }
            if err == "python" { return self.needPython() }
            if let err = err { return self.fail("Video Kintsugi couldn’t start", err) }
            self.web.load(URLRequest(url: self.server.url))
        }
    }

    func application(_ app: NSApplication, open urls: [URL]) {
        addPaths(urls.filter { $0.isFileURL }.map { $0.path })
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool { true }

    func applicationShouldTerminate(_ app: NSApplication) -> NSApplication.TerminateReply {
        guard pageReady, !quitting else { return .terminateNow }
        web.evaluateJavaScript("kintsugi.jobRunning()") { [weak self] result, _ in
            guard let self = self else { return }
            if (result as? Bool) == true {
                let a = NSAlert()
                a.messageText = "Quit while a job is running?"
                a.informativeText = "The job will be cancelled and nothing will be saved."
                a.addButton(withTitle: "Quit")
                a.addButton(withTitle: "Cancel")
                if a.runModal() != .alertFirstButtonReturn { return NSApp.reply(toApplicationShouldTerminate: false) }
            }
            self.quitting = true
            NSApp.reply(toApplicationShouldTerminate: true)
        }
        return .terminateLater
    }

    func applicationWillTerminate(_ note: Notification) {
        quitting = true
        server.stop()
    }

    // ---------------------------------------------------------------- talking to the page

    func js(_ code: String) {
        guard pageReady else { return }
        web.evaluateJavaScript(code, completionHandler: nil)
    }

    func json(_ value: Any) -> String {
        let data = (try? JSONSerialization.data(withJSONObject: value, options: [.fragmentsAllowed])) ?? Data("null".utf8)
        return String(decoding: data, as: UTF8.self)
    }

    func addPaths(_ paths: [String]) {
        guard !paths.isEmpty else { return }
        if pageReady { js("kintsugi.addPaths(\(json(paths)))") } else { pending += paths }
        window?.makeKeyAndOrderFront(nil)
    }

    func userContentController(_ uc: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String: Any], let cmd = body["cmd"] as? String else { return }
        let start = (body["start"] as? String).map { URL(fileURLWithPath: $0) }
        switch cmd {
        case "ready":
            pageReady = true
            if !pending.isEmpty { addPaths(pending); pending = [] }
        case "choose-files":
            chooseFiles(start)
        case "choose-folder":
            let panel = NSOpenPanel()
            panel.canChooseFiles = false
            panel.canChooseDirectories = true
            panel.canCreateDirectories = true
            panel.prompt = "Choose"
            panel.message = "Choose where to save the result"
            panel.directoryURL = start
            panel.beginSheetModal(for: window) { [weak self] r in
                guard let self = self, r == .OK, let u = panel.url else { return }
                self.js("kintsugi.setOutdir(\(self.json(u.path)))")
            }
        case "copy":
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(body["text"] as? String ?? "", forType: .string)
        default:
            break
        }
    }

    func chooseFiles(_ start: URL? = nil) {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = true
        panel.canChooseDirectories = false
        panel.allowedContentTypes = [.movie, .audio, .audiovisualContent]
        panel.message = "Choose the recordings to join, in any order, or one to fix its sound"
        panel.prompt = "Add"
        if let start = start { panel.directoryURL = start }
        panel.beginSheetModal(for: window) { [weak self] r in
            if r == .OK { self?.addPaths(panel.urls.map { $0.path }) }
        }
    }

    // links to other sites open in the browser; alert() and confirm() become sheets
    func webView(_ w: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if let u = action.request.url, u.host != "127.0.0.1", u.scheme == "http" || u.scheme == "https" || u.scheme == "mailto" {
            NSWorkspace.shared.open(u)
            return decisionHandler(.cancel)
        }
        decisionHandler(.allow)
    }

    func webView(_ w: WKWebView, createWebViewWith c: WKWebViewConfiguration, for action: WKNavigationAction,
                 windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let u = action.request.url { NSWorkspace.shared.open(u) }
        return nil
    }

    func webView(_ w: WKWebView, runJavaScriptAlertPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping () -> Void) {
        let a = NSAlert()
        a.messageText = message
        a.beginSheetModal(for: window) { _ in completionHandler() }
    }

    func webView(_ w: WKWebView, runJavaScriptConfirmPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping (Bool) -> Void) {
        let a = NSAlert()
        a.messageText = message
        a.addButton(withTitle: "OK")
        a.addButton(withTitle: "Cancel")
        a.beginSheetModal(for: window) { completionHandler($0 == .alertFirstButtonReturn) }
    }

    func webView(_ w: WKWebView, didFinish nav: WKNavigation!) {
        if w.url?.host == "127.0.0.1" { window.title = appName }
    }

    // ---------------------------------------------------------------- failures

    func serverExited(_ code: Int32) {
        guard !quitting else { return }
        fail("Video Kintsugi stopped", "Its server ended unexpectedly (exit \(code)). Details are in ~/Library/Logs/Video Kintsugi.log.")
    }

    func fail(_ title: String, _ text: String) {
        let a = NSAlert()
        a.alertStyle = .critical
        a.messageText = title
        a.informativeText = text
        a.addButton(withTitle: "Quit")
        a.addButton(withTitle: "Show Log")
        if a.runModal() == .alertSecondButtonReturn { NSWorkspace.shared.open(URL(fileURLWithPath: logPath)) }
        quitting = true
        NSApp.terminate(nil)
    }

    func needPython() {
        let a = NSAlert()
        a.alertStyle = .critical
        a.messageText = "Video Kintsugi needs Python 3"
        a.informativeText = "Python 3 comes with Apple’s command line developer tools. Install them, then open Video Kintsugi again."
        a.addButton(withTitle: "Install Tools")
        a.addButton(withTitle: "Quit")
        if a.runModal() == .alertFirstButtonReturn {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: "/usr/bin/xcode-select")
            p.arguments = ["--install"]
            try? p.run()
        }
        quitting = true
        NSApp.terminate(nil)
    }

    // ---------------------------------------------------------------- menus

    @objc func runJS(_ sender: NSMenuItem) { js(sender.representedObject as? String ?? "") }
    @objc func addRecordings(_ sender: Any?) { chooseFiles() }
    @objc func openInBrowser(_ sender: Any?) { NSWorkspace.shared.open(server.url) }
    @objc func openRepo(_ sender: Any?) { NSWorkspace.shared.open(repoURL) }
    @objc func showLog(_ sender: Any?) { NSWorkspace.shared.open(URL(fileURLWithPath: logPath)) }

    func buildMenu() {
        let bar = NSMenu()
        func menu(_ title: String, _ items: [NSMenuItem]) -> NSMenu {
            let m = NSMenu(title: title)
            items.forEach { m.addItem($0) }
            let top = NSMenuItem(title: title, action: nil, keyEquivalent: "")
            top.submenu = m
            bar.addItem(top)
            return m
        }
        func item(_ title: String, _ action: Selector?, _ key: String = "", _ mods: NSEvent.ModifierFlags = .command,
                  target: AnyObject? = nil) -> NSMenuItem {
            let i = NSMenuItem(title: title, action: action, keyEquivalent: key)
            i.keyEquivalentModifierMask = mods
            i.target = target
            return i
        }
        func page(_ title: String, _ code: String, _ key: String = "", _ mods: NSEvent.ModifierFlags = .command) -> NSMenuItem {
            let i = item(title, #selector(runJS(_:)), key, mods, target: self)
            i.representedObject = code
            return i
        }
        let sep = { NSMenuItem.separator() }

        _ = menu(appName, [
            page("About \(appName)", "kintsugi.about()"), sep(),
            item("Hide \(appName)", #selector(NSApplication.hide(_:)), "h"),
            item("Hide Others", #selector(NSApplication.hideOtherApplications(_:)), "h", [.command, .option]),
            item("Show All", #selector(NSApplication.unhideAllApplications(_:))), sep(),
            item("Quit \(appName)", #selector(NSApplication.terminate(_:)), "q"),
        ])
        _ = menu("File", [
            item("Add Recordings…", #selector(addRecordings(_:)), "o", target: self),
            page("Clear List", "kintsugi.clear()"), sep(),
            page("Join and Mend", "kintsugi.start()", "\r"),
            page("Cancel Job", "kintsugi.cancel()", "."),
            page("Show Result in Finder", "kintsugi.reveal()", "r", [.command, .shift]), sep(),
            item("Close Window", #selector(NSWindow.performClose(_:)), "w"),
        ])
        _ = menu("Edit", [
            item("Undo", Selector(("undo:")), "z"),
            item("Redo", Selector(("redo:")), "z", [.command, .shift]), sep(),
            item("Cut", #selector(NSText.cut(_:)), "x"),
            item("Copy", #selector(NSText.copy(_:)), "c"),
            item("Paste", #selector(NSText.paste(_:)), "v"),
            item("Select All", #selector(NSText.selectAll(_:)), "a"),
        ])
        _ = menu("View", [
            page("Desk", "kintsugi.view('desk')", "1"),
            page("Text", "kintsugi.view('text')", "2"), sep(),
            item("Open in Browser", #selector(openInBrowser(_:)), "b", [.command, .shift], target: self), sep(),
            item("Enter Full Screen", #selector(NSWindow.toggleFullScreen(_:)), "f", [.command, .control]),
        ])
        _ = menu("Presets", [
            page("Save Changes to Preset", "kintsugi.save()", "s"),
            page("Save as New Preset…", "kintsugi.saveAs()", "s", [.command, .shift]),
            page("Delete Preset…", "kintsugi.deletePreset()"), sep(),
            page("Restore Built-in Presets…", "kintsugi.restore()"),
        ])
        let win = menu("Window", [
            item("Minimize", #selector(NSWindow.performMiniaturize(_:)), "m"),
            item("Zoom", #selector(NSWindow.performZoom(_:))), sep(),
            item("Bring All to Front", #selector(NSApplication.arrangeInFront(_:))),
        ])
        let help = menu("Help", [
            page("How \(appName) Works", "kintsugi.howto()"),
            item("\(appName) on GitHub", #selector(openRepo(_:)), target: self),
            item("Show Log", #selector(showLog(_:)), target: self),
        ])
        NSApp.mainMenu = bar
        NSApp.windowsMenu = win
        NSApp.helpMenu = help
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
