#!/usr/bin/env python3
"""Live OpenGL view of the SensorStreamer stream.

From WatchHand's live_plot.py, itself from PoseTouch's gl_plot.py (one GLSL
program draws line strips straight from data coordinates, one textured quad
draws an image panel). Panels, one row each, on one time axis:

    pcm chN          a scrolling spectrogram of each audio channel sent
    acc, gyr, mag    x y z traces (g, rad/s, microtesla)
    motion ...       the fused device motion, split into quaternion,
                     rotation rate, user acceleration and gravity

Every stream is stamped on one clock on the watch, so one offset (the least
delay seen on any of them) places them all on this Mac's timeline: what
happened together on the wrist is drawn together here.

Keys: space pause | [ ] window length | r rescale | s save PNG | esc quit

Used by receive.py --plot; needs pyglet and numpy.
"""

from __future__ import annotations

import ctypes
import threading
import time

import numpy as np
import pyglet
from pyglet.gl import (
    GL_ARRAY_BUFFER, GL_BLEND, GL_CLAMP_TO_EDGE, GL_DYNAMIC_DRAW, GL_FALSE,
    GL_FLOAT, GL_LINEAR, GL_LINES, GL_LINE_STRIP, GL_MULTISAMPLE,
    GL_ONE_MINUS_SRC_ALPHA, GL_R32F, GL_RED, GL_SCISSOR_TEST, GL_SRC_ALPHA,
    GL_TEXTURE0, GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_TEXTURE_MIN_FILTER,
    GL_TEXTURE_WRAP_S, GL_TEXTURE_WRAP_T, GL_TRIANGLE_STRIP, GL_UNPACK_ALIGNMENT,
    GLuint, glActiveTexture, glBindBuffer, glBindTexture, glBindVertexArray,
    glBlendFunc, glBufferData, glBufferSubData, glClearColor, glDisable,
    glDrawArrays, glEnable, glEnableVertexAttribArray, glGenBuffers,
    glGenTextures, glGenVertexArrays, glPixelStorei, glScissor, glTexImage2D,
    glTexParameteri, glVertexAttribPointer, glViewport,
)
from pyglet.graphics.shader import Shader, ShaderProgram
from pyglet.window import key

from . import wire

CAPACITY = 65536            # trace samples per stream: 11 min at 100 Hz
MAX_DRAW_POINTS = 16384
TRACE_WIDTH = 2
MIN_WINDOW, MAX_WINDOW = 0.5, 30.0

# Spectrogram columns kept per channel: 375 col/s at 48 kHz, hop 128, so 8192
# is 22 s.
IMAGE_CAPACITY = 8192
SPEC_FFT = 512
SPEC_HOP = 128
SPEC_DB_FLOOR = -120.0

# Auto-ranging, as gl_plot does it: floor a little above the quietest fifth,
# ceiling just under the loudest, followed slowly.
SPEC_FLOOR_PCT, SPEC_CEIL_PCT = 20.0, 99.5
SPEC_MIN_SPAN = 20.0
AUTO_EVERY = 8
AUTO_RATE = 0.15

# Panel order, top to bottom.
RANK = {"pcm": 0, "acc": 1, "gyr": 2, "mag": 3, "motion": 4}

BG = (0.06, 0.07, 0.09)
PANEL_BG = (0.105, 0.115, 0.140, 1.0)
GRID = (0.20, 0.22, 0.26, 1.0)
ZERO = (0.34, 0.37, 0.43, 1.0)
TEXT = (198, 204, 214, 255)
DIM = (124, 132, 146, 255)
PALETTE = [
    (1.00, 0.42, 0.42), (0.32, 0.81, 0.40), (0.30, 0.67, 0.97),
    (1.00, 0.83, 0.23), (0.80, 0.36, 0.91), (0.13, 0.72, 0.81),
]

PAD_L, PAD_R, PAD_T, PAD_B, GAP = 66, 14, 40, 30, 12
FONT = ("Menlo", "Monaco", "DejaVu Sans Mono", "monospace")

