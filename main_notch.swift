import AppKit
import Foundation

// CLI: install.sh вызывает `MeshPultNotch --check-notch` — YES/NO, есть ли экран с чёлкой
if CommandLine.arguments.contains("--check-notch") {
    var hasNotch = false
    for s in NSScreen.screens where s.safeAreaInsets.top > 0 { hasNotch = true }
    print(hasNotch ? "YES" : "NO")
    exit(0)
}

// MeshPult (notch-пульт): невидимая зона над вырезом MacBook.
// Наведение мышью на чёлку → всплывает чёрная панель-продолжение челки:
// уроки дня → клик по уроку → ученики → кнопки 5/4/3/2/Н; «без д/з»; обновление.
// Мозг — CLI-мост ~/antigravity/mesh/mesh_remote.py (переиспользует
// mesh_automation_v3: токен-цепочка, расписание, профили, оценки).

struct Lesson {
    let group: String
    let groupId: Int
    let subject: String
    let begin: String?
    let end: String?
    let isCurrent: Bool
    let scheduleLessonId: Int?
}

struct PultConfig {
    var python = NSHomeDirectory() + "/antigravity/mesh/venv/bin/python"
    var bridge = NSHomeDirectory() + "/antigravity/mesh/mesh_remote.py"
    var notchWidth: CGFloat = 220
    var panelWidth: CGFloat = 400

    static func load() -> PultConfig {
        var c = PultConfig()
        let path = NSHomeDirectory() + "/.config/mesh-remote/config.json"
        guard let data = FileManager.default.contents(atPath: path),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return c }
        if let p = obj["python"] as? String, !p.isEmpty { c.python = p }
        if let b = obj["bridge"] as? String, !b.isEmpty { c.bridge = b }
        if let w = obj["notchWidth"] as? Double { c.notchWidth = CGFloat(w) }
        if let w = obj["panelWidth"] as? Double { c.panelWidth = CGFloat(w) }
        return c
    }
}

let cfg = PultConfig.load()

/// Вычисление области чёлки текущего экрана
func notchRect() -> (screen: NSScreen, rect: NSRect)? {
    // ищем ВСТРОЕННЫЙ экран с чёлкой (у встроенного safeAreaInsets.top > 0);
    // может быть не NSScreen.main, если человек работает на внешнем мониторе
    for screen in NSScreen.screens {
        let top = screen.safeAreaInsets.top
        if top <= 0 { continue }
        let w = cfg.notchWidth
        let rect = NSRect(x: screen.frame.midX - w / 2,
                          y: screen.frame.maxY - top,
                          width: w,
                          height: top)
        return (screen, rect)
    }
    return nil
}

func tag(_ kind: String) -> String {
    switch kind {
    case "absence": return "Н"
    case "nohomework": return "без д/з"
    default: return "оценка"
    }
}

/// Прозрачное окно-ловушка над чёлкой (всегда на экране)
final class NotchLureWindow: NSWindow {
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}

