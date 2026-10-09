//
//  ExtendedRuntimeSessionManager.swift
//  SensorStreamer
//
//  Keeps the app running with the wrist down. Without a session watchOS
//  suspends the app when the screen goes dark and the capture stops with it.
//  A WKExtendedRuntimeSession needs no HealthKit authorisation; `mindfulness`
//  (declared in WKBackgroundModes) gives an hour. From PoseTouch/WatchHand.
//
//  Constraints: it must be started while the app is frontmost, it ends if the
//  app loses frontmost status (a crown press, switching apps - a lowered wrist
//  does not count), and it expires.
//

import Foundation
import WatchKit

final class ExtendedRuntimeSessionManager: NSObject {

    private var session: WKExtendedRuntimeSession?

    /// On the main queue whenever the session's state changes.
    var onStatusChange: ((String) -> Void)?

    var isRunning: Bool { session?.state == .running }

    func start() {
        guard session == nil || session?.state == .invalid else {
            print("ExtendedRuntimeSession: already active")
            return
        }
        let session = WKExtendedRuntimeSession()
        session.delegate = self
        session.start()
        self.session = session
    }

    func stop() {
        session?.invalidate()
        session = nil
    }

    private func report(_ status: String) {
        print("ExtendedRuntimeSession: \(status)")
        DispatchQueue.main.async { [onStatusChange] in onStatusChange?(status) }
    }

    /// `notApprovedToStartSession` means the Info.plist lacks the matching
    /// WKBackgroundModes entry, not that anything went wrong at runtime.
    private static func describe(_ reason: WKExtendedRuntimeSessionInvalidationReason,
                                 error: Error?) -> String {
        switch reason {
        case .none: return "ended"
        case .sessionInProgress: return "one already running"
        case .expired: return "expired"
        case .resignedFrontmost: return "app left the foreground"
        case .suppressedBySystem: return "suppressed by the system"
        case .error:
            let text = error?.localizedDescription ?? "unknown"
            print("ExtendedRuntimeSession: \(text)")
            if text.contains("must be active") { return "refused - app not active" }
            return "error - \(text.prefix(40))"
        @unknown default: return "reason \(reason.rawValue)"
        }
    }
}

extension ExtendedRuntimeSessionManager: WKExtendedRuntimeSessionDelegate {

    func extendedRuntimeSessionDidStart(_ extendedRuntimeSession: WKExtendedRuntimeSession) {
        if let expires = extendedRuntimeSession.expirationDate {
            report(String(format: "runtime up, %.0f min", expires.timeIntervalSinceNow / 60))
        } else {
            report("runtime up")
        }
    }

    func extendedRuntimeSessionWillExpire(_ extendedRuntimeSession: WKExtendedRuntimeSession) {
        report("runtime expiring")
    }

    func extendedRuntimeSession(_ extendedRuntimeSession: WKExtendedRuntimeSession,
                                didInvalidateWith reason: WKExtendedRuntimeSessionInvalidationReason,
                                error: Error?) {
        report("runtime \(Self.describe(reason, error: error))")
        session = nil
    }
}