VERT = """#version 330 core
layout(location = 0) in vec2 position;
uniform vec4 domain;   // t0, t1, y0, y1
uniform vec4 rect;     // x, y, w, h in window points
uniform vec2 screen;   // window size in points
uniform vec2 offset;   // nudge in points; see TRACE_WIDTH
void main() {
    float u = (position.x - domain.x) / max(domain.y - domain.x, 1e-9);
    float v = (position.y - domain.z) / max(domain.w - domain.z, 1e-9);
    vec2 p = vec2(rect.x + u * rect.z, rect.y + v * rect.w) + offset;
    gl_Position = vec4(p / screen * 2.0 - 1.0, 0.0, 1.0);
}
"""

FRAG = """#version 330 core
uniform vec4 color;
out vec4 frag_colour;
void main() { frag_colour = color; }
"""

IMAGE_VERT = """#version 330 core
layout(location = 0) in vec2 position;
layout(location = 1) in vec2 texcoord;
uniform vec4 domain;
uniform vec4 rect;
uniform vec2 screen;
out vec2 uv;
void main() {
    float u = (position.x - domain.x) / max(domain.y - domain.x, 1e-9);
    float v = (position.y - domain.z) / max(domain.w - domain.z, 1e-9);
    vec2 p = vec2(rect.x + u * rect.z, rect.y + v * rect.w);
    uv = texcoord;
    gl_Position = vec4(p / screen * 2.0 - 1.0, 0.0, 1.0);
}
"""

# Inferno over [range.x, range.y] dB.
IMAGE_FRAG = """#version 330 core
in vec2 uv;
uniform sampler2D image;
uniform vec2 range;
out vec4 frag_colour;

vec3 inferno(float t) {
    const vec3 c0 = vec3(0.00021894037, 0.0016510046, -0.019480898);
    const vec3 c1 = vec3(0.10651341949, 0.5639564368, 3.9327123889);
    const vec3 c2 = vec3(11.602493082, -3.9728539657, -15.942394106);
    const vec3 c3 = vec3(-41.703996131, 17.436398882, 44.354145199);
    const vec3 c4 = vec3(77.162935699, -33.402358942, -81.807309257);
    const vec3 c5 = vec3(-71.319428245, 32.626064264, 73.209519858);
    const vec3 c6 = vec3(25.131126225, -12.242668952, -23.070325003);
    return c0 + t * (c1 + t * (c2 + t * (c3 + t * (c4 + t * (c5 + t * c6)))));
}

void main() {
    float v = texture(image, uv).r;
    float t = clamp((v - range.x) / max(range.y - range.x, 1e-6), 0.0, 1.0);
    frag_colour = vec4(clamp(inferno(t), 0.0, 1.0), 1.0);
}
"""


class Clock:
    """The watch's clock on this Mac's timeline: the least delay seen on any
    stream (gl_plot's trick), shared by all of them because they share one
    clock on the watch. The stream that arrives fastest sets it, and the
    others are drawn where they belong relative to it."""

    def __init__(self):
        self.offset = None
        self.lock = threading.Lock()

    def note(self, host_time, sample_time):
        """Returns how far behind the least delay this sample arrived."""
        delay = host_time - sample_time
        with self.lock:
            if self.offset is None or delay < self.offset:
                self.offset = delay
            return delay - self.offset

    def reference(self, host_now):
        with self.lock:
            return host_now - (self.offset or 0.0)


