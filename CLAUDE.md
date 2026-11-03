# EmailAgent M1 PoC — Claude 工作守则

iSmart 海外售后邮件自动 triage / drafting / 合规检查 PoC。**结果永远送人工工作台，不主动 send。**

一封信的完整处理顺序见 [项目说明.md](项目说明.md)。联调拍板以 [决策日志.md](决策日志.md) 为准。阶段与关口以 [整体规划.md](整体规划.md) 为准；近两周日历见 [排期.md](排期.md)。现行生产技术路径见 [技术工作流.md](技术工作流.md)。

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

**Why**（用户原话）："涉及到法规的问题也人工"。红牌 / 客户主动行使 GDPR 等权利仍升人工。2026-09-15 拍板「区域限制关了」：不再仅因大区（中东/亚洲/拉美/非洲）跳过自动草稿。

### M2 已解锁：两阶段 gate（阶段 2 默认关）

**[agent/email_agent.py:region_gate_should_escalate()](agent/email_agent.py)**：

**阶段 1（硬敏感词，仍开）**：任意 region 命中 → 升人工
- `red_flags` 命中 `lawyer_letter` / `litigation_threat` / `media_cc` / `social_media_threat` / `safety_incident`
- body 含 GDPR / UK GDPR 删除请求 / 数据保护官联系 / 跨境数据传输 / subject access request 关键词

**阶段 2（region 路由，已关）**：历史上仅 `SUPPORTED_REGIONS_M2 = {North America, Europe, United Kingdom, Oceania}` 放行。2026-09-17 起 **不再按大区拦**，`M2_ENFORCE_REGION_GATE` 无效。2026-10-09 起：**选模板完全不看区域**，不因表单国家或「本国不支持」选 `REG-001`。仅应急 `M2_ROLLBACK_TO_M1=1` 回到仅 NA。2026-10-10 例外：表单 `Country/Region` 落在对照表「美洲」26 国时，固定「本区域不支持」草稿并保留转人工，见 [美洲表单国家固定回复.md](美洲表单国家固定回复.md)。

UK 识别：[agent/prompts.py:_resolve_region_key()](agent/prompts.py) 把 `region="Europe"` + `country ∈ {GB, UK, United Kingdom, England, Scotland, Wales, Northern Ireland, ...}` 重路由为 `United Kingdom`。

**M2 合规 picker**：[agent/prompts.py:pick_compliance_constraints()](agent/prompts.py) / `build_compliance_system(region, country)`：NA / EU / UK / OC 用已接条文；亚洲 / 中东 / 非洲 / 拉美用占位 ROLE + NA 硬规则，禁止编造当地法条编号。

**Region env vars**：
- `M2_ROLLBACK_TO_M1=1` —— 回退到 M1 行为（仅 NA 放行）
- `M2_ENFORCE_REGION_GATE` —— 已废弃，不恢复四区拦
- `M1_BYPASS_REGION_GATE=1` —— 旧 eval 兼容；阶段 2 已关时无效果

**Why M2 now**：[eval/runs/auto_intent_coverage.json](eval/runs/auto_intent_coverage.json) 5 万真流量里 EU/UK/Oceania 占 90%+；M1 NA-only 直接拒掉 90% 业务。

**MEA/APAC/LATAM 法规条文仍待补**（不挡草稿）：
1. 在 [agent/prompts.py](agent/prompts.py) 把 `ROLE_AND_LAWS_LATAM` / `_MEA` / `_APAC` 从占位替换为法规条款（LGPD / PIPL / APPI / PIPA / PDPA / India DPDP Act / Saudi PDPL / UAE DIFC DPL）
2. 跑全链路回归 + 抽样人工 review


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

