import CoreML
import CoreVideo
import UIKit

enum RealEsrganError: LocalizedError {
    case modelMissing
    case modelLoad(String)
    case invalidInput
    case predictionFailed(String)
    case cancelled

    var errorDescription: String? {
        switch self {
        case .modelMissing:
            return "未找到 Real-ESRGAN v3 模型。请在 Mac 执行 npm run ensure:sr-model，或在 App 内首次超分时联网自动下载"
        case .modelLoad(let msg):
            return "模型加载失败：\(msg)"
        case .invalidInput:
            return "无法读取视频画面"
        case .predictionFailed(let msg):
            return "AI 推理失败：\(msg)"
        case .cancelled:
            return "超分任务已取消"
        }
    }
}

private enum ModelKind {
    case multiArray
    case image
}

final class RealEsrganEngine {
    static let shared = RealEsrganEngine()

    private var model: MLModel?
    private var cpuFallbackModel: MLModel?
    private var modelSourceURL: URL?
    private var tileSize = 256
    private var scale = 2
    private var kind: ModelKind = .multiArray
    private var inputName = "input"
    private var outputName = "output"

    var isReady: Bool { model != nil }

    /// 送入模型前最长边上限（超出会先缩小，再超分并输出 1080×1920）
    let maxInputLongEdge = 512

    func loadModel(at url: URL, onProgress: ((String) -> Void)? = nil) throws {
        if model != nil { return }
        let loadURL = try RealEsrganModelLoader.compiledModelURL(for: url, onProgress: onProgress)
        modelSourceURL = url
        #if os(iOS)
        let unitOrder: [MLComputeUnits] = [.all, .cpuAndGPU, .cpuOnly]
        #else
        let unitOrder: [MLComputeUnits] = [.all]
        #endif
        var lastErr: Error?
        for units in unitOrder {
            let config = MLModelConfiguration()
            config.computeUnits = units
            do {
                let loaded = try MLModel(contentsOf: loadURL, configuration: config)
                model = loaded
                configureProfile(from: loaded)
                return
            } catch let err as RealEsrganError {
                throw err
            } catch {
                lastErr = error
            }
        }
        throw RealEsrganError.modelLoad(lastErr?.localizedDescription ?? "模型加载失败")
    }

    func loadBundledModel(onProgress: ((String) -> Void)? = nil) throws {
        guard let url = RealEsrganModelLoader.resolvedModelURL() else {
            throw RealEsrganError.modelMissing
        }
        try loadModel(at: url, onProgress: onProgress)
    }

    private func configureProfile(from model: MLModel) {
        let desc = model.modelDescription
        if desc.inputDescriptionsByName["input"] != nil {
            inputName = "input"
        } else {
            inputName = desc.inputDescriptionsByName.keys.first ?? "input"
        }
        if desc.outputDescriptionsByName["activation_out"] != nil {
            outputName = "activation_out"
        } else {
            outputName = desc.outputDescriptionsByName.keys.first ?? "output"
        }

        if let inImg = desc.inputDescriptionsByName[inputName]?.imageConstraint {
            kind = .image
            tileSize = max(1, inImg.pixelsWide)
            if let outImg = desc.outputDescriptionsByName[outputName]?.imageConstraint {
                scale = max(1, outImg.pixelsWide / max(1, inImg.pixelsWide))
            } else if let outArr = desc.outputDescriptionsByName[outputName]?.multiArrayConstraint,
                      outArr.shape.count >= 2 {
                let outEdge = outArr.shape.last?.intValue ?? (tileSize * 4)
                scale = max(1, outEdge / tileSize)
            } else {
                scale = 4
            }
            return
        }
        kind = .multiArray
        if let inArr = desc.inputDescriptionsByName[inputName]?.multiArrayConstraint,
           inArr.shape.count >= 2 {
            let h = inArr.shape[inArr.shape.count - 2].intValue
            let w = inArr.shape[inArr.shape.count - 1].intValue
            tileSize = max(1, max(h, w))
        } else {
            tileSize = 256
        }
        if let outArr = desc.outputDescriptionsByName[outputName]?.multiArrayConstraint,
           outArr.shape.count >= 2 {
            let outH = outArr.shape[outArr.shape.count - 2].intValue
            let outW = outArr.shape[outArr.shape.count - 1].intValue
            scale = max(1, min(outW, outH) / max(1, tileSize))
        } else {
            scale = 4
        }
    }

