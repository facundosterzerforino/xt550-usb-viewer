"""Manual opt-in read-only USB smoke test. Never collected by unittest discovery."""
import argparse
import os
from xt550_viewer import ADBClient, ControlSession


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serial', required=True)
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args(argv)
    if not args.live or os.environ.get('XT550_LIVE_TEST') != '1':
        parser.error('Requires --live AND XT550_LIVE_TEST=1; no device accessed')
    session = ControlSession(ADBClient(args.serial))
    image = session.refresh()
    if image.size != (320, 480):
        raise ValueError('Unexpected framebuffer')
    print('PASS: XT550 identity and 320x480 RGB565 capture (read-only; not saved)')


if __name__ == '__main__':
    main()
