"""
AzureLLMClient — 把 Azure OpenAI gpt-5.4 的"裸调用"封成一个 schema-aware client。

为什么要单独写一层而不是直接用 hello-agents.HelloAgentsLLM？

1. hello-agents 的 LLM 适配层不识别 gpt-5.4 的 reasoning_effort 参数；
   自动 cache 命中（usage.prompt_tokens_details.cached_tokens）也不透传出来。

2. 我们每个 Agent（Triage / Retrieval / Draft / Compliance / Reflection）
   几乎都要带 response_format=json_schema(strict)；HelloAgentsLLM 不会
   自动从 Pydantic model 转化这件事。

3. hello-agents 现在用的 tool 协议是 [TOOL_CALL:name:json] 文本解析，
   不是 OpenAI native function calling；M1 阶段我们不走 tools=参数，
   所以不依赖它的工具栈，M3 接 MCP 时再补 tool 适配。

暴露的方法（按使用频率）：

  client.invoke_json(schema=Model, messages=..., reasoning_effort="high")
      → Model 实例 + Usage（带 cached_tokens）

  client.stream(messages=..., reasoning_effort=...)
      → 流式生成器；专为工作台预览用。

  client.estimate_tokens(messages)
      → 用 tiktoken(o200k_base) 估算；做预算校验用。
"""

from __future__ import annotations

import os
import logging
from dataclasses import dataclass, field
from typing import (
    Any,
    Dict,
    Iterator,
    List,
    Literal,
    Optional,
    Type,
    TypeVar,
)

import tiktoken
from openai import AzureOpenAI
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

ReasoningEffort = Literal["low", "medium", "high"]

LOG = logging.getLogger(__name__)


class EmptyCompletionError(RuntimeError):
    """LLM 返回 200 但 visible content 为空——通常是 length 截断或推理 token 把预算吃光了。"""

    def __init__(self, *, role: str, finish_reason, usage, max_completion_tokens: int,
                 reasoning_effort: str) -> None:
        super().__init__(
            f"[{role}] empty completion: finish_reason={finish_reason} "
            f"max_completion_tokens={max_completion_tokens} "
            f"reasoning_effort={reasoning_effort} "
            f"usage(prompt={usage.prompt_tokens} completion={usage.completion_tokens} "
            f"reasoning={usage.reasoning_tokens} cached={usage.cached_tokens})"
        )
        self.role = role
        self.finish_reason = finish_reason
        self.usage = usage
        self.max_completion_tokens = max_completion_tokens
        self.reasoning_effort = reasoning_effort


# OpenAI/Azure 上 Structured Outputs 推荐的 json_schema 包装
def _pydantic_to_json_schema(model: Type[BaseModel]) -> Dict[str, Any]:
    """
    把 Pydantic BaseModel 转成 OpenAI Structured Outputs 要求的
    `json_schema` 子对象。

    Strict=true 比 pydantic 默认严格两件事，必须手动加：

    1. 顶层 + 所有嵌套 object 都要 `additionalProperties: false`
       （pydantic 的 ConfigDict(extra="forbid") 已经天然给顶层加上，
       但 nested dataclass / 子 model 上的额外字段不会自动带，要递归补）

    2. 每个 properties 里的字段必须出现在 `required` 数组里——
       OpenAI strict 模式不允许 "schema 里声明但允许省略" 的字段。
       pydantic 会把有 default 的字段踢出 required，要把它们再放回去。
       （模型可以输出 null/缺省，但 schema 必须声明它。）
    """
    schema = model.model_json_schema()

    def _walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                props = node["properties"]
                req = list(node.get("required", []))
                # 把所有 properties 全部加入 required
                for k in props:
                    if k not in req:
                        req.append(k)
                node["required"] = req
                node["additionalProperties"] = False
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    _walk(schema)
    # 顶层 pydantic 已经给 extra="forbid"，但兜底再声明一次
    schema.setdefault("additionalProperties", False)
    return {
        "name": schema.get("title", model.__name__),
        "schema": schema,
        "strict": True,
    }


