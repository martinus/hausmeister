import tempfile
import unittest
from pathlib import Path

from support import FEDORA_44, load, write

hm = load()

UBUNTU_2404 = 'PRETTY_NAME="Ubuntu 24.04.1 LTS"\nNAME="Ubuntu"\nVERSION_ID="24.04"\nID=ubuntu\nID_LIKE=debian\n'
MINT = 'NAME="Linux Mint"\nVERSION="22 (Wilma)"\nID=linuxmint\nID_LIKE="ubuntu debian"\nVERSION_ID="22"\n'


class DetectDistroTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.no_ostree = self.tmp / "no-ostree-booted"

    def detect(self, text, ostree_booted=False):
        os_release = write(self.tmp / "os-release", text)
        marker = self.no_ostree
        if ostree_booted:
            marker = write(self.tmp / "ostree-booted", "")
        return hm.detect_distro(os_release, marker)

    def test_fedora(self):
        self.assertEqual(self.detect(FEDORA_44), hm.Distro("fedora", "44", "Fedora Linux 44"))

    def test_fedora_atomic(self):
        self.assertEqual(self.detect(FEDORA_44, ostree_booted=True).id, "fedora-atomic")

    def test_ubuntu(self):
        self.assertEqual(self.detect(UBUNTU_2404), hm.Distro("ubuntu", "24.04", "Ubuntu 24.04"))

    def test_mint_is_not_ubuntu(self):
        distro = self.detect(MINT)
        self.assertEqual((distro.id, distro.version_id), ("linuxmint", "22"))
        self.assertNotIn(distro.id, hm.KNOWN_DISTROS)

    def test_missing_file(self):
        distro = hm.detect_distro(self.tmp / "missing", self.no_ostree)
        self.assertEqual(distro, hm.Distro("linux", "", "Linux"))

    def test_comments_and_bad_lines(self):
        distro = self.detect('# comment\n\nnot a pair\nID=arch\nNAME="Arch Linux\nVERSION_ID=rolling\n')
        self.assertEqual((distro.id, distro.version_id, distro.name), ("arch", "rolling", "Linux rolling"))


if __name__ == "__main__":
    unittest.main()
