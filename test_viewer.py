"""Offline tests: no Tk window, ADB connection, or real device."""
import contextlib
import importlib
import io
from pathlib import Path
import subprocess
import unittest


class CLITests(unittest.TestCase):
    def test_serial_is_mandatory_and_control_is_opt_in(self):
        path = Path(__file__).with_name('xt550_viewer.py')
        self.assertTrue(path.exists(), 'Portable viewer implementation is missing')
        m = importlib.import_module('xt550_viewer')
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                m.parse_args([])
        args = m.parse_args(['--serial', 'TEST_DEVICE'])
        self.assertEqual(args.serial, 'TEST_DEVICE')
        self.assertFalse(args.enable_control)
        self.assertTrue(m.parse_args(['--serial', 'TEST_DEVICE', '--enable-control']).enable_control)
        for serial in ['', 'TEST_DEVICE; reboot', 'any device', '*']:
            with self.subTest(serial=serial), self.assertRaises(ValueError):
                m.ADBClient(serial)


    def test_entrypoint_builds_only_explicit_readonly_session(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch
        m = importlib.import_module('xt550_viewer')
        self.assertTrue(hasattr(m, 'main'), 'Application entrypoint missing')
        root = MagicMock()
        with patch.dict('sys.modules', {'tkinter': SimpleNamespace(Tk=lambda: root)}), patch.object(m, 'Viewer') as viewer:
            m.main(['--serial', 'TEST_DEVICE'])
        session = viewer.call_args.args[1]
        self.assertEqual(session.client.serial, 'TEST_DEVICE')
        self.assertFalse(session.enable_control)
        root.mainloop.assert_called_once()


class FrameTests(unittest.TestCase):
    def test_only_exact_verified_rgb565_frames_decode(self):
        import struct
        m = importlib.import_module('xt550_viewer')
        self.assertTrue(hasattr(m, 'frame_to_image'), 'Frame decoder missing')
        header = (16, 307200, 320, 480, 11, 5, 0, 5, 5, 6, 0, 0)
        data = struct.pack('<3H', 0xf800, 0x07e0, 0x001f) + bytes(307194)
        image = m.frame_to_image(header, data)
        self.assertEqual(image.size, (320, 480))
        self.assertEqual([image.getpixel((x, 0)) for x in range(3)],
                         [(255, 0, 0), (0, 255, 0), (0, 0, 255)])
        for bad in [(), header[:-1], (32,) + header[1:],
                    (16, 4000000000, 320, 480) + header[4:],
                    (16, 307200, 480, 320) + header[4:],
                    header[:4] + (0,) + header[5:],
                    header[:-1] + (8,)]:
            with self.subTest(header=bad), self.assertRaises(ValueError):
                m.frame_to_image(bad, data)
        for bad_data in [b'', data[:-1], data + b'x']:
            with self.assertRaises(ValueError):
                m.frame_to_image(header, bad_data)


class FakeSocket:
    def __init__(self, incoming):
        self.incoming = bytearray(incoming)
        self.sent = []
        self.closed = False
    def recv(self, count):
        part = bytes(self.incoming[:min(count, 4096)])
        del self.incoming[:len(part)]
        return part
    def sendall(self, data):
        self.sent.append(data)
    def close(self):
        self.closed = True
    def __enter__(self):
        return self
    def __exit__(self, *args):
        self.close()


class TransportTests(unittest.TestCase):
    def test_capture_uses_reaped_screencap_and_sync_pull(self):
        """framebuffer: on Android 2.3 adbd leaves one zombie screencap per frame."""
        import struct
        from unittest.mock import patch
        m = importlib.import_module('xt550_viewer')
        client = m.ADBClient('TEST_DEVICE')
        raw = struct.pack('<3I', 320, 480, 4) + struct.pack('<3H', 0xf800, 0x07e0, 0x001f) + bytes(307194)
        def sync_reply(payload, chunk=65536):
            out = b'OKAY'
            for i in range(0, len(payload), chunk):
                part = payload[i:i + chunk]
                out += b'DATA' + struct.pack('<I', len(part)) + part
            return out + b'DONE' + struct.pack('<I', 0)
        shell = FakeSocket(b'OKAYOKAY')
        sync = FakeSocket(b'OKAY' + sync_reply(raw))
        with patch('socket.create_connection', side_effect=[shell, sync]) as connect:
            image = client.capture()
        self.assertEqual(image.size, (320, 480))
        self.assertEqual([image.getpixel((x, 0)) for x in range(3)], [(255, 0, 0), (0, 255, 0), (0, 0, 255)])
        self.assertEqual(connect.call_count, 2)
        path = m.CAPTURE_PATH.encode()
        self.assertEqual(shell.sent, [b'001ahost:transport:TEST_DEVICE',
                                      ('%04x' % len(b'shell:screencap ' + path)).encode() + b'shell:screencap ' + path])
        self.assertEqual(sync.sent[:2], [b'001ahost:transport:TEST_DEVICE', b'0005sync:'])
        self.assertEqual(sync.sent[2], b'RECV' + struct.pack('<I', len(path)) + path)
        self.assertNotIn(b'000cframebuffer:', shell.sent + sync.sent)
        self.assertTrue(shell.closed and sync.closed)
        bad_raws = [struct.pack('<3I', 480, 320, 4) + bytes(307200), struct.pack('<3I', 320, 480, 1) + bytes(307200),
                    raw[:-1], raw + b'x', b'']
        for bad in bad_raws:
            with self.subTest(size=len(bad)), self.assertRaises(ValueError):
                with patch('socket.create_connection', side_effect=[FakeSocket(b'OKAYOKAY'), FakeSocket(b'OKAY' + sync_reply(bad))]):
                    client.capture()
        oversize = b'OKAYOKAY' + b'DATA' + struct.pack('<I', 400000)
        failures = [(b'OKAYOKAYscreencap: error', b'OKAY' + sync_reply(raw)),
                    (b'OKAYOKAY', b'OKAYOKAYFAIL' + struct.pack('<I', 4) + b'nope'),
                    (b'OKAYOKAY', oversize), (b'OKAYOKAY', b'OKAYOKAYDATA'), (b'FAIL0004nope', b'')]
        for shell_in, sync_in in failures:
            socks = [FakeSocket(shell_in), FakeSocket(sync_in)]
            with self.subTest(shell=shell_in[:12]), patch('socket.create_connection', side_effect=socks), \
                    self.assertRaises((ValueError, ConnectionError)):
                client.capture()
            self.assertTrue(all(s.closed or not s.sent for s in socks))

    def test_shell_quotes_root_and_bounds_output(self):
        from unittest.mock import patch
        m = importlib.import_module('xt550_viewer')
        self.assertTrue(hasattr(m.ADBClient, 'shell'), 'Shell transport missing')
        client = m.ADBClient('TEST_DEVICE')
        sock = FakeSocket(b'OKAYOKAYhello\r\n')
        with patch('socket.create_connection', return_value=sock):
            self.assertEqual(client.shell('echo hello', root=True), 'hello\n')
        self.assertEqual(sock.sent[-1], b"0018shell:su -c 'echo hello'")
        sock = FakeSocket(b'OKAYOKAY' + b'x' * 200001)
        with patch('socket.create_connection', return_value=sock), self.assertRaises(ValueError):
            client.shell('getprop ro.product.model')


def run_gesture_shell(command, failure='none'):
    """Run real host sh with only mock event/sleep functions, never ADB."""
    script = '''
event_count=0
sendevent() {
    event_count=$((event_count + 1))
    echo "$*"
    case "$event_count:$failure" in
        2:event|6:release1|7:release2|8:release3) return 1 ;;
    esac
    return 0
}
sleep() {
    echo "sleep $*"
    case "$failure" in sleep) return 1 ;; esac
    return 0
}
'''
    return subprocess.run(['sh', '-c', 'failure=' + failure + '\n' + script + command],
                          capture_output=True, text=True, timeout=10)


class InputTests(unittest.TestCase):
    def test_exact_type_a_protocol_and_safe_input_builders(self):
        m = importlib.import_module('xt550_viewer')
        self.assertTrue(hasattr(m, 'gesture_command'), 'Validated input builders missing')
        command = m.gesture_command([(10, 20), (100, 200)], True)
        events = [(3, 48, 1), (3, 53, 10), (3, 54, 20), (0, 2, 0), (0, 0, 0),
                  (3, 48, 1), (3, 53, 100), (3, 54, 200), (0, 2, 0), (0, 0, 0)]
        expected = [f'sendevent /dev/input/event3 {a} {b} {c}' for a, b, c in events]
        expected += ['sleep 1', 'sendevent /dev/input/event3 3 48 0',
                     'sendevent /dev/input/event3 0 2 0', 'sendevent /dev/input/event3 0 0 0']
        result = run_gesture_shell(command)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(),
                         [event.removeprefix('sendevent ') for event in expected])
        for points in [[], [(0, 0)] * 25, [(-1, 0)], [(320, 0)], [(0, 480)],
                       [(True, 0)], [(0.0, 1)], [('1; reboot', 0)], [(1,)], None]:
            with self.subTest(points=points), self.assertRaises(ValueError):
                m.gesture_command(points)
        for code in [3, 4, 82, 26, 24, 25, 67, 66, 19, 20, 21, 22, 61]:
            self.assertEqual(m.key_command(code), f'input keyevent {code}')
        for bad in [True, '3; reboot', 999]:
            with self.assertRaises(ValueError):
                m.key_command(bad)
        self.assertEqual(m.text_command('Hello world!'), "input text 'Hello world!'")
        for bad in ['', 'á', 'a\n', 'x; reboot', '$(id)', '`id`', 'a&b', 'x|id', 'x>y', "a'b", '%s']:
            with self.subTest(text=bad), self.assertRaises(ValueError):
                m.text_command(bad)

    def test_gesture_aggregates_failures_without_skipping_any_release(self):
        m = importlib.import_module('xt550_viewer')
        command = m.gesture_command([(10, 20)], True)
        for failure in ['event', 'sleep', 'release1', 'release2', 'release3']:
            with self.subTest(failure=failure):
                result = run_gesture_shell(command, failure)
                self.assertEqual(result.stdout.splitlines()[-3:],
                                 ['/dev/input/event3 3 48 0',
                                  '/dev/input/event3 0 2 0', '/dev/input/event3 0 0 0'])
                self.assertNotEqual(result.returncode, 0, result.stderr)