/// Панель-продолжение чёлки
final class NotchPanelWindow: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { true }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    var lureWindow: NotchLureWindow!
    var panel: NotchPanelWindow!
    var contentView: PanelView!
    var lessons: [Lesson] = []
    var tokenExp = ""
    var lastAction = ""
    var autoHideTimer: Timer?
    var studentsCache: [Int: [[String: Any]]] = [:]
    var gradesCache: [Int: [Int: [String]]] = [:]   // scheduleLessonId → studentId → [оценки]
    var absenceCache: [Int: Set<Int>] = [:]          // scheduleLessonId → studentId (Н, выставлен пультом)
    var expandedKeys: Set<String> = []               // раскрытые блоки/уроки
    var blockChoice: [String: Int] = [:]             // blockKey → индекс активного урока в блоке
    var cacheDay = ""                                // для ежедневной очистки кэшей
    var preloadedDay = ""                            // день предзагрузки (ученики/оценки)
    var resultLine = ""
    var selectedLessonIdx: Int?

    /// Ежедневная очистка кэшей: ученики/оценки/подсветки перестраиваются с нового дня
    private func todayYMD() -> String {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd"
        return f.string(from: Date())
    }
    func clearCachesIfNewDay() {
        let t = todayYMD()
        if cacheDay != t {
            studentsCache.removeAll()
            gradesCache.removeAll()
            absenceCache.removeAll()
            expandedKeys.removeAll()
            selectedLessonIdx = nil
            cacheDay = t
            preloadedDay = ""   // новый день → предзагрузка заново
        }
    }

    /// Предзагрузка данных дня: ученики всех групп + оценки всех уроков,
    /// чтобы при первом же открытии панели клик не ждал загрузки
    func preloadDayData() {
        guard preloadedDay != cacheDay, !lessons.isEmpty else { return }
        preloadedDay = cacheDay
        var groups = Set(lessons.map { $0.groupId })
        groups.remove(0)
        for g in groups where studentsCache[g] == nil {
            loadStudents(groupId: g, rerender: false)
        }
        for l in lessons {
            if let lid = l.scheduleLessonId, gradesCache[lid] == nil {
                loadGrades(lessonId: lid)
            }
        }
    }
    let queue = DispatchQueue(label: "mesh-pult", qos: .utility)

    func applicationDidFinishLaunching(_ n: Notification) {
        app = self
        installLure()
        makePanel()
        panel?.orderOut(nil)
        refreshAll()
        Timer.scheduledTimer(withTimeInterval: 300, repeats: true) { [weak self] _ in
            self?.refreshAll()
        }
    }

    // MARK: ловушка-наведение

    func installLure() {
        guard let (screen, rect) = notchRect() else { return }
        let w = NotchLureWindow(contentRect: rect,
                                styleMask: [.borderless],
                                backing: .buffered, defer: false)
                w.level = .statusBar
        w.isOpaque = false
        w.backgroundColor = .clear
        w.isReleasedWhenClosed = false
        w.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .ignoresCycle]
        let view = LureView(frame: rect)
        w.contentView = view
        w.makeKeyAndOrderFront(nil)
        w.orderFrontRegardless()
        lureWindow = w
    }

    func showPanel() {
        guard let (screen, _) = notchRect(), panel != nil, let app else { return }
        clearCachesIfNewDay()
        let h = min(400, screen.visibleFrame.height - 140)
        let frame = NSRect(x: screen.frame.midX - cfg.panelWidth / 2,
                           y: screen.frame.maxY - h,
                           width: cfg.panelWidth,
                           height: h)
        panel.setFrame(frame, display: true)
        clearCachesIfNewDay()
        contentView.updateContent()
        panel.orderFrontRegardless()
        contentView.refreshData()
        restartIdleTimer()
    }

    func hidePanel() {
        panel?.orderOut(nil)
        autoHideTimer?.invalidate()
        autoHideTimer = nil
    }

    /// мышь ушла из чёлки: спрятать почти сразу,
    /// но дать 0.4 с, чтобы курсор успел войти в панель
    func hideSoon() {
        autoHideTimer?.invalidate()
        autoHideTimer = Timer.scheduledTimer(withTimeInterval: 0.4, repeats: false) { [weak self] _ in
            self?.hidePanel()
        }
    }

    /// мышь вошла в панель — отменить запланированное скрытие
    func cancelHide() {
        autoHideTimer?.invalidate()
        autoHideTimer = nil
    }

    /// панель открыта: подстраховка — скрыть после 5 с полного бездействия
    func restartIdleTimer() {
        autoHideTimer?.invalidate()
        autoHideTimer = Timer.scheduledTimer(withTimeInterval: 5.0, repeats: false) { [weak self] _ in
            self?.hidePanel()
        }
    }

    // MARK: мост-CLI

    func bridge(_ args: [String], completion: @escaping (Result<[String: Any], Error>) -> Void) {
        queue.async {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: cfg.python)
            p.arguments = [cfg.bridge] + args
            let outPipe = Pipe(), errPipe = Pipe()
            p.standardOutput = outPipe; p.standardError = errPipe
            do { try p.run() } catch {
                DispatchQueue.main.async { completion(.failure(error)) }
                return
            }
            let data = outPipe.fileHandleForReading.readDataToEndOfFile()
            let errData = errPipe.fileHandleForReading.readDataToEndOfFile()
            p.waitUntilExit()
            DispatchQueue.main.async {
                let text = String(data: data, encoding: .utf8) ?? ""
                if let obj = (try? JSONSerialization.jsonObject(with: Data(text.utf8))) as? [String: Any] {
                    if obj["error"] != nil {
                        completion(.failure(NSError(domain: "mesh", code: 1, userInfo: [NSLocalizedDescriptionKey: String(describing: obj["error"]!)])))
                    } else {
                        completion(.success(obj))
                    }
                } else {
                    let e = String(data: errData, encoding: .utf8) ?? "?"
                    completion(.failure(NSError(domain: "mesh", code: 2, userInfo: [NSLocalizedDescriptionKey: "нет JSON от моста: \(e.prefix(180))"])))
                }
            }
        }
    }

    func refreshAll() {
        clearCachesIfNewDay()
        if panel?.isVisible == true { contentView.headerText(vals: lessons) }
        bridge(["today"]) { [weak self] res in
            guard let self else { return }
            switch res {
            case .success(let data):
                var list: [Lesson] = []
                for l in (data["lessons"] as? [[String: Any]]) ?? [] {
                    list.append(Lesson(
                        group: l["group"] as? String ?? "?",
                        groupId: l["group_id"] as? Int ?? 0,
                        subject: l["subject"] as? String ?? "?",
                        begin: l["begin"] as? String,
                        end: l["end"] as? String,
                        isCurrent: l["is_current"] as? Bool ?? false,
                        scheduleLessonId: l["schedule_lesson_id"] as? Int))
                }
                self.lessons = list
                if let i = self.selectedLessonIdx, !self.lessons.indices.contains(i) {
                    self.selectedLessonIdx = nil
                }
                self.contentView?.headerText(vals: list)
                // предзагрузка: чтобы клик по любому уроку не ждал сеть
                self.preloadDayData()
                self.fetchStatus()
            case .failure(let e):
                self.resultLine = "МЭШ: \(e.localizedDescription)"
                if self.panel?.isVisible == true { self.contentView.headerText(vals: self.lessons) }
            }
        }
    }

    func fetchStatus() {
        bridge(["status"]) { [weak self] res in
            if case .success(let d) = res {
                guard let self else { return }
                self.tokenExp = d["token_expires"] as? String ?? ""
                if self.panel?.isVisible == true { self.contentView.headerText(vals: self.lessons) }
            }
        }
    }

    // MARK: действия

    func perform(kind: String, studentId: Int? = nil, grade: Int? = nil,
                 lessonId: Int? = nil, name: String = "", blockKey: String? = nil) {
        var args = ["act", kind]
        if let studentId { args += ["--student-id", String(studentId)] }
        if let grade { args += ["--grade", String(grade)] }
        if let lessonId, lessonId != 0 { args += ["--schedule-lesson-id", String(lessonId)] }
        resultLine = "выполняю \(tag(kind)) \(name)…"
        if panel?.isVisible == true { contentView.headerText(vals: lessons) }
        bridge(args) { [weak self] res in
            guard let self else { return }
            switch res {
            case .success:
                self.resultLine = "✅ \(tag(kind)) \(name) — готово"
                // Н: запомнить для подсветки (API чтения отсутствий недоступен)
                if kind == "absence", let lessonId, lessonId != 0, let studentId {
                    var set = self.absenceCache[lessonId] ?? Set<Int>()
                    set.insert(studentId)
                    self.absenceCache[lessonId] = set
                }
                // обновить подсветку оценок урока
                if let lessonId, lessonId != 0 { self.loadGrades(lessonId: lessonId) }
            case .failure(let e):
                self.resultLine = "⚠️ \(e.localizedDescription)"
            }
            if self.panel?.isVisible == true { self.contentView.headerText(vals: self.lessons) }
        }
    }

    @objc func doGrade(_ sender: TagButton) {
        guard let info = sender.payload,
              let kind = info["kind"] as? String else { return }
        // для двойного блока активный урок выбирается блок-кнопками
        var lessonId = info["lessonId"] as? Int
        if let bk = info["blockKey"] as? String,
           let li = blockChoice[bk], lessons.indices.contains(li) {
            lessonId = lessons[li].scheduleLessonId
            // если не загрузили оценки этого урока — подгрузим при первом открытии
            if let lid = lessonId, gradesCache[lid] == nil { loadGrades(lessonId: lid) }
        }
        perform(kind: kind,
                studentId: info["student"] as? Int,
                grade: info["grade"] as? Int,
                lessonId: lessonId,
                name: info["name"] as? String ?? "",
                blockKey: info["blockKey"] as? String)
    }

    @objc func doChooseLesson(_ sender: TagButton) {
        guard let info = sender.payload,
              let bk = info["blockKey"] as? String,
              let li = info["lessonIdx"] as? Int, lessons.indices.contains(li) else { return }
        blockChoice[bk] = li
        if let lid = lessons[li].scheduleLessonId, gradesCache[lid] == nil {
            loadGrades(lessonId: lid)
        }
        contentView.headerText(vals: lessons)
    }

    @objc func pickLesson(_ sender: LessonRowButton) {
        let idx = sender.lessonIndex
        guard lessons.indices.contains(idx) else { return }
        let key = "les\(idx)"
        if expandedKeys.contains(key) { expandedKeys.remove(key) } else { expandedKeys.insert(key) }
        let l = lessons[idx]
        contentView.headerText(vals: lessons)
        guard expandedKeys.contains(key) else { return }
        if studentsCache[l.groupId] == nil { loadStudents(groupId: l.groupId) }
        if let lid = l.scheduleLessonId, gradesCache[lid] == nil { loadGrades(lessonId: lid) }
    }

    @objc func pickBlock(_ sender: LessonRowButton) {
        guard let app = app else { return }
        let key = sender.blockKey
        if app.expandedKeys.contains(key) { app.expandedKeys.remove(key) } else { app.expandedKeys.insert(key) }
        if app.expandedKeys.contains(key) {
            // при раскрытии блока грузим оценки всех уроков этой группы
            // (blockKey выглядит как "blk<start>-<count>-<groupId>")
            let parts = key.split(separator: "-").compactMap { Int($0) }
            if let gid = parts.last {
                for l in app.lessons where l.groupId == gid {
                    if let lid = l.scheduleLessonId, app.gradesCache[lid] == nil {
                        app.loadGrades(lessonId: lid)
                    }
                }
            }
        }
        contentView.headerText(vals: app.lessons)
    }

    /// активный урок в раскрытом двойном блоке: содержит «сейчас», иначе последний
    func activeLessonIdxInBlock(_ key: String, block: [Lesson]) -> Int {
        if let chosen = blockChoice[key], block.indices.contains(chosen) { return chosen }
        for (k, l) in block.enumerated() where l.isCurrent { return k }
        return block.count - 1
    }

    func loadStudents(groupId: Int, rerender: Bool = true) {
        bridge(["students", "--group-id", String(groupId)]) { [weak self] res in
            guard let self else { return }
            switch res {
            case .success(let d):
                self.studentsCache[groupId] = (d["students"] as? [[String: Any]]) ?? []
                if rerender == true && self.panel?.isVisible == true {
                    self.contentView.headerText(vals: self.lessons)
                }
            case .failure(let e):
                self.resultLine = "⚠️ \(e.localizedDescription)"
                self.contentView.headerText(vals: self.lessons)
            }
        }
    }

    func loadGrades(lessonId: Int) {
        bridge(["grades", "--schedule-lesson-id", String(lessonId)]) { [weak self] res in
            guard let self else { return }
            switch res {
            case .success(let d):
                var g: [Int: [String]] = [:]
                for (k, v) in (d["grades"] as? [String: Any]) ?? [:] {
                    guard let sid = Int(k), let names = v as? [String] else { continue }
                    g[sid] = names
                }
                self.gradesCache[lessonId] = g
                if self.panel?.isVisible == true { self.contentView.headerText(vals: self.lessons) }
            case .failure:
                break   // нет оценок / ошибка чтения — просто без подсветки
            }
        }
    }

    func loadStudents(groupId: Int) {
        bridge(["students", "--group-id", String(groupId)]) { [weak self] res in
            guard let self else { return }
            switch res {
            case .success(let d):
                self.studentsCache[groupId] = (d["students"] as? [[String: Any]]) ?? []
                self.contentView.headerText(vals: self.lessons)
            case .failure(let e):
                self.resultLine = "⚠️ \(e.localizedDescription)"
                self.contentView.headerText(vals: self.lessons)
            }
        }
    }

    @objc func doNoHW(_ sender: NSButton) {
        perform(kind: "nohomework", name: "группа текущего урока")
    }
    @objc func doRefresh(_ sender: NSButton) {
        resultLine = "обновляю расписание…"
        if panel?.isVisible == true { contentView.headerText(vals: lessons) }
        refreshAll()
    }
    @objc func doClose(_ sender: NSButton) { hidePanel() }

    // MARK: окно панели

    func makePanel() {
        let size = NSSize(width: cfg.panelWidth, height: 400)
        panel = NotchPanelWindow(contentRect: NSRect(origin: .zero, size: size),
                                 styleMask: [.borderless, .nonactivatingPanel],
                                 backing: .buffered, defer: false)
        panel.level = .statusBar
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.isFloatingPanel = true
        panel.hidesOnDeactivate = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        contentView = PanelView()
        panel.contentView = contentView
    }
}

