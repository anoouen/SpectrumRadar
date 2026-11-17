"""Spatial Audio Visualizer: decoded PCM playback and 96-band visualization."""
import sys
import threading
from pathlib import Path
import numpy as np
from scipy.fft import rfft, rfftfreq
import soundfile as sf
import sounddevice as sd
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets
from scipy.signal import stft, istft

BANDS = 96
NFFT = 8192

def channel_residual(audio, rate):
    """Удаление общей составляющей с сохранением стороны."""
    result = np.zeros_like(audio, dtype=np.float32)

    # Обрабатываем частями, чтобы не расходовать слишком много памяти.
    block = rate * 10
    overlap = 4096

    for start in range(0, len(audio), block):
        if QtCore.QThread.currentThread().isInterruptionRequested():
            raise InterruptedError()
        end = min(start + block, len(audio))
        a = max(0, start - overlap)
        b = min(len(audio), end + overlap)

        chunk = audio[a:b]
        if len(chunk) < 2048:
            chunk = np.pad(chunk, ((0, 2048 - len(chunk)), (0, 0)))

        spectra = []
        for channel in range(2):
            _, _, spectrum = stft(
                chunk[:, channel],
                fs=rate,
                nperseg=2048,
                noverlap=1536,
                boundary="zeros",
                padded=True,
            )
            spectra.append(spectrum)

        left, right = spectra
        magnitude_l = np.abs(left)
        magnitude_r = np.abs(right)

        # Общая энергия учитывает и совпадение фаз.
        # При одинаковых L и R остаток равен нулю.
        cross = left * np.conj(right)
        agreement = np.clip(
            cross.real / (magnitude_l * magnitude_r + 1e-12),
            0.0,
            1.0,
        )
        common = np.minimum(magnitude_l, magnitude_r) * agreement

        gain_l = np.clip(
            (magnitude_l - common) / (magnitude_l + 1e-12), 0, 1
        )
        gain_r = np.clip(
            (magnitude_r - common) / (magnitude_r + 1e-12), 0, 1
        )

        for channel, spectrum, gain in (
            (0, left, gain_l),
            (1, right, gain_r),
        ):
            _, restored = istft(
                spectrum * gain,
                fs=rate,
                nperseg=2048,
                noverlap=1536,
                boundary=True,
            )
            offset = start - a
            result[start:end, channel] = restored[
                offset:offset + end - start
            ]

    return result

class Loader(QtCore.QThread):
    loaded = QtCore.Signal(object, int, str)
    failed = QtCore.Signal(str)

    def __init__(self, path):
        super().__init__()
        self.path = path

    def run(self):
        try:
            data, rate = sf.read(self.path, dtype='float32', always_2d=True)
            if not len(data):
                raise ValueError('Файл не содержит аудио')
            if data.shape[1] == 1:
                data = np.repeat(data, 2, axis=1)
            data = np.ascontiguousarray(data[:, :2])
            residual = channel_residual(data, rate)
            self.loaded.emit((data, residual), rate, self.path)
        except InterruptedError:
            return
        except Exception as exc:
            self.failed.emit(str(exc))


class Engine:
    def __init__(self):
        self.residual = None
        self.data = None
        self.rate = 48000
        self.pos = 0
        self.playing = False
        self.volume = .7
        self.mono = False
        self.listen_side = False
        self.side_mix = 0.0
        self.stream = None
        self.lock = threading.Lock()

    def callback(self, out, frames, timing, status):
        out.fill(0)
        with self.lock:
            if not self.playing or self.data is None:
                return
            end = min(self.pos + frames, len(self.data))
            count = end - self.pos
            out[:count] = self.data[self.pos:end] * self.volume
            if self.mono:
                out[:count] = out[:count].mean(axis=1, keepdims=True)
            # Short crossfade avoids clicks when toggling the monitor.
            target = float(self.listen_side)
            if count and (target or self.side_mix):
                step = 1.0 / max(1, int(self.rate * .02))
                direction = 1 if target > self.side_mix else -1
                mix = np.clip(self.side_mix + direction*step*np.arange(1,count+1),0,1)
                if target == self.side_mix:
                    mix.fill(target)
                
                residual = self.residual
                if residual is not None:
                    difference = residual[self.pos:end] * self.volume
                    out[:count] *= 1 - mix[:, None]
                    out[:count] += difference * mix[:, None]

                self.side_mix = float(mix[-1])

            self.pos = end
            if end == len(self.data):
                self.playing = False

    def close(self):
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None

    def install(self, data, rate):
        self.close()
        self.data, self.rate, self.pos = data, rate, 0
        self.playing = False
        self.stream = sd.OutputStream(samplerate=rate, channels=2, dtype='float32',
                                      callback=self.callback, blocksize=1024)
        self.stream.start()

    def position(self):
        with self.lock:
            pos = self.pos
            if self.playing and self.stream:
                pos -= int(self.stream.latency * self.rate)
            return max(0, pos)

    def seek(self, sample):
        with self.lock:
            self.pos = max(0, min(int(sample), len(self.data)))


