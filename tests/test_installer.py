import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

class InstallerTests(unittest.TestCase):
    def setUp(self):
        # Docker tmpfs /tmp may be noexec; the installer fixture needs an executable path.
        self.tmp=tempfile.TemporaryDirectory(dir=os.environ.get('TEST_EXEC_TMP','/data'));self.root=Path(self.tmp.name)
        shutil.copy('install.sh',self.root/'install.sh')
        shutil.copy('tests/fake_docker.sh',self.root/'docker');os.chmod(self.root/'docker',0o700)
        self.env={**os.environ,'PATH':str(self.root)+':'+os.environ['PATH'],'FAKE_DOCKER_LOG':str(self.root/'calls')}
    def tearDown(self):self.tmp.cleanup()
    def run_install(self,inputs,**env):
        return subprocess.run(['sh',str(self.root/'install.sh')],input=inputs,text=True,capture_output=True,env={**self.env,**env},timeout=5)
    def test_old_docker_rejected(self):
        r=self.run_install('',FAKE_DOCKER_VERSION='1.13.1');self.assertNotEqual(r.returncode,0)
        self.assertFalse((self.root/'.env').exists())
    def test_unsupported_20_10_patch_rejected(self):
        r=self.run_install('',FAKE_DOCKER_VERSION='20.10.9');self.assertNotEqual(r.returncode,0)
    def test_existing_container_never_overwritten(self):
        r=self.run_install('',FAKE_DOCKER_EXISTS='1');self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        self.assertNotIn('build',(self.root/'calls').read_text());self.assertFalse((self.root/'.env').exists())
    def test_cancel_no_mutations(self):
        r=self.run_install('1\n8088\nno\n');self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        self.assertFalse((self.root/'.env').exists());self.assertNotIn('build',(self.root/'calls').read_text())
    def test_invalid_port_no_build(self):
        for port in ('bad','80','99999','8088;touch /tmp/not-allowed'):
            with self.subTest(port=port):self.assertNotEqual(self.run_install('1\n'+port+'\nyes\n').returncode,0)
        self.assertFalse((self.root/'.env').exists())
    def test_build_failure_keeps_private_env(self):
        r=self.run_install('1\n18889\nyes\n');self.assertNotEqual(r.returncode,0)
        env=(self.root/'.env').read_text();self.assertIn('WEB_BIND=127.0.0.1',env)
        self.assertEqual((self.root/'.env').stat().st_mode&0o777,0o600)
        self.assertEqual(len(env.split('SETUP_TOKEN=')[1].strip()),48)
        self.assertNotIn('run ',(self.root/'calls').read_text())

if __name__=='__main__':unittest.main()