class Stream:
    """Lock-guarded ring buffer of one trace stream's channels, timed on the
    watch's clock."""

    def __init__(self, key, field_names, clock):
        self.key = key
        self.field_names = field_names
        self.clock = clock
        self.lag = 0.0
        self.t = np.zeros(CAPACITY, dtype=np.float64)
        self.v = np.zeros((CAPACITY, len(field_names)), dtype=np.float32)
        self.head = 0
        self.count = 0
        self.lock = threading.Lock()
        self.yscale = 1.0

    def push_many(self, host_time, times, values):
        lag = self.clock.note(host_time, float(times[0]))
        with self.lock:
            self.lag = lag if lag > self.lag else self.lag + (lag - self.lag) * 1e-3
            for t, row in zip(times, values):
                i = self.head
                self.t[i] = t
                self.v[i] = row
                self.head = (i + 1) % CAPACITY
                if self.count < CAPACITY:
                    self.count += 1

    def snapshot(self, since):
        with self.lock:
            count, head = self.count, self.head
            if count == 0:
                return None, None
            if count < CAPACITY:
                t = self.t[:count].copy()
                v = self.v[:count].copy()
            else:
                t = np.concatenate((self.t[head:], self.t[:head]))
                v = np.concatenate((self.v[head:], self.v[:head]))
        if count > 1 and np.any(np.diff(t) < 0):
            order = np.argsort(t, kind="stable")
            t, v = t[order], v[order]
        start = int(np.searchsorted(t, since))
        return t[start:], v[start:]