class FakeDevice:
    def __init__(self):
        self.model, self.touch = 'XT550', 'ms-msg20xx'
        self.calls = []
        self.failure = None
        self.result = '__VIEWER_RC:0\n'
    def shell(self, command, root=False):
        self.calls.append((command, root))
        if command == 'getprop ro.product.model':
            return self.model
        if command == 'cat /sys/class/input/event3/device/name':
            return self.touch
        return self.result
    def capture(self):
        if self.failure:
            raise self.failure
        m = importlib.import_module('xt550_viewer')
        return m.frame_to_image(m.FRAME_HEADER, bytes(307200))


class ActionTests(unittest.TestCase):
    def test_intermediate_gesture_failure_invalidates_and_clears_queue(self):
        m = importlib.import_module('xt550_viewer')

        class ShellDevice(FakeDevice):
            def shell(self, command, root=False):
                if not root:
                    return super().shell(command, root)
                self.executed = run_gesture_shell(command, 'event')
                return self.executed.stdout

        device = ShellDevice()
        session = m.ControlSession(device, True)
        session.refresh()
        generation = session.generation
        self.assertTrue(session.enqueue('touch', ([(10, 20)], False)))
        self.assertTrue(session.enqueue('key', 3))
        with self.assertRaises(RuntimeError):
            session.execute_next()
        self.assertIn('__VIEWER_RC:1', device.executed.stdout.splitlines())
        self.assertEqual(device.executed.returncode, 0, 'Marker shell must survive gesture failure')
        self.assertEqual(device.executed.stdout.splitlines()[-4:-1],
                         ['/dev/input/event3 3 48 0',
                          '/dev/input/event3 0 2 0', '/dev/input/event3 0 0 0'])
        self.assertFalse(session.ready)
        self.assertTrue(session.commands.empty())
        self.assertGreater(session.generation, generation)
        self.assertFalse(session.enqueue('key', 3))
        self.assertFalse(session.execute_next())

    def test_readonly_never_enqueues_or_injects_and_enabled_revalidates_each_action(self):
        m = importlib.import_module('xt550_viewer')
        self.assertTrue(hasattr(m, 'ControlSession'), 'Fail-closed control state missing')
        device = FakeDevice()
        readonly = m.ControlSession(device)
        self.assertFalse(readonly.enqueue('key', 3))
        readonly.refresh()
        self.assertFalse(readonly.enqueue('key', 3))
        self.assertFalse(readonly.execute_next())
        self.assertFalse(any(root for _, root in device.calls))
        session = m.ControlSession(device, enable_control=True)
        self.assertFalse(session.enqueue('key', 3))
        session.refresh()
        for kind, value in [('key', 3), ('text', 'Hello'), ('touch', ([(1, 2)], False))]:
            self.assertTrue(session.enqueue(kind, value))
            device.calls.clear()
            self.assertTrue(session.execute_next())
            self.assertEqual(device.calls[:2], [('getprop ro.product.model', False),
                             ('cat /sys/class/input/event3/device/name', False)])
            self.assertTrue(device.calls[-1][1])
        for kind, value in [('shell', 'reboot'), ('key', '3; reboot'),
                            ('text', 'x; reboot'), ('touch', ([(-1, 0)], False))]:
            with self.assertRaises(ValueError):
                session.enqueue(kind, value)


    def test_failures_drop_queued_input_and_require_fresh_frame_and_fresh_gesture(self):
        m = importlib.import_module('xt550_viewer')
        for failure in [ValueError('bad frame'), ConnectionError('disconnected')]:
            device = FakeDevice()
            session = m.ControlSession(device, True)
            session.refresh()
            self.assertTrue(session.enqueue('touch', ([(1, 2)], False)))
            device.failure = failure
            with self.assertRaises(type(failure)):
                session.refresh()
            self.assertFalse(session.ready, 'Capture failure must immediately disable controls')
            self.assertFalse(session.enqueue('key', 3))
            self.assertFalse(session.execute_next())
            device.failure = None
            session.refresh()
            self.assertFalse(session.execute_next(), 'Old queue must not survive reconnection')
            self.assertTrue(session.enqueue('key', 3))
            self.assertTrue(session.execute_next())
        device = FakeDevice()
        session = m.ControlSession(device, True)
        session.refresh()
        self.assertTrue(hasattr(session, 'generation'), 'Gestures need a connection generation')
        old = session.generation
        session.invalidate()
        session.refresh()
        self.assertFalse(session.enqueue('touch', ([(1, 2)], False), generation=old))
        self.assertTrue(session.enqueue('touch', ([(1, 2)], False), generation=session.generation))

    def test_identity_change_or_root_error_disables_and_discards_queue(self):
        m = importlib.import_module('xt550_viewer')
        for field, value in [('model', 'OTHER'), ('touch', 'OTHER'), ('result', '__VIEWER_RC:1')]:
            device = FakeDevice()
            session = m.ControlSession(device, True)
            session.refresh()
            session.enqueue('key', 3)
            session.enqueue('key', 4)
            setattr(device, field, value)
            device.calls.clear()
            with self.assertRaises((ValueError, RuntimeError)):
                session.execute_next()
            self.assertFalse(session.ready)
            self.assertTrue(session.commands.empty())
            if field != 'result':
                self.assertFalse(any(root for _, root in device.calls))


