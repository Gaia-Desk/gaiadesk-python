"""The pure parts of the MCP client: the protocol _meta and tool-name aliases."""

import unittest

import helpers  # noqa: F401  (puts src/ on sys.path)

from gaiadesk import MCP_PROTOCOL_VERSION, resolve_tool_name, tool_name_alias
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

    def test_tool_name_alias(self):
        self.assertEqual(tool_name_alias("gaiadesk.exec"), "gaiadesk_exec")
        self.assertEqual(tool_name_alias("gaiadesk_job_run"), "gaiadesk.job_run")
        self.assertEqual(tool_name_alias("gaiadesk.copy_files"), "gaiadesk_copy_files")
        self.assertIsNone(tool_name_alias("other.exec"))
        self.assertIsNone(tool_name_alias("gaiadesk"))

    def test_resolve_tool_name(self):
        dotted = {"gaiadesk.exec", "gaiadesk.job_run"}
        plain = {"gaiadesk_exec", "gaiadesk_job_run"}
        self.assertEqual(resolve_tool_name("gaiadesk_exec", dotted), "gaiadesk.exec")
        self.assertEqual(resolve_tool_name("gaiadesk.exec", dotted), "gaiadesk.exec")
        self.assertEqual(resolve_tool_name("gaiadesk.job_run", plain), "gaiadesk_job_run")
        self.assertEqual(resolve_tool_name("gaiadesk.nope", plain), "gaiadesk.nope")
        self.assertEqual(resolve_tool_name("gaiadesk.exec"), "gaiadesk.exec")


if __name__ == "__main__":
    unittest.main()
