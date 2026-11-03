"""从邮件正文抽 VIN / 品牌 / 已知问题类型，并与已有 Outlook category 合并。

规则（用户约定）：
  - 三类标签：问题类型、VIN、品牌。没有对应信息 → 这一维不处理。
  - 已有同类标签且值正确 → 保留。
  - VIN / 品牌值不对 → 再打一条正确的，不删旧标签。
  - 没有人工红标时，「问题:」按完整最新一段选定，再核对一遍；对不上整段就换掉或去掉。
  - 有人工红标时，「问题:」改成和红标同字。
"""
from __future__ import annotations

import re
from typing import Optional

VIN_PREFIX = "VIN:"
BRAND_PREFIX = "品牌:"
ISSUE_PREFIX = "问题:"

_VIN_RE = re.compile(r"\b([A-HJ-NPR-Z0-9]{17})\b")

# 网页表单 / 多语言字段名
_BRAND_FIELD_RE = re.compile(
    r"(?:^|\n)\s*(?:[•·\-\*]?\s*)?"
    r"(?:Brand|Marque|Marca|Marke|Mærke|Märke|Merkki|Značka|מותג)\s*[:：]\s*(.+)",
    re.I,
)

_KNOWN_BRANDS = {
    "mg": "MG",
    "mg motor": "MG",
    "morris garages": "MG",
    "roewe": "Roewe",
    "荣威": "Roewe",
    "riddara": "Riddara",
    "maxus": "Maxus",
}

# 车型暗示品牌；不靠正文里单独的 "MG iSMART" 打品牌。
_MODEL_IMPLIES_BRAND = (
    (re.compile(r"\bMG\s*[345]\b", re.I), "MG"),
    (re.compile(r"\bMG4\b", re.I), "MG"),
    (re.compile(r"\bZS\s*EV\b", re.I), "MG"),
    (re.compile(r"\bMG\s*ZS\b", re.I), "MG"),
    (re.compile(r"\bMG\s*HS\b", re.I), "MG"),
    (re.compile(r"\bMarvel\s*R\b", re.I), "MG"),
    (re.compile(r"\bCyberster\b", re.I), "MG"),
)

# 不参与正文打分：补信息 / 关闭类，不是客户问题类型。
_SKIP_L2_SCORE = frozenset({
    "请求完整车辆信息", "请求VIN/提示VIN位置", "VIN不正确",
    "请求视频/截图", "后台分析中", "关闭-已解决", "关闭-无效邮件",
    # 引导 PDF 是回复形式，不是客户问题类型。
    "App-引导手册",
})

# 知识库二级分类。AUTO: 不当真值。人工光杆 L2 是准的，问题: 应对齐它。
_KNOWN_L2 = frozenset({
    "App-账号管理相关", "App-绑车解绑相关", "App-车控车态相关",
    "App-消息中心相关", "App-软件更新", "App-预约充电相关",
    "FICM-激活相关", "FICM-CPAA相关", "FICM-软件更新", "FICM-隐私协议相关",
    "FICM-地图相关", "FICM-网络相关", "FICM-蓝牙相关", "FICM-Online Service相关",
    "FICM-Voice Service相关", "FICM-Radio相关",
    "非iSMART-充电相关", "非iSMART-功能咨询", "非iSMART-车身控制相关",
    "iSMART-数字钥匙", "APP-蓝牙钥匙", "iSMART-流量套餐", "iSMART-功能咨询",
    "iSMART-支持车区域", "TSP-敏感数据要求",
    "Adas-ACC", "Adas-AEB", "Adas-LKA", "Adas-TJA",
})

# 8.20–9.10 坐席光杆问题标（准）。
_HUMAN_ISSUE_L2 = frozenset({
    "App-绑车解绑相关", "iSMART-支持区域", "App-车控车态相关",
    "非iSMART-售前售后服务", "App-账号管理相关", "非iSMART-车身控制相关",
    "iSMART-功能咨询", "App-蓝牙钥匙", "FICM-地图相关", "非iSMART-功能咨询",
    "App-地图相关", "iSMART-用户手册相关", "App-消息中心相关",
    "FICM-CPAA相关", "FICM-软件更新", "iSMART-解绑需求", "FICM-网络相关",
    "非iSMART-充电相关", "FICM-Online Service相关", "非iSMART-不支持车型",
    "FICM-隐私协议相关", "FICM-Radio相关", "App-引导手册", "FICM-激活相关",
    "App-优化建议", "FICM-系统设置", "非iSMART-IPK相关",
    "FICM-自动重启或黑屏", "FICM-蓝牙相关", "APP-充电管理相关",
    "非iSMART-空调相关", "App-软件更新", "iSMART-翻译相关",
    "FICM-Voice Service相关", "App-能耗统计相关", "Adas-ACC",
    "iSMART-ISA相关", "FICM-界面层级复杂", "FICM-Music相关",
    "FICM-Volume相关", "新需求-OTA升级",
    "FICM-LOGIN相关", "APP-纯抱怨",
})

