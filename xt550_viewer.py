"""USB screen viewer, specifically tested on Motorola XT550 Android 2.3.6."""
import argparse
import re
import shlex
import socket
import struct
import queue
import threading


class ControlSession:
    """One I/O worker; UI only enqueues validated actions, never shell strings."""
    def __init__(self, client, enable_control=False):
        self.client = client
        self.enable_control = enable_control
        self.ready = False
        self.generation = 0
        self.commands = queue.Queue(maxsize=25)
        self.lock = threading.RLock()

    def validate_identity(self):
        model = self.client.shell('getprop ro.product.model').strip()
        name = self.client.shell('cat /sys/class/input/event3/device/name').strip()
        if model != 'XT550' or name != 'ms-msg20xx':
            raise ValueError('Device or touchscreen identity mismatch')

    def invalidate(self):
        with self.lock:
            self.ready = False
            self.generation += 1
            while True:
                try:
                    self.commands.get_nowait()
                except queue.Empty:
                    break

    def refresh(self):
        with self.lock:
            try:
                self.validate_identity()
                image = self.client.capture()
                self.ready = True
                return image
            except Exception:
                self.invalidate()
                raise

    def enqueue(self, kind, value, generation=None):
        if not self.lock.acquire(blocking=False):
            return False
        try:
            if not self.enable_control or not self.ready:
                return False
            if generation is not None and generation != self.generation:
                return False
            if kind == 'key':
                command = key_command(value)
            elif kind == 'text':
                command = text_command(value)
            elif kind == 'touch':
                command = gesture_command(*value)
            else:
                raise ValueError('Unsupported action')
            try:
                self.commands.put_nowait(command)
                return True
            except queue.Full:
                return False
        finally:
            self.lock.release()

    def execute_next(self):
        with self.lock:
            if not self.enable_control or not self.ready:
                return False
            try:
                command = self.commands.get_nowait()
            except queue.Empty:
                return False
            try:
                self.validate_identity()
                result = self.client.shell(command + '; rc=$?; echo __VIEWER_RC:$rc', root=True)
                if '__VIEWER_RC:0' not in result.splitlines():
                    raise RuntimeError('Device did not confirm input success')
                return True
            except Exception:
                self.invalidate()
                raise


def receive_exact(sock, count):
    result = bytearray()
    while len(result) < count:
        part = sock.recv(count - len(result))
        if not part:
            raise ConnectionError('ADB disconnected')
        result.extend(part)
    return bytes(result)


def adb_service(sock, name):
    payload = name.encode('utf-8')
    sock.sendall(f'{len(payload):04x}'.encode() + payload)
    status = receive_exact(sock, 4)
    if status == b'FAIL':
        size = int(receive_exact(sock, 4), 16)
        if size > 4096:
            raise ValueError('Oversize ADB error')
        receive_exact(sock, size)  # Device errors can contain private information.
        raise ConnectionError('ADB service refused; check the selected USB device')
    if status != b'OKAY':
        raise ConnectionError('Invalid ADB response')


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serial', required=True, help='Exact USB serial; never auto-selected')
    parser.add_argument('--enable-control', action='store_true', help='Explicitly allow root input')
    return parser.parse_args(argv)


FRAME_HEADER = (16, 307200, 320, 480, 11, 5, 0, 5, 5, 6, 0, 0)


def validate_header(header):
    if tuple(header) != FRAME_HEADER:
        raise ValueError('Only XT550 320x480 RGB565 framebuffer is supported')


CAPTURE_PATH = '/data/local/tmp/xt550-viewer.raw'
SCREENCAP_HEADER = (320, 480, 4)  # width, height, PIXEL_FORMAT_RGB_565
CAPTURE_SIZE = 12 + FRAME_HEADER[1]


def raw_to_image(raw):
    """Decode Android 2.3 `screencap` raw output (12-byte header + RGB565)."""
    if len(raw) != CAPTURE_SIZE or struct.unpack('<3I', raw[:12]) != SCREENCAP_HEADER:
        raise ValueError('Only XT550 320x480 RGB565 screencap output is supported')
    return frame_to_image(FRAME_HEADER, raw[12:])


def frame_to_image(header, data):
    from PIL import Image
    validate_header(header)
    if len(data) != FRAME_HEADER[1]:
        raise ValueError('Invalid framebuffer length')
    return Image.frombytes('RGB', (320, 480), data, 'raw', 'BGR;16', 0, 1)


