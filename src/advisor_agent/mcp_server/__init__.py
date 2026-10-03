"""FastMCP server exposing the Google tools the agent may use (LLD 3.9).

Only this package talks to Google (Calendar v3, Docs v1, Gmail v1). The agent reaches it
exclusively over MCP via `advisor_agent.mcp_client.ToolClient`.
"""