# 知识库写法 → 人工常用名（问题: 用人工名，坐席不用再改）。
_ISSUE_L2_TO_HUMAN = {
    "iSMART-支持车区域": "iSMART-支持区域",
    "APP-蓝牙钥匙": "App-蓝牙钥匙",
    "FICM-自动重启": "FICM-自动重启或黑屏",
    "绑车": "App-绑车解绑相关",
    "IPK相关": "非iSMART-IPK相关",
    "App-无法记忆充电模式": "APP-充电管理相关",
    "App-预约充电相关": "APP-充电管理相关",
    # 坐席红标用蓝牙钥匙，不用知识库的「数字钥匙」。
    "iSMART-数字钥匙": "App-蓝牙钥匙",
}

_GENERIC_HUMAN_ISSUE = frozenset({"iSMART-支持区域"})


def canonicalize_issue_l2(name: str | None) -> Optional[str]:
    raw = (name or "").strip()
    if not raw:
        return None
    if raw.startswith(ISSUE_PREFIX):
        raw = raw[len(ISSUE_PREFIX):]
    elif raw.startswith("AUTO:"):
        raw = raw[5:]
    return _ISSUE_L2_TO_HUMAN.get(raw, raw)


def _is_human_issue_tag(c: str) -> bool:
    t = (c or "").strip()
    if not t or t.startswith(ISSUE_PREFIX) or t.startswith("AUTO:"):
        return False
    if t.startswith(VIN_PREFIX) or t.startswith(BRAND_PREFIX):
        return False
    return t in _HUMAN_ISSUE_L2 or t in _KNOWN_L2


def human_issue_display_names(categories: list[str] | None) -> list[str]:
    """人工光杆红标的原文，保持原顺序。问题: 必须和这些字一样。"""
    found: list[str] = []
    seen: set[str] = set()
    for raw in categories or []:
        name = (raw or "").strip()
        if not _is_human_issue_tag(name) or name in seen:
            continue
        seen.add(name)
        found.append(name)
    return found


def align_issue_tags_to_red(categories: list[str] | None) -> list[str]:
    """有人工红标时，问题: 只保留和红标同名的那几条，去掉旧的短名或另一类。

    红标本身、VIN、品牌、AUTO:、关闭类都不动。没有红标则不改。
    """
    existing = [str(c).strip() for c in (categories or []) if str(c).strip()]
    reds = human_issue_display_names(existing)
    if not reds:
        return existing
    want = [f"{ISSUE_PREFIX}{name}" for name in reds]
    kept = [c for c in existing if not c.startswith(ISSUE_PREFIX)]
    seen = set(kept)
    for tag in want:
        if tag not in seen:
            kept.append(tag)
            seen.add(tag)
    return kept


def pick_human_issue_l2(categories: list[str] | None) -> Optional[str]:
    """已有人工光杆问题标 → 当作准的问题类型。AUTO: / 问题: 不算。"""
    found: list[str] = []
    for raw in human_issue_display_names(categories):
        if _is_human_issue_tag(raw):
            found.append(canonicalize_issue_l2(raw) or raw)
    if not found:
        return None
    specific = [n for n in found if n not in _GENERIC_HUMAN_ISSUE]
    return specific[0] if specific else found[0]

_FORM_LINE_RE = re.compile(
    r"(?im)^[^\n]{0,40}(?:"
    r"Issue Type|Type de probl[eè]me|Tipo de problema|Tipo di problema|"
    r"Brand|Marque|Marca|Paese|Country|Pays|VIN|Vehicle Identification|"
    r"Numero d.identificazione|Modello|Vehicle Model"
    r")\s*[:：].*$",
)


def _field_value(line: str) -> str:
    return re.sub(r"\s+", " ", (line or "").strip()).strip(" .;，。")


def extract_vin(body: str) -> Optional[str]:
    if not body:
        return None
    m = _VIN_RE.search(body)
    if not m:
        return None
    vin = m.group(1).upper()
    if vin.startswith("VIN_MASK"):
        return None
    return vin


def extract_brand(body: str) -> Optional[str]:
    if not body:
        return None
    m = _BRAND_FIELD_RE.search(body)
    if m:
        raw = _field_value(m.group(1))
        key = raw.lower()
        if key in _KNOWN_BRANDS:
            return _KNOWN_BRANDS[key]
        for alias, canon in _KNOWN_BRANDS.items():
            if alias in key or key.startswith(alias):
                return canon
        return None
    for rx, brand in _MODEL_IMPLIES_BRAND:
        if rx.search(body):
            return brand
    return None


