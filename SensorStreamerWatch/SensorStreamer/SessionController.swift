//
//  SessionController.swift
//  SensorStreamer
//
//  Runs a session: AudioCapture taps the microphones, MotionCapture polls
//  CoreMotion, and everything leaves as the binary datagrams in Wire.swift
//  over one UDP socket to the Mac. Owns the settings the watch screen edits
//  and the status lines it shows.
//
//  The audio capture runs even when audio is not being sent: the UDP socket
//  only works under the audio-session exemption (see AudioCapture), so an
//  IMU-only stream still needs a live capture session.
//

import AVFoundation
import Foundation
import Network
import WatchKit

@Observable
final class SessionController {

    static let shared = SessionController()

    // MARK: - Settings (remembered across launches)

    var host: String { didSet { defaults.set(host, forKey: "host") } }
    var port: Int { didSet { defaults.set(port, forKey: "port") } }
    /// Send the microphone. Live: flipping it mid-run starts or stops the pcm
    /// datagrams; the capture itself keeps running (see the header).
    var audioOn: Bool {
        didSet { defaults.set(audioOn, forKey: "audio"); sendAudio = audioOn }
    }
    /// Tap channel to send, or -1 for all of them interleaved.
    var micChannel: Int { didSet { defaults.set(micChannel, forKey: "mic_channel") } }
    var measurementMode: Bool {
        didSet {
            defaults.set(measurementMode, forKey: "measurement")
            audio.captureMode = measurementMode ? .measurement : .default
        }
    }
    /// Send the IMU. Live.
    var imuOn: Bool {
        didSet {
            defaults.set(imuOn, forKey: "imu")
            guard isRunning else { return }
            if imuOn { motion.start(motionOptions) } else { stopMotion() }
        }
    }
    /// Also send CoreMotion's fused device motion.
    var fusedMotion: Bool { didSet { defaults.set(fusedMotion, forKey: "fused") } }
    var imuRate: Int { didSet { defaults.set(imuRate, forKey: "imu_rate") } }

    private var motionOptions: MotionCapture.Options {
        MotionCapture.Options(accelerometer: true, gyro: true, magnetometer: true,
                              deviceMotion: fusedMotion, rate: Double(imuRate))
    }

    // MARK: - Status for the screen

    private(set) var isRunning = false
    private(set) var statusText = "idle"
    private(set) var audioStatus = "audio idle"
    private(set) var motionStatus = "motion idle"
    private(set) var socketStatus = "socket idle"
    private(set) var runtimeStatus = "runtime idle"
    private(set) var rateStatus = ""
    /// Internet check results, one line each, for the current run.
    private(set) var netStatus = ""

    // MARK: - Machinery

    @ObservationIgnored private let defaults = UserDefaults.standard
    @ObservationIgnored private let audio = AudioCapture()
    @ObservationIgnored private let motion = MotionCapture()
    @ObservationIgnored private let runtime = ExtendedRuntimeSessionManager()
    @ObservationIgnored private let internet = InternetCheck()
    @ObservationIgnored private var socket: SocketClient?
    /// Datagrams are assembled, sent and counted here, in order, off the
    /// audio and motion threads.
    @ObservationIgnored private let pack = DispatchQueue(label: "SensorStreamer.pack", qos: .userInteractive)
    @ObservationIgnored private var activated = false
    @ObservationIgnored private var sendAudio = true
    @ObservationIgnored private var activeChannels: [Int] = []

    // pack queue
    @ObservationIgnored private var packer: PCMPacker?
    @ObservationIgnored private var batches: [StreamKind: RecordBatch] = [:]
    @ObservationIgnored private var activeRate = 100.0
    @ObservationIgnored private var sent = 0
    @ObservationIgnored private var dropped = 0
    @ObservationIgnored private var bytesSent = 0
    @ObservationIgnored private var textIndex: UInt32 = 0
    @ObservationIgnored private var lastReport = (time: 0.0, sent: 0, bytes: 0)

    // main queue
    @ObservationIgnored private var heartbeat: Timer?
    @ObservationIgnored private var preempt: Timer?
    @ObservationIgnored private var pathMonitor: NWPathMonitor?
    @ObservationIgnored private var pathSatisfied = true
    @ObservationIgnored private var reactivations = 0
    @ObservationIgnored private var heartbeats = 0

    /// Re-activate this many seconds after the last activation, before the
    /// ~36 s at which watchOS withdraws the networking grant.
    private static let preemptInterval: TimeInterval = 28

    private init() {
        host = defaults.string(forKey: "host") ?? "192.168.12.39"
        port = defaults.object(forKey: "port") as? Int ?? 5005
        audioOn = defaults.object(forKey: "audio") as? Bool ?? true
        micChannel = defaults.object(forKey: "mic_channel") as? Int ?? 0
        measurementMode = defaults.object(forKey: "measurement") as? Bool ?? true
        imuOn = defaults.object(forKey: "imu") as? Bool ?? true
        fusedMotion = defaults.object(forKey: "fused") as? Bool ?? false
        imuRate = defaults.object(forKey: "imu_rate") as? Int ?? 100
        sendAudio = audioOn
        audio.captureMode = measurementMode ? .measurement : .default
    }