class SpatialMap(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.setMinimumSize(420, 420)
        self.level = np.zeros(BANDS)
        self.pan = np.zeros(BANDS)
        self.depth = np.zeros(BANDS)
        self.experimental = False
        self.colors = [QtGui.QColor.fromHsvF(.72 * (1-i/(BANDS-1)), .85, 1) for i in range(BANDS)]

    def advance(self, level, pan, depth):
        self.level += (level-self.level) * np.where(level > self.level, .45, .10)
        self.pan += (pan-self.pan) * .18
        self.depth += (depth-self.depth) * .12
        self.update()

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QtGui.QColor('#0a101c'))
        cx, cy = self.width()/2, self.height()/2
        radius = min(cx, cy)-54
        p.setPen(QtGui.QPen(QtGui.QColor('#24344b'), 1))
        for fraction in (.33, .66, 1):
            p.drawEllipse(QtCore.QPointF(cx, cy), radius*fraction, radius*fraction)
        p.drawLine(QtCore.QPointF(cx-radius, cy), QtCore.QPointF(cx+radius, cy))
        p.drawLine(QtCore.QPointF(cx, cy-radius), QtCore.QPointF(cx, cy+radius))
        p.setPen(QtGui.QColor('#9baec8'))
        for text, x, y in [('FRONT', cx-26, cy-radius-18), ('BACK', cx-20, cy+radius+30),
                           ('LEFT', cx-radius-42, cy+5), ('RIGHT', cx+radius+5, cy+5)]:
            p.drawText(QtCore.QPointF(x, y), text)
        for i, energy in enumerate(self.level):
            if energy < .015:
                continue
            x = cx + self.pan[i]*radius*.88
            # Vertical coordinate is a frequency lane, NOT measured elevation/distance.
            y = cy - (i/(BANDS-1)-.5)*radius*.7
            if self.experimental:
                y = cy - self.depth[i]*radius*.65
            size = 5+energy*23
            color = QtGui.QColor(self.colors[i])
            gradient = QtGui.QRadialGradient(QtCore.QPointF(x, y), size*2.5)
            color.setAlphaF(min(.85, energy*.85))
            gradient.setColorAt(0, color)
            color.setAlpha(0)
            gradient.setColorAt(1, color)
            p.setPen(QtCore.Qt.PenStyle.NoPen)
            p.setBrush(gradient)
            p.drawEllipse(QtCore.QPointF(x, y), size*2.5, size*2.5)
            if self.experimental:
                # Mirror explicitly displays the unresolved front/back ambiguity.
                p.setOpacity(.35)
                p.drawEllipse(QtCore.QPointF(x, 2*cy-y), size*2.5, size*2.5)
                p.setOpacity(1)
        p.setPen(QtGui.QPen(QtGui.QColor('#dbe9fa'), 2))
        p.setBrush(QtGui.QColor('#182438'))
        p.drawEllipse(QtCore.QPointF(cx, cy), 13, 17)
        p.drawLine(QtCore.QPointF(cx-5, cy-16), QtCore.QPointF(cx, cy-23))
        p.drawLine(QtCore.QPointF(cx, cy-23), QtCore.QPointF(cx+5, cy-16))
        p.setPen(QtGui.QColor('#92a4bd'))
        p.drawText(15, self.height()-16, '20 Hz   •   violet → blue → green → red   •   20 kHz')


class SeekSlider(QtWidgets.QSlider):
    """Player timeline: the entire track accepts clicks and dragging."""
    seekRequested = QtCore.Signal()

    def __init__(self):
        super().__init__(QtCore.Qt.Orientation.Horizontal)
        self.setObjectName('audioTimeline')
        self.setMinimumHeight(34)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.setToolTip('Нажмите в любом месте дорожки или перетащите для перемотки')
        self.setStyleSheet('QSlider#audioTimeline::groove:horizontal { height:8px; background:#34445e; border-radius:4px; } '
                           'QSlider#audioTimeline::sub-page:horizontal { background:#65baff; border-radius:4px; } '
                           'QSlider#audioTimeline::handle:horizontal { width:14px; margin:-4px 0; background:#9bd3ff; border-radius:7px; }')

    def position_value(self, x):
        option = QtWidgets.QStyleOptionSlider()
        self.initStyleOption(option)
        style = self.style()
        groove = style.subControlRect(QtWidgets.QStyle.ComplexControl.CC_Slider, option,
                                      QtWidgets.QStyle.SubControl.SC_SliderGroove, self)
        handle = style.subControlRect(QtWidgets.QStyle.ComplexControl.CC_Slider, option,
                                      QtWidgets.QStyle.SubControl.SC_SliderHandle, self)
        span = max(1, groove.width()-handle.width())
        position = round(x-groove.x()-handle.width()/2)
        return QtWidgets.QStyle.sliderValueFromPosition(self.minimum(), self.maximum(),
                                                        max(0, min(span, position)), span, option.upsideDown)

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.setFocus()
            self.setSliderDown(True)
            self.setValue(self.position_value(event.position().x()))
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.isSliderDown():
            self.setValue(self.position_value(event.position().x()))
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton and self.isSliderDown():
            self.setValue(self.position_value(event.position().x()))
            self.setSliderDown(False)
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        previous = self.value()
        super().keyPressEvent(event)
        if self.value() != previous:
            self.seekRequested.emit()


class DropAudioButton(QtWidgets.QPushButton):
    fileDropped = QtCore.Signal(str)
    def __init__(self, text):
        super().__init__(text)
        self.setAcceptDrops(True)
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and any(u.isLocalFile() and Path(u.toLocalFile()).suffix.lower() in ('.wav', '.mp3', '.flac') for u in event.mimeData().urls()):
            event.acceptProposedAction()
    def dropEvent(self, event):
        for url in event.mimeData().urls():
            if url.isLocalFile() and Path(url.toLocalFile()).suffix.lower() in ('.wav', '.mp3', '.flac'):
                self.fileDropped.emit(url.toLocalFile())
                event.acceptProposedAction()
                break