def gesture_command(points, long_press=False):
    if not isinstance(points, (list, tuple)) or not 1 <= len(points) <= 24:
        raise ValueError('Expected 1-24 touch samples')
    events = []
    for point in points:
        if (not isinstance(point, (list, tuple)) or len(point) != 2
                or any(type(v) is not int for v in point)
                or not 0 <= point[0] < 320 or not 0 <= point[1] < 480):
            raise ValueError('Coordinates outside the verified screen')
        x, y = point
        for typ, code, value in [(3, 48, 1), (3, 53, x), (3, 54, y), (0, 2, 0), (0, 0, 0)]:
            events.append(f'sendevent /dev/input/event3 {typ} {code} {value}')
    if long_press:
        events.append('sleep 1')
    for typ, code, value in [(3, 48, 0), (0, 2, 0), (0, 0, 0)]:
        events.append(f'sendevent /dev/input/event3 {typ} {code} {value}')
    # Preserve any failure while still attempting every release event. Keep exit
    # inside a subshell so execute_next's surrounding status marker still runs.
    return '(err=0; ' + '; '.join(event + ' || err=1' for event in events) + '; exit "$err")'


def key_command(code):
    if type(code) is not int or code not in (3, 4, 82, 26, 24, 25, 67, 66, 19, 20, 21, 22, 61):
        raise ValueError('Unsupported hardware key')
    return 'input keyevent ' + str(code)


def text_command(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9 .,!?_@:+/()=-]{1,256}', value):
        raise ValueError('Use 1-256 basic ASCII characters; shell metacharacters are rejected')
    return 'input text ' + shlex.quote(value)


class Viewer:
    def __init__(self, root, session, start_workers=True):
        import tkinter as tk
        from tkinter import ttk
        self.root, self.session = root, session
        self.stop = threading.Event()
        self.events = queue.Queue(maxsize=2)
        self.points, self.last_image, self.photo = [], None, None
        self.gesture_generation = None
        self.status = tk.StringVar(value='Connecting (read-only by default)...')
        self.zoom = tk.DoubleVar(value=1.25)
        self.long_press = tk.BooleanVar(value=False)
        root.title('Motorola XT550 - USB viewer')
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.resizable(False, False)
        body = ttk.Frame(root, padding=10)
        body.pack()
        ttk.Label(body, text='Motorola XT550 / Android 2.3.6').pack(anchor='w')
        bar = ttk.Frame(body)
        bar.pack(fill='x', pady=5)
        self.button_labels = []
        for label, code in [('Home', 3), ('Back', 4), ('Menu', 82), ('Power', 26), ('Vol -', 25), ('Vol +', 24)]:
            self.button_labels.append(label)
            ttk.Button(bar, text=label, width=7, command=lambda c=code: self.send('key', c),
                       state='normal' if session.enable_control else 'disabled').pack(side='left')
        options = ttk.Frame(body)
        options.pack(fill='x', pady=5)
        ttk.Checkbutton(options, text='Long press (1s)', variable=self.long_press).pack(side='left')
        ttk.Label(options, text='Zoom').pack(side='left')
        ttk.Combobox(options, textvariable=self.zoom, values=['1.0', '1.25', '1.5'],
                     width=5, state='readonly').pack(side='left')
        self.zoom.trace_add('write', lambda *_: self.redraw())
        self.canvas = tk.Canvas(body, width=400, height=600, highlightthickness=0,
                                background='#171717', takefocus=True)
        self.canvas.pack(anchor='w')
        for event, callback in [('<ButtonPress-1>', self.press), ('<B1-Motion>', self.drag),
                                ('<ButtonRelease-1>', self.release), ('<KeyPress>', self.key)]:
            self.canvas.bind(event, callback)
        entrybar = ttk.Frame(body)
        entrybar.pack(fill='x', pady=5)
        self.text = ttk.Entry(entrybar, width=34,
                              state='normal' if session.enable_control else 'disabled')
        self.text.pack(side='left', fill='x', expand=True)
        self.text.bind('<Return>', lambda _: self.write_text())
        ttk.Button(entrybar, text='Type ASCII', command=self.write_text,
                   state='normal' if session.enable_control else 'disabled').pack(side='left')
        ttk.Label(body, text='Click: touch / drag-release: swipe / basic ASCII only.\n'
                  'No lock bypass. Use the device normally to unlock.').pack(anchor='w')
        ttk.Label(body, textvariable=self.status, wraplength=400).pack(anchor='w')
        root.after(80, self.poll)
        if start_workers:
            threading.Thread(target=self.frames, daemon=True).start()

    def frames(self):
        import time
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                image = self.session.refresh()
                self.publish(('image', image))
                while not self.stop.is_set() and time.monotonic() - started < 1:
                    self.session.execute_next()
                    self.stop.wait(.05)
            except Exception:
                # Session invalidates synchronously; Tk polling cannot re-enable it.
                self.publish(('status', 'Capture/control unavailable; controls disabled. Check USB and device identity.'))
                self.stop.wait(max(.15, 1 - (time.monotonic() - started)))

    def publish(self, event):
        if self.events.full():
            try:
                self.events.get_nowait()
            except queue.Empty:
                pass
        self.events.put_nowait(event)

    def send(self, kind, value, generation=None):
        try:
            accepted = self.session.enqueue(kind, value, generation)
            self.status.set('Input queued' if accepted else 'Read-only, busy, or not validated: input blocked')
            return accepted
        except ValueError:
            self.status.set('Invalid input: use verified coordinates/keys or basic ASCII without shell characters')
            return False

    def poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == 'image':
                    self.last_image = value
                    self.redraw()
                    self.status.set('USB frame ~1 fps / ' + ('root control enabled' if self.session.enable_control else 'read-only'))
                else:
                    self.status.set(value)
                    self.points = []
        except queue.Empty:
            pass
        if not self.stop.is_set():
            self.root.after(80, self.poll)

    def redraw(self):
        if self.last_image is None:
            return
        from PIL import Image, ImageTk
        scale = self.zoom.get()
        width, height = round(320 * scale), round(480 * scale)
        picture = self.last_image.resize((width, height), Image.Resampling.NEAREST)
        self.photo = ImageTk.PhotoImage(picture)
        self.canvas.configure(width=width, height=height)
        self.canvas.delete('all')
        self.canvas.create_image(0, 0, image=self.photo, anchor='nw')

    def point(self, event):
        return (min(319, max(0, int(event.x / self.zoom.get()))),
                min(479, max(0, int(event.y / self.zoom.get()))))

    def press(self, event):
        self.canvas.focus_set()
        self.gesture_generation = self.session.generation
        self.points = [self.point(event)] if self.session.ready else []

    def drag(self, event):
        point = self.point(event)
        if self.points and point != self.points[-1] and len(self.points) < 1000:
            self.points.append(point)

    def release(self, event):
        if not self.points:
            return
        points, self.points = self.points + [self.point(event)], []
        if len(points) > 20:
            points = [points[round(i * (len(points) - 1) / 19)] for i in range(20)]
        self.send('touch', (points, self.long_press.get()), self.gesture_generation)

    def write_text(self):
        value = self.text.get()
        if value and self.send('text', value):
            self.text.delete(0, 'end')

    def key(self, event):
        codes = {'BackSpace': 67, 'Return': 66, 'Escape': 4, 'Up': 19,
                 'Down': 20, 'Left': 21, 'Right': 22, 'Tab': 61}
        if event.keysym in codes:
            self.send('key', codes[event.keysym])
        elif event.char and 32 <= ord(event.char) <= 126:
            self.send('text', event.char)

    def close(self):
        self.stop.set()
        self.root.destroy()


