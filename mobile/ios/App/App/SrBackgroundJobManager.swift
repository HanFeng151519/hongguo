import Foundation
import UIKit
import UserNotifications

enum SrNotificationHelper {
    static func requestPermission(completion: @escaping (Bool) -> Void) {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) { granted, _ in
            DispatchQueue.main.async { completion(granted) }
        }
    }

    static func notifySuccess(filename: String) {
        let content = UNMutableNotificationContent()
        content.title = "视频超分完成"
        content.body = "「\(filename)」已处理为 1080×1920，打开 App 在成片库中保存"
        content.sound = .default
        let req = UNNotificationRequest(
            identifier: "sr_done_\(UUID().uuidString)",
            content: content,
            trigger: nil
        )
        UNUserNotificationCenter.current().add(req)
    }

    static func notifyFailure(_ message: String) {
        let content = UNMutableNotificationContent()
        content.title = "视频超分失败"
        content.body = message
        content.sound = .default
        let req = UNNotificationRequest(
            identifier: "sr_fail_\(UUID().uuidString)",
            content: content,
            trigger: nil
        )
        UNUserNotificationCenter.current().add(req)
    }
}

struct SrJobState: Codable {
    let id: String
    var status: String
    var progress: Double
    var message: String
    var outputPath: String?
    var outputWidth: Int?
    var outputHeight: Int?
    var displayFilename: String?
    var error: String?
}

final class SrBackgroundJobManager {
    static let shared = SrBackgroundJobManager()

    private let workQueue = DispatchQueue(label: "com.hongguo.crawl.sr.bg", qos: .userInitiated)
    private let lock = NSLock()
    private var bgTaskId: UIBackgroundTaskIdentifier = .invalid
    private var job: SrJobState?
    private var cancelRequested = false
    private weak var plugin: RealEsrganPlugin?

    private let storageKey = "sr_background_job"

    func attach(plugin: RealEsrganPlugin) {
        self.plugin = plugin
        job = loadPersisted()
    }

    func currentJob() -> SrJobState? {
        lock.lock()
        defer { lock.unlock() }
        return job
    }

