//
//  AudioCapture.swift
//  SensorStreamer
//
//  Captures the microphones with one AVAudioEngine: a tap on the input node
//  hands every block (all channels, Float32, the hardware rate: 48 kHz and
//  three channels on the Ultra) to `onBuffer`. Nothing is played.
//
//  Two things from PoseTouch and WatchHand are kept because they were learned
//  the hard way there:
//
//   * The session is activated with `activate(options:)`, the asynchronous,
//     watchOS-only call, and not `setActive(true)`. Both leave the session
//     active; only the first satisfies the audio exemption in TN3135 that lets
//     a watch app open a UDP socket at all. Without it NWConnection fails with
//     POSIX 50 and looks like a missing route. The grant lapses ~36 s after
//     each activation (FB24377808), so SessionController re-activates every
//     28 s, and runs this capture even when audio is not being sent.
//   * The engine does not restart itself: a configuration change stops it and
//     drops its tap, and an interruption deactivates the session under it.
//     Both are observed and recovered from here.
//
//  The category stays `.playAndRecord`, which is what the exemption was seen
//  to work with; `.record` alone is untested.
//

import Accelerate
import AVFoundation
import Foundation
import WatchKit

final class AudioCapture {

    /// `.measurement` asks the system for the least processing it offers: no
    /// automatic gain, no noise suppression, no echo cancellation. `.default`
    /// is the fallback. Changed between runs only.
    var captureMode: AVAudioSession.Mode = .measurement

    /// Every tap block, with the wall-clock time of its first sample. Called
    /// on the audio thread; the buffer is only valid for the duration of the call.
    var onBuffer: ((AVAudioPCMBuffer, Double) -> Void)?
    /// Short human-readable events, on the main queue.
    var onStatus: ((String) -> Void)?

    private(set) var isRunning = false
    private(set) var sampleRate: Double = 0
    private(set) var channelCount = 0
    private(set) var inputPeak: Float = 0
    private(set) var tapCallbacks = 0
    private(set) var restarts = 0
    private var isInterrupted = false

    private let engine = AVAudioEngine()
    private var observers: [NSObjectProtocol] = []

    deinit {
        observers.forEach(NotificationCenter.default.removeObserver)
    }

    // MARK: - Session

    func setup() {
        if AVAudioApplication.shared.recordPermission == .undetermined {
            AVAudioApplication.requestRecordPermission { allowed in
                if !allowed { print("AudioCapture: microphone permission denied") }
            }
        }
        observe()
    }

    /// Category, mode, and the asynchronous activation. `then` runs on the
    /// main queue once the session says it is active, which is when the engine
    /// can be started and the socket opened.
    private func configureSession(then: @escaping () -> Void) {
        let session = AVAudioSession.sharedInstance()
        do {
            if session.category != .playAndRecord || session.mode != captureMode {
                try session.setCategory(.playAndRecord, mode: captureMode, options: [])
            }
        } catch {
            note("could not set category - \(error.localizedDescription)")
        }
        if !session.prefersNoInterruptionsFromSystemAlerts {
            try? session.setPrefersNoInterruptionsFromSystemAlerts(true)
        }
        session.activate(options: []) { [weak self] activated, error in
            if let error {
                self?.note("activate failed - \(error.localizedDescription)")
            } else if !activated {
                self?.note("session did not activate")
            }
            DispatchQueue.main.async(execute: then)
        }
    }

    /// Re-activates the session without touching the engine, to restore the
    /// networking grant watchOS withdraws ~36 s after each activation
    /// (FB24377808). `activate(options:)` alone leaves the tap running.
    func reactivate(_ completion: (() -> Void)? = nil) {
        AVAudioSession.sharedInstance().activate(options: []) { activated, error in
            if let error {
                print("AudioCapture: reactivate failed - \(error.localizedDescription)")
            } else if !activated {
                print("AudioCapture: reactivate did not activate")
            }
            completion?()
        }
    }

    /// What the session is actually doing, for the heartbeat.
    var sessionDescription: String {
        let session = AVAudioSession.sharedInstance()
        let ins = session.currentRoute.inputs.map { $0.portType.rawValue }.joined(separator: ",")
        let inFormat = engine.inputNode.outputFormat(forBus: 0)
        return String(format: "in [%@] %.0f Hz %d ch · mode %@ · lat in %.0f ms",
                      ins, inFormat.sampleRate, inFormat.channelCount,
                      session.mode.rawValue, session.inputLatency * 1000)
    }

    // MARK: - Run

