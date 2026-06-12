import AVFoundation
import Capacitor
import Foundation
import Photos

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
        CAPPluginMethod(name: "cancelBackgroundJob", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "saveToPhotos", returnType: CAPPluginReturnPromise),
    ]

    public override func load() {
        SrBackgroundJobManager.shared.attach(plugin: self)
    }

    @objc func isAvailable(_ call: CAPPluginCall) {
        let profile = SrIosModelProfile.parse(call.getString("srProfile"))
        call.resolve([
            "available": true,
            "downloadable": false,
            "backend": "coreimage-natural",
            "bundled": true,
            "srProfile": profile.rawValue,
        ])
    }

    @objc func requestNotificationPermission(_ call: CAPPluginCall) {
        SrNotificationHelper.requestPermission { granted in
            call.resolve(["granted": granted])
        }
    }

    @objc func prepareModel(_ call: CAPPluginCall) {
        call.resolve([
            "ok": true,
            "backend": "coreimage-natural",
            "path": "builtin",
        ])
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
        let outputScale = call.getString("outputScale") ?? "1080"
        let srProfile = SrIosModelProfile.parse(call.getString("srProfile"))
        
        // Parse output resolution
        let resolution: RealEsrganVideoProcessor.OutputResolution
        if outputScale.lowercased() == "4k" {
            resolution = .uhd4k
        } else {
            resolution = .hd1080p
        }

        SrNotificationHelper.requestPermission { granted in
            do {
                let jobId = try SrBackgroundJobManager.shared.start(
                    inputURL: inputURL,
                    displayFilename: displayFilename,
                    outputResolution: resolution,
                    srProfile: srProfile
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
        SrBackgroundJobManager.shared.clearJobMetadata()
        call.resolve(["ok": true])
    }

    @objc func cancelBackgroundJob(_ call: CAPPluginCall) {
        SrBackgroundJobManager.shared.cancelJob()
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
                    "backend": "coreimage-natural",
                ])
            } catch {
                call.reject(error.localizedDescription)
            }
        }
    }

    private func resolveFileURL(_ path: String) -> URL? {
        let trimmed = path.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty { return nil }

        if trimmed.hasPrefix("file://") {
            if let url = URL(string: trimmed), url.isFileURL {
                return url
            }
            if let decoded = trimmed.removingPercentEncoding, let url = URL(string: decoded) {
                return url
            }
        }
        if trimmed.hasPrefix("/") {
            return URL(fileURLWithPath: trimmed)
        }
        if trimmed.hasPrefix("capacitor://"), let webURL = URL(string: trimmed) {
            return bridge?.localURL(fromWebURL: webURL)
        }
        if trimmed.contains("://"), let webURL = URL(string: trimmed) {
            return bridge?.localURL(fromWebURL: webURL)
        }
        return nil
    }

    private func saveVideoToPhotos(call: CAPPluginCall, videoURL: URL) {
        PHPhotoLibrary.shared().performChanges({
            _ = PHAssetChangeRequest.creationRequestForAssetFromVideo(atFileURL: videoURL)
        }) { success, error in
            DispatchQueue.main.async {
                if success {
                    NSLog("Video saved to photos: \(videoURL.lastPathComponent)")
                    call.resolve(["success": true, "via": "photos"])
                } else {
                    NSLog("Failed to save video: \(error?.localizedDescription ?? "Unknown error")")
                    call.reject("保存失败：\(error?.localizedDescription ?? "未知错误")")
                }
            }
        }
    }

    private func requestPhotoLibraryAddAccess(then handler: @escaping (Bool) -> Void) {
        if #available(iOS 14, *) {
            PHPhotoLibrary.requestAuthorization(for: .addOnly) { status in
                handler(status == .authorized)
            }
        } else {
            PHPhotoLibrary.requestAuthorization { status in
                handler(status == .authorized)
            }
        }
    }

    @objc func saveToPhotos(_ call: CAPPluginCall) {
        guard let videoPath = call.getString("videoPath"), !videoPath.isEmpty else {
            call.reject("缺少 videoPath")
            return
        }

        guard let videoURL = resolveFileURL(videoPath) else {
            call.reject("无效的视频路径")
            return
        }

        guard FileManager.default.fileExists(atPath: videoURL.path) else {
            call.reject("视频文件不存在：\(videoURL.lastPathComponent)")
            return
        }

        requestPhotoLibraryAddAccess { granted in
            guard granted else {
                DispatchQueue.main.async {
                    call.reject("没有相册写入权限，请在 设置 → 隐私与安全性 → 照片 中允许「添加照片」")
                }
                return
            }
            self.saveVideoToPhotos(call: call, videoURL: videoURL)
        }
    }
}
