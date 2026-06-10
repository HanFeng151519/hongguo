import Capacitor
import Foundation

@objc(RealEsrganPlugin)
public class RealEsrganPlugin: CAPPlugin, CAPBridgedPlugin {
    public let identifier = "RealEsrganPlugin"
    public let jsName = "RealEsrgan"
    public let pluginMethods: [CAPPluginMethod] = [
        CAPPluginMethod(name: "isAvailable", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "prepareModel", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "superResolveVideo", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "requestNotificationPermission", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "startSuperResolveInBackground", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "getBackgroundJobStatus", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "clearBackgroundJob", returnType: CAPPluginReturnPromise),
    ]

    public override func load() {
        SrBackgroundJobManager.shared.attach(plugin: self)
    }

    @objc func isAvailable(_ call: CAPPluginCall) {
        let present = RealEsrganModelLoader.isModelPresent()
        call.resolve([
            "available": present,
            "downloadable": true,
            "backend": present ? "coreml" : "none",
            "bundled": RealEsrganModelLoader.bundledModelURL() != nil,
        ])
    }

    @objc func requestNotificationPermission(_ call: CAPPluginCall) {
        SrNotificationHelper.requestPermission { granted in
            call.resolve(["granted": granted])
        }
    }

    @objc func prepareModel(_ call: CAPPluginCall) {
        DispatchQueue.global(qos: .userInitiated).async {
            do {
                let url = try RealEsrganModelLoader.ensureModelDownloaded { msg in
                    self.notifyListeners("progress", data: ["message": msg, "progress": 0.05])
                }
                try RealEsrganEngine.shared.loadModel(at: url) { msg in
                    self.notifyListeners("progress", data: ["message": msg, "progress": 0.08])
                }
                call.resolve(["ok": true, "backend": "coreml", "path": url.lastPathComponent])
            } catch {
                call.reject(error.localizedDescription)
            }
        }
    }

    @objc func startSuperResolveInBackground(_ call: CAPPluginCall) {
        guard let inputPath = call.getString("inputPath"), !inputPath.isEmpty else {
            call.reject("缺少 inputPath")
            return
        }
        guard let inputURL = resolveFileURL(inputPath) else {
            call.reject("无法读取输入视频路径")
            return
        }
        let displayFilename = call.getString("displayFilename") ?? "video_sr.mp4"

        SrNotificationHelper.requestPermission { granted in
            do {
                let jobId = try SrBackgroundJobManager.shared.start(
                    inputURL: inputURL,
                    displayFilename: displayFilename
                )
                call.resolve([
                    "jobId": jobId,
                    "started": true,
                    "notificationGranted": granted,
                ])
            } catch {
                call.reject(error.localizedDescription)
            }
        }
    }

    @objc func getBackgroundJobStatus(_ call: CAPPluginCall) {
        guard let job = SrBackgroundJobManager.shared.currentJob() else {
            call.resolve(["status": "idle"])
            return
        }
        call.resolve([
            "jobId": job.id,
            "status": job.status,
            "progress": job.progress,
            "message": job.message,
            "outputPath": job.outputPath ?? "",
            "outputWidth": job.outputWidth ?? 0,
            "outputHeight": job.outputHeight ?? 0,
            "displayFilename": job.displayFilename ?? "",
            "error": job.error ?? "",
        ])
    }

    @objc func clearBackgroundJob(_ call: CAPPluginCall) {
        SrBackgroundJobManager.shared.clearJob()
        call.resolve(["ok": true])
    }

    @objc func superResolveVideo(_ call: CAPPluginCall) {
        guard let inputPath = call.getString("inputPath"), !inputPath.isEmpty else {
            call.reject("缺少 inputPath")
            return
        }
        guard let inputURL = resolveFileURL(inputPath) else {
            call.reject("无法读取输入视频路径")
            return
        }

        DispatchQueue.global(qos: .userInitiated).async {
            do {
                let modelUrl = try RealEsrganModelLoader.ensureModelDownloaded { msg in
                    self.notifyListeners("progress", data: ["message": msg, "progress": 0.05])
                }
                try RealEsrganEngine.shared.loadModel(at: modelUrl) { msg in
                    self.notifyListeners("progress", data: ["message": msg, "progress": 0.08])
                }
                let processor = RealEsrganVideoProcessor()
                let result = try processor.process(inputURL: inputURL) { fraction, message in
                    self.notifyListeners("progress", data: [
                        "progress": fraction,
                        "message": message,
                    ])
                }
                call.resolve([
                    "outputPath": result.outputURL.absoluteString,
                    "outputWidth": result.outputWidth,
                    "outputHeight": result.outputHeight,
                    "frameCount": result.frameCount,
                    "backend": "coreml",
                ])
            } catch {
                call.reject(error.localizedDescription)
            }
        }
    }

    private func resolveFileURL(_ path: String) -> URL? {
        if path.hasPrefix("file://"), let url = URL(string: path) {
            return url
        }
        if path.hasPrefix("/") {
            return URL(fileURLWithPath: path)
        }
        return nil
    }
}