class ViewerTests(unittest.TestCase):
    def test_headless_ui_wires_zoom_keys_text_and_generation_bound_gestures(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch
        m = importlib.import_module('xt550_viewer')
        self.assertTrue(hasattr(m, 'Viewer'), 'Portable Tk UI missing')
        class Variable:
            def __init__(self, value=None):
                self.value = value
            def get(self):
                return self.value
            def set(self, value):
                self.value = value
            def trace_add(self, *args):
                pass
        fake_tk = SimpleNamespace(StringVar=Variable, DoubleVar=Variable, BooleanVar=Variable,
                                  Canvas=MagicMock(), ttk=MagicMock())
        with patch.dict('sys.modules', {'tkinter': fake_tk}):
            device = FakeDevice()
            session = m.ControlSession(device, True)
            view = m.Viewer(MagicMock(), session, start_workers=False)
        self.assertEqual(set(view.button_labels), {'Home', 'Back', 'Menu', 'Power', 'Vol -', 'Vol +'})
        event = SimpleNamespace(x=40, y=60)
        view.press(event)
        view.release(event)
        self.assertTrue(session.commands.empty())
        session.refresh()
        view.press(event)
        view.drag(SimpleNamespace(x=80, y=120))
        view.release(event)
        self.assertTrue(session.execute_next())
        view.press(event)
        session.invalidate()
        session.refresh()
        view.release(event)
        self.assertTrue(session.commands.empty())
        view.send('key', 3)
        view.key(SimpleNamespace(keysym='Return', char='\r'))
        view.text.get.return_value = 'Hello'
        view.write_text()
        self.assertEqual(session.commands.qsize(), 3)
        for _ in range(3):
            self.assertTrue(session.execute_next())
        with patch('PIL.ImageTk.PhotoImage', return_value=object()):
            view.last_image = device.capture()
            view.redraw()
        view.canvas.configure.assert_called_with(width=400, height=600)
        view.events.put(('image', device.capture()))
        with patch.object(view, 'redraw') as redraw:
            view.poll()
        redraw.assert_called_once()
        view.close()
        self.assertTrue(view.stop.is_set())
        view.root.destroy.assert_called_once()

    def test_worker_capture_failure_disables_controls_without_ui_poll(self):
        from unittest.mock import MagicMock
        m = importlib.import_module('xt550_viewer')
        self.assertTrue(hasattr(m, 'Viewer'), 'Portable Tk worker missing')
        device = FakeDevice()
        session = m.ControlSession(device, True)
        session.refresh()
        session.enqueue('key', 3)
        device.failure = ConnectionError('offline')
        view = m.Viewer.__new__(m.Viewer)
        view.session = session
        view.events = __import__('queue').Queue(maxsize=2)
        view.stop = MagicMock()
        view.stop.is_set.side_effect = [False, True]
        view.frames()
        self.assertFalse(session.ready)
        self.assertTrue(session.commands.empty())
        self.assertFalse(any(root for _, root in device.calls))


class LiveGuardTests(unittest.TestCase):
    def test_live_capture_requires_both_explicit_switch_and_environment(self):
        from unittest.mock import patch
        path = Path(__file__).with_name('live_test.py')
        self.assertTrue(path.exists(), 'Opt-in live test missing')
        live = importlib.import_module('live_test')
        for argv, env in [(['--serial', 'TEST_DEVICE'], {}),
                          (['--serial', 'TEST_DEVICE', '--live'], {}),
                          (['--serial', 'TEST_DEVICE'], {'XT550_LIVE_TEST': '1'})]:
            with patch.dict('os.environ', env, clear=True), patch('socket.create_connection', side_effect=AssertionError('Network forbidden')):
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as result:
                    live.main(argv)
                self.assertEqual(result.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
