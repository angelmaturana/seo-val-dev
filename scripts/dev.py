#!/usr/bin/env python3
import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from http.server import HTTPServer, SimpleHTTPRequestHandler

REPO = Path(__file__).resolve().parent.parent
DEV_DIST = REPO / '.dev-dist'

class DevHandler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        path = super().translate_path(path)
        rel = os.path.relpath(path, REPO)
        return str(DEV_DIST / rel)

    def do_GET(self):
        path = self.translate_path(self.path)
        if os.path.isdir(path):
            if not self.path.endswith('/'):
                self.send_response(301)
                self.send_header('Location', self.path + '/')
                self.end_headers()
                return
            index = os.path.join(path, 'index.html')
            if os.path.exists(index):
                self.path = self.path + '/index.html'
                return super().do_GET()
        if not os.path.exists(path):
            notfound = DEV_DIST / '404.html'
            if notfound.exists():
                self.send_response(404)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                self.wfile.write(notfound.read_bytes())
                return
        return super().do_GET()

def build_dev(minify):
    args = [sys.executable, str(REPO / 'scripts' / 'build.py'), '--out', str(DEV_DIST), '--minify', 'true' if minify else 'false']
    result = subprocess.run(args, cwd=REPO, capture_output=True, text=True)
    return result.returncode, result.stdout, result.stderr

def watch_src(callback, interval=0.5):
    mtimes = {}
    for p in (REPO / 'src').rglob('*'):
        if p.is_file():
            mtimes[p] = p.stat().st_mtime
    while True:
        changed = False
        for p in list(mtimes.keys()):
            if not p.exists():
                del mtimes[p]
                changed = True
            else:
                mtime = p.stat().st_mtime
                if mtime != mtimes.get(p):
                    mtimes[p] = mtime
                    changed = True
        for p in (REPO / 'src').rglob('*'):
            if p.is_file() and p not in mtimes:
                mtimes[p] = p.stat().st_mtime
                changed = True
        if changed:
            callback()
        time.sleep(interval)

def main():
    parser = argparse.ArgumentParser(description='Dev server for seovaldemoro')
    parser.add_argument('--port', type=int, default=3001)
    parser.add_argument('--minify', action='store_true')
    args = parser.parse_args()

    server = HTTPServer(('localhost', args.port), DevHandler)
    print(f'Serving .dev-dist on http://localhost:{args.port}')

    last_error = None

    def rebuild():
        nonlocal last_error
        code, out, err = build_dev(args.minify)
        if code != 0:
            last_error = out + err
            print('Build failed:')
            print(last_error)
        else:
            last_error = None
            print('Build OK')

    rebuild()

    def signal_handler(sig, frame):
        print('\nStopping dev server')
        server.server_close()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)

    def watcher():
        watch_src(rebuild)

    import threading
    t = threading.Thread(target=watcher, daemon=True)
    t.start()

    server.serve_forever()

if __name__ == '__main__':
    main()
