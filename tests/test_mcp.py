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

    def test_the_names_from_0_10_324(self):
        # gaiadesk-cli mcp's tools (agent/src/agent_access/mcp, client/src/mcp_tools.rs).
        tools = ["exec", "copy_files", "job_run", "job_list", "job_logs", "job_kill", "forward_start", "forward_stop", "open_session",
                 "close_session", "screenshot", "click", "drag", "move_pointer", "pointer_position", "press_button", "press_keys",
                 "hold_keys", "type_text", "scroll", "wait"]
        for t in tools:
            name = "gaiadesk_" + t
            self.assertEqual(tool_name_alias(tool_name_alias(name)), name)
            self.assertEqual(resolve_tool_name(name), name, "with no list, the name as given")
            self.assertEqual(resolve_tool_name("gaiadesk." + t, {name}), name)

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
