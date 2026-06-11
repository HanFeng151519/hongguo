import AVFoundation
import CoreImage
import Foundation

final class RealEsrganVideoProcessor {
    private enum OutputSpec {
        static let width = 1080
        static let height = 1920
    }

    private static let ciContext = CIContext(options: [.useSoftwareRenderer: false])

    struct Result {
        let outputURL: URL
        let outputWidth: Int
        let outputHeight: Int
        let frameCount: Int
    }

    func process(
        inputURL: URL,
        progress: @escaping (_ fraction: Double, _ message: String) -> Void,
        shouldCancel: (() -> Bool)? = nil
    ) throws -> Result {
        if shouldCancel?() == true { throw RealEsrganError.cancelled }
        if !RealEsrganEngine.shared.isReady {
            try RealEsrganEngine.shared.loadBundledModel()
        }

        let asset = AVAsset(url: inputURL)
        guard let videoTrack = asset.tracks(withMediaType: .video).first else {
            throw RealEsrganError.invalidInput
        }

        let duration = asset.duration.seconds
        if duration > 120 {
            throw NSError(
                domain: "RealEsrgan",
                code: 413,
                userInfo: [NSLocalizedDescriptionKey: "本地超分暂限 2 分钟以内视频，更长请用 Mac 后端"]
            )
        }

        let srcSize = videoTrack.naturalSize.applying(videoTrack.preferredTransform)
        let srcW = Int(abs(srcSize.width))
        let srcH = Int(abs(srcSize.height))
        if srcW < 1 || srcH < 1 {
            throw RealEsrganError.invalidInput
        }

        let outW = OutputSpec.width
        let outH = OutputSpec.height
        let fps = max(videoTrack.nominalFrameRate, 24)
        let estimatedFrames = max(1, Int(duration * Double(fps)))

        let tempVideo = FileManager.default.temporaryDirectory
            .appendingPathComponent("sr_video_\(UUID().uuidString).mp4")
        let finalURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("sr_final_\(UUID().uuidString).mp4")
        defer {
            try? FileManager.default.removeItem(at: tempVideo)
        }

        try writeUpscaledVideo(
            asset: asset,
            track: videoTrack,
            to: tempVideo,
            outW: outW,
            outH: outH,
            estimatedFrames: estimatedFrames,
            progress: progress,
            shouldCancel: shouldCancel
        )

        if shouldCancel?() == true { throw RealEsrganError.cancelled }
        progress(0.92, "正在合并原声音轨…")
        try muxAudio(from: asset, videoURL: tempVideo, to: finalURL)

        progress(1.0, "超分完成")
        return Result(outputURL: finalURL, outputWidth: outW, outputHeight: outH, frameCount: estimatedFrames)
    }

    private func writeUpscaledVideo(
        asset: AVAsset,
        track: AVAssetTrack,
        to outputURL: URL,
        outW: Int,
        outH: Int,
        estimatedFrames: Int,
        progress: @escaping (_ fraction: Double, _ message: String) -> Void,
        shouldCancel: (() -> Bool)? = nil
    ) throws {
        let reader = try AVAssetReader(asset: asset)
        let outputSettings: [String: Any] = [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
        ]
        let readerOutput = AVAssetReaderTrackOutput(track: track, outputSettings: outputSettings)
        readerOutput.alwaysCopiesSampleData = false
        guard reader.canAdd(readerOutput) else { throw RealEsrganError.invalidInput }
        reader.add(readerOutput)
        guard reader.startReading() else { throw RealEsrganError.invalidInput }

        try? FileManager.default.removeItem(at: outputURL)
        let writer = try AVAssetWriter(outputURL: outputURL, fileType: .mp4)
        let writerInput = AVAssetWriterInput(mediaType: .video, outputSettings: [
            AVVideoCodecKey: AVVideoCodecType.h264,
            AVVideoWidthKey: outW,
            AVVideoHeightKey: outH,
            AVVideoCompressionPropertiesKey: [
                AVVideoAverageBitRateKey: 8_000_000,
                AVVideoProfileLevelKey: AVVideoProfileLevelH264HighAutoLevel,
            ],
        ])
        writerInput.expectsMediaDataInRealTime = false
        writerInput.transform = .identity
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(
            assetWriterInput: writerInput,
            sourcePixelBufferAttributes: [
                kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
                kCVPixelBufferWidthKey as String: outW,
                kCVPixelBufferHeightKey as String: outH,
            ]
        )
        guard writer.canAdd(writerInput) else { throw RealEsrganError.invalidInput }
        writer.add(writerInput)
        guard writer.startWriting() else {
            throw RealEsrganError.predictionFailed(writer.error?.localizedDescription ?? "写入失败")
        }
        writer.startSession(atSourceTime: .zero)

        var frameIndex = 0
        var pts = CMTime.zero

        while reader.status == .reading {
            if shouldCancel?() == true { throw RealEsrganError.cancelled }
            guard let sample = readerOutput.copyNextSampleBuffer(),
                  let pixelBuffer = CMSampleBufferGetImageBuffer(sample) else { break }
            pts = CMSampleBufferGetPresentationTimeStamp(sample)

            let modelInput = try prepareModelInput(
                from: pixelBuffer,
                transform: track.preferredTransform,
                maxLongEdge: RealEsrganEngine.shared.maxInputLongEdge
            )
            let upscaled = try RealEsrganEngine.shared.upscale(pixelBuffer: modelInput)
            let outputFrame = try resizeToTarget(upscaled)

            while !writerInput.isReadyForMoreMediaData {
                Thread.sleep(forTimeInterval: 0.005)
            }
            if !adaptor.append(outputFrame, withPresentationTime: pts) {
                throw RealEsrganError.predictionFailed("写入帧失败")
            }
            frameIndex += 1
            let frac = min(0.9, Double(frameIndex) / Double(max(estimatedFrames, frameIndex)) * 0.9)
            progress(frac, "Real-ESRGAN 超分 \(frameIndex)/\(estimatedFrames) 帧…")
        }

        writerInput.markAsFinished()
        let group = DispatchGroup()
        group.enter()
        writer.finishWriting {
            group.leave()
        }
        group.wait()
        if writer.status != .completed {
            throw RealEsrganError.predictionFailed(writer.error?.localizedDescription ?? "编码失败")
        }
    }