class Window(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('Spatial Audio Visualizer — v1.1')
        self.resize(1400, 850)
        self.engine = Engine()
        self.loader = None
        self.last_pos = -1
        self.history = np.full((2, BANDS, 360), -90., dtype=np.float32)
        self.window = np.hanning(NFFT).astype(np.float32)
        root = QtWidgets.QWidget()
        self.setCentralWidget(root)
        layout = QtWidgets.QVBoxLayout(root)
        controls = QtWidgets.QHBoxLayout()
        layout.addLayout(controls)
        self.open_button = DropAudioButton('Открыть / перетащить аудио')
        self.open_button.fileDropped.connect(self.open_path)
        self.open_button.clicked.connect(self.open_file)
        controls.addWidget(self.open_button)
        self.play_button = QtWidgets.QPushButton('▶ Воспроизведение')
        self.play_button.clicked.connect(self.play)
        controls.addWidget(self.play_button)
        pause = QtWidgets.QPushButton('Ⅱ Пауза')
        pause.clicked.connect(lambda: setattr(self.engine, 'playing', False))
        controls.addWidget(pause)
        stop = QtWidgets.QPushButton('■ Стоп')
        stop.clicked.connect(self.stop)
        controls.addWidget(stop)
        controls.addStretch()
        self.mode = QtWidgets.QComboBox()
        self.mode.addItems(['Stereo', 'Experimental 360°'])
        self.mode.currentIndexChanged.connect(self.change_mode)
        controls.addWidget(self.mode)
        controls.addWidget(QtWidgets.QLabel('Громкость'))
        volume = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        volume.setRange(0, 100)
        volume.setValue(70)
        volume.setMaximumWidth(150)
        volume.valueChanged.connect(lambda v: setattr(self.engine, 'volume', v/100))
        controls.addWidget(volume)
        self.title = QtWidgets.QLabel('Откройте WAV, MP3 или FLAC')
        layout.addWidget(self.title)
        splitter = QtWidgets.QSplitter()
        layout.addWidget(splitter, 1)
        plots = QtWidgets.QWidget()
        pl = QtWidgets.QVBoxLayout(plots)
        pl.setContentsMargins(0, 0, 0, 0)
        self.images = []
        lut = pg.colormap.get('inferno').getLookupTable()
        for channel in ('Левый канал', 'Правый канал'):
            plot = pg.PlotWidget(title=channel)
            plot.setLabel('left', 'Частота', units='Hz')
            plot.setLabel('bottom', 'Время относительно воспроизведения', units='s')
            plot.getAxis('left').setTicks([[(0, '20'), (32, '200'), (64, '2k'), (95, '20k')]])
            plot.setMouseEnabled(x=False, y=False)
            plot.hideButtons()
            image = pg.ImageItem(axisOrder='row-major')
            image.setImage(self.history[len(self.images)], autoLevels=False)
            image.setLookupTable(lut)
            image.setLevels([-85, -15])
            image.setRect(QtCore.QRectF(-12, 0, 12, BANDS))
            plot.addItem(image)
            plot.setRange(xRange=(-12, 0), yRange=(0, BANDS), padding=0)
            self.images.append(image)
            pl.addWidget(plot)
        splitter.addWidget(plots)
        self.spatial = SpatialMap()
        splitter.addWidget(self.spatial)
        splitter.setSizes([650, 700])
        self.note = QtWidgets.QLabel()
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        seekrow = QtWidgets.QHBoxLayout()
        layout.addLayout(seekrow)
        self.seekbar = SeekSlider()
        self.seekbar.setRange(0, 10000)
        self.seekbar.sliderReleased.connect(self.seek)
        self.seekbar.seekRequested.connect(self.seek)
        seekrow.addWidget(self.seekbar, 1)
        self.time = QtWidgets.QLabel('0:00 / 0:00')
        seekrow.addWidget(self.time)
        self.change_mode(0)
        self.space_shortcut = QtGui.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key.Key_Space), self)
        self.space_shortcut.setContext(QtCore.Qt.ShortcutContext.WindowShortcut)
        self.space_shortcut.setAutoRepeat(False)
        self.space_shortcut.activated.connect(self.toggle_playback)
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(33)
        self.make_bands()

    def make_bands(self):
        freq = rfftfreq(NFFT, 1/self.engine.rate)
        self.edges = np.geomspace(20, min(20000, self.engine.rate/2), BANDS+1)
        self.indices = [np.flatnonzero((freq >= a) & (freq < b)) for a, b in zip(self.edges[:-1], self.edges[1:])]
        self.indices = [idx if len(idx) else np.array([np.argmin(abs(freq-np.sqrt(a*b)))])
                        for idx, a, b in zip(self.indices, self.edges[:-1], self.edges[1:])]

    def change_mode(self, index):
        self.spatial.experimental = bool(index)
        self.note.setText('360°: условная карта по разнице уровней, фазе и межканальной задержке. '
                          'В Current зеркальные облака показывают неопределённость FRONT/BACK; это не достоверная локализация.' if index else
                          'Stereo: горизонталь — баланс L/R; вертикаль и цвет — частота. '
                          'Переднее/заднее направление и расстояние не определяются.')

    def open_file(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, 'Открыть аудио', '', 'Аудио (*.wav *.mp3 *.flac);;Все файлы (*)')
        if not path:
            return
        self.open_path(path)

    def open_path(self, path):
        if self.loader and self.loader.isRunning():
            return
        self.engine.playing = False
        self.open_button.setEnabled(False)
        self.processing.show()
        self.title.setText('Предварительная обработка…')
        self.loader = Loader(path)
        self.loader.loaded.connect(self.loaded)
        self.loader.failed.connect(self.failed)
        self.loader.finished.connect(lambda: self.open_button.setEnabled(True))
        self.loader.start()

    def loaded(self, data, rate, path):
        try:
            self.engine.install(data, rate)
            self.make_bands()
            self.clear()
            self.title.setText(f'{Path(path).name}  •  {rate} Hz  •  96 полос')
            return True
        except Exception as exc:
            self.failed(str(exc))
            return False

    def failed(self, error):
        self.title.setText('Не удалось открыть аудио')
        QtWidgets.QMessageBox.warning(self, 'Ошибка аудио', error)

    def toggle_playback(self):
        if self.engine.playing:
            self.engine.playing = False
        else:
            self.play()

    def play(self):
        if self.engine.data is not None and self.engine.stream:
            if self.engine.pos >= len(self.engine.data):
                self.engine.seek(0)
                self.clear()
            self.engine.playing = True

    def stop(self):
        self.engine.playing = False
        if self.engine.data is not None:
            self.engine.seek(0)
        self.clear()

    def clear(self):
        self.history.fill(-90)
        self.last_pos = -1
        self.column = self.engine.position() // max(1, round(self.engine.rate/30)) - 1
        self.spatial.level.fill(0)
        for i, image in enumerate(self.images):
            image.setImage(self.history[i], autoLevels=False)
        self.spatial.update()

    def seek(self):
        if self.engine.data is not None:
            self.engine.seek(self.seekbar.value()/10000 * len(self.engine.data))
            self.clear()

    @staticmethod
    def clock(seconds):
        return f'{int(seconds)//60}:{int(seconds)%60:02d}'

    def tick(self):
        e = self.engine
        if e.data is None:
            return
        pos = e.position()
        self.time.setText(f'{self.clock(pos/e.rate)} / {self.clock(len(e.data)/e.rate)}')
        if not self.seekbar.isSliderDown():
            self.seekbar.setValue(round(pos/len(e.data)*10000))
        if pos == self.last_pos:
            return
        self.last_pos = pos
        # Use a trailing PCM window at the estimated audible output position.
        frame = np.zeros((NFFT, 2), dtype=np.float32)
        chunk = e.data[max(0, pos-NFFT):pos]
        if len(chunk):
            frame[-len(chunk):] = chunk
        if e.mono:
            frame[:] = frame.mean(axis=1, keepdims=True)
        spectrum = rfft(frame*self.window[:, None], axis=0) / (NFFT/2)
        power = abs(spectrum)**2
        band = np.array([power[idx].sum(axis=0) for idx in self.indices])
        db = 10*np.log10(band+1e-12)
        pan = (band[:, 1]-band[:, 0])/(band.sum(axis=1)+1e-12)
        level = np.clip((10*np.log10(band.sum(axis=1)+1e-12)+75)/60, 0, 1)
        depth = np.zeros(BANDS)
        if self.spatial.experimental:
            cross = spectrum[:, 0]*np.conj(spectrum[:, 1])
            for i, idx in enumerate(self.indices):
                coherence = abs(cross[idx].sum())/(np.sqrt(power[idx, 0].sum()*power[idx, 1].sum())+1e-12)
                phase = np.angle(cross[idx].sum())
                delay = np.clip(phase/(2*np.pi*np.sqrt(self.edges[i]*self.edges[i+1])*.00065), -1, 1)
                pan[i] = .8*pan[i]+.2*delay
                depth[i] = np.sqrt(max(0, 1-pan[i]**2))*(.25+.65*coherence)
        self.spatial.advance(level, pan, depth)
        # Columns are sampled on the playback clock, independent of timer jitter.
        step = max(1, round(e.rate/30))
        column = pos//step
        previous = getattr(self, 'column', column-1)
        count = min(360, max(0, column-previous))
        self.column = column
        if count:
            self.history = np.roll(self.history, -count, axis=2)
            self.history[:, :, -count:] = db.T[:, :, None]
            for i, image in enumerate(self.images):
                image.setImage(self.history[i], autoLevels=False)

    def closeEvent(self, event):
        self.timer.stop()
        self.engine.close()
        if self.loader and self.loader.isRunning():
            self.loader.wait()
        event.accept()


