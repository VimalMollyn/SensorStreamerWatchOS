//
//  InternetCheck.swift
//  SensorStreamer
//
//  Reaches past the LAN three ways and reports what came back, so a run can
//  show whether the watch itself has internet access while it streams. There
//  is no ICMP ping for a watchOS app; these are the equivalents it can do:
//
//    tcp       an HTTP GET over a raw NWConnection to captive.apple.com:80
//    udp dns   an A query to 1.1.1.1:53 over a raw NWConnection
//    https     URLSession to the same page (which watchOS may route through
//              the paired iPhone, unlike the raw connections)
//
//  The raw connections only work under the audio exemption (see
//  AudioCapture), so this runs while a session is live. Each result names
//  the interface the connection used: `wifi` is the watch's own radio.
//

import Foundation
import Network

final class InternetCheck {

    /// One line per probe, on the main queue.
    var onResult: ((String) -> Void)?

    private let queue = DispatchQueue(label: "SensorStreamer.internet")
    private static let timeout: TimeInterval = 8

    func run() {
        tcpHTTP(host: "captive.apple.com", path: "/hotspot-detect.html")
        udpDNS(server: "1.1.1.1", name: "apple.com")
        https(URL(string: "https://captive.apple.com/hotspot-detect.html")!)
    }

    /// One NWConnection with a single outcome and a deadline.
    private final class Probe {
        let label: String
        let connection: NWConnection
        let start = Date()
        var lastState = "setup"
        private var done = false
        private let report: (String) -> Void

        init(label: String, connection: NWConnection, report: @escaping (String) -> Void) {
            self.label = label
            self.connection = connection
            self.report = report
        }

        var elapsedMs: Int { Int(Date().timeIntervalSince(start) * 1000) }

        var via: String {
            guard let path = connection.currentPath else { return "nopath" }
            let types = path.availableInterfaces.map { "\($0.type)" }
            return types.isEmpty ? "noiface" : types.joined(separator: "+")
        }

        func finish(_ text: String) {
            guard !done else { return }
            done = true
            connection.cancel()
            report("\(label): \(text)")
        }
    }

    private func start(_ probe: Probe, onReady: @escaping () -> Void) {
        probe.connection.stateUpdateHandler = { state in
            switch state {
            case .ready:
                probe.lastState = "ready"
                onReady()
            case .waiting(let error):
                probe.lastState = "waiting \(SocketClient.describe(error))"
            case .failed(let error):
                probe.finish("failed \(SocketClient.describe(error)) after \(probe.elapsedMs) ms")
            case .cancelled:
                break
            default:
                probe.lastState = "\(state)"
            }
        }
        probe.connection.start(queue: queue)
        queue.asyncAfter(deadline: .now() + Self.timeout) {
            probe.finish("timeout (\(probe.lastState))")
        }
    }

    private func tcpHTTP(host: String, path: String) {
        let parameters = NWParameters.tcp
        parameters.serviceClass = .interactiveVoice
        let connection = NWConnection(host: NWEndpoint.Host(host), port: 80, using: parameters)
        let probe = Probe(label: "tcp \(host):80", connection: connection, report: report)
        start(probe) {
            let request = "GET \(path) HTTP/1.1\r\nHost: \(host)\r\nConnection: close\r\n\r\n"
            probe.connection.send(content: Data(request.utf8), completion: .contentProcessed { error in
                if let error {
                    probe.finish("send failed \(SocketClient.describe(error))")
                    return
                }
                probe.connection.receive(minimumIncompleteLength: 1, maximumLength: 2048) { data, _, _, error in
                    if let error {
                        probe.finish("receive failed \(SocketClient.describe(error))")
                        return
                    }
                    let status = data.flatMap { String(data: $0, encoding: .utf8) }?
                        .components(separatedBy: "\r\n").first ?? "no data"
                    probe.finish("\(status) in \(probe.elapsedMs) ms via \(probe.via)")
                }
            })
        }
    }

    private func udpDNS(server: String, name: String) {
        let parameters = NWParameters.udp
        parameters.serviceClass = .interactiveVoice
        let id = UInt16.random(in: 1...0xfffe)
        let connection = NWConnection(host: NWEndpoint.Host(server), port: 53, using: parameters)
        let probe = Probe(label: "udp dns \(server)", connection: connection, report: report)
        start(probe) {
            probe.connection.send(content: Self.dnsQuery(name, id: id), completion: .contentProcessed { error in
                if let error {
                    probe.finish("send failed \(SocketClient.describe(error))")
                    return
                }
                probe.connection.receiveMessage { data, _, _, error in
                    if let error {
                        probe.finish("receive failed \(SocketClient.describe(error))")
                        return
                    }
                    guard let data, data.count >= 12 else {
                        probe.finish("short reply")
                        return
                    }
                    let bytes = [UInt8](data)
                    let replyId = UInt16(bytes[0]) << 8 | UInt16(bytes[1])
                    let rcode = bytes[3] & 0x0f
                    let answers = Int(bytes[6]) << 8 | Int(bytes[7])
                    guard replyId == id else {
                        probe.finish("reply id mismatch")
                        return
                    }
                    probe.finish("\(name) A: \(answers) answer(s), rcode \(rcode), in \(probe.elapsedMs) ms via \(probe.via)")
                }
            })
        }
    }

    /// A standard query, recursion desired, one question: `name` A IN.
    private static func dnsQuery(_ name: String, id: UInt16) -> Data {
        var data = Data([UInt8(id >> 8), UInt8(id & 0xff), 0x01, 0x00, 0, 1, 0, 0, 0, 0, 0, 0])
        for label in name.split(separator: ".") {
            data.append(UInt8(label.utf8.count))
            data.append(contentsOf: Array(label.utf8))
        }
        data.append(contentsOf: [0, 0, 1, 0, 1])
        return data
    }

    private func https(_ url: URL) {
        let start = Date()
        var request = URLRequest(url: url)
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.timeoutInterval = Self.timeout
        let host = url.host ?? "?"
        URLSession.shared.dataTask(with: request) { [weak self] data, response, error in
            let ms = Int(Date().timeIntervalSince(start) * 1000)
            if let error {
                self?.report("https \(host): \(error.localizedDescription) after \(ms) ms")
                return
            }
            let code = (response as? HTTPURLResponse)?.statusCode ?? 0
            self?.report("https \(host): \(code), \(data?.count ?? 0) bytes in \(ms) ms (URLSession)")
        }.resume()
    }

    private func report(_ line: String) {
        print("InternetCheck: \(line)")
        DispatchQueue.main.async { [onResult] in onResult?(line) }
    }
}