class Spectrogram:
    """STFT of one audio channel, one magnitude column (dB) per hop, kept as
    (rows, IMAGE_CAPACITY) plus a time per column. Gaps in the sample index
    are padded with silence so columns stay on a uniform grid."""

    def __init__(self, key, sample_rate, clock, fft=SPEC_FFT, hop=SPEC_HOP):
        self.key = key
        self.clock = clock
        self.rate = sample_rate
        self.fft = min(fft, 4096)
        self.hop = max(1, min(hop, self.fft))
        self.rows = self.fft // 2 + 1
        self.columns = np.full((self.rows, IMAGE_CAPACITY), SPEC_DB_FLOOR, dtype=np.float32)
        self.col_t = np.zeros(IMAGE_CAPACITY, dtype=np.float64)
        self.head = 0
        self.count = 0
        self.lock = threading.Lock()
        self.lag = 0.0
        self.dropped = 0
        self.field_names = []
        self.range_low = None
        self.range_high = None
        self._auto_tick = 0
        self.window = np.hanning(self.fft).astype(np.float32)
        self._scale = 2.0 / max(np.sum(self.window), 1e-9)
        self._pending = np.zeros(0, dtype=np.float32)
        self._processed = None
        self._next_index = None
        self.base_index = None
        self.base_time = None

    def push(self, host_time, t0, first_index, sample_rate, samples):
        samples = np.asarray(samples, dtype=np.float32) / 32768.0
        if not samples.size:
            return
        restart = (sample_rate != self.rate or
                   (self.base_index is not None and
                    first_index + int(self.rate) < self._next_index))
        if self.base_index is None or restart:
            self.rate = sample_rate
            self.base_index = first_index
            self.base_time = t0
            self._processed = first_index
            self._next_index = first_index
            self._pending = np.zeros(0, dtype=np.float32)
        lag = self.clock.note(host_time, self.time_of(first_index))
        self.lag = lag if lag > self.lag else self.lag + (lag - self.lag) * 1e-3
        if first_index < self._next_index:
            self.dropped += samples.size
            return
        if first_index > self._next_index:
            missing = first_index - self._next_index
            self.dropped += missing
            self._pending = np.concatenate((self._pending, np.zeros(missing, dtype=np.float32)))
        self._pending = np.concatenate((self._pending, samples))
        self._next_index = first_index + samples.size
        self._transform()

    def time_of(self, index):
        return self.base_time + (index - self.base_index) / self.rate

    def _transform(self):
        while self._pending.size >= self.fft:
            frame = self._pending[:self.fft] * self.window
            spectrum = np.abs(np.fft.rfft(frame)) * self._scale
            column = 20.0 * np.log10(spectrum + 1e-10)
            centre = self._processed + self.fft / 2.0
            self._store(column, self.time_of(centre))
            self._pending = self._pending[self.hop:]
            self._processed += self.hop

    def _store(self, column, t):
        with self.lock:
            i = self.head
            self.columns[:, i] = column
            self.col_t[i] = t
            self.head = (i + 1) % IMAGE_CAPACITY
            if self.count < IMAGE_CAPACITY:
                self.count += 1

    def snapshot(self, since):
        with self.lock:
            count, head = self.count, self.head
            if count == 0:
                return None, None
            if count < IMAGE_CAPACITY:
                t = self.col_t[:count].copy()
                v = self.columns[:, :count].copy()
            else:
                t = np.concatenate((self.col_t[head:], self.col_t[:head]))
                v = np.concatenate((self.columns[:, head:], self.columns[:, :head]), axis=1)
        start = int(np.searchsorted(t, since))
        return t[start:], v[:, start:]

    def rescale(self):
        self.range_low = None
        self.range_high = None

    def domain(self):
        return 0.0, self.rate / 2.0

    def ylabels(self):
        nyquist = self.rate / 2.0
        return [(f, f"{nyquist * f / 1000:.1f}k") for f in (0.0, 0.25, 0.5, 0.75, 1.0)]

    def colour_range(self, data):
        self._auto_tick += 1
        if self.range_low is not None and self._auto_tick % AUTO_EVERY:
            return self.range_low, self.range_high
        step = max(1, data.shape[1] // 256)
        sample = data[::2, ::step]
        low = float(np.percentile(sample, SPEC_FLOOR_PCT))
        high = float(np.percentile(sample, SPEC_CEIL_PCT))
        if high - low < SPEC_MIN_SPAN:
            high = low + SPEC_MIN_SPAN
        if self.range_low is None:
            self.range_low, self.range_high = low, high
        else:
            self.range_low += (low - self.range_low) * AUTO_RATE
            self.range_high += (high - self.range_high) * AUTO_RATE
        return self.range_low, self.range_high

    def describe(self, shown):
        return f"{self.fft}pt   {shown[0]:.0f}..{shown[1]:.0f} dB"


def _tick_step(span, target=6):
    raw = span / target
    magnitude = 10.0 ** np.floor(np.log10(max(raw, 1e-6)))
    for mult in (1, 2, 5, 10):
        if raw <= mult * magnitude:
            return mult * magnitude
    return 10 * magnitude


class LivePlot(pyglet.window.Window):
    def __init__(self, window_seconds=5.0, caption="SensorStreamer live",
                 spec_fft=SPEC_FFT, spec_hop=SPEC_HOP, db_range=None, hidden=(),
                 snapshot=None, exit_after=None):
        try:
            config = pyglet.gl.Config(double_buffer=True, major_version=3,
                                      minor_version=3, forward_compatible=True,
                                      sample_buffers=1, samples=4)
            super().__init__(1180, 760, caption=caption, resizable=True, config=config)
        except pyglet.window.NoSuchConfigException:
            super().__init__(1180, 760, caption=caption, resizable=True)

        self.window_seconds = float(np.clip(window_seconds, MIN_WINDOW, MAX_WINDOW))
        self.paused = False
        self.frozen_at = 0.0
        self.clock = Clock()
        self.streams = {}
        self.order = []
        self._streams_lock = threading.Lock()
        self._buffers = {}
        self._labels = {}
        self._time_labels = []
        self._batch = pyglet.graphics.Batch()
        self._laid_out = None
        self.spec_fft = spec_fft
        self.spec_hop = spec_hop
        self.db_range = None if db_range is None else (float(db_range[0]), float(db_range[1]))
        self.hidden = set(hidden)
        self.snapshot_path = snapshot
        self.exit_after = exit_after
        self._started = time.time()
        self._frames_drawn = 0

        self.program = ShaderProgram(Shader(VERT, "vertex"), Shader(FRAG, "fragment"))
        self._scratch = self._make_buffer(4096)
        self.image_program = ShaderProgram(Shader(IMAGE_VERT, "vertex"),
                                           Shader(IMAGE_FRAG, "fragment"))
        self._image_quad = self._make_image_buffer()
        self._textures = {}

        glClearColor(*BG, 1.0)
        glEnable(GL_BLEND)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        glEnable(GL_MULTISAMPLE)

        self._header = pyglet.text.Label("", font_name=FONT, font_size=11, color=TEXT,
                                         batch=self._batch, x=PAD_L, y=0, anchor_y="top")
        self._hint = pyglet.text.Label("space pause · [ ] window · r rescale · s png · esc quit",
                                       font_name=FONT, font_size=10, color=DIM, batch=self._batch,
                                       x=0, y=0, anchor_x="right", anchor_y="top")
        self._waiting = pyglet.text.Label("waiting for datagrams…", font_name=FONT, font_size=14,
                                          color=DIM, x=0, y=0, anchor_x="center", anchor_y="center")
        pyglet.clock.schedule_interval(self._tick, 1 / 60.0)

    def _tick(self, dt):
        if self.exit_after is not None and time.time() - self._started >= self.exit_after:
            if self.snapshot_path and self._frames_drawn:
                self.save_png(self.snapshot_path)
            self.close()

    # -- ingest (receiver thread) -----------------------------------------

    def _is_hidden(self, tag):
        return tag in self.hidden or tag.split(" ")[0] in self.hidden

    def _register(self, stream_key, stream):
        with self._streams_lock:
            self.streams[stream_key] = stream
            self.order = sorted(self.streams,
                                key=lambda k: (k[0], RANK.get(k[1].split(" ")[0], 9), k[1]))
        return stream

    def push_audio(self, device_id, channel, host_time, t0, first_index, sample_rate, samples):
        tag = f"pcm ch{channel}"
        if self._is_hidden(tag):
            return
        stream_key = (device_id, tag)
        stream = self.streams.get(stream_key)
        if stream is None:
            stream = self._register(stream_key, Spectrogram(stream_key, sample_rate, self.clock,
                                                            self.spec_fft, self.spec_hop))
        stream.push(host_time, t0, first_index, sample_rate, samples)

    def push_imu(self, device_id, kind, host_time, times, values):
        if kind == "motion":
            groups = [(tag, values[:, lo:hi], names) for tag, lo, hi, names in wire.MOTION_GROUPS]
        else:
            names = wire.FIELDS.get(kind, [f"v{i}" for i in range(values.shape[1])])
            groups = [(kind, values, names)]
        for tag, v, names in groups:
            if self._is_hidden(tag):
                continue
            stream_key = (device_id, tag)
            stream = self.streams.get(stream_key)
            if stream is None or len(stream.field_names) != v.shape[1]:
                stream = self._register(stream_key, Stream(stream_key, names, self.clock))
            stream.push_many(host_time, times, v)

    # -- GL plumbing ------------------------------------------------------

    def _make_buffer(self, vertices):
        vao, vbo = GLuint(), GLuint()
        glGenVertexArrays(1, ctypes.byref(vao))
        glGenBuffers(1, ctypes.byref(vbo))
        glBindVertexArray(vao)
        glBindBuffer(GL_ARRAY_BUFFER, vbo)
        glBufferData(GL_ARRAY_BUFFER, vertices * 2 * 4, None, GL_DYNAMIC_DRAW)
        glEnableVertexAttribArray(0)
        glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 0, ctypes.c_void_p(0))
        glBindVertexArray(0)
        return vao, vbo

    def _make_image_buffer(self):
        vao, vbo = GLuint(), GLuint()
        glGenVertexArrays(1, ctypes.byref(vao))
        glGenBuffers(1, ctypes.byref(vbo))
        glBindVertexArray(vao)
        glBindBuffer(GL_ARRAY_BUFFER, vbo)
        glBufferData(GL_ARRAY_BUFFER, 4 * 4 * 4, None, GL_DYNAMIC_DRAW)
        glEnableVertexAttribArray(0)
        glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 16, ctypes.c_void_p(0))
        glEnableVertexAttribArray(1)
        glVertexAttribPointer(1, 2, GL_FLOAT, GL_FALSE, 16, ctypes.c_void_p(8))
        glBindVertexArray(0)
        return vao, vbo

    def _texture_for(self, stream_key):
        texture = self._textures.get(stream_key)
        if texture is None:
            texture = GLuint()
            glGenTextures(1, ctypes.byref(texture))
            glBindTexture(GL_TEXTURE_2D, texture)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
            self._textures[stream_key] = texture
        return texture

    def _upload(self, buffer, points):
        vao, vbo = buffer
        data = np.ascontiguousarray(points, dtype=np.float32)
        glBindVertexArray(vao)
        glBindBuffer(GL_ARRAY_BUFFER, vbo)
        glBufferSubData(GL_ARRAY_BUFFER, 0, data.nbytes, data.ctypes.data_as(ctypes.c_void_p))
        return len(data)

    def _draw(self, buffer, points, mode, colour):
        count = self._upload(buffer, points)
        self.program["color"] = colour
        glBindVertexArray(buffer[0])
        glDrawArrays(mode, 0, count)

    def _draw_trace(self, buffer, points, colour, scale):
        count = self._upload(buffer, points)
        self.program["color"] = colour
        glBindVertexArray(buffer[0])
        pixel = 1.0 / max(scale, 1e-9)
        for dx in range(TRACE_WIDTH):
            for dy in range(TRACE_WIDTH):
                self.program["offset"] = (dx * pixel, dy * pixel)
                glDrawArrays(GL_LINE_STRIP, 0, count)
        self.program["offset"] = (0.0, 0.0)

    # -- layout -----------------------------------------------------------

    def _relayout(self, keys):
        for labels in self._labels.values():
            for label in labels.values():
                label.delete()
        self._labels.clear()
        for stream_key in keys:
            stream = self.streams[stream_key]
            labels = {"title": pyglet.text.Label("", font_name=FONT, font_size=11, color=TEXT,
                                                 batch=self._batch, anchor_y="center")}
            for i in range(6):
                labels[f"y{i}"] = pyglet.text.Label("", font_name=FONT, font_size=9, color=DIM,
                                                    batch=self._batch, anchor_x="right",
                                                    anchor_y="center")
            for channel, name in enumerate(stream.field_names):
                colour = PALETTE[channel % len(PALETTE)]
                labels[f"legend{channel}"] = pyglet.text.Label(
                    name, font_name=FONT, font_size=10, batch=self._batch,
                    color=tuple(int(c * 255) for c in colour) + (255,),
                    anchor_x="right", anchor_y="center")
            self._labels[stream_key] = labels
        for label in self._time_labels:
            label.delete()
        self._time_labels = [
            pyglet.text.Label("", font_name=FONT, font_size=10, color=DIM, batch=self._batch,
                              anchor_x="center", anchor_y="top")
            for _ in range(12)
        ]
        self._laid_out = list(keys)

    # -- drawing ----------------------------------------------------------

    def on_draw(self):
        self.clear()
        width, height = self.width, self.height
        framebuffer = self.get_framebuffer_size()
        glViewport(0, 0, *framebuffer)
        scale = framebuffer[0] / max(width, 1)

        with self._streams_lock:
            keys = list(self.order)

        now = self.frozen_at if self.paused else time.time()
        span = self.window_seconds
        self._header.y = height - 10
        self._header.text = (f"{len(keys)} panel(s) · {span:g} s window" + ("  · PAUSED" if self.paused else ""))
        self._hint.x, self._hint.y = width - PAD_R, height - 10

        if not keys:
            self._batch.draw()
            self._waiting.x, self._waiting.y = width // 2, height // 2
            self._waiting.draw()
            return
        if keys != self._laid_out:
            self._relayout(keys)

        panel_h = (height - PAD_T - PAD_B - GAP * (len(keys) - 1)) / len(keys)
        plot_w = max(width - PAD_L - PAD_R, 1.0)
        step = _tick_step(span)
        ticks = [-k * step for k in range(int(span / step) + 1) if k * step <= span]
        reference = self.clock.reference(now)

        self.program.use()
        self.program["screen"] = (float(width), float(height))
        self.program["offset"] = (0.0, 0.0)

        for row, stream_key in enumerate(keys):
            stream = self.streams[stream_key]
            top = height - PAD_T - row * (panel_h + GAP)
            rect = (PAD_L, top - panel_h, plot_w, panel_h)
            if isinstance(stream, Spectrogram):
                self._draw_image(stream, stream_key, rect, top, span, reference, scale)
                self.program.use()
            else:
                self._draw_traces(stream, stream_key, rect, top, span, reference, scale, ticks)

        for i, label in enumerate(self._time_labels):
            if i < len(ticks):
                label.text = f"{ticks[i]:g}s" if ticks[i] else "now"
                label.x = PAD_L + (1 + ticks[i] / span) * plot_w
                label.y = PAD_B - 8
            else:
                label.text = ""
        glBindVertexArray(0)
        self._batch.draw()
        self._frames_drawn += 1

    def _draw_traces(self, stream, stream_key, rect, top, span, reference, scale, ticks):
        times, values = stream.snapshot(reference - span)
        if times is not None and len(times):
            peak = float(np.max(np.abs(values))) if values.size else 0.0
            rate = float(np.count_nonzero(times > reference - 1.0))
        else:
            peak, rate = 0.0, 0.0
        target = max(peak * 1.15, 1e-3)
        if target > stream.yscale:
            stream.yscale = target
        else:
            stream.yscale += (target - stream.yscale) * 0.02
        yscale = stream.yscale

        self.program["rect"] = rect
        self.program["domain"] = (-span, 0.0, -yscale, yscale)
        self._draw(self._scratch, [(-span, -yscale), (0.0, -yscale), (-span, yscale), (0.0, yscale)],
                   GL_TRIANGLE_STRIP, PANEL_BG)
        grid = []
        for level in (-yscale, -yscale / 2, yscale / 2, yscale):
            grid += [(-span, level), (0.0, level)]
        for tick in ticks:
            grid += [(tick, -yscale), (tick, yscale)]
        self._draw(self._scratch, grid, GL_LINES, GRID)
        self._draw(self._scratch, [(-span, 0.0), (0.0, 0.0)], GL_LINES, ZERO)

        if times is not None and len(times) > 1:
            stride = max(1, len(times) // MAX_DRAW_POINTS)
            x = (times[::stride] - reference).astype(np.float32)
            y = values[::stride]
            glEnable(GL_SCISSOR_TEST)
            glScissor(int(rect[0] * scale), int(rect[1] * scale), int(rect[2] * scale), int(rect[3] * scale))
            for channel in range(y.shape[1]):
                buffers = self._buffers.setdefault(stream_key, [])
                while len(buffers) <= channel:
                    buffers.append(self._make_buffer(MAX_DRAW_POINTS))
                self._draw_trace(buffers[channel], np.column_stack((x, y[:, channel])),
                                 PALETTE[channel % len(PALETTE)] + (1.0,), scale)
            glDisable(GL_SCISSOR_TEST)

        labels = self._labels[stream_key]
        device_id, tag = stream_key
        latency = f"   {stream.lag * 1000:4.0f} ms behind" if stream.lag > 0.02 else ""
        labels["title"].text = f"{device_id} · {tag}   {rate:5.0f} Hz{latency}"
        labels["title"].x, labels["title"].y = PAD_L + 8, top - 12
        for i, (name, level) in enumerate((("y0", -yscale), ("y1", 0.0), ("y2", yscale))):
            label = labels[name]
            label.text = f"{level:+.3g}" if level else "0"
            label.x = PAD_L - 8
            label.y = rect[1] + (level / yscale * 0.5 + 0.5) * rect[3]
        for i in range(3, 6):
            labels[f"y{i}"].text = ""
        legend_x = self.width - PAD_R - 8
        for channel in reversed(range(len(stream.field_names))):
            label = labels[f"legend{channel}"]
            label.x, label.y = legend_x, top - 12
            legend_x -= label.content_width + 12

    def _draw_image(self, stream, stream_key, rect, top, span, reference, scale):
        times, columns = stream.snapshot(reference - span)
        low, high = stream.domain()
        domain = (-span, 0.0, low, high)
        per_second = 0.0
        shown = None

        if times is None or len(times) < 2:
            self.program.use()
            self.program["rect"] = rect
            self.program["domain"] = domain
            self._draw(self._scratch, [(-span, low), (0.0, low), (-span, high), (0.0, high)],
                       GL_TRIANGLE_STRIP, PANEL_BG)
        else:
            data = np.ascontiguousarray(columns, dtype=np.float32)
            rows, cols = data.shape
            shown = self.db_range if self.db_range is not None else stream.colour_range(data)

            glActiveTexture(GL_TEXTURE0)
            glBindTexture(GL_TEXTURE_2D, self._texture_for(stream_key))
            glPixelStorei(GL_UNPACK_ALIGNMENT, 1)
            glTexImage2D(GL_TEXTURE_2D, 0, GL_R32F, cols, rows, 0, GL_RED, GL_FLOAT,
                         data.ctypes.data_as(ctypes.c_void_p))

            x0 = float(times[0] - reference)
            x1 = float(times[-1] - reference)
            half = (x1 - x0) / max(cols - 1, 1) / 2.0
            quad = np.array([
                (x0 - half, low, 0.0, 0.0),
                (x1 + half, low, 1.0, 0.0),
                (x0 - half, high, 0.0, 1.0),
                (x1 + half, high, 1.0, 1.0),
            ], dtype=np.float32)
            self.image_program.use()
            self.image_program["screen"] = (float(self.width), float(self.height))
            self.image_program["rect"] = rect
            self.image_program["domain"] = domain
            self.image_program["range"] = shown
            self.image_program["image"] = 0
            vao, vbo = self._image_quad
            glBindVertexArray(vao)
            glBindBuffer(GL_ARRAY_BUFFER, vbo)
            glBufferSubData(GL_ARRAY_BUFFER, 0, quad.nbytes, quad.ctypes.data_as(ctypes.c_void_p))
            glEnable(GL_SCISSOR_TEST)
            glScissor(int(rect[0] * scale), int(rect[1] * scale), int(rect[2] * scale), int(rect[3] * scale))
            glDrawArrays(GL_TRIANGLE_STRIP, 0, 4)
            glDisable(GL_SCISSOR_TEST)
            glBindVertexArray(0)
            per_second = float(np.count_nonzero(times > reference - 1.0))

        labels = self._labels[stream_key]
        device_id, tag = stream_key
        latency = f"   {stream.lag * 1000:4.0f} ms behind" if stream.lag > 0.02 else ""
        lost = f"   {stream.dropped} lost" if stream.dropped else ""
        detail = f"   {stream.describe(shown)}" if shown is not None else ""
        labels["title"].text = f"{device_id} · {tag}   {per_second:5.0f} col/s{detail}{latency}{lost}"
        labels["title"].x, labels["title"].y = PAD_L + 8, top - 12
        ylabels = stream.ylabels()
        for i in range(6):
            label = labels[f"y{i}"]
            if i < len(ylabels):
                frac, text = ylabels[i]
                label.text = text
                label.x = PAD_L - 8
                label.y = rect[1] + frac * rect[3]
            else:
                label.text = ""

    # -- input ------------------------------------------------------------

    def save_png(self, path=None):
        if path is None:
            path = time.strftime("sensorstreamer_%Y-%m-%d_%H-%M-%S.png")
        pyglet.image.get_buffer_manager().get_color_buffer().save(str(path))
        print(f"  saved {path}")

    def on_key_press(self, symbol, modifiers):
        if symbol == key.ESCAPE:
            self.close()
        elif symbol == key.SPACE:
            self.paused = not self.paused
            self.frozen_at = time.time()
        elif symbol in (key.BRACKETLEFT, key.MINUS):
            self.window_seconds = max(MIN_WINDOW, self.window_seconds / 2)
        elif symbol in (key.BRACKETRIGHT, key.EQUAL, key.PLUS):
            self.window_seconds = min(MAX_WINDOW, self.window_seconds * 2)
        elif symbol == key.R:
            for stream in self.streams.values():
                if isinstance(stream, Spectrogram):
                    stream.rescale()
                else:
                    stream.yscale = 1e-3
        elif symbol == key.S:
            self.save_png()
        return pyglet.event.EVENT_HANDLED

    def run(self):
        pyglet.app.run()
