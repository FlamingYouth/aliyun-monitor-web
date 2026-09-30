"""Isolated browser-test server. Never copied into production Docker image."""
import os
from app import create_app
from tests.fakes import FakeCloud, FakeNotifier

app = create_app(os.environ.get('DATA_DIR','/data'),FakeCloud(),FakeNotifier(),scheduler=False)
app.config['TESTING'] = True
