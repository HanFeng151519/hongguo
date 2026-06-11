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
        let present = RealEsrganModelLoader.isModelPresent()
        call.resolve([
            "available": present,
            "downloadable": false,
            "backend": present ? "coreml-v3" : "none",
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
                call.resolve(["ok": true, "backend": "coreml-v3", "path": url.lastPathComponent])
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
        let outputScale = call.getString("outputScale") ?? "1080"
        
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
                    outputResolution: resolution
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
    
    @objc func saveToPhotos(_ call: CAPPluginCall) {
        guard let videoPath = call.getString("videoPath"), !videoPath.isEmpty else {
            call.reject("缺少 videoPath")
            return
        }
        
        guard let videoURL = resolveFileURL(videoPath) else {
            call.reject("无效的视频路径")
            return
        }
        
        // Check if file exists
        guard FileManager.default.fileExists(atPath: videoURL.path) else {
            call.reject("视频文件不存在")
            return
        }
        
        // Request permission to write to photo library
        PHPhotoLibrary.requestAuthorization { [weak self] status in
            guard let self = self else { return }
            
            if status != .authorized && status != .limited {
                DispatchQueue.main.async {
                    call.reject("没有相册写入权限，请在设置中授予权限")
                }
                return
            }
            
            // Save video to photo library
            PHPhotoLibrary.shared().performChanges({
                // First, try to re-encode with AVAssetExportSession for better compatibility
                let asset = AVURLAsset(url: videoURL)
                guard let exportSession = AVAssetExportSession(asset: asset, presetName: AVAssetExportPresetHighestQuality) else {
                    NSLog("Failed to create export session")
                    return
                }
                
                let tempURL = FileManager.default.temporaryDirectory.appendingPathComponent("export_\(UUID().uuidString).mp4")
                exportSession.outputURL = tempURL
                exportSession.outputFileType = .mp4
                exportSession.shouldOptimizeForNetworkUse = true
                
                // Wait for export to complete synchronously within the change block
                let semaphore = DispatchSemaphore(value: 0)
                exportSession.exportAsynchronously {
                    semaphore.signal()
                }
                semaphore.wait()
                
                if exportSession.status == .completed, let exportedURL = exportSession.outputURL {
                    // Use the re-encoded video
                    _ = PHAssetChangeRequest.creationRequestForAssetFromVideo(atFileURL: exportedURL)
                    // Clean up temp file after a delay
                    DispatchQueue.global().asyncAfter(deadline: .now() + 2) {
                        try? FileManager.default.removeItem(at: exportedURL)
                    }
                } else {
                    // Fallback to original file
                    NSLog("Export failed: \(exportSession.error?.localizedDescription ?? "Unknown"), using original file")
                    _ = PHAssetChangeRequest.creationRequestForAssetFromVideo(atFileURL: videoURL)
                }
            }) { success, error in
                DispatchQueue.main.async {
                    if success {
                        NSLog("Video saved to photos successfully: \(videoURL.lastPathComponent)")
                        call.resolve(["success": true])
                    } else {
                        NSLog("Failed to save video: \(error?.localizedDescription ?? "Unknown error")")
                        call.reject("保存失败：\(error?.localizedDescription ?? "未知错误")")
                    }
                }
            }
        }
    }
}