    // MARK: - Lifecycle

    /// Once, when the screen first appears.
    func activate() {
        guard !activated else { return }
        activated = true
        audio.setup()
        audio.onStatus = { [weak self] status in
            self?.audioStatus = status
            self?.sendText(status)
        }
        audio.onBuffer = { [weak self] buffer, time in
            self?.ingest(buffer, time: time)
        }
        motion.onStatus = { [weak self] status in
            self?.motionStatus = status
            self?.sendText(status)
        }
        motion.onSample = { [weak self] kind, time, values in
            self?.ingest(kind, time: time, values: values)
        }
        runtime.onStatusChange = { [weak self] status in
            self?.runtimeStatus = status
            self?.sendText(status)
        }
        internet.onResult = { [weak self] line in
            guard let self else { return }
            self.netStatus = self.netStatus.isEmpty || self.netStatus == "checking…"
                ? line : self.netStatus + "\n" + line
            self.sendText("net: " + line)
        }
    }

    /// Reaches past the LAN (see InternetCheck). Needs a live session for the
    /// raw connections; runs once by itself a few seconds after Start.
    func checkInternet() {
        guard isRunning else {
            netStatus = "press Start first"
            return
        }
        netStatus = "checking…"
        internet.run()
    }

    func start() {
        guard !isRunning else { return }
        guard !host.isEmpty else {
            statusText = "set the Mac's IP first"
            return
        }
        isRunning = true
        statusText = "starting"
        rateStatus = ""
        netStatus = ""
        reactivations = 0
        heartbeats = 0
        pack.sync {
            packer = nil
            batches.removeAll()
            activeRate = Double(imuRate)
            sent = 0
            dropped = 0
            bytesSent = 0
            lastReport = (WallClock.now, 0, 0)
        }

        audio.start { [weak self] ok in
            guard let self else { return }
            guard ok else {
                self.statusText = "audio failed"
                self.isRunning = false
                return
            }
            let available = max(1, self.audio.channelCount)
            self.activeChannels = self.micChannel < 0
                ? Array(0..<available) : [min(self.micChannel, available - 1)]
            let packer = PCMPacker(channels: self.activeChannels.count, rate: self.audio.sampleRate)
            packer.onDatagram = { [weak self] data in self?.send(data) }
            self.pack.sync { self.packer = packer }
            // The socket after the session is active: the grant that lets it
            // open exists because the session does.
            self.openSocket()
            self.runtime.start()
            self.startPathWatch()
            self.armPreempt()
            self.heartbeat?.invalidate()
            self.heartbeat = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { [weak self] _ in
                self?.beat()
            }
            if self.imuOn { self.motion.start(self.motionOptions) }
            DispatchQueue.main.asyncAfter(deadline: .now() + 4) { [weak self] in
                guard let self, self.isRunning else { return }
                self.checkInternet()
            }
            self.statusText = "running"
            self.sendText(String(format: "start: pcm %d ch at %.0f Hz, %d samples/datagram; imu %d Hz; %@",
                                 self.activeChannels.count, self.audio.sampleRate, packer.perDatagram,
                                 self.imuRate, self.motion.availability))
        }
    }

    func stop() {
        guard isRunning else { return }
        stopMotion()
        sendText("stop")
        heartbeat?.invalidate(); heartbeat = nil
        preempt?.invalidate(); preempt = nil
        pathMonitor?.cancel(); pathMonitor = nil
        audio.stop()
        runtime.stop()
        pack.sync {
            batches.values.forEach { $0.flush() }
            batches.removeAll()
            packer = nil
        }
        socket?.close()
        socket = nil
        isRunning = false
        statusText = "stopped"
        socketStatus = "socket idle"
    }

    private func stopMotion() {
        motion.stop()
        pack.async { [weak self] in
            self?.batches.values.forEach { $0.flush() }
        }
    }

    // MARK: - Socket

    private func openSocket() {
        socket?.close()
        guard let portNumber = UInt16(exactly: port) else {
            socketStatus = "bad port"
            return
        }
        let client = SocketClient(host: host, port: portNumber)
        client.onState = { [weak self] state in
            DispatchQueue.main.async { self?.socketStatus = "socket \(state)" }
        }
        client.onSendError = { [weak self] error in
            DispatchQueue.main.async {
                self?.socketStatus = "send failed \(SocketClient.describe(error))"
            }
        }
        client.open()
        socket = client
        socketStatus = "socket opening"
    }

    private func reactivateNetwork(_ reason: String) {
        reactivations += 1
        print("SessionController: net: \(reason) (#\(reactivations))")
        audio.reactivate()
        armPreempt()
    }