    func isCancelRequested() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        return cancelRequested
    }

    /// 取消进行中的任务并清除记录（可删除卡住的本机超分）
    func cancelJob() {
        lock.lock()
        cancelRequested = true
        let snapshot = job
        job = nil
        lock.unlock()
        UserDefaults.standard.removeObject(forKey: storageKey)
        cleanupOutputFiles(for: snapshot)
    }

    func clearJob() {
        cancelJob()
    }

    func start(inputURL: URL, displayFilename: String) throws -> String {
        lock.lock()
        if let existing = job, existing.status == "running" {
            lock.unlock()
            throw NSError(
                domain: "RealEsrgan",
                code: 409,
                userInfo: [NSLocalizedDescriptionKey: "已有超分任务在进行中，请先取消或等待完成"]
            )
        }
        cancelRequested = false
        let jobId = UUID().uuidString
        job = SrJobState(
            id: jobId,
            status: "running",
            progress: 0,
            message: "准备中…",
            outputPath: nil,
            outputWidth: nil,
            outputHeight: nil,
            displayFilename: displayFilename,
            error: nil
        )
        persistLocked()
        lock.unlock()

        beginBackgroundTask()
        workQueue.async { [weak self] in
            self?.runJob(inputURL: inputURL, jobId: jobId, displayFilename: displayFilename)
        }
        return jobId
    }

    private func runJob(inputURL: URL, jobId: String, displayFilename: String) {
        defer {
            lock.lock()
            cancelRequested = false
            lock.unlock()
            endBackgroundTask()
        }
        do {
            try throwIfCancelled()
            update(jobId: jobId) { j in
                j.message = "加载模型…"
                j.progress = 0.05
            }
            let modelUrl = try RealEsrganModelLoader.ensureModelDownloaded { [weak self] msg in
                guard let self else { return }
                if self.isCancelRequested() { return }
                self.update(jobId: jobId) { j in
                    j.message = msg
                    j.progress = 0.08
                }
                self.emitProgress(message: msg, progress: 0.08)
            }
            try throwIfCancelled()
            try RealEsrganEngine.shared.loadModel(at: modelUrl) { [weak self] msg in
                guard let self else { return }
                if self.isCancelRequested() { return }
                self.update(jobId: jobId) { j in
                    j.message = msg
                    j.progress = 0.1
                }
                self.emitProgress(message: msg, progress: 0.1)
            }

            let processor = RealEsrganVideoProcessor()
            let result = try processor.process(inputURL: inputURL, progress: { [weak self] fraction, message in
                guard let self else { return }
                if self.isCancelRequested() { return }
                self.update(jobId: jobId) { j in
                    j.progress = fraction
                    j.message = message
                }
                self.emitProgress(message: message, progress: fraction)
            }, shouldCancel: { [weak self] in
                self?.isCancelRequested() == true
            })

            try throwIfCancelled()
            let persisted = try persistOutput(result.outputURL, jobId: jobId)
            let outputUri = persisted.absoluteString

            update(jobId: jobId) { j in
                j.status = "done"
                j.progress = 1
                j.message = "超分完成"
                j.outputPath = outputUri
                j.outputWidth = result.outputWidth
                j.outputHeight = result.outputHeight
            }
            SrNotificationHelper.notifySuccess(filename: displayFilename)
            emitComplete(jobId: jobId)
        } catch let err as RealEsrganError {
            if err == .cancelled {
                handleCancelled(jobId: jobId)
                return
            }
            let msg = err.localizedDescription
            update(jobId: jobId) { j in
                j.status = "failed"
                j.error = msg
                j.message = msg
            }
            SrNotificationHelper.notifyFailure(msg)
            emitComplete(jobId: jobId)
        } catch {
            if isCancelRequested() {
                handleCancelled(jobId: jobId)
                return
            }
            let msg = error.localizedDescription
            update(jobId: jobId) { j in
                j.status = "failed"
                j.error = msg
                j.message = msg
            }
            SrNotificationHelper.notifyFailure(msg)
            emitComplete(jobId: jobId)
        }
    }

    private func throwIfCancelled() throws {
        if isCancelRequested() { throw RealEsrganError.cancelled }
    }

    private func handleCancelled(jobId: String) {
        let snapshot: SrJobState? = {
            lock.lock()
            defer { lock.unlock() }
            return job
        }()
        cleanupOutputFiles(for: snapshot)
        lock.lock()
        job = nil
        lock.unlock()
        UserDefaults.standard.removeObject(forKey: storageKey)
        DispatchQueue.main.async { [weak self] in
            self?.plugin?.notifyListeners("jobComplete", data: [
                "jobId": jobId,
                "status": "cancelled",
                "outputPath": "",
                "outputWidth": 0,
                "outputHeight": 0,
                "displayFilename": snapshot?.displayFilename ?? "",
                "error": "任务已取消",
            ])
        }
    }

    private func cleanupOutputFiles(for snapshot: SrJobState?) {
        guard let snapshot else { return }
        if let path = snapshot.outputPath, let url = URL(string: path) {
            try? FileManager.default.removeItem(at: url)
        }
        if let docs = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first {
            let dest = docs.appendingPathComponent("sr_\(snapshot.id).mp4")
            try? FileManager.default.removeItem(at: dest)
        }
    }

    private func persistOutput(_ tempURL: URL, jobId: String) throws -> URL {
        guard let docs = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first else {
            return tempURL
        }
        let dest = docs.appendingPathComponent("sr_\(jobId).mp4")
        try? FileManager.default.removeItem(at: dest)
        try FileManager.default.copyItem(at: tempURL, to: dest)
        try? FileManager.default.removeItem(at: tempURL)
        return dest
    }

    private func update(jobId: String, mutate: (inout SrJobState) -> Void) {
        lock.lock()
        guard var j = job, j.id == jobId else {
            lock.unlock()
            return
        }
        mutate(&j)
        job = j
        persistLocked()
        lock.unlock()
    }

    private func emitProgress(message: String, progress: Double) {
        DispatchQueue.main.async { [weak self] in
            self?.plugin?.notifyListeners("progress", data: [
                "message": message,
                "progress": progress,
            ])
        }
    }

    private func emitComplete(jobId: String) {
        let snapshot: SrJobState? = {
            lock.lock()
            defer { lock.unlock() }
            guard let j = job, j.id == jobId else { return nil }
            return j
        }()
        guard let snapshot else { return }
        DispatchQueue.main.async { [weak self] in
            self?.plugin?.notifyListeners("jobComplete", data: [
                "jobId": snapshot.id,
                "status": snapshot.status,
                "outputPath": snapshot.outputPath ?? "",
                "outputWidth": snapshot.outputWidth ?? 0,
                "outputHeight": snapshot.outputHeight ?? 0,
                "displayFilename": snapshot.displayFilename ?? "",
                "error": snapshot.error ?? "",
            ])
        }
    }

    private func beginBackgroundTask() {
        DispatchQueue.main.async {
            if self.bgTaskId != .invalid { return }
            self.bgTaskId = UIApplication.shared.beginBackgroundTask(withName: "RealESRGAN") { [weak self] in
                self?.endBackgroundTask()
            }
        }
    }

    private func endBackgroundTask() {
        DispatchQueue.main.async {
            if self.bgTaskId != .invalid {
                UIApplication.shared.endBackgroundTask(self.bgTaskId)
                self.bgTaskId = .invalid
            }
        }
    }

    private func persistLocked() {
        guard let job else {
            UserDefaults.standard.removeObject(forKey: storageKey)
            return
        }
        if let data = try? JSONEncoder().encode(job) {
            UserDefaults.standard.set(data, forKey: storageKey)
        }
    }

    private func loadPersisted() -> SrJobState? {
        guard let data = UserDefaults.standard.data(forKey: storageKey) else { return nil }
        return try? JSONDecoder().decode(SrJobState.self, from: data)
    }
}

extension RealEsrganError: Equatable {
    static func == (lhs: RealEsrganError, rhs: RealEsrganError) -> Bool {
        switch (lhs, rhs) {
        case (.modelMissing, .modelMissing), (.invalidInput, .invalidInput), (.cancelled, .cancelled):
            return true
        case (.modelLoad(let a), .modelLoad(let b)):
            return a == b
        case (.predictionFailed(let a), .predictionFailed(let b)):
            return a == b
        default:
            return false
        }
    }
}
