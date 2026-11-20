// On-device text recognition for Case Mission Control.
// Usage: case-ocr <image-path>
// Prints JSON: {"width":..,"height":..,"lines":[{"text":..,"confidence":..}]}
// Uses Apple's Vision framework, so no image data leaves the Mac.

import AppKit
import Foundation
import Vision

struct Line: Encodable {
    let text: String
    let confidence: Float
}

struct Output: Encodable {
    let width: Int
    let height: Int
    let lines: [Line]
}

func fail(_ message: String) -> Never {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    exit(1)
}

guard CommandLine.arguments.count == 2 else { fail("usage: case-ocr <image-path> | --warmup") }

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = true

// Loading Vision's text models can take a minute on a busy Mac the first time.
// `--warmup` pays that cost up front on a tiny rendered image.
if CommandLine.arguments[1] == "--warmup" {
    let size = NSSize(width: 360, height: 90)
    let warm = NSImage(size: size)
    warm.lockFocus()
    NSColor.white.setFill()
    NSRect(origin: .zero, size: size).fill()
    ("Case warm up" as NSString).draw(at: NSPoint(x: 16, y: 26), withAttributes: [.font: NSFont.systemFont(ofSize: 34)])
    warm.unlockFocus()
    guard let cg = warm.cgImage(forProposedRect: nil, context: nil, hints: nil) else { fail("warmup image failed") }
    try? VNImageRequestHandler(cgImage: cg, options: [:]).perform([request])
    print((request.results ?? []).compactMap { $0.topCandidates(1).first?.string }.joined(separator: " "))
    exit(0)
}

let url = URL(fileURLWithPath: CommandLine.arguments[1])
guard let image = NSImage(contentsOf: url),
      let cgImage = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    fail("could not read image at \(url.path)")
}

do {
    try VNImageRequestHandler(cgImage: cgImage, options: [:]).perform([request])
} catch {
    fail("text recognition failed: \(error.localizedDescription)")
}

// Reading order: top-to-bottom, then left-to-right. Vision's origin is bottom-left.
let observations = (request.results ?? []).sorted { a, b in
    let rowA = (1 - a.boundingBox.midY), rowB = (1 - b.boundingBox.midY)
    if abs(rowA - rowB) > 0.012 { return rowA < rowB }
    return a.boundingBox.minX < b.boundingBox.minX
}

let lines = observations.compactMap { observation -> Line? in
    guard let candidate = observation.topCandidates(1).first else { return nil }
    let text = candidate.string.trimmingCharacters(in: .whitespacesAndNewlines)
    return text.isEmpty ? nil : Line(text: text, confidence: candidate.confidence)
}

let output = Output(width: cgImage.width, height: cgImage.height, lines: lines)
let data = try JSONEncoder().encode(output)
FileHandle.standardOutput.write(data)
