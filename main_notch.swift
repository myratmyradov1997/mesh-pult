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
    var absenceCache: [Int: [Int: Int]] = [:]        // scheduleLessonId → studentId → причина (1 = Н, 2 = УП)
    var expandedKeys: Set<String> = []               // раскрытые блоки/уроки
    var blockChoice: [String: Int] = [:]             // blockKey → индекс активного урока в блоке
    var cacheDay = ""                                // для ежедневной очистки кэшей
    var preloadedDay = ""                            // день предзагрузки (ученики/оценки)
    var resultLine = ""
    var selectedLessonIdx: Int?
    var markIds: [Int: [Int: [MarkRef]]] = [:]      // scheduleLessonId → studentId → оценки с id
    var absenceIds: [Int: [Int: Int]] = [:]          // scheduleLessonId → studentId → id отметки Н
    var history: [DoneAction] = []                   // что поставил пульт: «Отменить» удаляет последнее
    var inFlight: Set<String> = []                   // запросы в пути: kind-student-lesson
    var rollCall: [String: Set<Int>] = [:]           // ключ раскрытия → отмеченные отсутствующие

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
            markIds.removeAll()
            absenceIds.removeAll()
            history.removeAll()
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

    /// Записать в МЭШ сразу. Ошибочную запись снимает повторный клик или «↩︎ Отменить».
    func perform(kind: String, studentIds: [Int] = [], grade: Int? = nil,
                 lessonIds: [Int] = [], name: String) {
        let a = ActionRef(kind: kind, studentIds: studentIds, grade: grade, name: name)
        // nohomework без урока — «текущий урок» по расписанию
        for lid in (lessonIds.isEmpty ? [0] : lessonIds) {
            let keys = studentIds.map { flightKey(kind, $0, lid, grade) }
            if keys.contains(where: inFlight.contains) { continue }   // двойной клик, запрос ещё идёт
            inFlight.formUnion(keys)
            var args = ["act", kind]
            if studentIds.count == 1 { args += ["--student-id", String(studentIds[0])] }
            if studentIds.count > 1 { args += ["--student-ids", studentIds.map(String.init).joined(separator: ",")] }
            if let grade { args += ["--grade", String(grade)] }
            if lid != 0 { args += ["--schedule-lesson-id", String(lid)] }
            resultLine = "выполняю \(a.title)…"
            rerender()
            bridge(args) { [weak self] res in
                guard let self else { return }
                self.inFlight.subtract(keys)
                switch res {
                case .success(let d):
                    let failed = (d["failed"] as? [String: Any])?.count ?? 0
                    self.resultLine = failed == 0
                        ? "✅ \(a.title) — готово"
                        : "⚠️ \(a.title) — не прошло у \(failed) уч."
                    let created = ((d["created"] as? [String: Any]) ?? [:]).values.compactMap { $0 as? Int }
                    if !created.isEmpty, lid != 0, kind != "nohomework" {
                        self.history.append(DoneAction(kind: kind, ids: created, lessonId: lid, title: a.title))
                    }
                    // Н: подсветить сразу, не дожидаясь перечитывания урока
                    if kind == "absence", lid != 0 {
                        for sid in (d["done"] as? [Int]) ?? studentIds {
                            self.absenceCache[lid, default: [:]][sid] = 1
                        }
                    }
                    // обновить подсветку оценок и Н урока (и id записей для удаления)
                    if lid != 0 { self.loadGrades(lessonId: lid) }
                case .failure(let e):
                    self.resultLine = "⚠️ \(a.title): \(e.localizedDescription)"
                }
                self.rerender()
            }
        }
    }

    /// Удалить записи из журнала (kind: mark | absence) по их id
    func deleteRecords(kind: String, ids: [Int], lessonId: Int, title: String) {
        let key = "del-\(kind)-\(ids.map(String.init).joined(separator: ","))"
        guard !ids.isEmpty, !inFlight.contains(key) else { return }
        inFlight.insert(key)
        resultLine = "удаляю \(title)…"
        rerender()
        bridge(["delete", kind, "--ids", ids.map(String.init).joined(separator: ",")]) { [weak self] res in
            guard let self else { return }
            self.inFlight.remove(key)
            switch res {
            case .success(let d):
                let failed = (d["failed"] as? [String: Any])?.count ?? 0
                self.resultLine = failed == 0 ? "🗑 удалено: \(title)" : "⚠️ \(title): не удалено \(failed) из \(ids.count)"
                self.history.removeAll { $0.ids.contains(where: ids.contains) }
            case .failure(let e):
                self.resultLine = "⚠️ удаление \(title): \(e.localizedDescription)"
            }
            self.loadGrades(lessonId: lessonId)
            self.rerender()
        }
    }

    func flightKey(_ kind: String, _ student: Int, _ lessonId: Int, _ grade: Int?) -> String {
        "\(kind)-\(student)-\(lessonId)" + (grade.map { "-\($0)" } ?? "")
    }

    /// идёт запрос по этой кнопке — подсветить жёлтым
    func isInFlight(kind: String, student: Int, lessonId: Int?, grade: Int?) -> Bool {
        guard let lessonId else { return false }
        return inFlight.contains(flightKey(kind, student, lessonId, grade))
    }

    func rerender() {
        if panel?.isVisible == true { contentView.headerText(vals: lessons) }
    }

    /// «↩︎ Отменить»: удалить последнее, что пульт поставил в этой сессии
    @objc func doUndo(_ sender: NSButton) {
        guard let last = history.popLast() else {
            resultLine = "нечего отменять: пульт сегодня ещё ничего не ставил"
            rerender()
            return
        }
        deleteRecords(kind: last.kind, ids: last.ids, lessonId: last.lessonId, title: last.title)
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
        let sid = info["student"] as? Int
        let grade = info["grade"] as? Int
        let name = info["name"] as? String ?? ""
        // повторный клик по уже выставленному на этом уроке — удалить запись
        if let sid, let lid = lessonId, lid != 0 {
            if kind == "mark", let grade,
               let ref = markIds[lid]?[sid]?.last(where: { $0.value == String(grade) }) {
                deleteRecords(kind: "mark", ids: [ref.id], lessonId: lid, title: "\(grade) \(name)")
                return
            }
            if kind == "absence", let reason = absenceCache[lid]?[sid] {
                if reason == 2 {
                    resultLine = "УП у \(name) снимается только в журнале МЭШ"
                    rerender()
                } else if let aid = absenceIds[lid]?[sid] {
                    deleteRecords(kind: "absence", ids: [aid], lessonId: lid, title: "Н \(name)")
                } else {
                    resultLine = "Н \(name): обновляю данные урока, нажми ещё раз"
                    loadGrades(lessonId: lid)
                    rerender()
                }
                return
            }
        }
        perform(kind: kind,
                studentIds: sid.map { [$0] } ?? [],
                grade: grade,
                lessonIds: lessonId.flatMap { $0 == 0 ? nil : [$0] } ?? [],
                name: name)
    }

    // MARK: перекличка

    @objc func doRollStart(_ sender: TagButton) {
        guard let k = sender.payload?["expKey"] as? String else { return }
        rollCall[k] = []
        rerender()
    }

    @objc func doRollToggle(_ sender: TagButton) {
        guard let k = sender.payload?["expKey"] as? String,
              let sid = sender.payload?["student"] as? Int, rollCall[k] != nil else { return }
        if rollCall[k]!.contains(sid) { rollCall[k]!.remove(sid) } else { rollCall[k]!.insert(sid) }
        rerender()
    }

    @objc func doRollCancel(_ sender: TagButton) {
        guard let k = sender.payload?["expKey"] as? String else { return }
        rollCall[k] = nil
        rerender()
    }

    /// «Отправить Н»: все отмеченные одним пакетом, тоже через окно отмены
    @objc func doRollSubmit(_ sender: TagButton) {
        guard let k = sender.payload?["expKey"] as? String,
              let ids = rollCall[k], !ids.isEmpty,
              let lids = sender.payload?["lessonIds"] as? [Int], !lids.isEmpty else { return }
        rollCall[k] = nil
        let where_ = lids.count > 1 ? " на \(lids.count) урока" : ""
        perform(kind: "absence", studentIds: ids.sorted(), lessonIds: lids,
              name: "перекличка: \(ids.count) уч.\(where_)")
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
                var a: [Int: Int] = [:]
                for (k, v) in (d["absences"] as? [String: Any]) ?? [:] {
                    guard let sid = Int(k) else { continue }
                    a[sid] = (v as? Int) ?? 1
                }
                self.absenceCache[lessonId] = a
                var mi: [Int: [MarkRef]] = [:]
                for (k, v) in (d["mark_ids"] as? [String: Any]) ?? [:] {
                    guard let sid = Int(k), let rs = v as? [[String: Any]] else { continue }
                    mi[sid] = rs.compactMap { r in
                        guard let id = r["id"] as? Int, let val = r["value"] as? String else { return nil }
                        return MarkRef(id: id, value: val)
                    }
                }
                self.markIds[lessonId] = mi
                var ai: [Int: Int] = [:]
                for (k, v) in (d["absence_ids"] as? [String: Any]) ?? [:] {
                    if let sid = Int(k), let id = v as? Int { ai[sid] = id }
                }
                self.absenceIds[lessonId] = ai
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

/// Блик «liquid glass»: полупрозрачный градиент из верхнего левого угла
final class GlassSheenView: NSView {
    override func makeBackingLayer() -> CALayer {
        let g = CAGradientLayer()
        g.colors = [
            NSColor.white.withAlphaComponent(0.22).cgColor,
            NSColor.white.withAlphaComponent(0.08).cgColor,
            NSColor.white.withAlphaComponent(0.0).cgColor
        ]
        g.locations = [0, 0.28, 0.75]
        g.startPoint = CGPoint(x: 0, y: 1)
        g.endPoint = CGPoint(x: 1, y: 0)
        return g
    }
    override func acceptsFirstMouse(for e: NSEvent?) -> Bool { false }
    override func hitTest(_ point: NSPoint) -> NSView? { nil }  // клики сквозь блик
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
        // ловушка невидима: чёлку рисует сама macOS (скруглённая),
        // а мы лишь ловим мышь поверх неё — иначе чёлка выглядит «прямоугольной»
        NSColor.clear.setFill()
        dirtyRect.fill()
    }
    override var isOpaque: Bool { false }
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

/// Что отправляем в МЭШ (для строки результата)
struct ActionRef {
    let kind: String
    let studentIds: [Int]
    let grade: Int?
    let name: String

    var title: String {
        let what = kind == "mark" ? (grade.map(String.init) ?? "оценка") : tag(kind)
        return kind == "absence" && studentIds.count > 1 ? name : "\(what) \(name)"
    }
}

/// Поставленное пультом: id записей, чтобы «Отменить» мог их удалить
struct DoneAction {
    let kind: String
    let ids: [Int]
    let lessonId: Int
    let title: String
}

struct MarkRef {
    let id: Int
    let value: String
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
        // «liquid glass»: тонкая тёмная подложка + размытие под панелью
        layer?.backgroundColor = NSColor(calibratedWhite: 0.06, alpha: 0.30).cgColor
        layer?.cornerRadius = 20
        layer?.masksToBounds = true
        layer?.borderWidth = 0.6
        layer?.borderColor = NSColor.white.withAlphaComponent(0.22).cgColor

        // размытие всего, что за панелью
        let blur = NSVisualEffectView(frame: bounds)
        blur.autoresizingMask = [.width, .height]
        blur.material = .underWindowBackground
        blur.blendingMode = .behindWindow
        blur.state = .active
        blur.appearance = NSAppearance(named: .darkAqua)
        blur.wantsLayer = true
        blur.layer?.cornerRadius = 20
        blur.layer?.masksToBounds = true
        addSubview(blur, positioned: .below, relativeTo: nil)

        // блик стекла: мягкий градиент из левого верхнего угла
        let sheen = GlassSheenView(frame: bounds)
        sheen.autoresizingMask = [.width, .height]
        addSubview(sheen, positioned: .below, relativeTo: nil)
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
            button("↩︎ Отменить", action: #selector(AppDelegate.doUndo(_:))),
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
        let font = NSFont.systemFont(ofSize: 11)
        b.font = font
        // текст кнопок на тёмном стекле — белый, иначе системный labelColor сливается
        b.attributedTitle = NSAttributedString(string: s, attributes: [.foregroundColor: NSColor.white, .font: font])
        b.wantsLayer = true
        b.layer?.backgroundColor = NSColor.white.withAlphaComponent(0.10).cgColor
        b.layer?.cornerRadius = 8
        b.layer?.borderWidth = 0.5
        b.layer?.borderColor = NSColor.white.withAlphaComponent(0.18).cgColor
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
                let hdr = blockButton("\(dot) \(time)  \(l.subject) · \(l.group) · \(count) урока\(absenceSummary(block))",
                                      key: key, width: cfg.panelWidth - 28)
                documentStack.addArrangedSubview(hdr)
                if app.expandedKeys.contains(key) {
                    let activeIdx = app.activeLessonIdxInBlock(key, block: block)
                    addChooserRow(block: block, key: key, activeIdx: activeIdx)
                    addStudentsRow(block: block, activeIdx: activeIdx, blockKey: key, expKey: key)
                }
                i = j
            } else {
                let t = [l.begin, l.end].compactMap { $0 }.joined(separator: "–")
                let dot = l.isCurrent ? "🟢" : "⚪️"
                let row = rowButton("\(dot) \(t)  \(l.subject) · \(l.group)\(absenceSummary([l]))",
                                    action: #selector(AppDelegate.pickLesson(_:)),
                                    index: i, width: cfg.panelWidth - 28)
                documentStack.addArrangedSubview(row)
                if app.expandedKeys.contains("les\(i)") {
                    addStudentsRow(block: [l], activeIdx: 0, blockKey: nil, expKey: "les\(i)")
                }
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
            let title = "\(k + 1)-й (\(ls.begin ?? ""))"
            let b = TagButton(title: title, target: app, action: #selector(AppDelegate.doChooseLesson(_:)))
            b.bezelStyle = .inline
            b.controlSize = .small
            let bf = NSFont.systemFont(ofSize: 10, weight: .medium)
            b.font = bf
            b.attributedTitle = NSAttributedString(string: title, attributes: [.foregroundColor: NSColor.white, .font: bf])
            b.payload = ["kind": "choose", "blockKey": key, "lessonIdx": i_of(block, ls)]
            b.wantsLayer = true
            b.layer?.cornerRadius = 6
            b.layer?.borderWidth = 0.5
            b.layer?.borderColor = NSColor.white.withAlphaComponent(0.16).cgColor
            if k == activeIdx {
                b.layer?.backgroundColor = NSColor.systemTeal.withAlphaComponent(0.35).cgColor
            } else {
                b.layer?.backgroundColor = NSColor.white.withAlphaComponent(0.10).cgColor
            }
            row.addArrangedSubview(b)
        }
        wrap.addArrangedSubview(row)
        documentStack.addArrangedSubview(wrap)
    }

    /// « · Н 2 · УП 5» для строки урока/блока: ученик считается один раз на блок
    private func absenceSummary(_ block: [Lesson]) -> String {
        var reasons: [Int: Int] = [:]
        for l in block {
            guard let lid = l.scheduleLessonId else { continue }
            for (sid, r) in app?.absenceCache[lid] ?? [:] where reasons[sid] == nil { reasons[sid] = r }
        }
        let up = reasons.values.filter { $0 == 2 }.count
        let n = reasons.count - up
        return (n > 0 ? " · Н \(n)" : "") + (up > 0 ? " · УП \(up)" : "")
    }

    private func i_of(_ block: [Lesson], _ l: Lesson) -> Int {
        app?.lessons.firstIndex(where: { $0.scheduleLessonId == l.scheduleLessonId }) ?? 0
    }

    func addStudentsRow(block: [Lesson], activeIdx: Int, blockKey: String?, expKey: String) {
        guard let app, let first = block.first, first.groupId != 0 else { return }
        let groupId = first.groupId
        let wrap = NSStackView()
        wrap.orientation = .vertical
        wrap.spacing = 3
        wrap.alignment = .leading
        if let st = app.studentsCache[groupId] {
            if let marked = app.rollCall[expKey] {
                renderRollCall(into: wrap, students: st, block: block, activeIdx: activeIdx,
                               expKey: expKey, marked: marked)
            } else {
                let start = smallButton("📋 Перекличка", action: #selector(AppDelegate.doRollStart(_:)),
                                        payload: ["expKey": expKey])
                wrap.addArrangedSubview(indented([start]))
                renderStudents(into: wrap, students: st, block: block, activeIdx: activeIdx, blockKey: blockKey)
            }
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

    private func indented(_ views: [NSView]) -> NSStackView {
        let row = NSStackView()
        row.orientation = .horizontal
        row.spacing = 6
        row.alignment = .centerY
        let sp = NSView(); sp.widthAnchor.constraint(equalToConstant: 14).isActive = true
        row.addArrangedSubview(sp)
        views.forEach { row.addArrangedSubview($0) }
        return row
    }

    private func smallButton(_ s: String, action: Selector, payload: [String: Any],
                             bg: NSColor = NSColor.white.withAlphaComponent(0.10),
                             color: NSColor = .white, width: CGFloat? = nil) -> TagButton {
        let b = TagButton(title: s, target: app, action: action)
        let bf = NSFont.systemFont(ofSize: 11, weight: .medium)
        b.bezelStyle = .inline
        b.controlSize = .small
        b.font = bf
        b.attributedTitle = NSAttributedString(string: s, attributes: [.foregroundColor: color, .font: bf])
        b.payload = payload
        b.wantsLayer = true
        b.layer?.cornerRadius = 6
        b.layer?.borderWidth = 0.5
        b.layer?.borderColor = NSColor.white.withAlphaComponent(0.16).cgColor
        b.layer?.backgroundColor = bg.cgColor
        if let width {
            b.widthAnchor.constraint(equalToConstant: width).isActive = true
            b.alignment = .left
        }
        return b
    }

    /// Перекличка: клик по ученику — отметить отсутствующим; уже отсутствующие — без клика
    func renderRollCall(into wrap: NSStackView, students: [[String: Any]], block: [Lesson],
                        activeIdx: Int, expKey: String, marked: Set<Int>) {
        let activeLid = block[activeIdx].scheduleLessonId
        let hint = block.count > 1 ? "Отметь отсутствующих (урок \(activeIdx + 1)-й)" : "Отметь отсутствующих"
        wrap.addArrangedSubview(indented([label(hint, size: 10, bold: false,
                                                color: NSColor.white.withAlphaComponent(0.6))]))
        for st in students {
            guard let id = st["id"] as? Int, let name = st["name"] as? String else { continue }
            if let lid = activeLid, let r = app?.absenceCache[lid]?[id] {
                let done = label("\(r == 2 ? "УП" : "Н") · \(name) — уже в журнале", size: 12, bold: false,
                                 color: NSColor.white.withAlphaComponent(0.4))
                wrap.addArrangedSubview(indented([done]))
                continue
            }
            let on = marked.contains(id)
            let b = smallButton(on ? "Н · \(name)" : name, action: #selector(AppDelegate.doRollToggle(_:)),
                                payload: ["expKey": expKey, "student": id],
                                bg: on ? NSColor.systemOrange.withAlphaComponent(0.45) : NSColor.white.withAlphaComponent(0.04),
                                width: cfg.panelWidth - 80)
            wrap.addArrangedSubview(indented([b]))
        }
        var btns: [NSView] = []
        let ids = block.compactMap { $0.scheduleLessonId }
        if let lid = activeLid {
            btns.append(smallButton("Отправить Н (\(marked.count))", action: #selector(AppDelegate.doRollSubmit(_:)),
                                    payload: ["expKey": expKey, "lessonIds": [lid]],
                                    bg: NSColor.systemOrange.withAlphaComponent(marked.isEmpty ? 0.15 : 0.45)))
        }
        if ids.count > 1 {
            btns.append(smallButton("Н на все \(ids.count) урока", action: #selector(AppDelegate.doRollSubmit(_:)),
                                    payload: ["expKey": expKey, "lessonIds": ids],
                                    bg: NSColor.systemOrange.withAlphaComponent(marked.isEmpty ? 0.15 : 0.30)))
        }
        btns.append(smallButton("Отмена", action: #selector(AppDelegate.doRollCancel(_:)),
                                payload: ["expKey": expKey]))
        wrap.addArrangedSubview(indented(btns))
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
            let activeLesson = block[activeIdx]
            // отсутствующий (Н/УП) на активном уроке, иначе на любом уроке блока — приглушить имя
            let reason = activeLesson.scheduleLessonId.flatMap { app?.absenceCache[$0]?[id] }
                ?? block.lazy.compactMap { $0.scheduleLessonId.flatMap { app?.absenceCache[$0]?[id] } }.first
            let nameText = reason == nil ? name : "\(reason == 2 ? "УП" : "Н") · \(name)"
            let nameL = label(nameText, size: 12, bold: false,
                              color: reason == nil ? NSColor.white : NSColor.white.withAlphaComponent(0.4))
            nameL.widthAnchor.constraint(equalToConstant: 170).isActive = true
            nameL.lineBreakMode = .byTruncatingTail
            row.addArrangedSubview(nameL)
            for g in [5, 4, 3, 2, 0] {
                var rep: [String: Any] = g == 0
                    ? ["kind": "absence", "student": id, "name": name, "lessonId": activeLesson.scheduleLessonId ?? 0]
                    : ["kind": "mark", "student": id, "grade": g, "name": name, "lessonId": activeLesson.scheduleLessonId ?? 0]
                if blockKey != nil { rep["blockKey"] = blockKey }
                let b = TagButton(title: g == 0 ? "Н" : String(g), target: app, action: #selector(AppDelegate.doGrade(_:)))
                let bf = NSFont.systemFont(ofSize: 10, weight: .medium)
                b.bezelStyle = .inline
                b.controlSize = .small
                b.font = bf
                b.attributedTitle = NSAttributedString(
                    string: g == 0 ? "Н" : String(g),
                    attributes: [.foregroundColor: NSColor.white, .font: bf])
                b.payload = rep
                b.wantsLayer = true
                b.layer?.cornerRadius = 6
                b.layer?.borderWidth = 0.5
                b.layer?.borderColor = NSColor.white.withAlphaComponent(0.16).cgColor
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
                              app?.absenceCache[lid]?[id] != nil else { continue }
                        b.layer?.backgroundColor = NSColor.systemOrange.withAlphaComponent(0.45).cgColor
                        break
                    }
                }
                // запрос в пути — жёлтым поверх остальных подсветок
                if app?.isInFlight(kind: g == 0 ? "absence" : "mark", student: id,
                                   lessonId: activeLesson.scheduleLessonId, grade: g == 0 ? nil : g) == true {
                    b.layer?.backgroundColor = NSColor.systemYellow.withAlphaComponent(0.55).cgColor
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
    let font = NSFont.systemFont(ofSize: 12, weight: .medium)
    b.attributedTitle = NSAttributedString(string: title, attributes: [.foregroundColor: NSColor.white, .font: font])
    b.wantsLayer = true
    b.layer?.cornerRadius = 6
    b.layer?.backgroundColor = NSColor.white.withAlphaComponent(0.06).cgColor
    b.setButtonType(.momentaryPushIn)
    b.widthAnchor.constraint(equalToConstant: width).isActive = true
    b.alignment = .left
    return b
}

    private func rowButton(_ title: String, action: Selector, index: Int, width: CGFloat) -> LessonRowButton {
        let b = LessonRowButton(title: title, target: app, action: action)
        b.lessonIndex = index
        b.bezelStyle = .recessed
        let font = NSFont.systemFont(ofSize: 12)
        b.font = font
        // на тёмной панели текст не должен уезжать в системный labelColor (чёрный)
        b.attributedTitle = NSAttributedString(string: title, attributes: [.foregroundColor: NSColor.white, .font: font])
        b.wantsLayer = true
        b.layer?.cornerRadius = 6
        b.layer?.backgroundColor = NSColor.white.withAlphaComponent(0.06).cgColor
        b.imageScaling = .scaleNone
        b.setButtonType(.momentaryPushIn)
        b.widthAnchor.constraint(equalToConstant: width).isActive = true
        b.alignment = .left
        return b
    }
}