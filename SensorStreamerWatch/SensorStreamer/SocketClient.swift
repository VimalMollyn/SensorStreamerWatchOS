//
//  SocketClient.swift
//  SensorStreamer
//
//  One UDP connection from the watch to the Mac, sending binary datagrams.
//  From WatchHand (and PoseTouch before it). The one thing worth knowing is in
//  AudioCapture: the connection only becomes ready while an audio session
//  activated with `activate(options:)` is live, and that grant lapses ~36 s
//  after each activation.
//

import Foundation
import Network

final class SocketClient {

    let host: NWEndpoint.Host
    let port: NWEndpoint.Port
    private var connection: NWConnection?
    private let queue = DispatchQueue(label: "SensorStreamer.socket")

    /// Most recent connection state, in words. Updated on the socket queue.
    private(set) var state = "setup"
    var onState: ((String) -> Void)?
    var onSendError: ((NWError) -> Void)?
    private(set) var sendFailures = 0
    private(set) var datagrams = 0
    private(set) var bytes = 0

    private let flightLock = NSLock()
    private var _inFlight = 0
    /// Bytes handed to `send` whose completion has not fired: the one signal a
    /// sender gets that datagrams are being delayed rather than delivered.
    var bytesInFlight: Int {
        flightLock.lock(); defer { flightLock.unlock() }
        return _inFlight
    }

    var isReady: Bool { connection?.state == .ready }

    var pathDescription: String {
        guard let path = connection?.currentPath else { return "nopath" }
        let types = path.availableInterfaces.map { "\($0.type)" }
        return types.isEmpty ? "noiface" : types.joined(separator: "+")
    }

    static func describe(_ error: NWError) -> String {
        switch error {
        case .posix(let code): return "posix \(code.rawValue)"
        case .dns(let type): return "dns \(type)"
        case .tls(let status): return "tls \(status)"
        default: return "unknown"
        }
    }

    init(host: String, port: UInt16) {
        self.host = NWEndpoint.Host(host)
        self.port = NWEndpoint.Port(integerLiteral: port)
    }

    func open() {
        connection?.cancel()
        let parameters = NWParameters.udp
        // TN3135 asks that the traffic the audio exemption permits be tagged.
        parameters.serviceClass = .interactiveVoice
        let connection = NWConnection(host: host, port: port, using: parameters)
        connection.stateUpdateHandler = { [weak self] newState in
            guard let self else { return }
            switch newState {
            case .ready: self.state = "ready"
            case .waiting(let error): self.state = "waiting \(Self.describe(error))"
            case .failed(let error): self.state = "failed \(Self.describe(error))"
            case .cancelled: self.state = "cancelled"
            default: self.state = "\(newState)"
            }
            print("SocketClient: \(self.state)")
            self.onState?(self.state)
        }
        connection.start(queue: queue)
        self.connection = connection
        sendFailures = 0
    }

    func close() {
        connection?.cancel()
        connection = nil
    }

    /// One datagram.
    func send(_ data: Data) {
        guard let connection else { return }
        flightLock.lock(); _inFlight += data.count; flightLock.unlock()
        connection.send(content: data, completion: .contentProcessed { [weak self] error in
            guard let self else { return }
            self.flightLock.lock(); self._inFlight -= data.count; self.flightLock.unlock()
            if let error {
                self.sendFailures += 1
                self.onSendError?(error)
            } else {
                self.sendFailures = 0
                self.datagrams += 1
                self.bytes += data.count
            }
        })
    }
}
