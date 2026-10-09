//
//  Wire.swift
//  SensorStreamer
//
//  The datagram format, the clock everything is stamped with, and the two
//  packers that turn samples into datagrams. Parsed by sensorstreamer/wire.py.
//
//  Every datagram is a 24-byte little-endian header and a payload:
//
//      0   4   magic   "SSW1"
//      4   1   kind    0 text, 1 pcm, 2 acc, 3 gyr, 4 mag, 5 motion
//      5   1   ch      values per sample: pcm channels, or floats per IMU record
//      6   2   count   samples (pcm), records (IMU) or bytes (text) that follow
//      8   4   index   pcm: index of the first sample since start; IMU: of the
//                      first record; text: a sequence number. A gap is a loss.
//      12  4   rate    Float32, nominal samples per second
//      16  8   t0      Float64, the watch's wall clock (unix s) at the first sample
//      24  ... payload
//
//  pcm      count x ch Int16, interleaved; sample i is at t0 + i / rate.
//  IMU      count records of Float32 x (1 + ch): [t - t0, v0, v1, ...].
//           acc, gyr: x y z (g, rad/s). mag: x y z (microtesla).
//           motion: quaternion w x y z, rotation rate x y z, user
//           acceleration x y z, gravity x y z.
//  text     UTF-8; "hb ..." is the heartbeat, anything else an event.
//
//  Audio and motion share one clock: the audio tap's host time
//  (mach_absolute_time) and CoreMotion's since-boot timestamps are both turned
//  into wall time with anchors taken at the same instant, so the Mac can put
//  the streams on one axis without guessing.
//

import AVFoundation
import Foundation

enum StreamKind: UInt8 {
    case text = 0, pcm = 1, acc = 2, gyr = 3, mag = 4, motion = 5
}

enum Wire {
    static let magic = Array("SSW1".utf8)
    static let headerSize = 24

    static func datagram(kind: StreamKind, channels: Int, count: Int, index: UInt32,
                         rate: Double, t0: Double, payload: Data) -> Data {
        var data = Data(capacity: headerSize + payload.count)
        data.append(contentsOf: magic)
        data.append(kind.rawValue)
        data.append(UInt8(clamping: channels))
        append(&data, UInt16(clamping: count).littleEndian)
        append(&data, index.littleEndian)
        append(&data, Float(rate).bitPattern.littleEndian)
        append(&data, t0.bitPattern.littleEndian)
        data.append(payload)
        return data
    }

    static func text(_ text: String, index: UInt32) -> Data {
        let payload = Data(text.utf8)
        return datagram(kind: .text, channels: 0, count: payload.count, index: index,
                        rate: 0, t0: WallClock.now, payload: payload)
    }

    private static func append<T>(_ data: inout Data, _ value: T) {
        withUnsafeBytes(of: value) { data.append(contentsOf: $0) }
    }
}

enum WallClock {
    /// Wall clock minus each monotonic clock, read together once. Wall-clock
    /// steps after that do not reach the stream; the Mac places it anyway.
    private static let anchors: (host: Double, uptime: Double) = {
        let wall = Date().timeIntervalSince1970
        return (wall - AVAudioTime.seconds(forHostTime: mach_absolute_time()),
                wall - ProcessInfo.processInfo.systemUptime)
    }()

    static var now: Double { Date().timeIntervalSince1970 }

    /// An audio tap's `when.hostTime`.
    static func fromHostTime(_ hostTime: UInt64) -> Double {
        anchors.host + AVAudioTime.seconds(forHostTime: hostTime)
    }

    /// A CMLogItem's `timestamp`.
    static func fromUptime(_ uptime: TimeInterval) -> Double {
        anchors.uptime + uptime
    }
}

/// Collects IMU records of one kind and emits a datagram every `perDatagram`.
final class RecordBatch {
    let kind: StreamKind
    let channels: Int
    let rate: Double
    let perDatagram: Int
    var onDatagram: ((Data) -> Void)?

    private var t0 = 0.0
    private var values: [Float] = []
    private var count = 0
    private(set) var index: UInt32 = 0

    init(kind: StreamKind, channels: Int, rate: Double, perDatagram: Int) {
        self.kind = kind
        self.channels = channels
        self.rate = rate
        self.perDatagram = max(1, perDatagram)
    }

    func append(_ t: Double, _ sample: [Float]) {
        if count == 0 { t0 = t }
        values.append(Float(t - t0))
        values.append(contentsOf: sample)
        count += 1
        if count >= perDatagram { flush() }
    }

    func flush() {
        guard count > 0 else { return }
        let payload = values.withUnsafeBufferPointer { Data(buffer: $0) }
        onDatagram?(Wire.datagram(kind: kind, channels: channels, count: count, index: index,
                                  rate: rate, t0: t0, payload: payload))
        index &+= UInt32(count)
        count = 0
        values.removeAll(keepingCapacity: true)
    }
}

/// Int16 audio, `channels` interleaved, `perDatagram` samples a datagram.
/// Stamped by sample index from the first block's time, so the wall time is
/// the sample clock's; a jump in the tap's clock (an engine restart) becomes
/// a hole of the right size in the index.
final class PCMPacker {
    let channels: Int
    let rate: Double
    let perDatagram: Int
    var onDatagram: ((Data) -> Void)?

    private var pending: [Int16] = []
    private var pendingT0 = 0.0
    private(set) var index: UInt32 = 0
    private(set) var skipped = 0

    init(channels: Int, rate: Double) {
        self.channels = max(1, channels)
        self.rate = rate
        // Under one Ethernet frame: 1 ch 480 samples (10 ms), 2 ch 352, 3 ch 224.
        perDatagram = min(480, 1408 / (2 * self.channels) / 16 * 16)
    }

    /// `samples` are interleaved Int16, `time` is the wall time of the first.
    func push(_ samples: [Int16], time: Double) {
        let frames = samples.count / channels
        guard frames > 0 else { return }
        if pending.isEmpty {
            pendingT0 = time
        } else {
            let expected = pendingT0 + Double(pending.count / channels) / rate
            if abs(time - expected) > 0.02 {
                let missed = Int(((time - expected) * rate).rounded())
                if missed > 0 { index &+= UInt32(missed) }
                skipped += pending.count / channels
                pending.removeAll(keepingCapacity: true)
                pendingT0 = time
            }
        }
        pending.append(contentsOf: samples)
        let stride = perDatagram * channels
        var start = 0
        while pending.count - start >= stride {
            let payload = pending[start..<(start + stride)].withUnsafeBufferPointer { Data(buffer: $0) }
            onDatagram?(Wire.datagram(kind: .pcm, channels: channels, count: perDatagram, index: index,
                                      rate: rate, t0: pendingT0, payload: payload))
            index &+= UInt32(perDatagram)
            pendingT0 += Double(perDatagram) / rate
            start += stride
        }
        pending.removeFirst(start)
    }
}