def _latest_issue_text(text: str) -> str:
    from .template_reply import _latest_customer_turn, _webform_score_text

    latest = _latest_customer_turn(text or "") or (text or "")
    return _webform_score_text(latest)


_ISSUE_TAG_PICK_SYSTEM = """\
You label one iSMART after-sales email with exactly one issue category.
Read the entire latest customer message, every sentence and the form Description.
Do not stop at the first clause. Ignore the Issue Type dropdown, the privacy-policy
footer, and quoted agent replies.
Choose the category that names the actual problem. A side remark must not decide it.
Steps the customer already tried (reinstall, unbind/bind, clear cache) are not the issue.
No messages inside the app while the phone icon shows a notification badge is
App-消息中心相关. A greeting "Hello MG," is not a voice-assistant fault.
Home charging that fails while a public charger works through the charger's own app
is 非iSMART-充电相关, even if the customer also says the app cannot talk to the car.
A successful bind that then asks to contact the previous owner is App-绑车解绑相关,
even if earlier sentences mention a token or the app not opening.
Reply with one allowed name exactly, or empty when there is no concrete fault.
"""

_ISSUE_TAG_VERIFY_SYSTEM = """\
You check a proposed issue category against the whole latest customer message.
ok is true only when that category describes the whole problem, not one clause.
ok is false when another sentence is the real problem. Example: do not accept
App-车控车态相关 just because the app "cannot talk to the car" when the message
is that home charging fails and public charging works. Do not accept
App-绑车解绑相关 or App-软件更新 just because the customer already tried
unbind/bind or reinstall. Do not accept FICM-Voice Service相关 for a greeting
"Hello MG,".
If ok is false, set category to the one allowed name that fits the whole message,
or empty if none fits. If ok is true, repeat the proposed category.
"""


def _allowed_issue_names() -> list[str]:
    names: set[str] = set()
    for raw in _HUMAN_ISSUE_L2 | _KNOWN_L2:
        name = canonicalize_issue_l2(raw)
        if name and name not in _SKIP_L2_SCORE:
            names.add(name)
    return sorted(names)


def _accept_issue_name(raw: str | None) -> Optional[str]:
    name = canonicalize_issue_l2(raw)
    if not name or name in _SKIP_L2_SCORE:
        return None
    if name not in set(_allowed_issue_names()):
        return None
    return name


def semantic_issue_l2(text: str, llm) -> tuple[Optional[str], str]:
    """读完整最新一段再定类，然后用同一段全文核对一遍。

    返回 (分类或 None, 状态)。状态是 ok / none / failed。
    failed 表示模型没叫通，调用方可以退回关键词。none 表示核对后没有能盖住全文的类。
    """
    import logging

    latest = (text or "").strip()
    if not latest or llm is None:
        return None, "failed"
    allowed = _allowed_issue_names()
    user = (
        "Allowed categories:\n"
        + "\n".join(f"- {name}" for name in allowed)
        + "\n\n--- Latest customer message ---\n"
        + latest[:6000]
    )
    log = logging.getLogger(__name__)
    try:
        from .schemas import IssueTagPick, IssueTagVerify

        picked, _ = llm.invoke_json(
            schema=IssueTagPick,
            messages=[
                {"role": "system", "content": _ISSUE_TAG_PICK_SYSTEM},
                {"role": "user", "content": user},
            ],
            reasoning_effort="low",
            max_completion_tokens=2048,
            role="issue_tag",
        )
        proposed = _accept_issue_name(getattr(picked, "category", ""))
        checked, _ = llm.invoke_json(
            schema=IssueTagVerify,
            messages=[
                {"role": "system", "content": _ISSUE_TAG_VERIFY_SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"Proposed category: {proposed or '(none)'}\n\n"
                        + user
                    ),
                },
            ],
            reasoning_effort="low",
            max_completion_tokens=2048,
            role="issue_tag_verify",
        )
    except Exception as e:
        log.warning("[issue_tag] LLM failed: %s", e)
        return None, "failed"
    if not getattr(checked, "ok", False):
        corrected = _accept_issue_name(getattr(checked, "category", ""))
        if corrected and corrected != proposed:
            log.info("[issue_tag] verify corrected %s -> %s", proposed, corrected)
            return corrected, "ok"
        log.info("[issue_tag] verify rejected %s", proposed)
        return None, "none"
    final = _accept_issue_name(getattr(checked, "category", "")) or proposed
    if not final:
        return None, "none"
    return final, "ok"


