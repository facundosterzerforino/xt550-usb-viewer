# XT550 USB Viewer

Small Python/Tkinter screen viewer for the **Motorola XT550 running Android 2.3.6**. The original viewer was tested on that specific hardware: **320 × 480 RGB565**, touchscreen **`ms-msg20xx`**, **`/dev/input/event3`**, Linux **type-A multitouch**. This sanitized edition adds explicit device selection and fail-closed input controls, verified with offline tests. **Other phones, ROMs, resolutions, touch devices, or Android versions are not claimed to work.** The screencap capture path was retested on the XT550 (10 captures, 0.24 s each, no leftover processes).

No APK, device-side server, bundled ADB, runtime, firmware, or third-party binary is included. MIT licensed; see [LICENSE](LICENSE).

## Safety and scope

- **Read-only by default.** `--serial SERIAL` is mandatory. Input additionally requires **`--enable-control`** and an already working, user-authorized root setup (`su -c`). The viewer does not root or modify the OS.
- Connects only as a **client** to an **already running** local ADB server at `127.0.0.1:5037`; it never starts/kills ADB, opens a listening socket, enumerates/selects another device, or falls back to one.
- USB debugging, drivers/USB permissions, an appropriate ADB server, and any root authorization must already be configured by the owner. Use a USB-connected device, not an ADB-over-network target. An ADB serial is supplied at runtime, not stored in source/config.
- Requires exact model and touchscreen identity before each capture and again immediately before each root action. The screencap geometry, format, and length are validated before decoding pixel data. There is no generic shell input in the UI.
- Capture/identity/disconnect/action failures immediately disable control and discard pending input in the worker, independently of Tk polling. Recovery needs a fresh validated frame. Gestures started in an older connection generation are rejected, even if released after recovery.
- No screenshots/frame files, text logs, clipboard access, credentials, private machine paths, or persistent config. Frames exist only in memory. Typed text stays in the entry until accepted into the bounded in-memory queue; submitted text is not echoed in status/errors.
- No license acceptance, credential retrieval, lock-screen bypass, or automatic unlocking. **Use only a device you own or have permission to operate.** Unlock normally on the device. Do not use this to enter secrets. Root input can press real buttons and affect apps/data.

These checks prevent accidental cross-device input; they are not a security boundary against a compromised ADB server/rooted phone. ADB identity checks and injection use successive connections with the same explicit serial, not an atomic hardware attestation. An unplug/replug entirely between requests may not be observable. Never replace the selected device while input is queued. A command already in flight cannot be recalled, and an interrupted USB/root session may prevent the release events from reaching the phone.

## Requirements and declared versions

- CPython **3.11, 3.12, 3.13, or 3.14** (ordinary GIL builds; intended version range). Local offline verification: **3.14.9**, Windows. The other interpreter versions and Linux have not been executed for this release. CI is intentionally omitted from this initial release; local headless tests are the verification baseline.
- **Pillow 13.0.0**, exactly pinned in `requirements.txt` (Python >=3.11).
- Python's **Tkinter**, supplied with Python/the OS, **Tk 8.6 or 9.0 API**. The local interpreter reports **Tk API 9.0 / Tcl 9.0.4**. Offline tests mock the GUI; they do not establish real-window compatibility on every Tk version.
- A graphical desktop for the viewer. Unit tests need no display and never contact a phone. Tkinter must still be importable for the ImageTk boundary test.
- An existing USB ADB transport with `shell:` and `sync:` services and the stock `screencap` binary. ADB is externally managed and not pinned or distributed here; no particular Platform Tools build is certified by this repository.

