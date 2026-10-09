//
//  ContentView.swift
//  SensorStreamer
//

import SwiftUI

struct ContentView: View {
    @State private var session = SessionController.shared

    var body: some View {
        TabView {
            MainPage(session: session)
            SettingsPage(session: session)
        }
        .tabViewStyle(.page)
        .task { session.activate() }
    }
}

struct MainPage: View {
    @Bindable var session: SessionController

    var body: some View {
        ScrollView {
            VStack(spacing: 6) {
                Text(session.statusText)
                    .font(.footnote)
                    .multilineTextAlignment(.center)
                    .frame(maxWidth: .infinity)

                Text(session.socketStatus)
                    .font(.caption2)
                    .foregroundStyle(session.socketStatus == "socket ready" ? .green : .orange)

                if !session.rateStatus.isEmpty {
                    Text(session.rateStatus)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }

                Text(session.audioStatus)
                    .font(.caption2)
                    .multilineTextAlignment(.center)
                    .foregroundStyle(.secondary)

                Text(session.motionStatus)
                    .font(.caption2)
                    .multilineTextAlignment(.center)
                    .foregroundStyle(.secondary)

                Text(session.runtimeStatus)
                    .font(.caption2)
                    .foregroundStyle(session.runtimeStatus.hasPrefix("runtime up") ? .green : .secondary)

                Toggle("Audio", isOn: $session.audioOn)
                    .font(.caption2)
                Toggle("IMU", isOn: $session.imuOn)
                    .font(.caption2)

                Button("Start") { session.start() }
                    .disabled(session.isRunning)
                Button("Stop") { session.stop() }
                    .disabled(!session.isRunning)

                Button("Internet check") { session.checkInternet() }
                    .disabled(!session.isRunning)
                if !session.netStatus.isEmpty {
                    Text(session.netStatus)
                        .font(.caption2)
                        .multilineTextAlignment(.leading)
                        .foregroundStyle(session.netStatus.contains("200") ? Color.green : Color.secondary)
                }
            }
            .padding(.horizontal, 4)
        }
    }
}

struct SettingsPage: View {
    @Bindable var session: SessionController

    var body: some View {
        Form {
            Section("Mac") {
                TextField("IP address", text: $session.host)
                    .textContentType(.URL)
                TextField("Port", value: $session.port, format: .number)
            }
            Section("Audio") {
                Picker("Mic channel", selection: $session.micChannel) {
                    ForEach(0..<3, id: \.self) { Text("\($0)").tag($0) }
                    Text("all").tag(-1)
                }
                Toggle("Measurement mic mode", isOn: $session.measurementMode)
            }
            Section("IMU") {
                Picker("Rate", selection: $session.imuRate) {
                    ForEach([50, 100], id: \.self) { Text("\($0) Hz").tag($0) }
                }
                Toggle("Fused device motion", isOn: $session.fusedMotion)
            }
        }
        .disabled(session.isRunning)
    }
}

#Preview {
    ContentView()
}