def extract_known_issue_type(
    body: str,
    thread_context: str | None = None,
) -> Optional[str]:
    """按最新诉求 + 线程上下文判知识库二级分类。不信网页表单 Issue Type。"""
    parts = [body or ""]
    if thread_context:
        parts.append(thread_context)
    blob = "\n".join(parts)
    if not blob.strip():
        return None
    latest = _latest_issue_text(blob)
    hard = _hard_issue_l2(latest) or _hard_issue_l2(blob)
    if hard:
        return canonicalize_issue_l2(hard)
    extra = _tag_issue_l2_from_latest(latest)
    if extra:
        return extra
    from .template_reply import _is_infotainment_language_request
    if _is_infotainment_language_request(latest):
        return None
    return _score_issue_l2(blob)


def _tag_issue_l2_from_latest(latest: str) -> Optional[str]:
    """坐席口径：看最新一封在说什么，不被客服引用 / 表单下拉带偏。"""
    t = _without_tried_steps(latest or "").lower()
    if not t.strip():
        return None
    if re.search(
        r"(asks? me to|tells? me).{0,40}(german|deutsch|in german)|"
        r"selected language|wrong language|traduc|翻译",
        t,
    ) and not re.search(r"user manual", t):
        return "iSMART-翻译相关"
    if re.search(
        r"user manual|manual on the app|version of the user manual|"
        r"用户手册|manuale utente",
        t,
    ):
        return "iSMART-用户手册相关"
    if re.search(
        r"\b(app|ios|iphone)\s+update\b|update\s+(the\s+)?(app|ios|iphone)\b|"
        r"please for an app update|app not on (the )?home|"
        r"reinstall(ed)? the app|not showing on (the )?home screen",
        t,
    ) and not re.search(r"infotainment|ficm|head unit|ivi", t):
        return "App-软件更新"
    if re.search(
        r"scheduled charg|charging times|planificateur de charge|"
        r"charge planner|预约充电|充电计划|ladeterminerung|ladezeit",
        t,
    ):
        return "APP-充电管理相关"
    if re.search(r"preheat|vorklima|pre-heat|预热|预热座舱", t) and not re.search(
        r"charg", t
    ):
        return "iSMART-功能咨询"
    if re.search(
        r"sim or data|data in my .{0,12}(shows? )?not connected|"
        r"no (sim|data) (connection|connected)|tbox.{0,20}not connected|"
        r"\bt-?box\b.{0,40}(issue|problem|common)|"
        r"gps stopped|connection to networks? stopped",
        t,
    ):
        return "FICM-网络相关"
    if re.search(
        r"(digital(en)? (key|schl[uü]ssel)|bluetooth.?key|蓝牙钥匙).{0,80}"
        r"(start|starten|won.?t start)|"
        r"(start|starten).{0,40}(digital(en)? (key|schl))",
        t,
    ):
        return "App-蓝牙钥匙"
    if re.search(
        r"track my car|made an account to track|ex[- ]?partner.{0,40}app",
        t,
    ):
        return "App-账号管理相关"
    if re.search(
        r"radio (still )?(don.?t|does not|won.?t) come on|"
        r"new module.{0,30}radio|radio.{0,30}(not work|non funziona)",
        t,
    ):
        return "FICM-Radio相关"
    if re.search(
        r"no dealers around|no (mg )?dealer|没有经销商|kein händler",
        t,
    ):
        return "iSMART-支持区域"
    if re.search(
        r"after[\s-]?market|pl[au]g\s*[&+]?\s*play|pedal\s*(sprint|box)|"
        r"third[\s-]?party.{0,40}accessor|改装|加装|后装配件",
        t,
    ):
        return "非iSMART-功能咨询"
    if re.search(
        r"(already binded|already bound|i already bind|vehicle is bound|"
        r"car is bound).{0,160}"
        r"(will not connect|won.?t connect|not connect|remot|"
        r"no longer.{0,32}(access|control))|"
        r"app.{0,40}will not connect to the car",
        t,
    ):
        return "App-车控车态相关"
    if re.search(
        r"can.?t bind|cannot bind|unable to bind|not able to bind|"
        r"bind my (car|vehicle|mg)|koppelen|vincular|desvincular|"
        r"escanear.{0,20}(c[oó]digo|qr|barcode)|"
        r"scan.{0,25}(qr|barcode|codice)|二维码.{0,20}(识别|唔到|无法)|"
        r"car has bound|already bound|previous owner|former owner|"
        r"old owner still|unbind function",
        t,
    ):
        return "App-绑车解绑相关"
    if re.search(
        r"veicolo non a risposto|vehicle (has )?not respond|"
        r"no comunica(tion)? (con|with) (el |the )?(veh[ií]culo|car)|"
        r"sendet keine daten|no (puedo )?ver el estado|"
        r"timeout.{0,30}(doors|open close)|open close doors.{0,20}timeout",
        t,
    ):
        return "App-车控车态相关"
    return None