    /// Starts the capture. `completion(true)` once the tap is running.
    func start(_ completion: @escaping (Bool) -> Void) {
        guard !isRunning else { completion(true); return }
        guard AVAudioApplication.shared.recordPermission == .granted else {
            note("mic not permitted")
            completion(false)
            return
        }
        inputPeak = 0
        tapCallbacks = 0
        restarts = 0
        isInterrupted = false
        configureSession { [weak self] in
            guard let self else { return }
            let ok = self.startEngine()
            self.isRunning = ok
            self.note(ok ? "running" : "engine failed")
            completion(ok)
        }
    }

    func stop() {
        guard isRunning else { return }
        isRunning = false
        isInterrupted = false
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        note("stopped")
    }

    /// Installs the tap and starts the engine. Safe to call again after a
    /// stoppage: the input format may have changed underneath.
    private func startEngine() -> Bool {
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else {
            note("no audio input")
            return false
        }
        sampleRate = format.sampleRate
        channelCount = Int(format.channelCount)

        input.removeTap(onBus: 0)
        input.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak self] buffer, when in
            guard let self, buffer.frameLength > 0, let data = buffer.floatChannelData else { return }
            self.tapCallbacks += 1
            var peak: Float = 0
            vDSP_maxmgv(data[0], 1, &peak, vDSP_Length(buffer.frameLength))
            if peak > self.inputPeak { self.inputPeak = peak }
            let time = when.isHostTimeValid ? WallClock.fromHostTime(when.hostTime) : WallClock.now
            self.onBuffer?(buffer, time)
        }

        engine.prepare()
        do {
            try engine.start()
        } catch {
            input.removeTap(onBus: 0)
            note("engine start failed - \(error.localizedDescription)")
            return false
        }
        note("engine: " + sessionDescription)
        return true
    }

    // MARK: - Staying alive

    private func observe() {
        guard observers.isEmpty else { return }
        let center = NotificationCenter.default

        observers.append(center.addObserver(
            forName: .AVAudioEngineConfigurationChange, object: engine, queue: .main
        ) { [weak self] _ in
            guard let self, self.isRunning, !self.isInterrupted else { return }
            self.restarts += 1
            if self.startEngine() {
                self.note("engine reconfigured (\(self.restarts))")
            } else {
                self.note("reconfigure failed")
            }
        })

        observers.append(center.addObserver(
            forName: AVAudioSession.interruptionNotification, object: nil, queue: .main
        ) { [weak self] note in
            self?.handleInterruption(note)
        })

        observers.append(center.addObserver(
            forName: AVAudioSession.mediaServicesWereResetNotification, object: nil, queue: .main
        ) { [weak self] _ in
            guard let self, self.isRunning else { return }
            self.note("media services reset")
            try? AVAudioSession.sharedInstance().setCategory(.playAndRecord, mode: self.captureMode)
            self.isInterrupted = true
            _ = self.resume()
        })

        observers.append(center.addObserver(
            forName: WKExtension.applicationDidBecomeActiveNotification, object: nil, queue: .main
        ) { [weak self] _ in
            guard let self, self.isRunning, self.isInterrupted else { return }
            if self.resume() { self.note("resumed") }
        })
    }

    private func handleInterruption(_ notification: Notification) {
        guard isRunning,
              let raw = notification.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
              let type = AVAudioSession.InterruptionType(rawValue: raw) else { return }
        switch type {
        case .began:
            isInterrupted = true
            if engine.isRunning { engine.pause() }
            let reason = notification.userInfo?[AVAudioSessionInterruptionReasonKey] as? UInt
            note("interrupted: \(Self.describe(reason: reason))")
        case .ended:
            if resume() {
                note("resumed")
            } else {
                note("interrupted - open the app to resume")
            }
        @unknown default:
            break
        }
    }

    private func resume() -> Bool {
        do {
            try AVAudioSession.sharedInstance().setActive(true)
        } catch {
            print("AudioCapture: session would not activate - \(error.localizedDescription)")
            return false
        }
        if !engine.isRunning {
            do {
                try engine.start()
            } catch {
                guard startEngine() else { return false }
            }
        }
        isInterrupted = false
        restarts += 1
        return true
    }

    private static func describe(reason: UInt?) -> String {
        switch reason {
        case 0: return "another session took over"
        case 1: return "app suspended"
        case 2: return "mic muted"
        case 4: return "route disconnected"
        case 5: return "wrist down or locked"
        case .some(let other): return "reason \(other)"
        case nil: return "unknown"
        }
    }

    private func note(_ status: String) {
        print("AudioCapture: \(status)")
        DispatchQueue.main.async { [onStatus] in onStatus?(status) }
    }
}
