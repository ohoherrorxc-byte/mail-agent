"""
海外车辆售后问题邮件回复 Agent — M1 PoC 子包。

对外只暴露 EmailAgent 和 AzureLLMClient 这两个入口，其它都是实现细节。
"""

from .azure_llm import AzureLLMClient
from .email_agent import EmailAgent

__all__ = ["AzureLLMClient", "EmailAgent"]