_QUOTE_SPLIT = re.compile(
    r"(?is)(?:^|\n)\s*(?:from:\s*customer\.care|de:\s*customer\.care|"
    r"envoi[eé] à partir|enviado a partir|"
    r"-----original|sent from outlook)"
)


def customer_issue_l2_for_audit(body: str) -> Optional[str]:
    """只看客户最新描述，不看客服引用、不看表单下拉。"""
    from .template_reply import _form_issue_description, _webform_score_text

    desc = (_form_issue_description(body or "") or "").strip()
    latest = _latest_issue_text(body or "")
    raw = desc if len(desc) >= 20 else latest
    raw = _QUOTE_SPLIT.split(raw or "", 1)[0]
    raw = _webform_score_text(raw)
    if not (raw or "").strip():
        return None
    return canonicalize_issue_l2(_hard_issue_l2(raw) or _tag_issue_l2_from_latest(raw))


_TRIED_STEP_RE = re.compile(
    r"(?is)("
    r"i\s+tried\s+everything\s*:[^.!\n]{0,320}"
    r"|i\s+(?:have\s+)?already\s+tried\s*:[^.!\n]{0,320}"
    r"|i\s+(?:have\s+)?(?:already\s+)?re-?installed\s+the\s+app[^.!\n]{0,80}"
    r"|i\s+(?:already\s+)?tried\s+(?:to\s+)?re-?install(?:ed|ing)?\s+the\s+app[^.!\n]{0,80}"
    r"|i\s+(?:already\s+)?tried\s+(?:to\s+)?un-?bind"
    r"(?:\s*/\s*bind|\s+and\s+bind(?:\s+again)?)?[^.!\n]{0,80}"
    r")"
)

_APP_MESSAGE_CENTER_RE = re.compile(
    r"(?i)("
    r"(do not|don't|dont|cannot|can't|cant|not)\s+get\s+any\s+messages?\s+in\s+the\s+app"
    r"|no\s+messages?\s+in\s+the\s+app"
    r"|notification\s+bubbles?"
    r"|(app\s+(icon|symbol)).{0,48}(badge|bubble|notification)"
    r")"
)


def _without_tried_steps(text: str) -> str:
    """去掉「已经试过」的步骤。解绑、重装写在这里时不是主诉。"""
    return _TRIED_STEP_RE.sub(" ", text or "")


