"""The pure parts of the MCP client: the protocol _meta."""

import unittest

import helpers  # noqa: F401  (puts src/ on sys.path)

from gaiadesk import MCP_PROTOCOL_VERSION
from gaiadesk.mcp import with_protocol_meta


class McpHelpersTest(unittest.TestCase):
    def test_with_protocol_meta(self):
        self.assertEqual(
            with_protocol_meta(),
            {"_meta": {"io.modelcontextprotocol/protocolVersion": MCP_PROTOCOL_VERSION, "io.modelcontextprotocol/clientCapabilities": {}}},
        )
        p = with_protocol_meta({"name": "x", "_meta": {"progressToken": 7, "io.modelcontextprotocol/protocolVersion": "mine"}})
        self.assertEqual(p["name"], "x")
        self.assertEqual(p["_meta"]["io.modelcontextprotocol/protocolVersion"], "mine", "the caller wins")
        self.assertEqual(p["_meta"]["progressToken"], 7)
        self.assertEqual(p["_meta"]["io.modelcontextprotocol/clientCapabilities"], {})


if __name__ == "__main__":
    unittest.main()
