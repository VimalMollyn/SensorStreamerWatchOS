//
//  MotionCapture.swift
//  SensorStreamer
//
//  CoreMotion at a fixed rate: the raw accelerometer and gyroscope, the
//  magnetometer if the watch exposes one, and optionally the fused device
//  motion (attitude, bias-corrected rotation rate, user acceleration and
//  gravity). CMMotionManager needs no permission and keeps delivering while
//  the extended runtime session keeps the app alive. 100 Hz is the most it
//  gives on the watch; CMBatchedSensorManager goes higher but needs a workout
//  session, which is not minimal.
//

import CoreMotion
import Foundation

final class MotionCapture {

    struct Options {
        var accelerometer = true
        var gyro = true
        var magnetometer = true
        var deviceMotion = false
        var rate = 100.0
    }

    /// One sample: kind, wall time, values (see Wire.swift for the order).
    /// Called on the motion queue, in order.
    var onSample: ((StreamKind, Double, [Float]) -> Void)?
    /// Short human-readable events, on the main queue.
    var onStatus: ((String) -> Void)?

    private(set) var isRunning = false
    private(set) var samples = 0
    private(set) var errors = 0

    private let manager = CMMotionManager()
    private let queue: OperationQueue = {
        let queue = OperationQueue()
        queue.name = "SensorStreamer.motion"
        queue.maxConcurrentOperationCount = 1
        queue.qualityOfService = .userInteractive
        return queue
    }()

    var availability: String {
        "acc \(manager.isAccelerometerAvailable ? 1 : 0) gyr \(manager.isGyroAvailable ? 1 : 0) "
            + "mag \(manager.isMagnetometerAvailable ? 1 : 0) motion \(manager.isDeviceMotionAvailable ? 1 : 0)"
    }

    func start(_ options: Options) {
        guard !isRunning else { return }
        isRunning = true
        samples = 0
        errors = 0
        let interval = 1.0 / max(1.0, options.rate)
        var started: [String] = []

        if options.accelerometer, manager.isAccelerometerAvailable {
            manager.accelerometerUpdateInterval = interval
            manager.startAccelerometerUpdates(to: queue) { [weak self] data, error in
                guard let self else { return }
                guard let data else { self.failed("acc", error); return }
                let a = data.acceleration
                self.emit(.acc, data.timestamp, [Float(a.x), Float(a.y), Float(a.z)])
            }
            started.append("acc")
        }
        if options.gyro, manager.isGyroAvailable {
            manager.gyroUpdateInterval = interval
            manager.startGyroUpdates(to: queue) { [weak self] data, error in
                guard let self else { return }
                guard let data else { self.failed("gyr", error); return }
                let r = data.rotationRate
                self.emit(.gyr, data.timestamp, [Float(r.x), Float(r.y), Float(r.z)])
            }
            started.append("gyr")
        }
        if options.magnetometer, manager.isMagnetometerAvailable {
            manager.magnetometerUpdateInterval = interval
            manager.startMagnetometerUpdates(to: queue) { [weak self] data, error in
                guard let self else { return }
                guard let data else { self.failed("mag", error); return }
                let m = data.magneticField
                self.emit(.mag, data.timestamp, [Float(m.x), Float(m.y), Float(m.z)])
            }
            started.append("mag")
        }
        if options.deviceMotion, manager.isDeviceMotionAvailable {
            manager.deviceMotionUpdateInterval = interval
            manager.startDeviceMotionUpdates(using: .xArbitraryZVertical, to: queue) { [weak self] data, error in
                guard let self else { return }
                guard let data else { self.failed("motion", error); return }
                let q = data.attitude.quaternion
                let r = data.rotationRate
                let u = data.userAcceleration
                let g = data.gravity
                self.emit(.motion, data.timestamp, [
                    Float(q.w), Float(q.x), Float(q.y), Float(q.z),
                    Float(r.x), Float(r.y), Float(r.z),
                    Float(u.x), Float(u.y), Float(u.z),
                    Float(g.x), Float(g.y), Float(g.z),
                ])
            }
            started.append("motion")
        }
        note(started.isEmpty ? "no motion sensors (\(availability))"
             : "motion: \(started.joined(separator: " ")) at \(Int(options.rate)) Hz")
    }

    func stop() {
        guard isRunning else { return }
        isRunning = false
        manager.stopAccelerometerUpdates()
        manager.stopGyroUpdates()
        manager.stopMagnetometerUpdates()
        manager.stopDeviceMotionUpdates()
        note("motion stopped")
    }

    private func emit(_ kind: StreamKind, _ uptime: TimeInterval, _ values: [Float]) {
        samples += 1
        onSample?(kind, WallClock.fromUptime(uptime), values)
    }

    private func failed(_ what: String, _ error: Error?) {
        errors += 1
        if errors == 1 {
            note("\(what) failed - \(error?.localizedDescription ?? "no data")")
        }
    }

    private func note(_ status: String) {
        print("MotionCapture: \(status)")
        DispatchQueue.main.async { [onStatus] in onStatus?(status) }
    }
}