/// Вид-ловушка: прозрачный, ловит наведение/клик на чёлке
final class LureView: NSView {
    override func acceptsFirstMouse(for e: NSEvent?) -> Bool { true }
    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        trackingAreas.forEach(removeTrackingArea)
        let t = NSTrackingArea(rect: bounds,
                               options: [.mouseEnteredAndExited, .activeAlways, .mouseMoved],
                               owner: self, userInfo: nil)
        addTrackingArea(t)
    }
    override func mouseEntered(with event: NSEvent) { app?.showPanel() }
    override func mouseDown(with event: NSEvent) { app?.showPanel() }
    override func mouseExited(with event: NSEvent) { app?.hideSoon() }
    override func draw(_ dirtyRect: NSRect) {
        NSColor.black.setFill()
        bounds.fill()
    }
    override var isOpaque: Bool { true }
}

var app: AppDelegate?
let appImpl = NSApplication.shared
let delegatePult = AppDelegate()
appImpl.delegate = delegatePult
appImpl.setActivationPolicy(.accessory)
appImpl.run()

final class TagButton: NSButton {
    var payload: [String: Any]?
}

final class LessonRowButton: NSButton {
    var lessonIndex: Int = -1
    var blockKey: String = ""
}

// MARK: - Панель (содержимое)

