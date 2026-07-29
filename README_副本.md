# uxoxsn-OverseasVehicleEmailAgent

海外车辆售后问题邮件回复 Agent — Co-creation Project (M1 PoC)

> 一套基于 **Azure OpenAI GPT-5.4** + **hello-agents** 框架思路的
> "客户邮件 → 政策/案例检索 → 多语言草稿 → 人工复核 → 发送"
> 全流程智能体系统。

## 文档

- [开发指南.md](./开发指南.md) — 完整开发指南（架构、技术选型、模块设计、上下文工程、MCP 工具层、评测、部署、风险、迭代路线）

## 项目目标（摘要）

- 多语言（英 / 西 / 法 / 阿 / 葡）售后邮件自动理解 + 草稿生成；
- **草稿 + 人工一键发送**，外发动作强制人工审批；
- 全链路审计留痕，满足当地消费者保护法合规。

## 技术选型（摘要）

- 模型：`gpt-5.4`（Azure OpenAI 部署，单一模型按 `reasoning_effort` 切档）
  - 端点：`https://soimt-pm.cognitiveservices.azure.com/`
  - 部署名：`gpt-5.4`，模型名：`gpt-5.4`
- 范式：Triage → Context → Retrieval → Draft → Compliance → Reflection（参考 [开发指南 §3](./开发指南.md)）
- 工具：MCP 服务器集群（M2 才接真服务；M1 用本地 mock）
- 上下文：Azure OpenAI 自动 Prompt Caching + GSSC 流水线
- 输出：OpenAI Structured Outputs（`response_format=json_schema, strict=true`）
- 评估：M1 不打分；M2 起 BFCL-style + LLM-as-Judge

## 目录结构（M1）

```
uxoxsn-OverseasVehicleEmailAgent/
├── 开发指南.md                   # v0.2，模型层切到 gpt-5.4
├── README.md                     # 本文件
├── .env.example                  # Azure OpenAI 凭证模板
├── requirements.txt              # M1 最小依赖
├── agent/
│   ├── __init__.py               # 暴露 AzureLLMClient / EmailAgent
│   ├── azure_llm.py              # Azure OpenAI 封装（含 stream + cache 命中统计）
│   ├── schemas.py                # TriageResult / RetrievalResult / ComplianceResult / DraftReply
│   ├── prompts.py                # 所有 system prompt（gpt-5.4 自动 cache 的稳定前缀）
│   ├── email_agent.py            # 主循环（orchestrator）
│   └── tools/
│       ├── mock_crm.py           # M1：客户/车辆/保修的桩
│       └── mock_rag.py           # M1：政策/TSB/案例库的桩
├── scripts/
│   ├── run_demo.py               # 端到端 demo
│   └── sample_email.txt          # 一封英文保修查询样例
└── eval/                         # M2 起填种子集 + 评测脚本
    └── .gitkeep
```

## 快速启动

```bash
# 1. 复制环境变量模板
cp .env.example .env
# 然后把 AZURE_OPENAI_API_KEY / DEPLOYMENT / ENDPOINT 填进去

# 2. 装依赖（建议用 python>=3.10 虚拟环境）
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 3. 跑 demo
python scripts/run_demo.py
# 跑别的邮件：
python scripts/run_demo.py /path/to/your_email.txt
```

跑出来你会看到：
1. Triage / Retrieval / Draft / Compliance 四段 JSON
2. 草稿邮件正文（Markdown，给人工看的）
3. 每次 LLM 调用的 `prompt_tokens / completion_tokens / cached_tokens`，
   `cache_hit_rate` 直接打印

**不会发任何邮件**。草稿全部停在 `EmailAgentResult.draft`，由人工在工作台里点确认才走 `mcp-email.send_email`。

## 当前状态

- [x] v0.2 开发指南定稿（2026/07/21，模型切到 Azure OpenAI gpt-5.4）
- [x] M1 PoC 项目骨架（prompt / schema / agent / mock 工具 / demo）
- [ ] 等 Azure 凭证 → 跑 demo → 用真实 50 条样本评 intent 准确率（目标 ≥ 90%）
- [ ] M2 区域扩面 + 工作台
- [ ] M3 多 Agent / MCP 化
- [ ] M4 持续优化与托管化探索

## 关键约定（开始改代码前必读）

- **外发动作**永远经人工。`mcp-email.send_email` 强制校验 `approval_ticket_id`，没有就拒执行。
- **草稿质量**不达标的邮件不强行发——ComplianceChecker 打回 / Reflection 两次仍不通过 → 转人工，**绝不悄悄"通过"**。
- **PII 不进 LLM**：邮件正文里的信用卡/SSN/驾照号先 mask 再喂。
- **每次 LLM 调用都要留下 `cached_tokens`**：`/v1/usage` 里看不到的，要从 `resp.usage.prompt_tokens_details.cached_tokens` 自己提，做月度成本核对。

## 关联阅读

- hello-agents 文档主仓：[docs/](../docs/)
- 必读章节见 [开发指南 §附录 B](./开发指南.md)