Capture no longer uses the ADB `framebuffer:` service. On the XT550, Android 2.3.6 adbd forks `screencap` for every `framebuffer:` request and never reaps it, leaving one zombie process per frame (about 3,300 after a few hours; the PID limit is 32,768). The viewer now runs `shell:screencap /data/local/tmp/xt550-viewer-<PC pid>.raw` (adbd reaps shell children; one file per PC process so a viewer and a script never read each other's half-written capture; removed at exit) and copies the 307,212-byte raw file over `sync:` (no extra process). Verified on the phone: zombie count stayed at 0 with the viewer open.

## Install (Windows)

Install CPython 3.14.9 from the official Python distribution, including **Tcl/Tk and IDLE**. In PowerShell or cmd, from this repository:

```text
py -3.14 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m unittest discover -v
.venv\Scripts\python xt550_viewer.py --serial YOUR_DEVICE_SERIAL
```

For root controls, explicitly opt in:

```text
.venv\Scripts\python xt550_viewer.py --serial YOUR_DEVICE_SERIAL --enable-control
```

Use the literal serial of your own USB XT550, obtained through your existing device-management workflow; `YOUR_DEVICE_SERIAL` is only a placeholder. If no ADB server is running, the viewer reports unavailable and does **not** start one. This project does not install drivers or accept any SDK/vendor licenses.

## Install (Linux)

Install Python 3.11–3.14 plus its matching Tkinter/venv packages using your distribution's normal package manager. For Debian/Ubuntu these packages are typically `python3-tk` and `python3-venv`; versions depend on the distribution. Then, in the repository:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -v
.venv/bin/python xt550_viewer.py --serial YOUR_DEVICE_SERIAL
# Optional, existing root authorization required:
.venv/bin/python xt550_viewer.py --serial YOUR_DEVICE_SERIAL --enable-control
```

Use the Python executable for the selected version if `python3` is not that interpreter. USB permissions/udev and ADB setup remain outside this repository. No automatic privilege escalation occurs on the host.

## Controls and limitations

- Framebuffer snapshots at approximately **1 fps**, not video streaming; USB/root latency can make it slower. No audio, rotation handling, multi-finger gestures, recording, or background phone service.
- Zoom: **1.0×, 1.25×, 1.5×**. Screen coordinates are scaled/clamped to the verified portrait bounds.
- With controls enabled: click/tap; drag then release for a sampled swipe; optional **1-second long press**. At most 20 UI samples (24 at the validated command-builder boundary). Swipes are a burst of type-A samples, not a timed Android gesture.
- Touch protocol: contact size `ABS_MT_TOUCH_MAJOR=1`, X/Y, `SYN_MT_REPORT`, `SYN_REPORT`; release is exactly `ABS_MT_TOUCH_MAJOR=0`, `SYN_MT_REPORT`, `SYN_REPORT`, all on the verified event3 device. No type-B slot protocol or guessed fallback device.
- Each touch event and long-press sleep contributes to the aggregate command status. All three release events are attempted even after a failure; a nonzero aggregate status disables control and clears queued input. A successful final sync cannot mask an earlier failure. Release delivery still cannot be guaranteed after transport/root interruption.
- Home, Back, Menu, Power, volume −/+; keyboard arrows, Backspace, Return, Escape, Tab. These are ordinary key events, not unlock automation. On a black display, Power may wake/sleep it when control is enabled; read-only mode cannot wake it.
- Optional text: **1–256 basic ASCII characters**: letters, digits, spaces, and `.,!?_@:+/()=-`. Shell metacharacters, quotes, `%`, backslashes, control characters, accents/ñ, and other Unicode are deliberately rejected. Text is shell-quoted; no interpolation of arbitrary shell commands. Android 2.3 `input text`/IME behavior can still lose characters or mishandle spaces; this is not full keyboard emulation.
- Input queue: at most **25**. Input while capture/root I/O holds the safety lock is rejected as busy, not delayed invisibly. Root failures need a later fresh capture before further input. Buttons are permanently disabled in read-only mode; runtime validation is also enforced in the backend.
- The last displayed image may remain visible after disconnect; the status says unavailable and backend controls are disabled. No timeout-based rescue input is attempted.

## Tests

The offline suite contains **14 tests**. Its shell-status regressions require a POSIX `sh` on `PATH` (for example Git Bash on Windows, or the system shell on Linux). They run host shell functions mocking `sendevent` and `sleep`; they never execute those commands on a phone.

```sh
python -m unittest discover -v
python -m py_compile xt550_viewer.py test_viewer.py live_test.py
```

Offline coverage includes primary RGB565 colors; malformed/oversize/truncated frames; explicit serial/transport and Gingerbread capture without a request byte; root shell quoting and bounded output; coordinate/key/text injection rejection; exact type-A touch release; aggregate event/sleep/release failures with all releases attempted; surrounding status-marker survival; read-only and fresh-frame gates; identity changes; queue clearing after disconnect/failure; old in-progress gestures; and headless UI/worker wiring. Synthetic fixtures use only `TEST_DEVICE`. No phone, ADB process, window, or live server is required. No GitHub Actions workflow is included in this initial release; run the exact local commands above.

**Optional live test — never part of normal discovery or CI.** It only validates identity and reads one frame, without root input, displaying, or saving it. Run only when you deliberately want access to your own USB XT550, with both the environment flag and CLI switch:

```sh
# Linux / shell
XT550_LIVE_TEST=1 python live_test.py --live --serial YOUR_DEVICE_SERIAL
```

```powershell
# Windows PowerShell (for this invocation/session only; remove afterwards)
$env:XT550_LIVE_TEST = '1'
.venv\Scripts\python live_test.py --live --serial YOUR_DEVICE_SERIAL
Remove-Item Env:XT550_LIVE_TEST
```

Do not commit local configs, captures, serials, or environment files. `.gitignore` is defense in depth, not a privacy scanner.

## Español (resumen)

Visor USB probado originalmente **solo en Motorola XT550 con Android 2.3.6**, RGB565 320×480 y táctil `ms-msg20xx` tipo A en `event3`. Esta edición saneada tiene pruebas offline; no se volvió a probar con el teléfono. No promete compatibilidad con otros equipos.

Instalá Python con Tkinter y `Pillow==13.0.0`. Ejecutá `python xt550_viewer.py --serial YOUR_DEVICE_SERIAL` para **solo lectura**. El control requiere agregar explícitamente `--enable-control` y disponer de root previamente autorizado. No inicia/mata ADB, no elige otro dispositivo, no guarda imágenes/texto, no acepta licencias ni evade bloqueos. Ante desconexión o validación fallida bloquea controles y descarta la cola; para recuperarlos necesita una imagen nueva validada. Pruebas sin teléfono ni ventana: `python -m unittest discover -v`. Ver arriba instalación para Windows/Linux y limitaciones de texto, latencia y gestos.