final class PanelView: NSView {
    var headerLabel: NSTextField!
    var statusLabel: NSTextField!
    var resultLabel: NSTextField!
    var bodyScroll: NSScrollView!
    var documentStack: NSStackView!
    var selectedLessonIdx: Int?

    override var acceptsFirstResponder: Bool { true }

    override func keyDown(with event: NSEvent) {
        if event.keyCode == 53 { app?.hidePanel(); return }   // Esc
        super.keyDown(with: event)
    }

    override init(frame: NSRect) {
        super.init(frame: frame)
        commonInit()
    }
    required init?(coder: NSCoder) {
        super.init(coder: coder); commonInit()
    }

    func commonInit() {
        wantsLayer = true
        layer?.backgroundColor = NSColor(calibratedWhite: 0.07, alpha: 1).cgColor
        layer?.cornerRadius = 20
        // маска скругления только нижних углов делается визуально чёрной шапкой ниже

        let vstack = NSStackView()
        vstack.orientation = .vertical
        vstack.spacing = 8
        vstack.alignment = .leading
        vstack.translatesAutoresizingMaskIntoConstraints = false
        addSubview(vstack)

        headerLabel = label("Пульт МЭШ", size: 15, bold: true, color: .white)
        statusLabel = label("", size: 11, bold: false, color: NSColor.white.withAlphaComponent(0.65))
        resultLabel = label("", size: 11, bold: false, color: NSColor.systemTeal)

        let btnBar = NSStackView(views: [
            button("Без д/з сейчас", action: #selector(AppDelegate.doNoHW(_:))),
            button("Обновить", action: #selector(AppDelegate.doRefresh(_:))),
            button("✕", action: #selector(AppDelegate.doClose(_:))),
        ])
        btnBar.orientation = .horizontal
        btnBar.spacing = 8
        btnBar.alignment = .centerY

        bodyScroll = NSScrollView()
        bodyScroll.hasVerticalScroller = true
        bodyScroll.drawsBackground = false
        bodyScroll.translatesAutoresizingMaskIntoConstraints = false
        documentStack = NSStackView()
        documentStack.orientation = .vertical
        documentStack.spacing = 4
        documentStack.alignment = .leading
        documentStack.translatesAutoresizingMaskIntoConstraints = false
        bodyScroll.documentView = documentStack
        bodyScroll.automaticallyAdjustsContentInsets = false
        bodyScroll.contentInsets = NSEdgeInsets(top: 0, left: 0, bottom: 0, right: 0)

        NSLayoutConstraint.activate([
            vstack.topAnchor.constraint(equalTo: topAnchor, constant: 16),
            vstack.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 14),
            vstack.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -14),
            vstack.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -14),
            documentStack.leadingAnchor.constraint(equalTo: bodyScroll.contentView.leadingAnchor),
            documentStack.trailingAnchor.constraint(equalTo: bodyScroll.contentView.trailingAnchor),
            bodyScroll.widthAnchor.constraint(equalToConstant: cfg.panelWidth - 28),
            bodyScroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 180),
        ])
        vstack.addArrangedSubview(headerLabel)
        vstack.addArrangedSubview(statusLabel)
        vstack.addArrangedSubview(btnBar)
        vstack.addArrangedSubview(makeSeparator())
        vstack.addArrangedSubview(bodyScroll)
        vstack.addArrangedSubview(makeSeparator())
        vstack.addArrangedSubview(resultLabel)