    func upscale(pixelBuffer: CVPixelBuffer) throws -> CVPixelBuffer {
        guard let model else { throw RealEsrganError.modelMissing }
        switch kind {
        case .image:
            return try upscaleImageModel(model: model, pixelBuffer: pixelBuffer)
        case .multiArray:
            return try upscaleArrayModel(model: model, pixelBuffer: pixelBuffer)
        }
    }

    private func upscaleImageModel(model: MLModel, pixelBuffer: CVPixelBuffer) throws -> CVPixelBuffer {
        let srcW = CVPixelBufferGetWidth(pixelBuffer)
        let srcH = CVPixelBufferGetHeight(pixelBuffer)
        let outW = srcW * scale
        let outH = srcH * scale
        guard let outputPB = makePixelBuffer(width: outW, height: outH) else {
            throw RealEsrganError.invalidInput
        }

        let tilesX = Int(ceil(Double(srcW) / Double(tileSize)))
        let tilesY = Int(ceil(Double(srcH) / Double(tileSize)))

        for ty in 0..<tilesY {
            for tx in 0..<tilesX {
                let x0 = tx * tileSize
                let y0 = ty * tileSize
                let cropW = min(tileSize, srcW - x0)
                let cropH = min(tileSize, srcH - y0)
                let tileIn = try makeTilePixelBuffer(from: pixelBuffer, x: x0, y: y0, cropW: cropW, cropH: cropH)
                let tileOut = try predictTile(
                    model: model,
                    pixelBuffer: tileIn,
                    validOutW: cropW * scale,
                    validOutH: cropH * scale
                )
                try pasteTileBuffer(
                    tileOut,
                    into: outputPB,
                    destX: x0 * scale,
                    destY: y0 * scale,
                    validOutW: cropW * scale,
                    validOutH: cropH * scale
                )
            }
        }
        return outputPB
    }

    private func upscaleArrayModel(model: MLModel, pixelBuffer: CVPixelBuffer) throws -> CVPixelBuffer {
        let srcW = CVPixelBufferGetWidth(pixelBuffer)
        let srcH = CVPixelBufferGetHeight(pixelBuffer)
        let outW = srcW * scale
        let outH = srcH * scale
        guard let outputPB = makePixelBuffer(width: outW, height: outH) else {
            throw RealEsrganError.invalidInput
        }
        CVPixelBufferLockBaseAddress(outputPB, [])
        defer { CVPixelBufferUnlockBaseAddress(outputPB, []) }
        guard let outBase = CVPixelBufferGetBaseAddress(outputPB) else {
            throw RealEsrganError.invalidInput
        }
        let outBytesPerRow = CVPixelBufferGetBytesPerRow(outputPB)
        memset(outBase, 0, outBytesPerRow * outH)

        let tilesX = Int(ceil(Double(srcW) / Double(tileSize)))
        let tilesY = Int(ceil(Double(srcH) / Double(tileSize)))

        for ty in 0..<tilesY {
            for tx in 0..<tilesX {
                let x0 = tx * tileSize
                let y0 = ty * tileSize
                let cropW = min(tileSize, srcW - x0)
                let cropH = min(tileSize, srcH - y0)
                let inputArray = try makeInputArray(from: pixelBuffer, x: x0, y: y0, cropW: cropW, cropH: cropH)
                let prediction = try predictArray(model: model, input: inputArray)
                try pasteArrayTile(
                    prediction,
                    into: outBase,
                    outBytesPerRow: outBytesPerRow,
                    outW: outW,
                    outH: outH,
                    destX: x0 * scale,
                    destY: y0 * scale,
                    validOutW: cropW * scale,
                    validOutH: cropH * scale
                )
            }
        }
        return outputPB
    }