- 改路由、模板正文或选行 → 先按 [变更规范.md](变更规范.md) 填变更单，再动代码
- 改 enum → 三同步（schemas.py + prompts.py + 真值）
- 改 prompts → 先 `--triage-only` 跑 vN 回归，再考虑 full eval
- 改 region gate → 看 M2 解锁条件是否满足
- 加新真值样本 → CSV 在 `~/Documents/private/iSmart-email-ledger/`，先脱敏再 import
- **热词锁错桶**（点名功能 ≠ 主诉）：不要只加 prompt。四步写进 [决策日志.md](决策日志.md) §1.3 —— 检测器 → 有知识库行则强制 / 无行则转人工 → 候选禁错 id + 打分 0 → `TEMPLATE_PICK_SYSTEM` 一条 + 正反例（`eval/test_xlsx_scenario_routing.py`）。已有：OTA 范围≠ADAS 检测、VPN≠NET-001、中控熄屏≠CPAA-001、副账号≠GEN-005、表单 VIN≠GEN-005（连不上/绑车 how-to/车机没网）、点名 TBox≠TBOX-001（未换件走 STATUS-003）、privacy notice denied≠STATUS-003→FICM-PRIV-002、Hello MG 只唤 USB/AA≠CPAA-001→FICM-VOICE-001、how-to/咨询≠进店检修（谷电预约、充电功率、手册、前车主、数字钥匙通知、手机配车、漆面/保养咨询）、error 47→STATUS-006、扫码无法识别→BIND-008、网络不可用 toast→STATUS-003（不是 BIND-008）、缺功能页→DK-003、App 里程→DK-004、导航换语言→MAP-005、在线地图更新失败→MAP-002（不是 FOTA-001）、色号→SVC-004、油耗抱怨→NON-009、充电管理无数据→STATUS-012、第三方家充桩/Wallbox 充不上→CHG-001（不是 STATUS-012）、欧洲服务订阅→转人工、App 胎压通知→APP-NOTIFICATION-001、PLAN-001/NET-PLAN-001 下线（3G/到期套餐转人工，不是 NET-001）、冒用/非授权强制解绑→转人工（禁 GEN-001/BIND-001）、maintenance history→SVC-001（不是剩余保养天数）、仪表排放警告→NON-004（未确认不写 App 警告）、app not responding→STATUS-003、远控/车况停在 update mode→STATUS-003（无 VIN 也不改 GEN-002）、前车主锁 GPS→确认解绑+网络刷新、车机加语言→REQ-001（不是 FICM-001）、保养/兑现计划/谁联系→SVC-001、预约设置失败≠CHG-SCHED how-to、已绑定+远控失败→STATUS-003（不是绑车/GEN-003）、辅助电瓶/仪表报警→NON-001、表单已有 VIN/车型不要再要、第三方后装件→NON-010（不是 FICM-001）、无线 AA 升级问句→FICM-001（两问合写）、未确认不支持前绑车/二维码→GEN-001（禁 REG-001）、明确本区不支持→REG-001 no_auto、app says 续航骤降/App 不让充电→STATUS-012 要截图（禁 CHG-001/002）、手册车型不匹配→MAN-001+截图、10-26 审稿：卖车从 App 移除车辆→BIND-007、改注册邮箱→ACC-005、old owner→BIND-001、找回密码和删号合写、另一设备登录和删号合写、删旧车后 car not found 要账号/视频/新 VIN、电池配置看手册、两个电池容量要截图和证明、在问有没有充电目标→CHG-SCHED（设了又跳回仍 STATUS-013）、选不了数字钥匙且要开副账号合写、英文解绑再绑合写、车况停住→STATUS-003、描述只有 unable to connect to the application→转人工、钥匙留在后备箱→NON-007、没收到绑车码→ACC-004、另一辆车未登记→BIND-007、二维码太小→BIND-008（同信 e-call 补一句经销商）、due for service→SVC-001、只说 very bad app→COMP-001。**一封信多问**：有模板的按行回；≥2 条走 `detect_issue_lanes` 合写（含 FICM-001 升级问句）；只有一条也回。没有对应行的问先不回，不要整封 `semantic_mismatch`。
- **问题标签**：人工光杆 L2 是准的、保持红色，AI **不得改色**。AI 只打 `问题:`（淡紫 preset8）；`AUTO:` 灰。有人工红标则 `问题:` 改成和红标同字，去掉对不上的旧 `问题:`。没有红标时先读完整最新一段再定类，打完用同一段核对；只对上一句的类不留。已经试过的解绑、重装不当主诉。收不到 App 消息、图标有角标用消息中心。问候 Hello MG 不当语音。引导手册不当问题类型。明确说本区不支持时用支持区域，不跟绑车。无人工标时不要用页脚打隐私、不要空表单打车控。
- **已附图**：vision 抽到截图后，草稿删「请再提供 screenshot/photo」行；车机视频、App 版本仍可要（`_strip_screenshot_asks_when_attached`）。
- **关单**：客户明确已修好 / 已经能用（already resolved、works fine、successfully re-established）或客服已回后说了 Thank you / Thanks（不限字数；同一段还有新故障或新请求则不关，包括还在说 error、或补了报错截图）、会照做（will do that）→ `关闭-已解决` + 摘转人工、**不**改 isRead（`customer_issue_resolved`）。**纯催促** never respond / 一直不回、没有新的车/App 问题 → `关闭-无效邮件` + 已读、不转人工（`pure_chase_nudge`）。**银行公证/公司 Legalization/G.E.MI. 误转发**、**合作方推广自动回执**（partnership / We'll connect soon / Unsubscribe，且没有车/App 主诉）→ `关闭-无效邮件` + 已读（`close_invalid_junk`）。**内容重复**（同客户连点或隔天再发、正文几乎一样，不限间隔；或同一 VIN 的另一个邮箱描述相同，不限时间）→ `关闭-无效邮件` + 已读，摘掉 `转人工`，不建草稿，不再做问题分类；即使第二封引用了客服回复，也不再转人工。已有 `关闭-无效邮件` 的信也不再改 `问题:`。表单描述整段比较，不截末尾 400 字。跟进里仍有故障、且不是这封重复 → 仍 `转人工`。**全库只有打「关闭-无效邮件」才改 isRead。**

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
 不 markAsRead（**唯一例外**：打 `关闭-无效邮件`）/ 不删任何邮件 / 不删任何草稿。
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
| `PATCH body 含 isRead` | 单独禁；仅与 `关闭-无效邮件` 同 PATCH 时放行 |
| URL 白名单 substring 匹配 | calendar / events / contacts 等其他 Graph 资源 |
| `_created_draft_ids` 注册表 | PATCH 任何非本进程创建的 draft / 原邮件 / 别人邮件 |
| 显式 `send_mail` / `delete_message` / `move_message` / `mark_as_read` 方法 | 永远 raise `MailboxSafetyError`（无效关单走 `apply_close_invalid`） |

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