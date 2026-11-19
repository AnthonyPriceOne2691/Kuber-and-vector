"""Разбор «кто подписал» в deploy/vm/kit-fetch-falco.sh: код на Python внутри скрипта гоняем как есть
на выводе cosign в форматах v2 и v3. Сертификат тестовый, самоподписанный, ключ от него удалён."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = "https://github.com/falcosecurity/falco/.github/workflows/release.yaml@refs/tags/0.45.2"
PEM = """-----BEGIN CERTIFICATE-----
MIIBuTCCAV+gAwIBAgIUSXjjzav89dnRB1hjG7jhMbR2JuYwCgYIKoZIzj0EAwIw
FzEVMBMGA1UECgwMc2lnc3RvcmUuZGV2MB4XDTI2MTExOTEwNTQ1MFoXDTI2MTEy
MDEwNTQ1MFowFzEVMBMGA1UECgwMc2lnc3RvcmUuZGV2MFkwEwYHKoZIzj0CAQYI
KoZIzj0DAQcDQgAEJKyeII9cIc4+zUd9epZlUbPXeTva5/pQp6MVvziUAPxRIiz7
OV9OTT6HwI0OaERXypSbCxQIUYbOB1C4DzVua6OBiDCBhTBkBgNVHREBAf8EWjBY
hlZodHRwczovL2dpdGh1Yi5jb20vZmFsY29zZWN1cml0eS9mYWxjby8uZ2l0aHVi
L3dvcmtmbG93cy9yZWxlYXNlLnlhbWxAcmVmcy90YWdzLzAuNDUuMjAdBgNVHQ4E
FgQUdvpu6620TGA4iL3lZvHrsZMMjR8wCgYIKoZIzj0EAwIDSAAwRQIhANIU4b/0
2taoxQGm5RmslBafQ023UtR927eO+RkQlpDuAiAIOBT9DYxRcBLrdB1+PQA3H7WH
wqTJtY4pa8vzl60+yg==
-----END CERTIFICATE-----
"""
DER_B64 = "".join(PEM.strip().splitlines()[1:-1])

with open(os.path.join(ROOT, "deploy", "vm", "kit-fetch-falco.sh"), encoding="utf-8") as _f:
    CODE = re.search(r'signed_by=\$\(python3 -I - "\$W/sig\.json" <<\'PY\'\n(.*?)\nPY\n\)', _f.read(), re.S).group(1)


class SignerTest(unittest.TestCase):
    def signer(self, text: str) -> str:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sig.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            r = subprocess.run([sys.executable, "-I", "-", path], input=CODE, capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_cosign_v2_fields(self):
        out = self.signer(json.dumps([{"critical": {"image": {"docker-manifest-digest": "sha256:1"}},
                                       "optional": {"Issuer": "https://token.actions.githubusercontent.com",
                                                    "Subject": WORKFLOW, "githubWorkflowRef": "refs/tags/0.45.2"}}]))
        self.assertIn(f"Subject={WORKFLOW}", out)
        self.assertIn("githubWorkflowRef=refs/tags/0.45.2", out)

    def test_cosign_v3_certificate_der(self):
        out = self.signer(json.dumps([{"critical": {}, "optional": {},
                                       "verificationMaterial": {"certificate": {"rawBytes": DER_B64}}}]))
        self.assertEqual(out, f"сертификат {WORKFLOW}")

    def test_text_with_pem_and_garbage(self):
        self.assertEqual(self.signer("Verification for x --\n" + PEM + "\nok"), f"сертификат {WORKFLOW}")
        self.assertEqual(self.signer('{"optional": {"note": "MIInot-a-cert"}}'), "")
        self.assertEqual(self.signer("not json at all"), "")


if __name__ == "__main__":
    unittest.main()