    private func makePixelBuffer(width: Int, height: Int) -> CVPixelBuffer? {
        var pb: CVPixelBuffer?
        let attrs: [String: Any] = [
            kCVPixelBufferCGImageCompatibilityKey as String: true,
            kCVPixelBufferCGBitmapContextCompatibilityKey as String: true,
        ]
        CVPixelBufferCreate(kCFAllocatorDefault, width, height, kCVPixelFormatType_32BGRA, attrs as CFDictionary, &pb)
        return pb
    }

    private func makeTilePixelBuffer(
        from pixelBuffer: CVPixelBuffer,
        x: Int,
        y: Int,
        cropW: Int,
        cropH: Int
    ) throws -> CVPixelBuffer {
        guard let tile = makePixelBuffer(width: tileSize, height: tileSize) else {
            throw RealEsrganError.invalidInput
        }
        CVPixelBufferLockBaseAddress(pixelBuffer, .readOnly)
        CVPixelBufferLockBaseAddress(tile, [])
        defer {
            CVPixelBufferUnlockBaseAddress(pixelBuffer, .readOnly)
            CVPixelBufferUnlockBaseAddress(tile, [])
        }
        guard
            let srcBase = CVPixelBufferGetBaseAddress(pixelBuffer),
            let dstBase = CVPixelBufferGetBaseAddress(tile)
        else {
            throw RealEsrganError.invalidInput
        }
        let srcRow = CVPixelBufferGetBytesPerRow(pixelBuffer)
        let dstRow = CVPixelBufferGetBytesPerRow(tile)
        for row in 0..<tileSize {
            for col in 0..<tileSize {
                let sx = min(x + col, x + cropW - 1)
                let sy = min(y + row, y + cropH - 1)
                let so = sy * srcRow + sx * 4
                let dstOff = row * dstRow + col * 4
                let sp = srcBase.advanced(by: so).assumingMemoryBound(to: UInt8.self)
                let dp = dstBase.advanced(by: dstOff).assumingMemoryBound(to: UInt8.self)
                dp[0] = sp[0]
                dp[1] = sp[1]
                dp[2] = sp[2]
                dp[3] = 255
            }
        }
        return tile
    }

    private func predictTile(
        model: MLModel,
        pixelBuffer: CVPixelBuffer,
        validOutW: Int,
        validOutH: Int
    ) throws -> CVPixelBuffer {
        let full = try runPrediction(model: model, pixelBuffer: pixelBuffer)
        if CVPixelBufferGetWidth(full) == validOutW && CVPixelBufferGetHeight(full) == validOutH {
            return full
        }
        return try cropPixelBuffer(full, width: validOutW, height: validOutH)
    }

    private func runPrediction(model: MLModel, pixelBuffer: CVPixelBuffer) throws -> CVPixelBuffer {
        if CVPixelBufferGetWidth(pixelBuffer) != tileSize || CVPixelBufferGetHeight(pixelBuffer) != tileSize {
            throw RealEsrganError.invalidInput
        }
        do {
            return try performPrediction(model: model, pixelBuffer: pixelBuffer)
        } catch {
            let msg = error.localizedDescription
            if msg.contains("activation_out") || msg.contains("pixel buffer") {
                return try performPredictionWithCpuOnly(pixelBuffer: pixelBuffer)
            }
            throw RealEsrganError.predictionFailed(msg)
        }
    }

    private func performPrediction(model: MLModel, pixelBuffer: CVPixelBuffer) throws -> CVPixelBuffer {
        let feature = MLFeatureValue(pixelBuffer: pixelBuffer)
        let provider = try MLDictionaryFeatureProvider(dictionary: [inputName: feature])
        let out = try model.prediction(from: provider)
        return try extractOutputPixelBuffer(from: out)
    }