    private func armPreempt() {
        preempt?.invalidate()
        preempt = Timer.scheduledTimer(withTimeInterval: Self.preemptInterval, repeats: false) { [weak self] _ in
            guard let self, self.isRunning else { return }
            self.reactivateNetwork("preemptive reactivate")
        }
    }

    /// The grant's withdrawal shows as the path going unsatisfied; re-activating
    /// the session brings it back, after which the socket must be remade.
    /// NWPathMonitor keeps calling with the app in the background, which a
    /// main-thread timer does not.
    private func startPathWatch() {
        pathMonitor?.cancel()
        pathSatisfied = true
        let monitor = NWPathMonitor()
        monitor.pathUpdateHandler = { [weak self] path in
            let ok = path.status == .satisfied
            DispatchQueue.main.async {
                guard let self, self.isRunning else { return }
                if !ok, self.pathSatisfied {
                    self.reactivateNetwork("path lost - reactivating")
                } else if ok, !self.pathSatisfied {
                    self.openSocket()
                }
                self.pathSatisfied = ok
            }
        }
        monitor.start(queue: DispatchQueue.global(qos: .utility))
        pathMonitor = monitor
    }

    private func checkSocket() {
        guard isRunning, let socket else { return }
        if !socket.isReady || socket.sendFailures > 0 {
            audio.reactivate { [weak self] in
                DispatchQueue.main.async { self?.openSocket() }
            }
        }
    }

    /// Pack queue. Dropped rather than queued when the link is not taking
    /// them, so a stall never turns into a late flood.
    private func send(_ datagram: Data) {
        guard let socket, socket.isReady, pathSatisfied else {
            dropped += 1
            return
        }
        socket.send(datagram)
        sent += 1
        bytesSent += datagram.count
    }

    /// Any thread.
    private func sendText(_ text: String) {
        pack.async { [weak self] in
            guard let self, self.socket != nil else { return }
            self.send(Wire.text(text, index: self.textIndex))
            self.textIndex &+= 1
        }
    }

    // MARK: - Samples -> datagrams

    /// Audio thread: the chosen channels to Int16, interleaved, then over to
    /// the pack queue.
    private func ingest(_ buffer: AVAudioPCMBuffer, time: Double) {
        guard sendAudio, let data = buffer.floatChannelData else { return }
        let channels = activeChannels
        guard !channels.isEmpty else { return }
        let frames = Int(buffer.frameLength)
        let available = Int(buffer.format.channelCount)
        var samples = [Int16](repeating: 0, count: frames * channels.count)
        var k = 0
        for i in 0..<frames {
            for c in channels {
                let x = c < available ? data[c][i] : 0
                samples[k] = Int16(max(-32768, min(32767, (x * 32767).rounded())))
                k += 1
            }
        }
        pack.async { [weak self] in
            self?.packer?.push(samples, time: time)
        }
    }

    /// Motion queue.
    private func ingest(_ kind: StreamKind, time: Double, values: [Float]) {
        pack.async { [weak self] in
            guard let self else { return }
            let batch = self.batches[kind] ?? {
                // A datagram every 50 ms: five records at 100 Hz.
                let batch = RecordBatch(kind: kind, channels: values.count, rate: self.activeRate,
                                        perDatagram: Int(self.activeRate / 20))
                batch.onDatagram = { [weak self] data in self?.send(data) }
                self.batches[kind] = batch
                return batch
            }()
            batch.append(time, values)
        }
    }

    // MARK: - Heartbeat

    /// Main queue, every two seconds. Reads the pack queue's counters without
    /// hopping onto it: telemetry, not bookkeeping.
    private func beat() {
        guard isRunning else { return }
        heartbeats += 1
        let now = WallClock.now
        let dt = max(now - lastReport.time, 1e-3)
        let pps = Double(sent - lastReport.sent) / dt
        let kbps = Double(bytesSent - lastReport.bytes) / dt / 1000
        lastReport = (now, sent, bytesSent)
        rateStatus = String(format: "%.0f pkt/s · %.1f kB/s · %d dropped", pps, kbps, dropped)

        var fields = [
            String(format: "pps=%.1f", pps),
            String(format: "kbps=%.1f", kbps),
            "sent=\(sent)", "dropped=\(dropped)",
            "pcm=\(packer?.index ?? 0)", "skipped=\(packer?.skipped ?? 0)",
            "imu=\(motion.samples)",
            String(format: "inpk=%.3f", audio.inputPeak),
            "taps=\(audio.tapCallbacks)", "restarts=\(audio.restarts)",
            "socket=\(socket?.state ?? "none")", "path=\(socket?.pathDescription ?? "-")",
            "inflight=\(socket?.bytesInFlight ?? 0)", "react=\(reactivations)",
        ]
        if heartbeats % 5 == 1 {
            fields.append("session=\"\(audio.sessionDescription)\"")
        }
        sendText("hb " + fields.joined(separator: " "))
        checkSocket()
    }
}
