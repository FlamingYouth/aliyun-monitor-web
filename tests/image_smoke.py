"""Start the image's web server and check real HTTP with synthetic services only."""
import os
import subprocess
import tempfile
import time
import urllib.request

with tempfile.TemporaryDirectory() as directory:
    env = {**os.environ, 'DATA_DIR': directory, 'SETUP_TOKEN': 'codex-preview-test-token',
           'TEST_SETUP_TOKEN': 'codex-preview-test-token', 'MOCK_CLOUD': '1'}
    server = subprocess.Popen(['gunicorn', '--bind', '127.0.0.1:8080', '--workers', '1',
                               '--threads', '8', '--worker-tmp-dir', '/tmp', 'tests.preview:app'], env=env)
    try:
        deadline = time.monotonic() + 90
        while True:
            try:
                with urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3) as response:
                    assert response.status == 200
                break
            except Exception:
                if server.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError('Synthetic test web server did not become healthy')
                time.sleep(.25)
        for asset in ('/', '/static/app.js', '/static/style.css', '/static/extras.css'):
            with urllib.request.urlopen('http://127.0.0.1:8080' + asset, timeout=10) as response:
                assert response.status == 200 and response.read()
        subprocess.run(['python', '-m', 'tests.http_smoke'], env=env, check=True)
        print('Packaged-image HTTP/assets smoke PASS')
    finally:
        server.terminate()
        server.wait(timeout=30)