    private func performPredictionWithCpuOnly(pixelBuffer: CVPixelBuffer) throws -> CVPixelBuffer {
        if cpuFallbackModel == nil {
            guard let sourceURL = modelSourceURL else {
                throw RealEsrganError.predictionFailed("模型输出转换失败")
            }
            let loadURL = try RealEsrganModelLoader.compiledModelURL(for: sourceURL)
            let config = MLModelConfiguration()
            config.computeUnits = .cpuOnly
            cpuFallbackModel = try MLModel(contentsOf: loadURL, configuration: config)
        }
        guard let cpuModel = cpuFallbackModel else {
            throw RealEsrganError.predictionFailed("模型输出转换失败")
        }
        return try performPrediction(model: cpuModel, pixelBuffer: pixelBuffer)
    }

    private func extractOutputPixelBuffer(from out: MLFeatureProvider) throws -> CVPixelBuffer {
        guard let feature = out.featureValue(for: outputName) else {
            throw RealEsrganError.predictionFailed("输出为空")
        }
        if let pb = feature.imageBufferValue {
            return pb
        }
        if let arr = feature.multiArrayValue {
            return try multiArrayToPixelBuffer(arr)
        }
        throw RealEsrganError.predictionFailed("无法解析模型输出 \(outputName)")
    }

    private func multiArrayToPixelBuffer(_ array: MLMultiArray) throws -> CVPixelBuffer {
        let shape = array.shape.map { $0.intValue }
        let height: Int
        let width: Int
        if shape.count == 4, shape[1] == 3 {
            height = shape[2]
            width = shape[3]
        } else if shape.count == 3, shape[0] == 3 {
            height = shape[1]
            width = shape[2]
        } else {
            throw RealEsrganError.predictionFailed("不支持的输出维度")
        }
        guard let pb = makePixelBuffer(width: width, height: height) else {
            throw RealEsrganError.invalidInput
        }
        CVPixelBufferLockBaseAddress(pb, [])
        defer { CVPixelBufferUnlockBaseAddress(pb, []) }
        guard let base = CVPixelBufferGetBaseAddress(pb) else {
            throw RealEsrganError.invalidInput
        }
        let rowBytes = CVPixelBufferGetBytesPerRow(pb)
        let ptr = array.dataPointer.bindMemory(to: Float.self, capacity: array.count)
        let plane = height * width
        for y in 0..<height {
            for x in 0..<width {
                let idx = y * width + x
                let r = min(max(ptr[idx], 0), 1)
                let g = min(max(ptr[plane + idx], 0), 1)
                let b = min(max(ptr[plane * 2 + idx], 0), 1)
                let offset = y * rowBytes + x * 4
                let px = base.advanced(by: offset).assumingMemoryBound(to: UInt8.self)
                px[0] = UInt8(b * 255)
                px[1] = UInt8(g * 255)
                px[2] = UInt8(r * 255)
                px[3] = 255
            }
        }
        return pb
    }

    private func cropPixelBuffer(_ source: CVPixelBuffer, width: Int, height: Int) throws -> CVPixelBuffer {
        guard let dest = makePixelBuffer(width: width, height: height) else {
            throw RealEsrganError.invalidInput
        }
        CVPixelBufferLockBaseAddress(source, .readOnly)
        CVPixelBufferLockBaseAddress(dest, [])
        defer {
            CVPixelBufferUnlockBaseAddress(source, .readOnly)
            CVPixelBufferUnlockBaseAddress(dest, [])
        }
        guard
            let srcBase = CVPixelBufferGetBaseAddress(source),
            let dstBase = CVPixelBufferGetBaseAddress(dest)
        else {
            throw RealEsrganError.invalidInput
        }
        let srcRow = CVPixelBufferGetBytesPerRow(source)
        let dstRow = CVPixelBufferGetBytesPerRow(dest)
        for row in 0..<height {
            memcpy(
                dstBase.advanced(by: row * dstRow),
                srcBase.advanced(by: row * srcRow),
                width * 4
            )
        }
        return dest
    }

