import CoreML
import Foundation

enum RealEsrganModelLoader {
    private static let modelBasename = "RealESRGAN_v3"

    static func bundledModelURL() -> URL? {
        let bundle = Bundle.main
        print("[RealEsrgan] Looking for model in bundle...")
        print("[RealEsrgan] Bundle path: \(bundle.bundlePath)")
        print("[RealEsrgan] Resource path: \(bundle.resourcePath ?? "nil")")
        
        // Try multiple formats: mlmodelc (compiled), mlpackage, mlmodel
        let lookups: [(String, String, String?)] = [
            (modelBasename, "mlmodelc", nil),        // Compiled model (Xcode auto-converts)
            (modelBasename, "mlmodelc", "Models"),   // Compiled in Models subdir
            (modelBasename, "mlpackage", nil),       // Package format
            (modelBasename, "mlpackage", "Models"),  // Package in Models subdir
            (modelBasename, "mlmodel", nil),         // Legacy model
        ]
        for (name, ext, subdir) in lookups {
            if let url = bundle.url(forResource: name, withExtension: ext, subdirectory: subdir) {
                print("[RealEsrgan] Found model at: \(url.path) (format: .\(ext))")
                return url
            }
            print("[RealEsrgan] Not found: \(name).\(ext) in \(subdir ?? "root")")
        }
        
        // Fallback: search all resources
        guard let root = bundle.resourcePath else { return nil }
        let fm = FileManager.default
        guard let paths = try? fm.subpathsOfDirectory(atPath: root) else { return nil }
        print("[RealEsrgan] Searching in \(paths.count) paths...")
        
        // Search for any matching model file
        let patterns = ["\(modelBasename).mlmodelc", "\(modelBasename).mlpackage", "\(modelBasename).mlmodel"]
        for pattern in patterns {
            if let pkg = paths.first(where: { $0.hasSuffix(pattern) }) {
                let url = URL(fileURLWithPath: root).appendingPathComponent(pkg)
                print("[RealEsrgan] Found via search: \(url.path)")
                return url
            }
        }
        
        print("[RealEsrgan] Model not found in bundle")
        return nil
    }

    static func cachedModelURL() -> URL? {
        guard let dir = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first else {
            return nil
        }
        let fm = FileManager.default
        let pkg = dir.appendingPathComponent("\(modelBasename).mlpackage")
        if fm.fileExists(atPath: pkg.path) {
            return pkg
        }
        return nil
    }

    /// mlpackage 可直接加载；.mlmodel 需先编译为 .mlmodelc。
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
        let compiledURL = support.appendingPathComponent("RealESRGAN_legacy.mlmodelc", isDirectory: true)
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
        throw RealEsrganError.modelMissing
    }
}
