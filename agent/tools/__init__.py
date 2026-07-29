"""
M1 阶段：所有"外部依赖"都是本地 mock。

M2 起会换成真正的 MCP 服务器（mcp-email / mcp-crm / mcp-rag），
那时这些 mock 类的方法签名要保持稳定，让 EmailAgent 无需改动一行。
"""

from .mock_crm import MockCustomerCRM
from .mock_rag import MockKnowledgeBase

__all__ = ["MockCustomerCRM", "MockKnowledgeBase"]