    private func pasteTileBuffer(
        _ tile: CVPixelBuffer,
        into dest: CVPixelBuffer,
        destX: Int,
        destY: Int,
        validOutW: Int,
        validOutH: Int
    ) throws {
        CVPixelBufferLockBaseAddress(tile, .readOnly)
        CVPixelBufferLockBaseAddress(dest, [])
        defer {
            CVPixelBufferUnlockBaseAddress(tile, .readOnly)
            CVPixelBufferUnlockBaseAddress(dest, [])
        }
        guard
            let srcBase = CVPixelBufferGetBaseAddress(tile),
            let dstBase = CVPixelBufferGetBaseAddress(dest)
        else {
            throw RealEsrganError.invalidInput
        }
        let srcRow = CVPixelBufferGetBytesPerRow(tile)
        let dstRow = CVPixelBufferGetBytesPerRow(dest)
        for row in 0..<validOutH {
            let dy = destY + row
            memcpy(
                dstBase.advanced(by: dy * dstRow + destX * 4),
                srcBase.advanced(by: row * srcRow),
                validOutW * 4
            )
        }
    }

    private func makeInputArray(
        from pixelBuffer: CVPixelBuffer,
        x: Int,
        y: Int,
        cropW: Int,
        cropH: Int
    ) throws -> MLMultiArray {
        CVPixelBufferLockBaseAddress(pixelBuffer, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(pixelBuffer, .readOnly) }
        guard let base = CVPixelBufferGetBaseAddress(pixelBuffer) else {
            throw RealEsrganError.invalidInput
        }
        let bytesPerRow = CVPixelBufferGetBytesPerRow(pixelBuffer)
        let array = try MLMultiArray(shape: [1, 3, NSNumber(value: tileSize), NSNumber(value: tileSize)], dataType: .float32)
        let ptr = array.dataPointer.bindMemory(to: Float.self, capacity: array.count)
        let planeSize = tileSize * tileSize
        for row in 0..<tileSize {
            for col in 0..<tileSize {
                let sx = min(x + col, x + cropW - 1)
                let sy = min(y + row, y + cropH - 1)
                let offset = sy * bytesPerRow + sx * 4
                let px = base.advanced(by: offset).assumingMemoryBound(to: UInt8.self)
                let idx = row * tileSize + col
                ptr[idx] = Float(px[2]) / 255.0
                ptr[planeSize + idx] = Float(px[1]) / 255.0
                ptr[planeSize * 2 + idx] = Float(px[0]) / 255.0
            }
        }
        return array
    }

    private func predictArray(model: MLModel, input: MLMultiArray) throws -> MLMultiArray {
        let provider = try MLDictionaryFeatureProvider(dictionary: [inputName: input])
        let out = try model.prediction(from: provider)
        guard let result = out.featureValue(for: outputName)?.multiArrayValue else {
            throw RealEsrganError.predictionFailed("输出为空")
        }
        return result
    }

    private func pasteArrayTile(
        _ array: MLMultiArray,
        into base: UnsafeMutableRawPointer,
        outBytesPerRow: Int,
        outW: Int,
        outH: Int,
        destX: Int,
        destY: Int,
        validOutW: Int,
        validOutH: Int
    ) throws {
        let ptr = array.dataPointer.bindMemory(to: Float.self, capacity: array.count)
        let outTile = tileSize * scale
        let planeSize = outTile * outTile
        for row in 0..<validOutH {
            let dy = destY + row
            if dy < 0 || dy >= outH { continue }
            for col in 0..<validOutW {
                let dx = destX + col
                if dx < 0 || dx >= outW { continue }
                let idx = row * outTile + col
                let r = min(max(ptr[idx], 0), 1)
                let g = min(max(ptr[planeSize + idx], 0), 1)
                let b = min(max(ptr[planeSize * 2 + idx], 0), 1)
                let offset = dy * outBytesPerRow + dx * 4
                let px = base.advanced(by: offset).assumingMemoryBound(to: UInt8.self)
                px[0] = UInt8(b * 255)
                px[1] = UInt8(g * 255)
                px[2] = UInt8(r * 255)
                px[3] = 255
            }
        }
    }
}