@dataclass
class Usage:
    """把每次调用的成本/缓存关键指标统一暴露。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0           # reasoning_effort 专属；账单另算
    total_tokens: int = 0

    @property
    def cache_hit_rate(self) -> float:
        return self.cached_tokens / self.prompt_tokens if self.prompt_tokens else 0.0


@dataclass
class LLMCall:
    """一次调用的元数据，供工作台 / 审计 / 日志用。"""

    role: str                            # "triage" | "retriever" | ...
    model: str
    reasoning_effort: ReasoningEffort
    usage: Usage = field(default_factory=Usage)
    raw_text: str = ""                   # gpt-5.4 返回的原始 JSON 字符串


class AzureLLMClient:
    """
    单一封装 gpt-5.4 的 client。所有 EmailAgent 内部的 LLM 调用都走它。

    用法：

        from agent import AzureLLMClient
        from agent.schemas import TriageResult

        client = AzureLLMClient()              # 读环境变量
        result, call = client.invoke_json(
            schema=TriageResult,
            messages=[
                {"role": "system", "content": TRIAGE_SYSTEM},
                {"role": "user",   "content": email_body},
            ],
            reasoning_effort="low",
            role="triage",
        )
        print(result.intent, call.usage.cached_tokens)
    """

    def __init__(
        self,
        endpoint: Optional[str] = None,
        api_key: Optional[str] = None,
        deployment: Optional[str] = None,
        model_name: Optional[str] = None,
        api_version: Optional[str] = None,
        timeout: int = 120,
    ) -> None:
        self.endpoint = (
            endpoint or os.environ["AZURE_OPENAI_ENDPOINT"]
        ).rstrip("/")
        self.api_key = api_key or os.environ["AZURE_OPENAI_API_KEY"]
        self.deployment = deployment or os.environ["AZURE_OPENAI_DEPLOYMENT"]
        self.model_name = model_name or os.environ.get(
            "AZURE_OPENAI_MODEL_NAME", self.deployment
        )
        self.api_version = api_version or os.environ.get(
            "AZURE_OPENAI_API_VERSION", "2025-01-01-preview"
        )

        self.client = AzureOpenAI(
            api_key=self.api_key,
            api_version=self.api_version,
            azure_endpoint=self.endpoint,
            timeout=timeout,
        )

        # tiktoken o200k_base 是 gpt-5 系列默认编码
        try:
            self._enc = tiktoken.get_encoding("o200k_base")
        except KeyError:
            # 某些 tiktoken 版本还没 o200k_base；退化到 cl100k_base
            self._enc = tiktoken.get_encoding("cl100k_base")

        LOG.info(
            "AzureLLMClient ready: endpoint=%s deployment=%s api_version=%s",
            self.endpoint, self.deployment, self.api_version,
        )

    # ── 调用入口 ─────────────────────────────────────────────────────

    def invoke_json(
        self,
        *,
        schema: Type[T],
        messages: List[Dict[str, str]],
        reasoning_effort: ReasoningEffort = "medium",
        max_completion_tokens: int = 4096,
        role: str = "generic",
    ) -> tuple[T, LLMCall]:
        """
        同步调用，强制走 Structured Outputs（json_schema + strict=True），
        返回值直接是 Pydantic model 实例 + 调用元数据。

        M1 不传 tools=—— gpt-5.4 的 native function calling 等 M3 接 MCP 再启用。
        """
        resp = self.client.chat.completions.create(
            model=self.deployment,
            messages=messages,
            reasoning_effort=reasoning_effort,
            max_completion_tokens=max_completion_tokens,
            response_format={
                "type": "json_schema",
                "json_schema": _pydantic_to_json_schema(schema),
            },
        )

        choice0 = resp.choices[0]
        raw = (choice0.message.content or "").strip()
        finish_reason = getattr(choice0, "finish_reason", None)
        usage = _extract_usage(resp)

        if not raw:
            # o-series / gpt-5 在 strict json_schema + high reasoning 下，常见
            # finish_reason=='length'：reasoning 把 token 预算吃完了，visible
            # content 来不及生成。把 finish_reason + usage 抛出来，让上层决策
            # 是退档 reasoning 还是调高 max_completion_tokens。
            LOG.error(
                "[%s] empty content. finish_reason=%s usage=%s",
                role, finish_reason,
                usage,
            )
            raise EmptyCompletionError(
                role=role, finish_reason=finish_reason, usage=usage,
                max_completion_tokens=max_completion_tokens,
                reasoning_effort=reasoning_effort,
            )

        try:
            parsed = schema.model_validate_json(raw)
        except ValidationError as e:
            # strict schema 下很少见；如果发生，记原始 raw 便于排查
            LOG.error("JSON schema validation failed for role=%s: %s\nRAW:\n%s",
                      role, e, raw)
            raise

        call = LLMCall(
            role=role,
            model=self.deployment,
            reasoning_effort=reasoning_effort,
            usage=usage,
            raw_text=raw,
        )
        LOG.info(
            "[%s] reasoning=%s prompt=%d cached=%d completion=%d cache_hit=%.1f%%",
            role, reasoning_effort,
            usage.prompt_tokens, usage.cached_tokens, usage.completion_tokens,
            100.0 * usage.cache_hit_rate,
        )
        return parsed, call

    # ── M1.6: 多模态 vision 调用 ────────────────────────────────────────
    def invoke_json_with_images(
        self,
        *,
        schema: Type[T],
        text: str,
        image_bytes_list: List[bytes],
        image_mime: str = "image/png",
        reasoning_effort: ReasoningEffort = "low",
        max_completion_tokens: int = 8192,
        max_images: int = 3,
        role: str = "vision",
    ) -> tuple[T, LLMCall]:
        """
        同步调 vision（gpt-5.4 multimodal），强制走 json_schema + strict=True。

        限制：
        - max_images 默认 3，多图邮件防 token 爆炸
        - 单图大小由 caller enforce（建议 ≤4MB；下载前先按 mail.attachments[*].size 过滤）
        - image_bytes_list 限到前 max_images 张

        返回：parsed Pydantic 实例 + LLMCall（同 invoke_json）
        """
        if not image_bytes_list:
            raise ValueError("invoke_json_with_images 需至少 1 张图")

        import base64  # 延迟 import —— 多数调用方不走 vision
        content: List[Dict[str, Any]] = [{"type": "text", "text": text}]
        for img in image_bytes_list[:max_images]:
            b64 = base64.b64encode(img).decode("ascii")
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{image_mime};base64,{b64}"},
            })
        messages = [{"role": "user", "content": content}]

        # 完全复用 invoke_json 的 schema + error 处理逻辑
        resp = self.client.chat.completions.create(
            model=self.deployment,
            messages=messages,
            reasoning_effort=reasoning_effort,
            max_completion_tokens=max_completion_tokens,
            response_format={
                "type": "json_schema",
                "json_schema": _pydantic_to_json_schema(schema),
            },
        )

        choice0 = resp.choices[0]
        raw = (choice0.message.content or "").strip()
        finish_reason = getattr(choice0, "finish_reason", None)
        usage = _extract_usage(resp)

        if not raw:
            LOG.error(
                "[%s] vision empty content. finish_reason=%s usage=%s",
                role, finish_reason, usage,
            )
            raise EmptyCompletionError(
                role=role, finish_reason=finish_reason, usage=usage,
                max_completion_tokens=max_completion_tokens,
                reasoning_effort=reasoning_effort,
            )

        try:
            parsed = schema.model_validate_json(raw)
        except ValidationError as e:
            LOG.error("vision JSON schema validation failed for role=%s: %s\nRAW:\n%s",
                      role, e, raw)
            raise

        call = LLMCall(
            role=role,
            model=self.deployment,
            reasoning_effort=reasoning_effort,
            usage=usage,
            raw_text=raw,
        )
        LOG.info(
            "[%s] vision imgs=%d reasoning=%s prompt=%d completion=%d cache_hit=%.1f%%",
            role, min(len(image_bytes_list), max_images), reasoning_effort,
            usage.prompt_tokens, usage.completion_tokens,
            100.0 * usage.cache_hit_rate,
        )
        return parsed, call

    def stream(
        self,
        *,
        messages: List[Dict[str, str]],
        reasoning_effort: ReasoningEffort = "high",
        max_completion_tokens: int = 8192,
    ) -> Iterator[str]:
        """流式调用，仅返回文本片段（用于工作台实时预览）。"""
        stream = self.client.chat.completions.create(
            model=self.deployment,
            messages=messages,
            reasoning_effort=reasoning_effort,
            max_completion_tokens=max_completion_tokens,
            stream=True,
        )
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    # ── 工具 ─────────────────────────────────────────────────────────

    def estimate_tokens(self, messages: List[Dict[str, str]]) -> int:
        """上线前预算校验专用。用 o200k_base 编码。"""
        n = 0
        for msg in messages:
            n += 4   # 每个 message 固定 4 个 token overhead
            n += len(self._enc.encode(msg.get("content") or ""))
        return n


# ── helpers ────────────────────────────────────────────────────────────

def _extract_usage(resp: Any) -> Usage:
    """把 openai SDK 返回的 usage 对象拆成我们的 Usage dataclass。"""
    u = getattr(resp, "usage", None)
    if u is None:
        return Usage()

    prompt = getattr(u, "prompt_tokens", 0) or 0
    completion = getattr(u, "completion_tokens", 0) or 0
    total = getattr(u, "total_tokens", prompt + completion) or (prompt + completion)

    cached = 0
    reasoning = 0
    # Azure OpenAI 在 prompt_tokens_details / completion_tokens_details 里挂细节
    ptd = getattr(u, "prompt_tokens_details", None)
    if ptd is not None:
        cached = getattr(ptd, "cached_tokens", 0) or 0
    ctd = getattr(u, "completion_tokens_details", None)
    if ctd is not None:
        reasoning = getattr(ctd, "reasoning_tokens", 0) or 0

    return Usage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        cached_tokens=cached,
        reasoning_tokens=reasoning,
        total_tokens=total,
    )
