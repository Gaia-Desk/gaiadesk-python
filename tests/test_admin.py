"""Administrator work is not available over any API: the hosted API and a desk's
local and LAN APIs refuse it with ``admin_not_via_api`` (kind ``refused``), and
the SDK never asks for it there (only ``gaiadesk-cli exec --admin`` runs it)."""

import json
import os
import sys
import unittest

import helpers  # noqa: F401 (the package from src/)

sys.path.insert(0, os.path.join(helpers.HERE, "fixtures"))
import raw_server as rs  # noqa: E402
from test_raw_server import D, bounded  # noqa: E402

from gaiadesk import ADMIN_NOT_VIA_API, GaiaDesk, RefusedError, UsageError  # noqa: E402

REFUSAL = {"kind": "refused", "reason": "admin_not_via_api",
           "message": "administrator work is not available through the API; use gaiadesk-cli exec --admin"}


def answer(status, body):
    data = json.dumps(body).encode()
    return (b"HTTP/1.1 %d X\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n"
            % (status, len(data)) + data)


def gd(s):
    return GaiaDesk(api_key="ak_t", desk_token="gdagt_t", base_url=s.url, e2e="off", max_retries=0)


class AdminNotViaApi(unittest.TestCase):
    def server(self, mode):
        s = rs.RawServer(mode)
        self.addCleanup(s.close)
        return s

    def test_the_constant(self):
        self.assertEqual(ADMIN_NOT_VIA_API, "admin_not_via_api")

    def test_mint_403_maps_to_refused(self):
        s = self.server(answer(403, {"error": dict(REFUSAL, request_id="req_t")}))
        _, e, _ = bounded(lambda: gd(s).create_token(D, name="ops", scopes=["exec"]))
        self.assertIsInstance(e, RefusedError)
        self.assertEqual((e.kind, e.reason, e.status, e.exit_code), ("refused", ADMIN_NOT_VIA_API, 403, 254))

    def test_exec_refused_answer_maps_to_refused(self):
        s = self.server(answer(200, {"exit": 254, "desk": D, "stdout": "", "stderr": "", "error": REFUSAL}))
        _, e, _ = bounded(gd(s).exec, D, "whoami")
        self.assertIsInstance(e, RefusedError)
        self.assertEqual((e.kind, e.reason, e.exit_code), ("refused", ADMIN_NOT_VIA_API, 254))

    def test_a_streamed_exec_refused_at_once(self):
        s = self.server(answer(403, {"error": dict(REFUSAL, request_id="req_t")}))
        st = gd(s).exec_stream(D, "whoami")
        ex, _, _ = bounded(st.wait)
        self.assertEqual(ex.exit_code, 254)
        self.assertEqual((st.result["error"]["kind"], st.result["error"]["reason"]), ("refused", ADMIN_NOT_VIA_API))

    def test_the_admin_scope_is_refused_locally_on_every_api_transport(self):
        s = self.server(rs.OK)
        clients = (gd(s), GaiaDesk(transport="local", socket_path="/nonexistent.sock", desk_token="gdagt_t"),
                   GaiaDesk(transport="lan", base_url="https://10.0.0.2:7443/v1", fingerprint="ab" * 32, desk_token="gdagt_t"))
        for g in clients:
            with self.subTest(transport=g.backend), self.assertRaises(UsageError) as cm:
                g.create_token(D, name="ops", scopes=["exec", "admin"])
            self.assertEqual(cm.exception.reason, ADMIN_NOT_VIA_API)
        self.assertEqual(s.count("POST"), 0, "nothing was sent")


if __name__ == "__main__":
    unittest.main()