    private func muxAudio(from asset: AVAsset, videoURL: URL, to outputURL: URL) throws {
        let videoAsset = AVAsset(url: videoURL)
        guard let videoTrack = videoAsset.tracks(withMediaType: .video).first else {
            try FileManager.default.copyItem(at: videoURL, to: outputURL)
            return
        }
        let audioTracks = asset.tracks(withMediaType: .audio)
        if audioTracks.isEmpty {
            try FileManager.default.copyItem(at: videoURL, to: outputURL)
            return
        }

        let mix = AVMutableComposition()
        guard
            let compVideo = mix.addMutableTrack(withMediaType: .video, preferredTrackID: kCMPersistentTrackID_Invalid),
            let compAudio = mix.addMutableTrack(withMediaType: .audio, preferredTrackID: kCMPersistentTrackID_Invalid)
        else {
            throw RealEsrganError.invalidInput
        }

        let duration = videoAsset.duration
        try compVideo.insertTimeRange(CMTimeRange(start: .zero, duration: duration), of: videoTrack, at: .zero)
        if let audioTrack = audioTracks.first {
            let audioDuration = min(duration, asset.duration)
            try compAudio.insertTimeRange(CMTimeRange(start: .zero, duration: audioDuration), of: audioTrack, at: .zero)
        }

        guard let exporter = AVAssetExportSession(asset: mix, presetName: AVAssetExportPresetHighestQuality) else {
            throw RealEsrganError.invalidInput
        }
        exporter.outputURL = outputURL
        exporter.outputFileType = .mp4
        exporter.shouldOptimizeForNetworkUse = true
        let sem = DispatchSemaphore(value: 0)
        exporter.exportAsynchronously {
            sem.signal()
        }
        sem.wait()
        if exporter.status != .completed {
            throw RealEsrganError.predictionFailed(exporter.error?.localizedDescription ?? "音频合并失败")
        }
    }

    /// 方向校正 + 缩小，合并为一次 Core Image 渲染
    private func prepareModelInput(
        from pixelBuffer: CVPixelBuffer,
        transform: CGAffineTransform,
        maxLongEdge: Int
    ) throws -> CVPixelBuffer {
        var image = CIImage(cvPixelBuffer: pixelBuffer)
        let orientation = exifOrientation(from: transform)
        if orientation != 1 {
            image = image.oriented(forExifOrientation: orientation)
        }
        let srcW = image.extent.width
        let srcH = image.extent.height
        guard srcW > 0, srcH > 0 else {
            throw RealEsrganError.invalidInput
        }
        let longEdge = max(srcW, srcH)
        if longEdge > CGFloat(maxLongEdge) {
            let scale = CGFloat(maxLongEdge) / longEdge
            image = image.transformed(by: CGAffineTransform(scaleX: scale, y: scale))
        }
        let dstW = max(1, Int(image.extent.width.rounded()))
        let dstH = max(1, Int(image.extent.height.rounded()))
        guard let output = makePixelBuffer(width: dstW, height: dstH) else {
            throw RealEsrganError.invalidInput
        }
        Self.ciContext.render(image, to: output)
        return output
    }

    private func resizeToTarget(_ pixelBuffer: CVPixelBuffer) throws -> CVPixelBuffer {
        let targetW = CGFloat(OutputSpec.width)
        let targetH = CGFloat(OutputSpec.height)
        let srcW = CGFloat(CVPixelBufferGetWidth(pixelBuffer))
        let srcH = CGFloat(CVPixelBufferGetHeight(pixelBuffer))
        guard srcW > 0, srcH > 0 else {
            throw RealEsrganError.invalidInput
        }

        var image = CIImage(cvPixelBuffer: pixelBuffer)
        let scale = max(targetW / srcW, targetH / srcH)
        image = image.transformed(by: CGAffineTransform(scaleX: scale, y: scale))
        let extent = image.extent
        let cropRect = CGRect(
            x: extent.midX - targetW / 2,
            y: extent.midY - targetH / 2,
            width: targetW,
            height: targetH
        )
        image = image.cropped(to: cropRect)

        guard let output = makePixelBuffer(width: OutputSpec.width, height: OutputSpec.height) else {
            throw RealEsrganError.invalidInput
        }
        Self.ciContext.render(image, to: output)
        return output
    }

    private func makePixelBuffer(width: Int, height: Int) -> CVPixelBuffer? {
        var pb: CVPixelBuffer?
        let attrs: [String: Any] = [
            kCVPixelBufferCGImageCompatibilityKey as String: true,
            kCVPixelBufferCGBitmapContextCompatibilityKey as String: true,
        ]
        CVPixelBufferCreate(
            kCFAllocatorDefault,
            width,
            height,
            kCVPixelFormatType_32BGRA,
            attrs as CFDictionary,
            &pb
        )
        return pb
    }

    private func exifOrientation(from transform: CGAffineTransform) -> Int32 {
        switch (transform.a, transform.b, transform.c, transform.d) {
        case (0, 1, -1, 0):
            return 6
        case (0, -1, 1, 0):
            return 8
        case (-1, 0, 0, -1):
            return 3
        case (1, 0, 0, 1):
            return 1
        default:
            return 1
        }
    }
}
