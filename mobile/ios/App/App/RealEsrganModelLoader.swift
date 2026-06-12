import CoreML
import Foundation

enum SrIosModelProfile: String {
    case general
    case anime

    static func parse(_ raw: String?) -> SrIosModelProfile {
        let v = (raw ?? "").trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        return v == "anime" ? .anime : .general
    }

    var basename: String {
        switch self {
        case .anime:
            return "RealESRGAN_v3"
        case .general:
            return "RealESRGAN_general"
        }
    }

    var backendLabel: String {
        switch self {
        case .anime:
            return "animevideov3"
        case .general:
            return "general-x4v3"
        }
    }
}

enum RealEsrganModelLoader {
    static func bundledModelURL(for profile: SrIosModelProfile = .general) -> URL? {
        let bundle = Bundle.main
        let modelBasename = profile.basename
        let lookups: [(String, String, String?)] = [
            (modelBasename, "mlmodelc", nil),
            (modelBasename, "mlmodelc", "Models"),
            (modelBasename, "mlpackage", nil),
            (modelBasename, "mlpackage", "Models"),
            (modelBasename, "mlmodel", nil),
        ]
        for (name, ext, subdir) in lookups {
            if let url = bundle.url(forResource: name, withExtension: ext, subdirectory: subdir) {
                return url
            }
        }
        guard let root = bundle.resourcePath else { return nil }
        let fm = FileManager.default
        guard let paths = try? fm.subpathsOfDirectory(atPath: root) else { return nil }
        let patterns = ["\(modelBasename).mlmodelc", "\(modelBasename).mlpackage", "\(modelBasename).mlmodel"]
        for pattern in patterns {
            if let pkg = paths.first(where: { $0.hasSuffix(pattern) }) {
                return URL(fileURLWithPath: root).appendingPathComponent(pkg)
            }
        }
        return nil
    }

    static func cachedModelURL(for profile: SrIosModelProfile = .general) -> URL? {
        guard let dir = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first else {
            return nil
        }
        let pkg = dir.appendingPathComponent("\(profile.basename).mlpackage")
        if FileManager.default.fileExists(atPath: pkg.path) {
            return pkg
        }
        return nil
    }

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

    static func resolvedModelURL(for profile: SrIosModelProfile = .general) -> URL? {
        bundledModelURL(for: profile) ?? cachedModelURL(for: profile)
    }

    static func isModelPresent(for profile: SrIosModelProfile = .general) -> Bool {
        resolvedModelURL(for: profile) != nil
    }

    static func ensureModelDownloaded(
        for profile: SrIosModelProfile = .general,
        onProgress: ((String) -> Void)? = nil
    ) throws -> URL {
        if let url = resolvedModelURL(for: profile) {
            return url
        }
        throw RealEsrganError.modelMissing
    }
}