def _hard_issue_l2(text: str) -> Optional[str]:
    text = _without_tried_steps(text or "")
    t = text.lower()
    from .template_reply import (
        _is_app_blocks_charging,
        _is_app_says_range_drop,
        _is_bind_ask_before_region_confirm,
        _is_non_ismart_hardware_fault,
        _is_remote_ui_stuck_updating,
    )
    if _is_remote_ui_stuck_updating(text):
        return canonicalize_issue_l2("App-车控车态相关")
    from .template_reply import _is_explicit_region_unsupported
    if _is_explicit_region_unsupported(text):
        return canonicalize_issue_l2("iSMART-支持区域")
    if _is_non_ismart_hardware_fault(text):
        return canonicalize_issue_l2("非iSMART-车身控制相关")
    if _is_bind_ask_before_region_confirm(text):
        return canonicalize_issue_l2("App-绑车解绑相关")
    if _is_app_says_range_drop(text) or _is_app_blocks_charging(text):
        return canonicalize_issue_l2("APP-充电管理相关")
    qr = bool(re.search(r"qr\s*code|qrcode|codice qr|code qr|二维码", t))
    act = bool(re.search(
        r"activat|attivaz|attivar|aktivieren|激活|"
        r"not activated|non si attiv|doesn't work.{0,20}activation",
        t,
    ))
    if qr and act:
        return canonicalize_issue_l2("FICM-激活相关")
    if re.search(
        r"safetycert|safety\s*cert|security certificate|"
        r"servizio di sicurezza|security audit|avn id",
        t,
    ):
        return "FICM-激活相关"
    if re.search(
        r"(cannot|can't|unable|doesn't|does not|non (riesco|funziona)|无法).{0,50}"
        r"(activat|attivar|attivaz|激活)|"
        r"(activation|attivazione).{0,40}(doesn't|does not|fail|non funziona|无法)",
        t,
    ):
        return "FICM-激活相关"
    if re.search(
        r"attivar\w*.{0,50}(servizi|funzionalit.|online)|"
        r"(online (services|feature)|funzionalit. online).{0,40}(attivar|activat)",
        t,
    ):
        return "FICM-激活相关"
    if re.search(
        r"stops? charg|not charg|won't charg|will not charg|"
        r"ne charge|non carica|no carga|charging limit|full charge",
        t,
    ):
        from .template_reply import (
            _is_accessory_battery_fault,
            _is_app_blocks_charging,
            _is_app_says_range_drop,
            _is_charge_target_mismatch,
            _is_non_ismart_hardware_fault,
        )
        if _is_accessory_battery_fault(text) or _is_non_ismart_hardware_fault(text):
            return canonicalize_issue_l2("非iSMART-车身控制相关")
        if (
            _is_charge_target_mismatch(text)
            or _is_app_blocks_charging(text)
            or _is_app_says_range_drop(text)
        ):
            return canonicalize_issue_l2("APP-充电管理相关")
        return "非iSMART-充电相关"
    if re.search(
        r"(app|ismart).{0,24}says?.{0,80}(range|km|miles?)|"
        r"unplug.{0,64}(range|km|miles?).{0,40}(drop|reduc)",
        t,
    ):
        return canonicalize_issue_l2("APP-充电管理相关")
    if re.search(
        r"(can'?t|cannot|unable to)\s+find.{0,32}qr|"
        r"use\s+(the\s+)?vin\s+to\s+(register|bind)|"
        r"register.{0,24}on\s+the\s+(ismart\s+)?app",
        t,
    ):
        return canonicalize_issue_l2("App-绑车解绑相关")
    if re.search(r"\bcarplay\b|android auto|\bcpaa\b", t):
        from .template_reply import (
            _is_aa_infotainment_fault,
            _is_app_car_connect_allow_cpaa,
            _is_cpaa_pair_issue,
        )
        # 同封只是提到 CarPlay，主诉是 App 连不上车 → 车控。配对断开才算 CPAA。
        if _is_aa_infotainment_fault(text):
            return "FICM-CPAA相关"
        if _is_app_car_connect_allow_cpaa(text):
            return "App-车控车态相关"
        if _is_cpaa_pair_issue(text):
            return "FICM-CPAA相关"
        return "FICM-CPAA相关"
    if _APP_MESSAGE_CENTER_RE.search(text):
        return "App-消息中心相关"
    if re.search(
        r"previous owner|former owner|force unbind|unbind|"
        r"cannot bind|can't bind|qr code.{0,40}(scan|bind)|绑车|解绑|前车主",
        t,
    ) and not act:
        from .template_reply import _is_app_car_connect_fault, _is_bound_but_remote_fail
        if _is_app_car_connect_fault(text) or _is_bound_but_remote_fail(text):
            return "App-车控车态相关"
        return "App-绑车解绑相关"
    return None


def _score_issue_l2(text: str) -> Optional[str]:
    from .template_reply import (
        _RETIRED_TEMPLATE_IDS,
        _enriched_keyword_score,
        _latest_customer_turn,
        _load_enriched_json,
    )

    cleaned = _without_tried_steps(_FORM_LINE_RE.sub(" ", text))
    latest = _latest_customer_turn(cleaned)
    enriched = _load_enriched_json()
    if not enriched:
        return None
    scores: dict[str, float] = {}
    for rec in enriched.values():
        if str(rec.get("template_id") or "") in _RETIRED_TEMPLATE_IDS:
            continue
        l2 = (rec.get("l2_category") or "").strip()
        if not l2 or l2 in _SKIP_L2_SCORE or l2.startswith("新需求"):
            continue
        s_latest, _ = _enriched_keyword_score(rec, latest)
        s_full, _ = _enriched_keyword_score(rec, cleaned)
        s = s_latest * 2.0 + s_full * 0.4
        # 人工很少用「页脚隐私 / 空表单车控」当主标；无症状不抬这两桶。
        if l2 == "FICM-隐私协议相关":
            from .template_reply import _is_privacy_notice_loop
            if not _is_privacy_notice_loop(latest):
                continue
        if l2 == "FICM-Voice Service相关":
            from .template_reply import _is_hello_mg_phone_usb_voice
            if not _is_hello_mg_phone_usb_voice(latest):
                continue
        if l2 == "App-车控车态相关":
            from .template_reply import _is_app_car_connect_fault
            if not _is_app_car_connect_fault(latest):
                s *= 0.25
        if s > scores.get(l2, 0):
            scores[l2] = s
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    if not ranked or ranked[0][1] < 8.0:
        return None
    if len(ranked) > 1 and ranked[0][1] < ranked[1][1] + 3.0:
        return None
    return canonicalize_issue_l2(ranked[0][0])


def issue_l2_from_template(tmpl: Optional[dict]) -> Optional[str]:
    """草稿用的知识库行二级分类。GEN / 关闭类不算问题类型。"""
    if not tmpl:
        return None
    tid = str(tmpl.get("template_id") or "")
    if tid.startswith("GEN-"):
        return None
    l2 = (tmpl.get("l2_category") or tmpl.get("category") or "").strip()
    if not l2 or l2 in _SKIP_L2_SCORE:
        return None
    return canonicalize_issue_l2(l2)