        headerText(vals: [])
        updateContent()
    }

    private func label(_ s: String, size: CGFloat, bold: Bool, color: NSColor) -> NSTextField {
        let l = NSTextField(labelWithString: s)
        l.font = NSFont.systemFont(ofSize: size, weight: bold ? .semibold : .regular)
        l.textColor = color
        return l
    }
    private func makeSeparator() -> NSView {
        let v = NSView(frame: NSRect(x: 0, y: 0, width: 10, height: 1))
        v.heightAnchor.constraint(equalToConstant: 1).isActive = true
        v.widthAnchor.constraint(equalToConstant: cfg.panelWidth - 28).isActive = true
        v.wantsLayer = true
        v.layer?.backgroundColor = NSColor.white.withAlphaComponent(0.12).cgColor
        return v
    }
    private func button(_ s: String, action: Selector) -> NSButton {
        let b = NSButton(title: s, target: app, action: action)
        b.bezelStyle = .rounded
        b.controlSize = .small
        b.font = NSFont.systemFont(ofSize: 11)
        b.wantsLayer = true
        return b
    }

    // трекинг мыши на самой панели: вход отменяет скрытие, выход — планирует его
    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        trackingAreas.forEach(removeTrackingArea)
        let t = NSTrackingArea(rect: bounds,
                               options: [.mouseEnteredAndExited, .activeAlways],
                               owner: self, userInfo: nil)
        addTrackingArea(t)
    }
    override func mouseEntered(with event: NSEvent) { app?.cancelHide() }
    override func mouseExited(with event: NSEvent) { app?.hideSoon() }

    func updateContent() { /* позиции фиксированы; используем для заворачивания */ }

    func refreshData() {
        app?.refreshAll()
    }

    func headerText(vals: [Lesson]) {
        guard app != nil else { return }
        let n = app!.lessons.count
        var h = "Пульт МЭШ"
        if n > 0 {
            h = "Уроков: \(n)" + (app!.lessons.contains { $0.isCurrent } ? " · 🟢 идёт урок" : "")
        } else { h = "Уроков сегодня нет" }
        if !app!.tokenExp.isEmpty { h += " · токен до \(app!.tokenExp)" }
        headerLabel.stringValue = h
        statusLabel.stringValue = app!.lastAction.isEmpty ? " " : app!.lastAction
        resultLabel.stringValue = app!.resultLine
        buildRows()
    }

    func buildRows() {
        guard let app else { return }
        documentStack.arrangedSubviews.forEach { $0.removeFromSuperview() }
        documentStack.alignment = .leading
        if app.lessons.isEmpty {
            documentStack.addArrangedSubview(label("Пусто", size: 12, bold: false, color: .gray))
        }
        var i = 0
        while i < app.lessons.count {
            let l = app.lessons[i]
            var j = i + 1
            while j < app.lessons.count, app.lessons[j].groupId == l.groupId { j += 1 }
            let count = j - i
            if count >= 2 {
                // сдвоенные (и более) уроки одной группы — единый блок
                let block = Array(app.lessons[i..<j])
                let key = "blk\(i)-\(count)-\(l.groupId)"
                let time = [block.first!.begin, block.last!.end].compactMap({$0}).joined(separator: "–")
                let dot = block.contains { $0.isCurrent } ? "🟢" : "⚪️"
                let hdr = blockButton("\(dot) \(time)  \(l.subject) · \(l.group) · \(count) урока",
                                      key: key, width: cfg.panelWidth - 28)
                documentStack.addArrangedSubview(hdr)
                if app.expandedKeys.contains(key) {
                    let activeIdx = app.activeLessonIdxInBlock(key, block: block)
                    addChooserRow(block: block, key: key, activeIdx: activeIdx)
                    addStudentsRow(block: block, activeIdx: activeIdx, blockKey: key)
                }
                i = j
            } else {
                let t = [l.begin, l.end].compactMap { $0 }.joined(separator: "–")
                let dot = l.isCurrent ? "🟢" : "⚪️"
                let row = rowButton("\(dot) \(t)  \(l.subject) · \(l.group)",
                                    action: #selector(AppDelegate.pickLesson(_:)),
                                    index: i, width: cfg.panelWidth - 28)
                documentStack.addArrangedSubview(row)
                if app.expandedKeys.contains("les\(i)") { addStudentsRow(block: [l], activeIdx: 0, blockKey: nil) }
                i += 1
            }
        }
    }

    func addChooserRow(block: [Lesson], key: String, activeIdx: Int) {
        let wrap = NSStackView()
        wrap.orientation = .vertical
        wrap.spacing = 3
        wrap.alignment = .leading
        let labelTxt = label("Отметки идут в:", size: 10, bold: false, color: NSColor.white.withAlphaComponent(0.6))
        wrap.addArrangedSubview(labelTxt)
        let row = NSStackView()
        row.orientation = .horizontal
        row.spacing = 6
        row.alignment = .centerY
        let sp = NSView(); sp.widthAnchor.constraint(equalToConstant: 14).isActive = true
        row.addArrangedSubview(sp)
        for (k, ls) in block.enumerated() {
            let b = TagButton(title: "\(k + 1)-й (\(ls.begin ?? ""))", target: app, action: #selector(AppDelegate.doChooseLesson(_:)))
            b.bezelStyle = .inline
            b.controlSize = .small
            b.font = NSFont.systemFont(ofSize: 10)
            b.payload = ["kind": "choose", "blockKey": key, "lessonIdx": i_of(block, ls)]
            b.wantsLayer = true
            b.layer?.cornerRadius = 4
            if k == activeIdx {
                b.layer?.backgroundColor = NSColor.systemTeal.withAlphaComponent(0.35).cgColor
            }
            row.addArrangedSubview(b)
        }
        wrap.addArrangedSubview(row)
        documentStack.addArrangedSubview(wrap)
    }

    private func i_of(_ block: [Lesson], _ l: Lesson) -> Int {
        app?.lessons.firstIndex(where: { $0.scheduleLessonId == l.scheduleLessonId }) ?? 0
    }

    func addStudentsRow(block: [Lesson], activeIdx: Int, blockKey: String?) {
        guard let app, let first = block.first, first.groupId != 0 else { return }
        let lesson = block[activeIdx]
        let groupId = first.groupId
        let wrap = NSStackView()
        wrap.orientation = .vertical
        wrap.spacing = 3
        wrap.alignment = .leading
        if let st = app.studentsCache[groupId] {
            renderStudents(into: wrap, students: st, block: block, activeIdx: activeIdx, blockKey: blockKey)
        } else {
            let loading = NSStackView()
            loading.orientation = .horizontal
            loading.spacing = 0
            let sp = NSView(); sp.widthAnchor.constraint(equalToConstant: 14).isActive = true
            loading.addArrangedSubview(sp)
            loading.addArrangedSubview(label("Загружаю учеников…", size: 11, bold: false, color: NSColor.white.withAlphaComponent(0.6)))
            wrap.addArrangedSubview(loading)
        }
        documentStack.addArrangedSubview(wrap)
        if app.studentsCache[groupId] == nil {
            app.loadStudents(groupId: groupId, rerender: false)
        }
    }

    func renderStudents(into wrap: NSStackView, students: [[String: Any]],
                        block: [Lesson], activeIdx: Int, blockKey: String?) {
        // оценки/Н ВСЕХ уроков блока: активный урок — бирюза, соседний — фиолет
        for st in students {
            guard let id = st["id"] as? Int, let name = st["name"] as? String else { continue }
            let row = NSStackView()
            row.orientation = .horizontal
            row.spacing = 6
            row.alignment = .centerY
            let sp = NSView(); sp.widthAnchor.constraint(equalToConstant: 14).isActive = true
            row.addArrangedSubview(sp)
            let nameL = label("\(name)", size: 12, bold: false, color: NSColor.white)
            nameL.widthAnchor.constraint(equalToConstant: 170).isActive = true
            nameL.lineBreakMode = .byTruncatingTail
            row.addArrangedSubview(nameL)
            let activeLesson = block[activeIdx]
            for g in [5, 4, 3, 2, 0] {
                var rep: [String: Any] = g == 0
                    ? ["kind": "absence", "student": id, "name": name, "lessonId": activeLesson.scheduleLessonId ?? 0]
                    : ["kind": "mark", "student": id, "grade": g, "name": name, "lessonId": activeLesson.scheduleLessonId ?? 0]
                if blockKey != nil { rep["blockKey"] = blockKey }
                let b = TagButton(title: g == 0 ? "Н" : String(g), target: app, action: #selector(AppDelegate.doGrade(_:)))
                b.bezelStyle = .inline
                b.controlSize = .small
                b.font = NSFont.systemFont(ofSize: 10)
                b.payload = rep
                b.wantsLayer = true
                b.layer?.cornerRadius = 4
                if g != 0 {
                    for (k, ls) in block.enumerated() {
                        guard let lid = ls.scheduleLessonId,
                              let names = app?.gradesCache[lid]?[id] as? [String],
                              names.contains(String(g)) else { continue }
                        b.layer?.backgroundColor = (k == activeIdx
                            ? NSColor.systemTeal
                            : NSColor.systemPurple).withAlphaComponent(0.35).cgColor
                        break
                    }
                }
                if g == 0 {
                    for ls in block {
                        guard let lid = ls.scheduleLessonId,
                              let set = app?.absenceCache[lid], set.contains(id) else { continue }
                        b.layer?.backgroundColor = NSColor.systemOrange.withAlphaComponent(0.45).cgColor
                        break
                    }
                }
                b.widthAnchor.constraint(equalToConstant: 24).isActive = true
                row.addArrangedSubview(b)
            }
            wrap.addArrangedSubview(row)
        }
        if students.isEmpty {
            wrap.addArrangedSubview(label("   Ученики не найдены", size: 11, bold: false, color: .gray))
        }
    }

private func blockButton(_ title: String, key: String, width: CGFloat) -> LessonRowButton {
        let b = LessonRowButton(title: title, target: app, action: #selector(AppDelegate.pickBlock(_:)))
        b.blockKey = key
        b.bezelStyle = .recessed
        b.font = NSFont.systemFont(ofSize: 12, weight: .medium)
        b.wantsLayer = true
        b.layer?.cornerRadius = 6
        b.setButtonType(.momentaryPushIn)
        b.widthAnchor.constraint(equalToConstant: width).isActive = true
        b.alignment = .left
        return b
    }

    private func rowButton(_ title: String, action: Selector, index: Int, width: CGFloat) -> LessonRowButton {
        let b = LessonRowButton(title: title, target: app, action: action)
        b.lessonIndex = index
        b.bezelStyle = .recessed
        b.font = NSFont.systemFont(ofSize: 12)
        b.wantsLayer = true
        b.layer?.cornerRadius = 6
        b.imageScaling = .scaleNone
        b.setButtonType(.momentaryPushIn)
        b.widthAnchor.constraint(equalToConstant: width).isActive = true
        b.alignment = .left
        return b
    }
}