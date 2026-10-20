import AppKit
import Foundation

// MeshPult — «учительский пульт» МЭШ в строке меню.
// Мозг — CLI-мост mesh_remote.py (переиспользует mesh_automation_v3:
// токен-цепочка, расписание, профили, оценки/Н/без д/з).
// Конфиг: ~/.config/mesh-remote/config.json (опционально).

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

    static func load() -> PultConfig {
        var c = PultConfig()
        let path = NSHomeDirectory() + "/.config/mesh-remote/config.json"
        if let data = FileManager.default.contents(atPath: path),
           let obj = try? JSONSerialization.jsonObject(with: data) as? [String: String] {
            if let p = obj["python"], !p.isEmpty { c.python = p }
            if let b = obj["bridge"], !b.isEmpty { c.bridge = b }
        }
        return c
    }
}

let cfg = PultConfig.load()

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    var statusItem: NSStatusItem!
    let queue = DispatchQueue(label: "mesh-pult", qos: .utility)
    var lessons: [Lesson] = []
    var headerText = "Загрузка…"
    var lastAction = ""
    var tokenExp = ""
    var lessonSubmenus: [Int: NSMenu] = [:]          // index → submenu уроков
    var studentMenus: [Int: NSMenu] = [:]            // lessonIdx → меню учеников
    var menuInfo: [ObjectIdentifier: [String: Any]] = [:]
    var studentsCache: [Int: [[String: Any]]] = [:]  // groupId → студенты
    var headerItem: NSMenuItem!
    var actionItem: NSMenuItem!

    func applicationDidFinishLaunching(_ n: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.title = "🎓 …"
        statusItem.menu = buildMenu()
        refreshAll()
        Timer.scheduledTimer(withTimeInterval: 300, repeats: true) { [weak self] _ in self?.refreshAll() }
    }

    func buildMenu() -> NSMenu {
        let menu = NSMenu()
        menu.delegate = self
        headerItem = NSMenuItem(title: headerText, action: nil, keyEquivalent: "")
        headerItem.isEnabled = false
        menu.addItem(headerItem)
        menu.addItem(.separator())

        for (i, l) in lessons.enumerated() {
            let mi = NSMenuItem(title: lessonTitle(l), action: nil, keyEquivalent: "")
            let sub = studentMenu(lessonIndex: i, lesson: l)
            mi.submenu = sub
            menu.addItem(mi)
        }
        if lessons.isEmpty {
            let mi = NSMenuItem(title: "Уроков на сегодня нет", action: nil, keyEquivalent: "")
            mi.isEnabled = false
            menu.addItem(mi)
        }
        menu.addItem(.separator())

        actionItem = NSMenuItem(title: lastAction.isEmpty ? " " : lastAction, action: nil, keyEquivalent: "")
        actionItem.isEnabled = false
        menu.addItem(actionItem)

        let nh = NSMenuItem(title: "«Без д/з» на текущем уроке", action: #selector(noHomeworkNow(_:)), keyEquivalent: "")
        nh.target = self
        menu.addItem(nh)

        let ref = NSMenuItem(title: "Обновить (расписание/статус)", action: #selector(forceRefresh(_:)), keyEquivalent: "r")
        ref.target = self
        menu.addItem(ref)
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "Закрыть пульт", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
        return menu
    }

    func lessonTitle(_ l: Lesson) -> String {
        let dot = l.isCurrent ? "🟢" : "⚪"
        let time = [l.begin, l.end].compactMap { $0 }.joined(separator: "–")
        return "\(dot) \(time) \(l.subject) — \(l.group)"
    }

    // ученики: лениво, по открытию подменю урока
    func studentMenu(lessonIndex: Int, lesson: Lesson) -> NSMenu {
        let sub = NSMenu()
        studentMenus[lessonIndex] = sub
        sub.delegate = self
        let loading = NSMenuItem(title: "Загрузка учеников…", action: nil, keyEquivalent: "")
        loading.isEnabled = false
        sub.addItem(loading)
        if let cached = studentsCache[lesson.groupId] {
            renderStudents(into: sub, students: cached, lessonIndex: lessonIndex, lesson: lesson)
        }
        menuInfo[ObjectIdentifier(sub)] = ["groupId": lesson.groupId, "lessonIndex": lessonIndex,
                                           "lessonId": lesson.scheduleLessonId ?? 0]
        return sub
    }

    func renderStudents(into sub: NSMenu, students: [[String: Any]], lessonIndex: Int, lesson: Lesson) {
        sub.removeAllItems()
        let header = NSMenuItem(title: lessonTitle(lesson), action: nil, keyEquivalent: "")
        header.isEnabled = false
        sub.addItem(header)
        sub.addItem(.separator())
        for st in students {
            guard let id = st["id"] as? Int, let name = st["name"] as? String else { continue }
            let mi = NSMenuItem(title: name, action: nil, keyEquivalent: "")
            let sm = NSMenu()
            for g in [5, 4, 3, 2] {
                let a = NSMenuItem(title: "Оценка \(g)", action: #selector(doMark(_:)), keyEquivalent: "")
                a.target = self
                a.representedObject = ["kind": "mark", "student": id, "grade": g,
                                       "name": name, "lessonId": lesson.scheduleLessonId ?? 0]
                sm.addItem(a)
            }
            let ab = NSMenuItem(title: "Н (не был)", action: #selector(doMark(_:)), keyEquivalent: "")
            ab.target = self
            ab.representedObject = ["kind": "absence", "student": id, "name": name,
                                    "lessonId": lesson.scheduleLessonId ?? 0]
            sm.addItem(ab)
            mi.submenu = sm
            sub.addItem(mi)
        }
        if students.isEmpty {
            let e = NSMenuItem(title: "(ученики не найдены)", action: nil, keyEquivalent: "")
            e.isEnabled = false
            sub.addItem(e)
        }
    }

    func menuWillOpen(_ menu: NSMenu) {
        // подменю урока открыли → тянем его учеников, если ещё не в кэше
        guard let info = menuInfo[ObjectIdentifier(menu)],
              let groupId = info["groupId"] as? Int,
              studentsCache[groupId] == nil else { return }
        let lessonIndex = info["lessonIndex"] as? Int ?? 0
        let lesson = lessons.indices.contains(lessonIndex) ? lessons[lessonIndex] : Lesson(group: "?", groupId: groupId, subject: "?", begin: nil, end: nil, isCurrent: false, scheduleLessonId: nil)
        setHeader("Загружаю учеников \(lesson.group)…")
        bridge(["students", "--group-id", String(groupId)], group: groupId) { [weak self] res in
            guard let self else { return }
            switch res {
            case .success(let data):
                let st = data["students"] as? [[String: Any]] ?? []
                self.studentsCache[groupId] = st
                if let sm = self.studentMenus[lessonIndex] {
                    self.renderStudents(into: sm, students: st, lessonIndex: lessonIndex, lesson: lesson)
                }
                self.setHeader("Ученики \(lesson.group): \(st.count)")
            case .failure(let e):
                self.setHeader("Ошибка учеников: \(e.localizedDescription)")
            }
        }
    }

    func setHeader(_ s: String) {
        headerText = s
        headerItem?.title = s
    }

    // MARK: мост-CLI

    func bridge(_ args: [String], completion: @escaping (Result<[String: Any], Error>) -> Void) {
        queue.async {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: cfg.python)
            p.arguments = [cfg.bridge] + args
            let out = Pipe(), err = Pipe()
            p.standardOutput = out; p.standardError = err
            do { try p.run() } catch {
                DispatchQueue.main.async { completion(.failure(error)) }
                return
            }
            let data = out.fileHandleForReading.readDataToEndOfFile()
            let errData = err.fileHandleForReading.readDataToEndOfFile()
            p.waitUntilExit()
            DispatchQueue.main.async {
                let text = String(data: data, encoding: .utf8) ?? ""
                if let obj = (try? JSONSerialization.jsonObject(with: Data(text.utf8))) as? [String: Any] {
                    if obj["error"] != nil {
                        completion(.failure(NSError(domain: "mesh", code: 1,
                            userInfo: [NSLocalizedDescriptionKey: String(describing: obj["error"]!)])))
                    } else {
                        completion(.success(obj))
                    }
                } else {
                    let e = String(data: errData, encoding: .utf8) ?? "?"
                    completion(.failure(NSError(domain: "mesh", code: 2,
                        userInfo: [NSLocalizedDescriptionKey: "нет JSON от моста: \(e.prefix(180))"])))
                }
            }
        }
    }

    func bridge(_ args: [String], group: Int? = nil, completion: @escaping (Result<[String: Any], Error>) -> Void) {
        bridge(args, completion: completion)
    }

    func refreshAll() {
        statusItem.button?.title = "🎓 …"
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
                self.statusItem.menu?.delegate = nil
                self.statusItem.menu = self.buildMenu()
                self.headerText = list.isEmpty ? "Сегодня нет уроков" :
                    ("Уроков сегодня: \(list.count)" + (list.contains { $0.isCurrent } ? " · сейчас идёт урок" : ""))
                self.headerItem?.title = self.headerText
                self.statusItem.button?.title = "🎓" + (list.contains { $0.isCurrent } ? " 🟢" : "")
                self.fetchStatus()
            case .failure(let e):
                self.setHeader("Ошибка МЭШ: \(e.localizedDescription)")
                self.statusItem.button?.title = "🎓 🔴"
            }
        }
    }

    func fetchStatus() {
        bridge(["status"]) { [weak self] res in
            if case .success(let d) = res {
                self?.tokenExp = d["token_expires"] as? String ?? ""
                let expLine = self?.tokenExp.isEmpty == false ? " · токен до \(self!.tokenExp)" : ""
                self?.headerText = (self?.headerText ?? "") + expLine
                self?.headerItem?.title = self?.headerText ?? ""
            }
        }
    }

    // MARK: действия

    func tag(_ kind: String) -> String {
        switch kind {
        case "absence": return "Н"
        case "nohomework": return "без д/з"
        default: return "оценка"
        }
    }

    func perform(kind: String, studentId: Int? = nil, grade: Int? = nil,
                 lessonId: Int? = nil, name: String = "") {
        var args = ["act", kind]
        if let studentId { args += ["--student-id", String(studentId)] }
        if let grade { args += ["--grade", String(grade)] }
        if let lessonId, lessonId != 0 { args += ["--schedule-lesson-id", String(lessonId)] }
        setHeader("Выполняю \(tag(kind)) \(name)…")
        bridge(args) { [weak self] res in
            guard let self else { return }
            switch res {
            case .success:
                self.actionItem?.title = "Последнее действие: \(self.tag(kind)) \(name) → ОК"
                self.notify("\(name) — \(self.tag(kind))\(grade.map { " \($0)" } ?? "")", ok: true)
                self.setHeader("Готово: \(self.tag(kind)) \(name)")
            case .failure(let e):
                self.actionItem?.title = "ОШИБКА: \(e.localizedDescription)"
                self.notify("Ошибка: \(e.localizedDescription)", ok: false)
                self.setHeader("Не удалось: \(e.localizedDescription)")
            }
        }
    }

    @objc func doMark(_ sender: NSMenuItem) {
        guard let info = sender.representedObject as? [String: Any],
              let kind = info["kind"] as? String else { return }
        let student = info["student"] as? Int
        let grade = info["grade"] as? Int
        let name = info["name"] as? String ?? ""
        let lessonId = info["lessonId"] as? Int
        let label = name.isEmpty ? "группа (без д/з)" : name
        // клик осознанный; всё видно в журнале и в пункте «Последнее действие»
        perform(kind: kind, studentId: student, grade: grade, lessonId: lessonId, name: label)
    }

    @objc func noHomeworkNow(_ sender: NSMenuItem) {
        perform(kind: "nohomework", name: "без д/з")
    }

    @objc func forceRefresh(_ sender: NSMenuItem) {
        refreshAll()
    }

    func notify(_ msg: String, ok: Bool) {
        let esc = msg.replacingOccurrences(of: "\"", with: "'")
        subprocess(["osascript", "-e",
            "display notification \"\(esc.prefix(160))\" with title \"Пульт МЭШ\" subTitle \"\(ok ? "✅" : "⚠️")\"" ])
    }
}

func subprocess(_ args: [String]) {
    let p = Process(); p.executableURL = URL(fileURLWithPath: args[0])
    p.arguments = Array(args.dropFirst())
    p.standardOutput = Pipe(); p.standardError = Pipe()
    try? p.run()
}

func tagGlobal(_ kind: String) -> String {
    switch kind {
    case "absence": return "Н"
    case "nohomework": return "без д/з"
    default: return "оценка"
    }
}

let appImpl = NSApplication.shared
let delegatePult = AppDelegate()
appImpl.delegate = delegatePult
appImpl.setActivationPolicy(.accessory)
appImpl.run()
