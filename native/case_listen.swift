// On-device speech recognition for Human Emulator.
// Usage: case-listen <audio-file>   -> {"text": "...", "ms": 412, "locale": "en_GB"}
//        case-listen --prepare      -> installs the on-device speech model once, then {"ready": true}
// Uses Apple's SpeechAnalyzer, so audio never leaves the Mac.

import AVFoundation
import Foundation
import Speech

struct Output: Encodable {
    let text: String
    let ms: Int
    let locale: String
}

func fail(_ message: String, code: Int32 = 1) -> Never {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    exit(code)
}

func emit<T: Encodable>(_ value: T) -> Never {
    FileHandle.standardOutput.write(try! JSONEncoder().encode(value))
    exit(0)
}

func makeTranscriber() async throws -> (SpeechTranscriber, Locale) {
    let wanted = Locale(identifier: ProcessInfo.processInfo.environment["CASE_SPEECH_LOCALE"] ?? Locale.current.identifier)
    var locale = await SpeechTranscriber.supportedLocale(equivalentTo: wanted)
    if locale == nil {
        locale = await SpeechTranscriber.supportedLocale(equivalentTo: Locale(identifier: "en-US"))
    }
    guard let locale else { fail("no_locale: on-device speech recognition does not support this language", code: 3) }
    let transcriber = SpeechTranscriber(locale: locale, preset: .transcription)
    if let request = try await AssetInventory.assetInstallationRequest(supporting: [transcriber]) {
        try await request.downloadAndInstall()
    }
    return (transcriber, locale)
}

let args = CommandLine.arguments
guard args.count == 2 else { fail("usage: case-listen <audio-file> | --prepare") }

do {
    let (transcriber, locale) = try await makeTranscriber()
    if args[1] == "--prepare" {
        emit(["ready": true])
    }
    let started = Date()
    let file = try AVAudioFile(forReading: URL(fileURLWithPath: args[1]))
    let analyzer = SpeechAnalyzer(modules: [transcriber])
    async let text: String = transcriber.results.reduce(into: "") { partial, result in
        partial += String(result.text.characters)
    }
    if let last = try await analyzer.analyzeSequence(from: file) {
        try await analyzer.finalizeAndFinish(through: last)
    } else {
        await analyzer.cancelAndFinishNow()
    }
    let transcript = try await text.trimmingCharacters(in: .whitespacesAndNewlines)
    emit(Output(text: transcript, ms: Int(Date().timeIntervalSince(started) * 1000), locale: locale.identifier))
} catch {
    fail("speech recognition failed: \(error.localizedDescription)")
}
