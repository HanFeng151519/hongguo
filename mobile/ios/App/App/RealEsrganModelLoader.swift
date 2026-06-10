import CoreML
import Foundation
import ZIPFoundation

enum RealEsrganModelLoader {
    private static let downloadUrls = [
        "https://hf-mirror.com/mszpro/CoreML_RealESRGAN/resolve/main/RealESRGAN.mlmodel.zip",
        "https://huggingface.co/mszpro/CoreML_RealESRGAN/resolve/main/RealESRGAN.mlmodel.zip",
    ]

    static func bundledModelURL() -> URL? {
        let bundle = Bundle.main
        let lookups: [(String, String, String?)] = [
            ("RealESRGAN", "mlmodelc", "Models"),
            ("RealESRGAN_x2plus", "mlpackage", "Models"),
            ("RealESRGAN", "mlmodel", "Models"),
            ("RealESRGAN", "mlmodelc", nil),
            ("RealESRGAN_x2plus", "mlpackage", nil),
            ("RealESRGAN", "mlmodel", nil),
        ]
        for (name, ext, subdir) in lookups {
            if let url = bundle.url(forResource: name, withExtension: ext, subdirectory: subdir) {
                return url
            }
        }
        guard let root = bundle.resourcePath else { return nil }
        let fm = FileManager.default
        guard let paths = try? fm.subpathsOfDirectory(atPath: root) else { return nil }
        if let compiled = paths.first(where: { $0.hasSuffix("RealESRGAN.mlmodelc") }) {
            return URL(fileURLWithPath: root).appendingPathComponent(compiled)
        }
        if let pkg = paths.first(where: { $0.hasSuffix("RealESRGAN_x2plus.mlpackage") }) {
            return URL(fileURLWithPath: root).appendingPathComponent(pkg)
        }
        if let model = paths.first(where: { $0.hasSuffix("RealESRGAN.mlmodel") }) {
            return URL(fileURLWithPath: root).appendingPathComponent(model)
        }
        return nil
    }

    static func cachedModelURL() -> URL? {
        guard let dir = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first else {
            return nil
        }
        let fm = FileManager.default
        let compiled = dir.appendingPathComponent("RealESRGAN.mlmodelc")
        if fm.fileExists(atPath: compiled.path) {
            return compiled
        }
        let model = dir.appendingPathComponent("RealESRGAN.mlmodel")
        return fm.fileExists(atPath: model.path) ? model : nil
    }

    /// 原始 .mlmodel 需先编译为 .mlmodelc 才能加载；bundle 内或已编译文件直接返回。
    static func compiledModelURL(for sourceURL: URL, onProgress: ((String) -> Void)? = nil) throws -> URL {
        let ext = sourceURL.pathExtension.lowercased()
        if ext == "mlmodelc" || ext == "mlpackage" {
            return sourceURL
        }
        guard ext == "mlmodel" else {
            return sourceURL
        }

        guard let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first else {
            throw RealEsrganError.modelMissing
        }
        let compiledURL = support.appendingPathComponent("RealESRGAN.mlmodelc", isDirectory: true)
        let fm = FileManager.default

        if fm.fileExists(atPath: compiledURL.path) {
            let srcMtime = (try? fm.attributesOfItem(atPath: sourceURL.path)[.modificationDate]) as? Date
            let cmpMtime = (try? fm.attributesOfItem(atPath: compiledURL.path)[.modificationDate]) as? Date
            if let srcMtime, let cmpMtime, cmpMtime >= srcMtime {
                return compiledURL
            }
            try? fm.removeItem(at: compiledURL)
        }

        onProgress?("正在编译 Core ML 模型（首次约 1–3 分钟）…")
        let tempCompiled = try MLModel.compileModel(at: sourceURL)
        try fm.createDirectory(at: support, withIntermediateDirectories: true)
        if fm.fileExists(atPath: compiledURL.path) {
            try fm.removeItem(at: compiledURL)
        }
        try fm.copyItem(at: tempCompiled, to: compiledURL)
        return compiledURL
    }

    static func resolvedModelURL() -> URL? {
        bundledModelURL() ?? cachedModelURL()
    }

    static func isModelPresent() -> Bool {
        resolvedModelURL() != nil
    }

    static func ensureModelDownloaded(onProgress: ((String) -> Void)? = nil) throws -> URL {
        if let url = resolvedModelURL() {
            return url
        }
        onProgress?("正在下载 Real-ESRGAN 模型（约 64MB，仅首次）…")
        guard let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first else {
            throw RealEsrganError.modelMissing
        }
        try FileManager.default.createDirectory(at: support, withIntermediateDirectories: true)
        let zipPath = support.appendingPathComponent("RealESRGAN.mlmodel.zip")
        let modelPath = support.appendingPathComponent("RealESRGAN.mlmodel")
        defer { try? FileManager.default.removeItem(at: zipPath) }

        var lastErr: Error?
        for urlString in downloadUrls {
            guard let remote = URL(string: urlString) else { continue }
            do {
                onProgress?("下载中… \(remote.host ?? "")")
                let (tmp, _) = try syncDownload(from: remote)
                try? FileManager.default.removeItem(at: zipPath)
                try FileManager.default.moveItem(at: tmp, to: zipPath)
                let extractDir = support.appendingPathComponent("sr_unzip_\(UUID().uuidString)")
                try FileManager.default.createDirectory(at: extractDir, withIntermediateDirectories: true)
                defer { try? FileManager.default.removeItem(at: extractDir) }
                try FileManager.default.unzipItem(at: zipPath, to: extractDir)
                let found = try FileManager.default.subpathsOfDirectory(atPath: extractDir.path)
                    .first { $0.hasSuffix("RealESRGAN.mlmodel") }
                guard let found else { throw RealEsrganError.modelMissing }
                try? FileManager.default.removeItem(at: modelPath)
                try FileManager.default.moveItem(at: extractDir.appendingPathComponent(found), to: modelPath)
                return modelPath
            } catch {
                lastErr = error
            }
        }
        throw lastErr ?? RealEsrganError.modelMissing
    }

    private static func syncDownload(from url: URL) throws -> (URL, URLResponse) {
        var result: Result<(URL, URLResponse), Error>?
        let sem = DispatchSemaphore(value: 0)
        let task = URLSession.shared.downloadTask(with: url) { tmp, resp, err in
            if let err {
                result = .failure(err)
            } else if let tmp, let resp {
                result = .success((tmp, resp))
            } else {
                result = .failure(RealEsrganError.modelMissing)
            }
            sem.signal()
        }
        task.resume()
        sem.wait()
        switch result {
        case .success(let pair):
            return pair
        case .failure(let err):
            throw err
        case .none:
            throw RealEsrganError.modelMissing
        }
    }
}