class Preview(QtCore.QThread):
    ready = QtCore.Signal(object)

    def __init__(self, data, rate):
        super().__init__()
        self.data, self.rate = data, rate

    def run(self):
        freq = rfftfreq(4096, 1/self.rate)
        edges = np.geomspace(20, min(20000, self.rate/2), BANDS+1)
        bins = [np.where((freq >= a) & (freq < b))[0] for a,b in zip(edges[:-1],edges[1:])]
        bins = [b if len(b) else np.array([np.argmin(abs(freq-np.sqrt(edges[i]*edges[i+1])))]) for i,b in enumerate(bins)]
        hop = round(self.rate/30)
        result = np.zeros(((len(self.data)+hop-1)//hop, BANDS, 6), np.float32)
        window = np.hanning(4096)[:,None]
        for k in range(len(result)):
            if self.isInterruptionRequested():
                return
            center = k*hop
            frame = np.zeros((4096,2), np.float32)
            a,b = max(0,center-2048), min(len(self.data),center+2048)
            frame[a-center+2048:b-center+2048] = self.data[a:b]
            fft = rfft(frame*window,axis=0)/2048
            channels = np.column_stack((abs(fft)**2, abs((fft[:,0]+fft[:,1])/2)**2, abs((fft[:,0]-fft[:,1])/2)**2))
            result[k, :, :4] = [channels[idx].sum(axis=0) for idx in bins]
            # Same experimental level/phase/delay estimate as the live FFT path.
            cross = fft[:, 0] * np.conj(fft[:, 1])
            for i, idx in enumerate(bins):
                left, right = result[k, i, :2]
                cross_sum = cross[idx].sum()
                coherence = abs(cross_sum)/(np.sqrt(left*right)+1e-12)
                delay = np.clip(np.angle(cross_sum)/(2*np.pi*np.sqrt(edges[i]*edges[i+1])*.00065), -1, 1)
                pan = .8*(right-left)/(left+right+1e-12)+.2*delay
                result[k, i, 4] = pan
                result[k, i, 5] = np.sqrt(max(0, 1-pan**2))*(.25+.65*coherence)
        self.ready.emit(result)


def smooth_directions(angles, weights, percent, radius=None):
    """Symmetric circular mean; changes direction only, never event time."""
    if percent <= 0 or len(angles) < 2:
        return angles.copy()
    vectors = np.exp(1j*angles)*weights
    if percent >= 100 and radius is None:
        total = vectors.sum(axis=0, keepdims=True)
        mean = np.where(abs(total)>1e-10, np.angle(total), angles[:1])
        return np.broadcast_to(mean, angles.shape).copy()
    if radius is None:
        radius = max(1, round((len(angles)-1)*.5*(percent/100)**1.5))
    cumulative = np.concatenate((np.zeros_like(vectors[:1]), np.cumsum(vectors,axis=0)))
    indices = np.arange(len(angles))
    sums = cumulative[np.minimum(len(angles),indices+radius+1)]-cumulative[np.maximum(0,indices-radius)]
    return np.where(abs(sums)>1e-10,np.angle(sums),angles)


class EnhancedMap(SpatialMap):
    def future_trajectory(self, start, end):
        owner = self.owner
        percent = owner.smoothing.value() if owner.trails.currentIndex() else 0
        # Each audio event has a fixed symmetric neighbourhood, independent of
        # the moving viewport. Otherwise its angle drifts at the viewport edges.
        samples = round((owner.horizon.value()/10+.15)*30)
        padding = max(1, round((samples-1)*.5*(percent/100)**1.5)) if percent else 0
        first = max(0, start-padding)
        last = min(len(owner.features), end+padding)
        cache_key = (id(owner.features), owner.engine.mono, owner.group.currentIndex(), owner.mode.currentIndex())
        if getattr(self, '_component_key', None) != cache_key:
            self._component_key = cache_key
            self._component_cache = {}
        cache = self._component_cache
        # Retain only the sliding preview neighbourhood, not the whole file.
        for old in list(cache):
            if old < first or old >= last:
                del cache[old]
        trajectory = []
        for k in range(first, last):
            if k in cache:
                trajectory.append(cache[k])
                continue
            bands = owner.features[k]
            if owner.engine.mono:
                bands = owner.mono_bands(bands)
            cache[k] = owner.components(bands, k)
            trajectory.append(cache[k])
        if not trajectory:
            return [], np.empty((0, 0))
        angles = np.array([item[1] for item in trajectory], dtype=np.float64)*np.pi/2
        if percent:
            weights = np.array([item[0] for item in trajectory])
            angles = smooth_directions(angles, np.where(weights >= .12, weights, 0),
                                       percent, radius=padding)
        selection = slice(start-first, end-first)
        return trajectory[selection], angles[selection]

    def future_geometry(self):
        # Fit the active upper hemisphere, retaining the full arrival ring below.
        radius = max(1., min(self.width()/2-54, (self.height()-70)/1.24))
        return self.width()/2, 42+radius, radius, max(35., radius*.24)

    def paint_future_grid(self, p, cx, cy, radius, arrival_radius):
        p.fillRect(self.rect(), QtGui.QColor('#0a101c'))
        p.setPen(QtGui.QPen(QtGui.QColor('#24344b'), 1))
        p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        for fraction in (.33, .66, 1):
            r = radius*fraction
            p.drawArc(QtCore.QRectF(cx-r, cy-r, 2*r, 2*r), 0, 180*16)
        p.drawLine(QtCore.QPointF(cx-radius, cy), QtCore.QPointF(cx+radius, cy))
        p.drawLine(QtCore.QPointF(cx, cy-radius), QtCore.QPointF(cx, cy))
        p.setPen(QtGui.QColor('#9baec8'))
        for text, x, y in [('FRONT', cx-26, cy-radius-17),
                           ('LEFT', cx-radius-42, cy+5), ('RIGHT', cx+radius+5, cy+5)]:
            p.drawText(QtCore.QPointF(x, y), text)
        p.setPen(QtGui.QPen(QtGui.QColor('#8ba9ce'), 2))
        p.drawEllipse(QtCore.QPointF(cx, cy), arrival_radius, arrival_radius)
        p.setPen(QtGui.QPen(QtGui.QColor('#dbe9fa'), 2))
        p.setBrush(QtGui.QColor('#182438'))
        p.drawEllipse(QtCore.QPointF(cx, cy), 13, 17)
        p.drawLine(QtCore.QPointF(cx-5, cy-16), QtCore.QPointF(cx, cy-23))
        p.drawLine(QtCore.QPointF(cx, cy-23), QtCore.QPointF(cx+5, cy-16))
        p.setPen(QtGui.QColor('#92a4bd'))
        p.drawText(15, self.height()-10, '20 Hz   •   violet → blue → green → red   •   20 kHz')

    def paintEvent(self, event):
        owner = self.owner
        if owner.future.currentIndex() == 0:
            super().paintEvent(event)
            return
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        cx, cy, radius, arrival_radius = self.future_geometry()
        self.paint_future_grid(p, cx, cy, radius, arrival_radius)
        if owner.features is None:
            return
        now = owner.engine.position()/owner.engine.rate
        horizon = owner.horizon.value()/10
        start,end = max(0,int((now-.15)*30)),min(len(owner.features),int((now+horizon)*30)+1)
        previous = {}
        trajectory, angles = self.future_trajectory(start, end)
        if not trajectory:
            return
        if owner.group.currentIndex() == 0:
            # Batch segments into paths instead of thousands of pen/brush changes.
            levels = np.array([item[0] for item in trajectory])
            times = (np.arange(start,end)/30-now)/horizon
            radii = arrival_radius+np.maximum(0,times)*(radius-arrival_radius)
            xs = cx+np.sin(angles)*radii[:,None]
            ys = cy-np.cos(angles)*radii[:,None]
            alpha = np.clip(levels*np.where(times>=0,1,1+times*horizon/.15)[:,None],0,1)
            buckets = np.minimum(3,(alpha*4).astype(int))
            for j in range(BANDS):
                paths = [QtGui.QPainterPath() for _ in range(4)]
                for t in range(len(times)):
                    if levels[t,j] < .12 or alpha[t,j] <= 0:
                        continue
                    path = paths[buckets[t,j]]
                    x,y = float(xs[t,j]),float(ys[t,j])
                    if owner.trails.currentIndex():
                        if t and levels[t-1,j] >= .12:
                            path.moveTo(float(xs[t-1,j]),float(ys[t-1,j]))
                            path.lineTo(x,y)
                    elif (start+t)%3 == 0:
                        size = 2+float(levels[t,j])*5
                        path.addEllipse(QtCore.QPointF(x,y),size,size)
                for bucket,path in enumerate(paths):
                    if path.isEmpty():
                        continue
                    color = QtGui.QColor.fromHsvF(.72*(1-j/95),.85,1)
                    strength = (bucket+.5)/4
                    color.setAlphaF(strength)
                    p.setPen(QtGui.QPen(color,owner.line_width.value()/10 if owner.trails.currentIndex() else 2+strength*4))
                    p.setBrush(QtCore.Qt.BrushStyle.NoBrush if owner.trails.currentIndex() else QtGui.QBrush(color))
                    p.drawPath(path)
            return
        for offset,(level,pan,indices) in enumerate(trajectory):
            k = start+offset
            distance = (k/30-now)/horizon
            for j,(energy,balance,index) in enumerate(zip(level,pan,indices)):
                if energy < .12:
                    continue
                angle = angles[offset,j]
                r = arrival_radius+max(0,distance)*(radius-arrival_radius)
                point = QtCore.QPointF(cx+np.sin(angle)*r,cy-np.cos(angle)*r)
                color = QtGui.QColor(self.colors[int(index)])
                color.setAlphaF(float(np.clip(energy*(1 if distance>=0 else 1+distance*horizon/.15),0,1)))
                p.setPen(QtGui.QPen(color,owner.line_width.value()/10 if owner.trails.currentIndex() else 2+energy*4))
                p.setBrush(color)
                if owner.trails.currentIndex() and j in previous and previous[j][1] == k-1:
                    p.drawLine(previous[j][0],point)
                elif not owner.trails.currentIndex() and k%3 == 0:
                    p.drawEllipse(point,2+energy*5,2+energy*5)
                previous[j] = (point,k)


class EnhancedWindow(Window):
    def __init__(self):
        super().__init__()
        self.features = None
        self.analysis = None
        self.retired_analyses = []
        self.closing = False
        self.render_key = None
        self.map_key = None
        self.last_pos = -1
        self.settings = QtCore.QSettings('SpatialAudioVisualizer', 'SpatialAudioVisualizer')
        layout = self.centralWidget().layout()
        player_row = layout.takeAt(0).layout()
        player_widgets = []
        while player_row.count():
            item = player_row.takeAt(0)
            if item.widget():
                player_widgets.append(item.widget())
        player_row.deleteLater()
        self.title.setWordWrap(True)
        layout.removeWidget(self.title)
        self.statusBar().addWidget(self.title, 1)
        self.statusBar().setSizeGripEnabled(True)

        def group_box(title):
            box = QtWidgets.QGroupBox(title)
            grid = QtWidgets.QGridLayout(box)
            grid.setContentsMargins(10, 8, 10, 8)
            grid.setHorizontalSpacing(10)
            grid.setVerticalSpacing(6)
            layout.insertWidget(layout.count()-3, box)
            return box, grid

        player_box, player = group_box('Плеер')
        for col, widget in enumerate(w for w in player_widgets if w is not self.mode):
            player.addWidget(widget, 0, col)
        player.setColumnStretch(6, 1)

        def combo(items, tooltip):
            widget = QtWidgets.QComboBox()
            widget.addItems(items)
            widget.setToolTip(tooltip)
            return widget

        analysis_box, analysis = group_box('Анализ')
        self.group = combo(['Frequency', 'Source'], 'Frequency — частотные полосы; Source — приблизительные компоненты по балансу и динамике энергии.')
        self.output = combo(['Stereo', 'Mono'], 'Режим прослушивания: исходное Stereo или сведение в Mono.')
        self.output.currentIndexChanged.connect(lambda i: setattr(self.engine, 'mono', bool(i)))
        self.mode.setToolTip('Stereo — баланс L/R. Experimental 360° — условная оценка по уровням, фазе и задержке; физические направления не определяются точно.')
        for col, (text, widget) in enumerate((('Группировка', self.group), ('Пространственный анализ', self.mode), ('Прослушивание', self.output))):
            analysis.addWidget(QtWidgets.QLabel(text), 0, col*2)
            analysis.addWidget(widget, 0, col*2+1)
        self.side_monitor = QtWidgets.QCheckBox('Слушать различия каналов')
        self.side_monitor.setToolTip('Подавляет общие составляющие левого и правого каналов, позволяя услышать различающиеся звуковые компоненты')
        self.side_monitor.toggled.connect(self.toggle_side_monitor)
        analysis.addWidget(self.side_monitor, 1, 0, 1, 6)

        visual_box, visual = group_box('Визуализация')
        self.future = combo(['Current', 'Future'], 'Current — текущий звук; Future — предварительно проанализированные события. Радиус Future означает время до звука, а не расстояние.')
        self.trails = combo(['Particles', 'Trails'], 'Particles — частицы; Trails — цветные траектории Future.')
        self.diffmode = combo(['L/R Balance', 'Mid/Side'], 'Цвет Difference Waterfall: синий — L или Mid, оранжевый — R или Side.')
        self.show_difference = QtWidgets.QCheckBox('Difference Waterfall')
        self.show_difference.setChecked(True)
        show = self.show_difference
        for col, (text, widget) in enumerate((('Время', self.future), ('Отображение', self.trails), ('Различия', self.diffmode))):
            visual.addWidget(QtWidgets.QLabel(text), 0, col*2)
            visual.addWidget(widget, 0, col*2+1)
        visual.addWidget(show, 1, 4, 1, 2)

        def slider(column, text, minimum, maximum, default, formatter):
            label = QtWidgets.QLabel()
            widget = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
            widget.setRange(minimum, maximum)
            widget.setSingleStep(1)
            widget.setValue(default)
            def update(value):
                label.setText(text + ': ' + formatter(value))
            widget.valueChanged.connect(update)
            update(default)
            visual.addWidget(label, 1, column, 1, 2)
            visual.addWidget(widget, 2, column, 1, 2)
            return widget, label

        self.horizon, self.horizon_label = slider(0, 'Глубина предпросмотра', 5, 50, 20, lambda v: f'{v/10:.1f} с')
        self.smoothing, self.smoothing_label = slider(2, 'Сглаживание линий', 0, 100, 50, lambda v: f'{v}%')
        self.line_width, self.width_label = slider(4, 'Толщина линий', 5, 40, 12, lambda v: f'{v/10:.1f} px')
        visual.removeWidget(show)
        visual.addWidget(show, 3, 0, 1, 6)
        # Keep the controls compact and scrollable on narrow windows.
        controls = QtWidgets.QWidget()
        controls_layout = QtWidgets.QVBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(6)
        for box in (player_box, analysis_box, visual_box):
            layout.removeWidget(box)
            controls_layout.addWidget(box)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        scroll.setWidget(controls)
        scroll.setMinimumHeight(160)
        scroll.setMaximumHeight(controls.sizeHint().height()+24)
        layout.insertWidget(0, scroll)
        splitter = self.spatial.parentWidget()
        old = self.spatial
        self.spatial = EnhancedMap()
        self.spatial.owner = self
        splitter.replaceWidget(1,self.spatial)
        old.deleteLater()
        plot = pg.PlotWidget(title='Difference — синий: L / Mid • оранжевый: R / Side')
        plot.setMouseEnabled(x=False,y=False)
        plot.setLabel('bottom','Время',units='s')
        plot.getAxis('left').setTicks([[(0,'20'),(32,'200'),(64,'2k'),(95,'20k')]])
        left_layout = splitter.widget(0).layout()
        self.plot_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        self.plot_splitter.setChildrenCollapsible(False)
        self.plot_splitter.setHandleWidth(7)
        self.plot_splitter.setStyleSheet('QSplitter::handle { background:#34445e; }')
        while left_layout.count():
            widget = left_layout.takeAt(0).widget()
            if widget:
                widget.setMinimumHeight(120)
                self.plot_splitter.addWidget(widget)
        plot.setMinimumHeight(120)
        self.plot_splitter.addWidget(plot)
        for i in range(3):
            self.plot_splitter.setStretchFactor(i,1)
        left_layout.addWidget(self.plot_splitter)
        self.plot_splitter.setSizes([300,300,300])
        self.diffimage = pg.ImageItem(axisOrder='row-major')
        plot.addItem(self.diffimage)
        plot.setRange(xRange=(-12,0),yRange=(0,96),padding=0)
        def toggle_difference(visible):
            if not visible:
                self.visible_plot_sizes = self.plot_splitter.sizes()
            plot.setVisible(visible)
            if visible and hasattr(self, 'visible_plot_sizes'):
                self.plot_splitter.setSizes(self.visible_plot_sizes)
            self.invalidate()
        show.toggled.connect(toggle_difference)
        self.processing = QtWidgets.QLabel('⏳ Предварительная обработка…')
        self.processing.setStyleSheet('background:#72511a; color:#fff1b8; padding:8px; font-weight:bold; font-size:15px; border-radius:5px;')
        player.addWidget(self.processing, 0, 7)
        self.processing.hide()
        self.drag_origin = None
        for i in range(3):
            graph = self.plot_splitter.widget(i)
            graph.setRange(xRange=(-6, 6), yRange=(0, BANDS), padding=0)
            graph.setLabel('bottom', 'Прошлое ← Сейчас → Будущее', units='s')
            graph.addItem(pg.InfiniteLine(pos=0, angle=90, movable=False, pen=pg.mkPen('#ffffff', width=2)))
            graph.viewport().installEventFilter(self)
            graph.viewport().setCursor(QtCore.Qt.CursorShape.OpenHandCursor)
            graph.setToolTip('Потяните водопад влево или вправо для перемотки')
        self.main_splitter = splitter
        self.diffplot = plot
        for widget in (self.group, self.future, self.trails, self.output, self.mode, self.diffmode):
            widget.currentIndexChanged.connect(self.invalidate)
        for widget in (self.horizon, self.smoothing, self.line_width):
            widget.valueChanged.connect(self.invalidate)
        self.restore_settings()
        self.change_mode(self.mode.currentIndex())
        self.invalidate()

    def invalidate(self, *_):
        if self.sender() is None:
            self.render_key = None
        self.map_key = None
        self.last_pos = -1
        trails = bool(self.trails.currentIndex() and self.future.currentIndex())
        for widget in (self.line_width, self.width_label, self.smoothing, self.smoothing_label):
            widget.setEnabled(trails)
        self.horizon.setEnabled(bool(self.future.currentIndex()))
        self.diffmode.setEnabled(self.show_difference.isChecked())
        self.spatial.update()

    def restore_settings(self):
        for key, widget, default in (('width', self.line_width, 12), ('smoothing', self.smoothing, 50), ('horizon', self.horizon, 20), ('future', self.future, 0), ('trails', self.trails, 0), ('group', self.group, 0), ('mode', self.mode, 0), ('diffmode', self.diffmode, 0)):
            try:
                value = int(self.settings.value(key, default))
            except (ValueError, TypeError):
                value = default
            if isinstance(widget, QtWidgets.QSlider):
                widget.setValue(value)
            else:
                widget.setCurrentIndex(max(0, min(widget.count()-1, value)))
        self.show_difference.setChecked(self.settings.value('difference', True, type=bool))
        sizes = self.settings.value('visiblePlotSizes')
        if sizes is not None:
            try:
                sizes = [int(value) for value in sizes]
                if len(sizes) == 3 and all(value > 0 for value in sizes):
                    self.visible_plot_sizes = sizes
            except (TypeError, ValueError):
                pass
        for key, widget in (('geometry', self), ('mainSplitter', self.main_splitter), ('plotSplitter', self.plot_splitter)):
            value = self.settings.value(key)
            if value is not None:
                if widget is self:
                    self.restoreGeometry(value)
                else:
                    widget.restoreState(value)

    def save_settings(self):
        for key, widget in (('width', self.line_width), ('smoothing', self.smoothing), ('horizon', self.horizon), ('future', self.future), ('trails', self.trails), ('group', self.group), ('mode', self.mode), ('diffmode', self.diffmode)):
            self.settings.setValue(key, widget.value() if isinstance(widget, QtWidgets.QSlider) else widget.currentIndex())
        self.settings.setValue('difference', self.show_difference.isChecked())
        self.settings.setValue('geometry', self.saveGeometry())
        self.settings.setValue('mainSplitter', self.main_splitter.saveState())
        self.settings.setValue('plotSplitter', self.plot_splitter.saveState())
        sizes = self.plot_splitter.sizes() if self.show_difference.isChecked() else getattr(self, 'visible_plot_sizes', [300, 300, 300])
        self.settings.setValue('visiblePlotSizes', sizes)
        self.settings.sync()

    def change_mode(self, index):
        super().change_mode(index)
        self.note.setText(self.note.text() + ' Future: радиус — время до звука; траектории показывают баланс/межканальные различия, не физические направления. Source — приблизительные компоненты.')
        self.spatial.update()

    @staticmethod
    def mono_bands(bands):
        bands = bands.copy()
        bands[..., :2] = bands[..., 2, None]
        bands[..., 3:5] = 0
        bands[..., 5] = .9
        return bands

    def toggle_side_monitor(self, enabled):
        if enabled:
            self.previous_output = self.output.currentIndex()
            self.output.setCurrentIndex(0)
        self.engine.listen_side = enabled
        self.output.setEnabled(not enabled)
        if not enabled:
            self.output.setCurrentIndex(getattr(self,'previous_output',0))

    def loaded(self, data, rate, path):
        if self.closing:
            return
        data, residual = data

        if self.analysis and self.analysis.isRunning():
            self.analysis.requestInterruption()
            self.analysis.ready.disconnect(self.accept_features)
            self.retired_analyses.append(self.analysis)

        self.retired_analyses = [worker for worker in self.retired_analyses if worker.isRunning()]
        self.features = None
        if not super().loaded(data, rate, path):
            return
        self.engine.residual = residual

        self.analysis = Preview(data, rate)
        self.analysis.ready.connect(self.accept_features)
        self.analysis.finished.connect(self.release_retired)
        self.title.setText(self.title.text()+' • Предварительный анализ…')
        self.invalidate()
        self.analysis.start()

    def failed(self, error):
        self.processing.hide()
        if not self.closing:
            super().failed(error)

    def eventFilter(self, watched, event):
        if event.type() == QtCore.QEvent.Type.MouseButtonPress and event.button() == QtCore.Qt.MouseButton.LeftButton and self.engine.data is not None:
            graph = next((self.plot_splitter.widget(i) for i in range(3) if self.plot_splitter.widget(i).viewport() is watched), None)
            if graph is not None:
                self.drag_origin = (event.position().x(), self.engine.position(), self.engine.playing, graph.getViewBox().sceneBoundingRect().width())
                self.engine.playing = False
                return True
        if self.drag_origin is not None and event.type() in (QtCore.QEvent.Type.MouseMove, QtCore.QEvent.Type.MouseButtonRelease):
            x, sample, playing, width = self.drag_origin
            self.engine.seek(sample-(event.position().x()-x)/max(1, width)*12*self.engine.rate)
            self.render_key = None
            self.map_key = None
            self.tick()
            if event.type() == QtCore.QEvent.Type.MouseButtonRelease:
                self.drag_origin = None
                self.engine.playing = playing
            return True
        return super().eventFilter(watched, event)

    def release_retired(self):
        self.retired_analyses = [worker for worker in self.retired_analyses if worker.isRunning()]

    def clear(self):
        super().clear()
        self.render_key = None
        self.map_key = None
        if hasattr(self, 'diffimage'):
            self.diffimage.setImage(np.zeros((BANDS, 360, 3), np.uint8), autoLevels=False)

    def accept_features(self,features):
        if self.sender() is not self.analysis or self.closing:
            return
        self.features = features
        self.processing.hide()
        self.title.setText(self.title.text().replace('Предварительный анализ…', 'Future готов'))
        self.invalidate()

    def components(self,bands,k):
        energy = bands[:,:2].sum(axis=1)
        pan = (bands[:,1]-bands[:,0])/(energy+1e-12)
        if self.mode.currentIndex():
            pan = bands[:, 4]
        if not self.group.currentIndex():
            return np.clip((10*np.log10(energy+1e-12)+75)/60,0,1),pan,np.arange(BANDS)
        # Stable slots combine stereo position with rising/falling energy dynamics.
        past = self.features[max(0,k-4),:,:2].sum(axis=1)
        rising = energy > past*1.2
        slots = np.clip(((pan+1)*4).astype(int),0,7)*2+rising
        # Accumulate the 96 bands into 16 slots in compiled NumPy loops.
        totals = np.bincount(slots, weights=energy, minlength=16)
        pans = np.bincount(slots, weights=pan*energy, minlength=16)/(totals+1e-12)
        colors = np.bincount(slots, weights=np.arange(BANDS)*energy, minlength=16)/(totals+1e-12)
        levels = np.clip((10*np.log10(totals+1e-12)+75)/60, 0, 1)
        return levels, pans, colors

    def tick(self):
        if self.features is None:
            super().tick()
            return
        e = self.engine
        pos = e.position()
        k = min(len(self.features)-1,int(pos/e.rate*30))
        self.time.setText(f'{self.clock(pos/e.rate)} / {self.clock(len(e.data)/e.rate)}')
        if not self.seekbar.isSliderDown():
            self.seekbar.setValue(round(pos/len(e.data)*10000))
        key = (k, e.mono, self.diffmode.currentIndex(), self.show_difference.isChecked())
        map_key = (pos, e.mono, self.group.currentIndex(), self.mode.currentIndex(), self.future.currentIndex())
        if self.render_key == key and self.map_key == map_key and not e.playing:
            return
        first = k-180
        lo, hi = max(0, first), min(len(self.features), first+360)
        block = self.features[lo:hi]
        self.waterfall_offset = lo-first
        current_bands = self.features[k]
        if e.mono:
            current_bands = self.mono_bands(current_bands)
        if e.mono:
            block = self.mono_bands(block)
        if self.render_key != key:
            self.render_waterfalls(block)
            self.render_key = key
        levels,pans,colors = self.components(current_bands,k)
        target = np.zeros(96)
        positions = np.zeros(96)
        for i,(level,pan,color) in enumerate(zip(levels,pans,colors)):
            target[i],positions[i] = level,pan
            self.spatial.colors[i] = QtGui.QColor.fromHsvF(.72*(1-color/95),.85,1)
        depths = np.zeros(BANDS)
        if self.mode.currentIndex():
            if not self.group.currentIndex():
                depths[:] = current_bands[:, 5]
            else:
                bands = current_bands
                energy = bands[:, :2].sum(axis=1)
                past = self.features[max(0, k-4), :, :2].sum(axis=1)
                slots = np.clip(((bands[:, 4]+1)*4).astype(int), 0, 7)*2+(energy > past*1.2)
                for slot in range(len(levels)):
                    mask = slots == slot
                    depths[slot] = (bands[mask, 5]*energy[mask]).sum()/(energy[mask].sum()+1e-12)
        if e.playing:
            self.spatial.advance(target, positions, depths)
        elif self.map_key != map_key:
            self.spatial.level[:] = target
            self.spatial.pan[:] = positions
            self.spatial.depth[:] = depths
        self.last_pos = pos
        self.map_key = map_key
        self.spatial.update()

    def render_waterfalls(self, block):
        for i,image in enumerate(self.images):
            arr = np.full((96,360),-90,np.float32)
            arr[:,self.waterfall_offset:self.waterfall_offset+len(block)] = (10*np.log10(block[:,:,i]+1e-12)).T
            image.setImage(arr,autoLevels=False)
            image.setRect(QtCore.QRectF(-6,0,12,BANDS))
        if not self.show_difference.isChecked():
            return
        a,b = (0,1) if not self.diffmode.currentIndex() else (2,3)
        power = block[:,:,a]+block[:,:,b]
        balance = (block[:,:,b]-block[:,:,a])/(power+1e-12)
        brightness = np.clip((10*np.log10(power+1e-12)+80)/65,0,1)*abs(balance)
        rgb = np.zeros((96,360,3),np.uint8)
        colors = np.where(balance[:,:,None]>0,np.array([255,100,30]),np.array([40,130,255]))
        rgb[:,self.waterfall_offset:self.waterfall_offset+len(block)] = (colors*brightness[:,:,None]).transpose(1,0,2).astype(np.uint8)
        self.diffimage.setImage(rgb,autoLevels=False)
        self.diffimage.setRect(QtCore.QRectF(-6,0,12,96))

    def closeEvent(self, event):
        self.closing = True
        self.timer.stop()
        self.engine.playing = False
        workers = [w for w in [self.loader, self.analysis, *self.retired_analyses] if w and w.isRunning()]
        for worker in workers:
            worker.requestInterruption()
        if workers:
            event.ignore()
            self.setEnabled(False)
            self.title.setText('Завершение фоновых задач…')
            QtCore.QTimer.singleShot(50, self.close)
            return
        self.save_settings()
        self.engine.close()
        event.accept()



def main():
    app = QtWidgets.QApplication(sys.argv)
    app.setFont(QtGui.QFont('Segoe UI', 10))
    app.setStyle('Fusion')
    palette = app.palette()
    palette.setColor(QtGui.QPalette.ColorRole.Base, QtGui.QColor('#344b68'))
    palette.setColor(QtGui.QPalette.ColorRole.Text, QtGui.QColor('#ffffff'))
    palette.setColor(QtGui.QPalette.ColorRole.Highlight, QtGui.QColor('#00bfff'))
    palette.setColor(QtGui.QPalette.ColorRole.HighlightedText, QtGui.QColor('#ffffff'))
    app.setPalette(palette)
    app.setStyleSheet('QWidget { background:#111a29; color:#dce6f5; font-size:13px; } '
                      'QPushButton,QComboBox { background:#24344b; padding:8px; border-radius:5px; } '
                      'QPushButton:hover { background:#344e70; } QSlider::groove:horizontal '
                      '{ height:5px; background:#34445e; } QSlider::handle:horizontal '
                      '{ width:13px; margin:-5px 0; background:#65baff; border-radius:6px; }')
    app.setStyleSheet(app.styleSheet() + """
        QCheckBox {
            color: #ffffff;
            spacing: 8px;
            padding: 4px;
        }
        QCheckBox::indicator {
            width: 20px;
            height: 20px;
            border: 2px solid #a8bdd8;
            border-radius: 4px;
            background-color: #26364d;
        }
        QCheckBox::indicator:checked {
            background-color: #00bfff;
            border-color: #80e5ff;
        }
        QCheckBox::indicator:unchecked:hover {
            background-color: #405875;
            border-color: #ffffff;
        }
        QCheckBox::indicator:checked:hover {
            background-color: #45d4ff;
            border-color: #ffffff;
        }
    """)
    pg.setConfigOptions(antialias=False, background='#0a101c', foreground='#9baec8')
    win = EnhancedWindow()
    if win.settings.contains('geometry'):
        win.show()
    else:
        win.showMaximized()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
