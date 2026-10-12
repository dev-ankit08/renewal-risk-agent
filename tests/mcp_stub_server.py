"""A tiny MCP server over stdio, used by test_connectors to exercise the generic MCP connector."""
import json

from mcp.server.mcpserver import MCPServer

server = MCPServer("stub-erp")

INVOICES = {
    "C-1": [
        {"InvoiceNo": "INV-1", "Total": 1200.0, "Status": "OPEN", "DueDate": "2026-01-15", "Created": "2025-12-15"},
        {"InvoiceNo": "INV-2", "Total": 800.0, "Status": "PAID", "DueDate": "2025-11-15", "Created": "2025-10-15"},
    ],
}


@server.tool()
def query_invoices(customer: str) -> str:
    """Invoices for one customer."""
    return "Query results:\n\n" + json.dumps({"items": INVOICES.get(customer, [])})


if __name__ == "__main__":
    server.run()