def extract_mail_facts(
    body: str,
    thread_context: str | None = None,
    extra_text: str | None = None,
) -> dict[str, Optional[str]]:
    blob = "\n".join(p for p in (body or "", extra_text or "") if p)
    return {
        "vin": extract_vin(blob),
        "brand": extract_brand(blob),
        "issue": extract_known_issue_type(blob, thread_context),
    }


def vin_tag(vin: str) -> str:
    return f"{VIN_PREFIX}{vin}"


def brand_tag(brand: str) -> str:
    return f"{BRAND_PREFIX}{brand}"


def issue_tag(issue: str) -> str:
    """AI 问题标签带「问题:」前缀（淡紫）；人工光杆同名保持红色不动。"""
    name = canonicalize_issue_l2(issue) or (issue or "").strip()
    if not name:
        return ""
    if name.startswith(ISSUE_PREFIX):
        return name
    return f"{ISSUE_PREFIX}{name}"


def _parse_existing(categories: list[str]) -> dict[str, list[str]]:
    """把已有 category 按维归类（同一维可有多条，含历史错标）。"""
    out: dict[str, list[str]] = {
        "vin": [], "brand": [], "issue": [], "issue_ours": [],
    }
    for raw in categories or []:
        c = (raw or "").strip()
        if not c:
            continue
        if c.startswith(VIN_PREFIX):
            out["vin"].append(c[len(VIN_PREFIX):].upper())
        elif _VIN_RE.fullmatch(c):
            out["vin"].append(c.upper())
        elif c.startswith(BRAND_PREFIX):
            out["brand"].append(c[len(BRAND_PREFIX):])
        elif c.lower() in {b.lower() for b in _KNOWN_BRANDS.values()}:
            out["brand"].append(c)
        elif c.startswith(ISSUE_PREFIX):
            name = canonicalize_issue_l2(c[len(ISSUE_PREFIX):]) or c[len(ISSUE_PREFIX):]
            out["issue"].append(name)
            out["issue_ours"].append(name)  # 仅「问题:」算 AI 已打
        elif _is_human_issue_tag(c):
            # 人工光杆只作对齐真值，不算 AI 已打——仍可再打「问题:」紫标
            name = canonicalize_issue_l2(c) or c
            out["issue"].append(name)
    return out


def _norm(s: str) -> str:
    return (s or "").strip().casefold()


def merge_fact_tags(
    existing: list[str],
    facts: dict[str, Optional[str]],
) -> tuple[list[str], list[dict]]:
    """返回 (新 category 列表, 每维动作)。

    动作 kind: keep | add | skip_no_info
    """
    existing = [c for c in (existing or []) if (c or "").strip()]
    parsed = _parse_existing(existing)
    actions: list[dict] = []
    to_add: list[str] = []

    def handle(dim: str, value: Optional[str], make_tag) -> None:
        if not value:
            actions.append({"dim": dim, "kind": "skip_no_info", "value": None})
            return
        already = parsed[dim]
        want = canonicalize_issue_l2(value) if dim == "issue" else value
        if not want:
            actions.append({"dim": dim, "kind": "skip_no_info", "value": None})
            return
        ours = parsed.get("issue_ours") if dim == "issue" else already
        if any(_norm(a) == _norm(want) for a in (ours or [])):
            actions.append({"dim": dim, "kind": "keep", "value": want})
            return
        tag = make_tag(want)
        to_add.append(tag)
        kind = "add_correction" if (ours if dim == "issue" else already) else "add"
        actions.append({
            "dim": dim, "kind": kind, "value": want,
            "existing": list(already), "tag": tag,
        })

    handle("issue", facts.get("issue"), issue_tag)
    handle("vin", facts.get("vin"), vin_tag)
    handle("brand", facts.get("brand"), brand_tag)

    merged = list(existing)
    seen = {_norm(x) for x in merged}
    for tag in to_add:
        if _norm(tag) not in seen:
            merged.append(tag)
            seen.add(_norm(tag))
    return merged, actions


def thread_msgs_to_context(thread_msgs: list[dict] | None) -> str:
    if not thread_msgs:
        return ""
    parts = []
    for m in thread_msgs[-10:]:
        parts.append(m.get("body_text") or "")
    return "\n".join(parts)


