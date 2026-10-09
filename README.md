# Sensor Streamer watchOS

A minimal, hackable sensor streamer for Apple Watch: the watchOS sibling of
[SensorStreamerWearOS](https://github.com/VimalMollyn/SensorStreamerWearOS).
The watch streams its raw microphone audio and IMU over UDP to a computer on
the same network; the Mac side receives, records, and draws the streams
together on one time axis.

Built on the watchOS audio/UDP method worked out in PoseTouch and WatchHand
(see "How the watch is kept talking" below). Built for and installed on an
Apple Watch Ultra; the Mac side is verified against `scripts/fake_watch.py`.

## What it streams

| stream | what | rate | per datagram |
|---|---|---|---|
| `pcm` | Int16 microphone audio, one tap channel or all three interleaved, unprocessed (`.measurement` mode) | 48 kHz | 480 samples (10 ms) for one channel, 224 for three |
| `acc` | raw accelerometer x y z, g | 100 Hz (or 50) | 5 records (50 ms) |
| `gyr` | raw gyroscope x y z, rad/s | same | same |
| `mag` | raw magnetometer x y z, µT, only if the watch exposes one | same | same |
| `motion` | fused device motion (off by default): attitude quaternion w x y z, bias-corrected rotation rate, user acceleration, gravity | same | same |
| `text` | events (engine, socket, runtime) and a heartbeat every 2 s | | |

Everything is stamped with the watch's wall clock, and audio and motion are
converted from their monotonic clocks with anchors taken at the same instant,
so the Mac can lay them on one axis without guessing. One audio channel costs
about 100 kB/s; all three about 300 kB/s.

## Layout

    .envrc, pyproject.toml          direnv + uv Python environment (`sensorstreamer` package)
    sensorstreamer/wire.py          the datagram format
    sensorstreamer/receive.py       UDP receiver: stats, recording, --plot
    sensorstreamer/live_plot.py     pyglet/OpenGL viewer: spectrogram + IMU traces
    scripts/fake_watch.py           sends a synthetic stream, for testing without a watch
    scripts/build_watch.sh          xcodegen + xcodebuild + devicectl install/launch
    SensorStreamerWatch/project.yml xcodegen spec; the .xcodeproj is generated, not kept
    SensorStreamerWatch/SensorStreamer/   the watch app (standalone, no iPhone app)
        Wire.swift                  the datagram format, the clock, the packers
        AudioCapture.swift          microphone tap at 48 kHz, the TN3135 session dance
        MotionCapture.swift         CoreMotion at a fixed rate
        SessionController.swift     runs a session, owns settings, keeps the socket alive
        SocketClient.swift          UDP to the Mac
        ExtendedRuntimeSessionManager.swift   keeps running with the wrist down
        InternetCheck.swift         TCP, UDP and HTTPS probes past the LAN
        ContentView.swift           two pages: status/Start/Stop, settings

## Setup

    brew install xcodegen         # once
    direnv allow                  # creates ~/.venvs/SensorStreamerWatchOS via layout_uv
    uv sync

## Build and install the watch app

    scripts/build_watch.sh        # generate project, build, install, launch

The watch must show as available in `xcrun devicectl list devices` (paired
phone nearby, same network). Or open the generated project in Xcode and run on
the watch:

    cd SensorStreamerWatch && xcodegen generate && open SensorStreamer.xcodeproj

Signing uses the team in `project.yml` (automatic). Bundle id
`com.figlab.sensorstreamer`. Change both for your own account. The app icon
is the SensorStreamerWearOS icon, flattened onto white for watchOS.

## Run

On the Mac:

    sensorstreamer-receive --plot                  # live view
    sensorstreamer-receive --plot --out recordings # ...and record
    sensorstreamer-receive --echo                  # print every IMU record instead

It prints this Mac's IP addresses. On the watch, swipe to the settings page,
type that IP (port 5005), pick the microphone channel (0, 1, 2 or all) and
the IMU rate, swipe back, and press **Start**. The Audio and IMU toggles on
the main page can be flipped while running. The main page shows the socket
state, packets and kB per second, and engine, motion and runtime events.

**Internet check.** A few seconds after Start, and whenever the button is
pressed, the watch reaches past the LAN three ways and reports each on
screen and to the Mac (as `net:` events): an HTTP GET over a raw TCP
connection to `captive.apple.com:80`, a DNS query over raw UDP to
`1.1.1.1:53`, and a URLSession HTTPS fetch. Each line gives the round trip in
ms and the interface the raw connection used (`wifi` is the watch's own
radio; URLSession may be routed through the paired iPhone). There is no ICMP
ping for a watchOS app; this is the equivalent. The raw connections need the
live session, like the stream itself.

The viewer draws one panel per stream: a scrolling spectrogram per audio
channel, then traces for `acc`, `gyr`, `mag` and the four parts of `motion`.
Keys: `space` pause, `[` `]` window length, `r` rescale, `s` save PNG, `esc`.
`--hide 'motion gravity,mag'` leaves panels out; `--db -90,-20` pins the
spectrogram's range; `--window`, `--fft`, `--hop` as in gl_plot.

Recordings go in a timestamped folder: `audio.wav` (Int16, the channels as
sent; a lost datagram is a hole of silence of the right length), `audio.json`
(rate, channels, the wall time of sample 0), `acc.csv`, `gyr.csv`, ...
(`t` in watch wall-clock seconds, then the values), and `events.csv`.

To try the Mac side without a watch:

    uv run python scripts/fake_watch.py            # then sensorstreamer-receive --plot

## The wire

Binary UDP, little-endian, a 24-byte header then the payload
(`Wire.swift` and `wire.py` are the reference):

    0   4   magic   "SSW1"
    4   1   kind    0 text, 1 pcm, 2 acc, 3 gyr, 4 mag, 5 motion
    5   1   ch      values per sample: pcm channels, or floats per IMU record
    6   2   count   samples (pcm), records (IMU) or bytes (text) that follow
    8   4   index   pcm: index of the first sample since start; IMU: of the
                    first record; text: a sequence number. A gap is a loss.
    12  4   rate    Float32, nominal samples per second
    16  8   t0      Float64, the watch's wall clock (unix s) at the first sample
    24  ... payload

`pcm`: `count × ch` Int16 interleaved, sample i at `t0 + i / rate`. IMU:
`count` records of Float32 `[t - t0, v0, v1, ...]`, since CoreMotion's
timestamps jitter and are worth keeping. Text: UTF-8, `hb ...` is the
heartbeat.

## How the watch is kept talking

Things learned in PoseTouch and WatchHand that this app depends on:

* watchOS only lets an app open a UDP socket under TN3135's audio exemption,
  and only when the session was activated with `activate(options:)`, not
  `setActive(true)`. The grant lapses ~36 s after each activation
  (FB24377808); the session is re-activated every 28 s and whenever
  `NWPathMonitor` sees the path drop, after which the socket is reopened.
* Because of that, the audio capture runs even with the Audio toggle off: an
  IMU-only stream still needs a live audio session. The category is
  `.playAndRecord`, which is what the exemption was seen to work with.
* The engine does not restart itself after a configuration change or an
  interruption; both are observed. A `mindfulness` extended runtime session
  keeps the app alive with the wrist down (an hour).
* `.measurement` mode asks for unprocessed input (no AGC, no noise
  suppression, no echo cancellation). The settings page has a switch to
  `.default`.

## Not done

* 100 Hz is CoreMotion's ceiling on the watch without a workout session;
  `CMBatchedSensorManager` (watchOS 10, Ultra/Series 8+) gives 800 Hz
  accelerometer and 200 Hz device motion but needs an `HKWorkoutSession`.
* The magnetometer is streamed only if `CMMotionManager` says it is
  available; on the Ultra it may not be.
* Audio is sent at the hardware rate (48 kHz); decimate on the Mac if 16 kHz
  is what a model wants.
* Bonjour discovery of the Mac instead of a typed IP; recording on the watch.
