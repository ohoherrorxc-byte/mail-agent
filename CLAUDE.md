# EmailAgent M1 PoC — Claude 工作守则

iSmart 海外售后邮件自动 triage / drafting / 合规检查 PoC。**结果永远送人工工作台，不主动 send。**

## 项目范围

- **业务**：iSmart（MG 海外售后），真实流量 5013 条脱敏样本 + 77 条客服回复模板（[iSMART售后服务邮箱回复方法_20260308.xlsx](iSMART售后服务邮箱回复方法_20260308.xlsx)）
- **技术栈**：Azure OpenAI gpt-5.4 + Pydantic v2 + Python 3.11+
- **M1 严守**：仅 NA 地区 + 英语回复 + 32 值 Intent enum（见下）
- **M2 才解锁**：其余 5 区合规规则 + 多语言回复

## 关键架构决策

### 1. Intent enum：32 值（不是 6 值）
[agent/schemas.py:18-69](agent/schemas.py#L18) 的 `Intent` Literal，共 32 值，来源是 iSMART 售后回复方法 xlsx（按"回复模式 + 工单路由"聚类）。

**Why**：v5 试过 6 值 NA 保修视角 enum（warranty_claim/recall/.../other），套到 iSmart 真流量上 80% 进 `other` → intent accuracy 35.71%。改 32 值后到 **96.43%**。

**How**：改 enum 必须三同步 —— schemas.py + prompts.py TRIAGE_SYSTEM rubric + 真值标签 [eval/samples_real_seed.json](eval/samples_real_seed.json)。

### 2. Region gate：NA-only
共享函数 [agent/email_agent.py:region_gate_should_escalate()](agent/email_agent.py) 返回 `True` 时，[agent/email_agent.py](agent/email_agent.py) 主循环抛 `NeedsHumanEscalation`，[agent/outlook_drafts.py:should_skip()](agent/outlook_drafts.py) 返回跳过原因——两条路径走同一份判断。

6 区 enum：Europe / Asia / South America / Africa / Oceania / North America。

**Why**（用户原话）："涉及到法规的问题也人工"。M1 合规规则只有 NA 套（GDPR / UK GDPR / 沙特 PCA 等都没接）。

### M2 已解锁：两阶段 gate + 4 区细路由

**[agent/email_agent.py:region_gate_should_escalate()](agent/email_agent.py)** 改两阶段：

**阶段 1（硬敏感词）**：任意 region 命中 → 升人工
- `red_flags` 命中 `lawyer_letter` / `litigation_threat` / `media_cc` / `social_media_threat` / `safety_incident`
- body 含 GDPR / UK GDPR 删除请求 / 数据保护官联系 / 跨境数据传输 / subject access request 关键词

**阶段 2（region 路由）**：`triage.region ∈ SUPPORTED_REGIONS_M2 = {North America, Europe, United Kingdom, Oceania}` 放行；其它（Asia / South America / Africa）仍升人工。

UK 识别：[agent/prompts.py:_resolve_region_key()](agent/prompts.py) 把 `region="Europe"` + `country ∈ {GB, UK, United Kingdom, England, Scotland, Wales, Northern Ireland, ...}` 重路由为 `United Kingdom`。

**M2 合规 picker**：[agent/prompts.py:pick_compliance_constraints()](agent/prompts.py) / `build_compliance_system(region, country)` 按 region 返回 `ROLE_AND_LAWS_BY_REGION` / `COMPLIANCE_CONSTRAINTS_BY_REGION` 各 4 套（NA / EU / UK / OC）。亚洲 / 非洲 / 拉美仍是占位（`raise ValueError` if called），LLM 走不到那里。

**Region env vars**（应急用）：
- `M2_ROLLBACK_TO_M1=1` —— 回退到 M1 行为（仅 NA 放行）
- `M1_BYPASS_REGION_GATE=1` —— 跳阶段 2 region 校验，仅 eval 用
- 这俩 env var 生产路径不应设置

**Why M2 now**：[eval/runs/auto_intent_coverage.json](eval/runs/auto_intent_coverage.json) 5 万真流量里 EU/UK/Oceania 占 90%+；M1 NA-only 直接拒掉 90% 业务。

**M3 解锁条件**（Asia / South America / Africa）：
1. 在 [agent/prompts.py](agent/prompts.py) 把 `ROLE_AND_LAWS_LATAM` / `_MEA` / `_APAC` 从占位替换为法规条款（LGPD / PIPL / APPI / PIPA / PDPA / India DPDP Act / Saudi PDPL）
2. `SUPPORTED_REGIONS_M2` 扩到含 `Asia` / `South America` / `Africa`
3. 跑全链路回归 + 抽样人工 review


### 3. PII 脱敏 — 客户数据绝不能进项目
**位置**：`~/Documents/private/iSmart-email-ledger/`
- 源 CSV：`售后邮件台账-2.csv`（SharePoint 导出，1458 列 schema + 5013 行）
- 脱敏后：`售后邮件台账-2.redacted.json`
- 脚本：[scripts/redact_ledger.py](scripts/redact_ledger.py)

**规则**：VIN `[A-HJ-NPR-Z0-9]{17}` → `VIN_MASK_<sha256[:8]>`；email 用基础正则但**排除** Outlook inline-image CID；phone **必须显式国际区号前缀**才动手。

**Why**（用户原话）："用户的车辆识别码不要泄漏了"。

**git 边界**（[.gitignore](.gitignore)）：`*.csv` / `*台账*` / `samples_real_seed.json` / `.env.example`（Azure OpenAI key 占位符）

### 4. 评测协议：--triage-only 模式
[eval/run_eval.py](eval/run_eval.py) 支持 `--triage-only`，跳过 region gate / retrieval / drafting / compliance。

**Why**：M1 真样本里 0 条 NA → full eval 100% 被 gate 拦下，看不到 Triage 准确率。`--triage-only` 模式下 region gate 不触发，可在 EU/UK/AP 真样本上评 intent/urgency。

**跑法**：
```bash
python3 eval/run_eval.py --samples eval/samples_real_seed.json --triage-only --out eval/runs/vN.json
```

**Full eval（retrieval/drafting/compliance 全链路）**：
- 需要 NA 样本（真样本里 0 条 NA，会全被 region gate 拦下或 retrieval 拒答）
- 合成 NA 样本见 [eval/samples_real_na.json](eval/samples_real_na.json)（5 条覆盖主流 intent）
- **重要**：跑前确认 `M1_BYPASS_REGION_GATE=1` **不要** 设置——否则会绕过 region gate，让非 NA 客户拿到 NA 草稿；合规检查还会拦
- 跑前确认 [agent/tools/mock_rag.py](agent/tools/mock_rag.py) 所有 chunk `region` 标签用 enum 全名（"North America"），不要写 "NA" 缩写——否则 filter `!=` 永远 True，NA 客户也拿 0 hit（v6_full_na 这次踩过这个坑）

**M1 eval bypass env vars**（仅用于跑 eval）：
- `M1_BYPASS_REGION_GATE=1` —— 跳过 [agent/email_agent.py:155](agent/email_agent.py#L155) 的 region 守门
- `M1_BYPASS_RAG_REGION=1` —— 跳过 [agent/tools/mock_rag.py](agent/tools/mock_rag.py) 的 region 过滤，让 NA chunks 能被非 NA 真样本命中
- 这两个 env var 仅用于在真业务数据上跑 full eval 验证整链路；**生产路径里不应被设置**

**关键数字**（triage-only，30 真样本 + 5 NA 合成）：
| 版本 | Intent enum | intent acc | escalation rate |
|---|---|---|---|
| v5 | 6 值 | 35.71% | — |
| v6 | **32 值** | **96.43%** | 3.45% (1/30 P0 红牌) |

## 红牌规则（prompt 强约束）

`red_flags` 仅在以下情况触发：
- `lawyer_letter`：律师被点名 / "my lawyer" / "cc'd counsel"
- `litigation_threat`：法院 / 起诉 / 截止日期
- `media_cc` / `social_media_threat`：记者 / 媒体 / "going public" / 社交 handle
- `safety_incident`：客户**当前**驾驶中安全失效且拒绝驾驶（既往事故不算）

普通"愤怒语气"**不是** red_flag，是 urgency 调整（一般 P2）。

ADAS 4 类（`adas_acc`/`adas_aeb`/`adas_lka`/`adas_tja`）默认 P1（安全相关）。

## 修改前必读

- 改 enum → 三同步（schemas.py + prompts.py + 真值）
- 改 prompts → 先 `--triage-only` 跑 vN 回归，再考虑 full eval
- 改 region gate → 看 M2 解锁条件是否满足
- 加新真值样本 → CSV 在 `~/Documents/private/iSmart-email-ledger/`，先脱敏再 import

## 跑命令速查

```bash
# 脱敏原始 CSV（写项目外）
python3 scripts/redact_ledger.py

# 脱敏 5 万 inbox（也写项目外）
python3 scripts/redact_operations_mail.py

# 脱敏 4.2 万 outbox（客服回复）→ 写到 jsonl
python3 scripts/redact_cs_replies.py
#   → ~/Documents/private/iSmart-email-ledger/cs_replies.redacted.jsonl
#   → eval/runs/cs_reply_coverage.json

# 评测（triage-only）
python3 eval/run_eval.py --samples eval/samples_real_seed.json --triage-only --out eval/runs/vN.json

# 看历史报告
python3 -c "import json; print(json.dumps(json.load(open('eval/runs/v6_triage.json'))['metrics'], indent=2))"
```

## 历史数据语料

**[scripts/redact_operations_mail.py](scripts/redact_operations_mail.py)**：流式读 `文件库/operations_mail.xls`（53,558 行 / 191MB），脱敏后写到 `~/Documents/private/iSmart-email-ledger/operations_mail.redacted.jsonl`（96MB）。

**[scripts/redact_cs_replies.py](scripts/redact_cs_replies.py)**：流式读 `文件库/operations_mail_outbox.csv`（42,215 行 / 195MB），切出 CS 客服回复正文（去掉 quoted history），脱敏后写到 `~/Documents/private/iSmart-email-ledger/cs_replies.redacted.jsonl`（84MB）。

**为什么 outbox 也要读**：CS 客服回复是真业务里同等"应该怎么回"的标准答——比 77 条模板覆盖的子场景多得多（24/32 Intent 类别有真实样本；EU/UK/Oceania 占绝大多数）。
- RAG 检索：见 [scripts/build_cs_chunks.py](scripts/build_cs_chunks.py) + [agent/tools/mock_rag.py](agent/tools/mock_rag.py) 的 CS chunks 接入
- 模板扩充：见 [scripts/v4_draft_templates.py](scripts/v4_draft_templates.py)
- Fine-tune Intent：客服人工打的 category 已经是 ground truth（但缺点：没有 reason/log）

### M2.5 RAG + v4 模板

**RAG chunks**（[scripts/build_cs_chunks.py](scripts/build_cs_chunks.py)）：
- 读 [scripts/redact_cs_replies.py](scripts/redact_cs_replies.py) 输出的 jsonl，按 (intent × region) 桶分层取 top N（高频 intent 60/region，中频 30，低频 10；全局上限 3000）
- 每条截取前 700 字（按 sentence boundary 切）
- 写到 `~/Documents/private/iSmart-email-ledger/cs_chunks.jsonl`
- [agent/tools/mock_rag.py](agent/tools/mock_rag.py) 懒加载，合并 policy + CS chunks；token split 兼容 `_` 让 `app_binding` → `["app","binding"]`

**v4 模板**（[scripts/v4_draft_templates.py](scripts/v4_draft_templates.py)）：
- 通过 [eval/runs/v4_template_coverage_gap.json](eval/runs/v4_template_coverage_gap.json) 识别覆盖缺口，挑 `ismart_connectivity`（0→10 模板）等优先级 Intent
- 每个 (intent, sub_scenario) 桶从中位数长度挑 4 条 CS 回复，清洗（去 quoted history / PII / 短截留 placeholder）
- 写 `iSMART售后服务邮箱回复方法_v4_templates.xlsx`（134 条去重后 **94 条**；171 总模板，v4: 94）
- [agent/template_reply.py](agent/template_reply.py) `load_xlsx()` 改用 `glob("v*_*.xlsx")` reverse-sort（新版号优先），多 token Category 用 `,` 或 `/` 分隔后逐个查 CATEGORY_TO_INTENT 取首个命中
- v4 测试 [eval/test_m1_safety_hardening.py:test_v4_templates_load_via_template_reply](eval/test_m1_safety_hardening.py) 验证 ismart_connectivity 至少 1 条 v4 模板

**不在 git 范围内**：CSV / 台账 / 脱敏后的 jsonl 都在 `.gitignore`；项目内只保留 `eval/runs/auto_intent_coverage.json` / `cs_reply_coverage.json` 报告。

## Outlook 草稿同步（M1.5 模板回复 + 在 Drafts 落草稿）

[agent/outlook_drafts.py](agent/outlook_drafts.py) 把 32 值 Intent + xlsx 模板 直接落到 Outlook Drafts 文件夹。
**邮箱地址示例**：[https://outlook.cloud.microsoft/mail/](https://outlook.cloud.microsoft/mail/)

### ⚠️ 用户原话硬约束（代码 guard 强制）

1. **不能主动给客户发送邮件，只能建草稿** —— 永远不调 sendMail；
   只调 createReply（在 Drafts 落新草稿），人工事后审 / 改 / 点 Send。
2. **不能有任何删除动作** —— 不调 DELETE / 不 move 到 Deleted Items /
   不 markAsRead / 不删任何邮件 / 不删任何草稿。原邮件 isRead 保持原状。
3. **草稿正文必须全英文** —— 不论客户用什么语言（意 / 西 / 法 / 德 / 日 / 中）写邮件，
   草稿回复一律用英文。改写 / 起草 prompt 都必须强制 `customer_lang = "en-US"`。
   原因：客服团队 / 工程师内部协作语言是英语；客户在意自己的语言，
   但 iSmart 售后体系没有对应语言的客服坐席 → 多语言回复会失维保 / 责任追踪。
4. **草稿不得向客户承诺时间 / SLA** —— 不能写"we will get back to you in 1-2 business days"、
   "our engineer will contact you within 24 hours"、"X 小时内解决" 等具体时间承诺。
   可以说"we will follow up / our team will review and get back to you /
   we are looking into this" 这类无时间锚的话术。
   原因：技术诊断周期不可控（等日志 / 等零件 / 等 4S 店反馈），给具体时间
   容易违约 → 客户投诉升级。

`GraphClient` 的 `_request()` 在 HTTP 层 + 方法层 + draft id 注册表 三道守门：

| 守门 | 挡什么 |
|---|---|
| `method in {"DELETE"}` | 所有 DELETE 调用 |
| `POST .../sendMail` | sendMail（哪怕路径匹配白名单也挡） |
| `POST .../move` | move 到其他文件夹 |
| `PATCH body 含 isRead` | 改原邮件读未读状态 |
| URL 白名单 substring 匹配 | calendar / events / contacts 等其他 Graph 资源 |
| `_created_draft_ids` 注册表 | PATCH 任何非本进程创建的 draft / 原邮件 / 别人邮件 |
| 显式 `send_mail` / `delete_message` / `move_message` / `mark_as_read` 方法 | 永远 raise `MailboxSafetyError` |

**单元测试**：[eval/test_outlook_safety.py](eval/test_outlook_safety.py) 16 case 全过（11 挡 + 5 放行）。

### 跑法

```bash
# 离线 smoke test（无 Azure AD 认证，验模板匹配 + 跳过规则）
python3 -m agent.outlook_drafts --mock-mail scripts/mock_mail_smoke.json --no-llm

# 真连 Outlook —— 需要 .env 里 AZURE_AD_TENANT_ID + AZURE_AD_CLIENT_ID
python3 -m agent.outlook_drafts --once --top 20
# 首次跑会触发 device code flow：
#   打开： https://microsoft.com/devicelogin
#   输入 code： ABC-DEF-XYZ
# 登录一次后 token 缓存在 ~/.cache/emailagent/graph_token.json
```

### 安全注意（同样要在 .env.example 里写）

- **token 文件**：`~/.cache/emailagent/graph_token.json` 含 access + refresh token。
  - 文件权限已加 `chmod 600`（目录 `chmod 700`），见 [agent/outlook_drafts.py:110-124](agent/outlook_drafts.py#L110-L124)
  - 确认 iCloud / OneDrive / Dropbox **不在同步 `~/.cache/`**
  - 笔记本被偷 = 邮箱失陷；可在 Azure Portal → App registrations → "Sign out all users" 撤销
- **scope**：`Mail.ReadWrite` 含 send 权限（虽然代码不调 sendMail）。
  M2 拆两个 app reg：`Mail.Read`（读）+ `Mail.ReadWrite`（写草稿），最小权限。
- **PII**：当前 `process_mail` 把邮件正文直送 LLM，**未调用 `redact_ledger.redact_text()`**。
  M2 第一步要补：所有进 LLM 的内容先经 [scripts/redact_ledger.py](scripts/redact_ledger.py) 脱敏。
- **Azure AD app registration 准备**（5 分钟）：
  1. Azure Portal → App registrations → New
  2. Name = "EmailAgent M1.5 Drafts"，Account types = Single tenant，Redirect URI 留空
  3. API permissions → Microsoft Graph → Delegated → `Mail.ReadWrite` + `User.Read`
  4. Authentication → "Allow public client flows" = Yes
  5. Overview → 复制 Application (client) ID 和 Directory (tenant) ID 到 `.env`