def _apply_template_or_keyword_issue(
    facts: dict,
    issue_l2: str | None,
    extracted: Optional[str],
) -> None:
    """模型没叫通时的退路：模板二级分类，再用关键词。"""
    override = (issue_l2 or "").strip()
    if override and override not in _SKIP_L2_SCORE:
        facts["issue"] = canonicalize_issue_l2(override)
        if (
            facts["issue"] == "App-车控车态相关"
            and extracted == "App-绑车解绑相关"
        ):
            facts["issue"] = extracted
        if extracted == "iSMART-支持区域":
            facts["issue"] = extracted
        if (
            facts["issue"] == "FICM-CPAA相关"
            and extracted == "App-车控车态相关"
        ):
            facts["issue"] = extracted
    elif extracted:
        facts["issue"] = extracted
    else:
        facts["issue"] = None


def _replace_ai_issue_tags(categories: list[str], issue: str | None) -> list[str]:
    """只改 AI 的「问题:」。红标、VIN、品牌、AUTO: 留着。"""
    kept = [c for c in categories if not str(c).startswith("问题:")]
    name = (issue or "").strip()
    if name:
        kept.append(f"问题:{name}")
    return kept


_CLOSED_INVALID = ("关闭-无效邮件", "关闭:无效邮件")


def _skip_issue_classification(categories: list[str], classify_issue: bool) -> bool:
    """重复关单已经或即将标成关闭-无效邮件时，不再做问题分类。"""
    if not classify_issue:
        return True
    return any(c in _CLOSED_INVALID for c in categories)


def push_fact_tags(
    g,
    mail_id: str,
    body: str,
    *,
    thread_context: str | None = None,
    extra_text: str | None = None,
    issue_l2: str | None = None,
    llm=None,
    classify_issue: bool = True,
) -> dict:
    """把 VIN / 品牌 / 问题类型合并进 Outlook categories。失败不抛给主流程。

    extra_text：附件抽取的未脱敏正文（Word/PDF/截图 OCR），VIN 常只在附件里。
    有人工红标时，问题: 改成和红标同字。
    已有「关闭-无效邮件」，或调用方标明这封是重复关单（classify_issue=False）时，不打、不改「问题:」。
    没有红标且给了 llm 时，先读完整最新一段再定类，然后用同一段核对；
    核对不过就改成能盖住全文的类，盖不住就不留只对上一句的「问题:」。
    模型没叫通时，才退回 issue_l2 / 关键词。
    """
    if g is None or not mail_id:
        return {"applied": False, "reason": "no_graph"}
    if not hasattr(g, "get_categories") or not hasattr(g, "set_categories"):
        return {"applied": False, "reason": "no_category_api"}
    try:
        existing = g.get_categories(mail_id)
        if not isinstance(existing, list):
            existing = []
        existing = [str(c) for c in existing if str(c).strip()]
    except Exception as e:
        return {"applied": False, "error": f"{type(e).__name__}: {e}"}
    facts = extract_mail_facts(body, thread_context, extra_text)
    extracted = canonicalize_issue_l2(facts.get("issue"))
    reds = human_issue_display_names(existing)
    replace_issue = False
    skip_issue = _skip_issue_classification(existing, classify_issue)
    if skip_issue:
        facts["issue"] = None
    elif reds:
        # 红标是准的。问题: 改成和红标同字，不再另加正文猜的那条。
        facts["issue"] = None
    elif llm is not None:
        blob = "\n".join(p for p in (body or "", thread_context or "") if p)
        latest = _latest_issue_text(blob)
        if extra_text and extra_text.strip():
            latest = (latest + "\n" + extra_text.strip()[:1500]).strip()
        cat, status = semantic_issue_l2(latest, llm)
        if status == "ok" and cat:
            facts["issue"] = cat
            replace_issue = True
        elif status == "none":
            facts["issue"] = None
            replace_issue = True
        else:
            _apply_template_or_keyword_issue(facts, issue_l2, extracted)
    else:
        _apply_template_or_keyword_issue(facts, issue_l2, extracted)
    merged, actions = merge_fact_tags(existing, facts)
    if not skip_issue and reds:
        merged = align_issue_tags_to_red(merged)
    elif not skip_issue and replace_issue:
        merged = _replace_ai_issue_tags(merged, facts.get("issue"))
    if merged == existing:
        return {
            "applied": False,
            "actions": actions,
            "categories": existing,
        }
    try:
        g.set_categories(mail_id, merged)
        if hasattr(g, "ensure_issue_colors_for_categories"):
            try:
                g.ensure_issue_colors_for_categories(merged)
            except Exception:
                pass
        elif hasattr(g, "ensure_issue_master_category"):
            for a in actions:
                if a.get("dim") == "issue" and a.get("tag"):
                    try:
                        g.ensure_issue_master_category(str(a["tag"]))
                    except Exception:
                        pass
        return {
            "applied": True,
            "actions": actions,
            "categories": merged,
        }
    except Exception as e:
        return {
            "applied": False,
            "error": f"{type(e).__name__}: {e}",
            "actions": actions,
        }