class ADBClient:
    def __init__(self, serial):
        if not isinstance(serial, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', serial):
            raise ValueError('Invalid explicit device serial')
        self.serial = serial

    def connect(self):
        sock = socket.create_connection(('127.0.0.1', 5037), timeout=12)
        try:
            adb_service(sock, 'host:transport:' + self.serial)
        except Exception:
            sock.close()
            raise
        return sock

    def capture(self):
        # Android 2.3 adbd never reaps the screencap it forks for framebuffer:,
        # leaving one zombie per frame. A shell: child is reaped, and sync:
        # copies the file without starting any process.
        with self.connect() as sock:
            adb_service(sock, 'shell:screencap ' + CAPTURE_PATH)
            output = bytearray()
            while True:
                part = sock.recv(4096)
                if not part:
                    break
                output.extend(part)
                if len(output) > 4096:
                    break
        if output.strip():
            raise ValueError('screencap failed on the device')
        with self.connect() as sock:
            adb_service(sock, 'sync:')
            path = CAPTURE_PATH.encode('utf-8')
            sock.sendall(b'RECV' + struct.pack('<I', len(path)) + path)
            raw = bytearray()
            while True:
                kind = receive_exact(sock, 4)
                size = struct.unpack('<I', receive_exact(sock, 4))[0]
                if kind == b'DONE':
                    break
                if kind == b'FAIL':
                    receive_exact(sock, min(size, 4096))
                    raise ConnectionError('Could not read the capture file')
                if kind != b'DATA' or size > 65536 or len(raw) + size > CAPTURE_SIZE:
                    raise ValueError('Unexpected sync response')
                raw.extend(receive_exact(sock, size))
        return raw_to_image(bytes(raw))

    def shell(self, command, root=False):
        payload = 'su -c ' + shlex.quote(command) if root else command
        with self.connect() as sock:
            adb_service(sock, 'shell:' + payload)
            result = bytearray()
            while True:
                part = sock.recv(65536)
                if not part:
                    break
                result.extend(part)
                if len(result) > 200000:
                    raise ValueError('Oversize shell response')
        return result.decode('utf-8', 'replace').replace('\r', '')


def main(argv=None):
    args = parse_args(argv)
    session = ControlSession(ADBClient(args.serial), args.enable_control)
    import tkinter as tk
    root = tk.Tk()
    Viewer(root, session)
    root.mainloop()


if __name__ == '__main__':
    main()
