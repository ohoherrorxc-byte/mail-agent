"""mail_tags：VIN / 品牌 / 已知问题类型 抽取与合并。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.mail_tags import (
    extract_brand,
    extract_known_issue_type,
    extract_mail_facts,
    extract_vin,
    merge_fact_tags,
    push_fact_tags,
    canonicalize_issue_l2,
    pick_human_issue_l2,
    semantic_issue_l2,
)


def test_extract_vin_from_form():
    body = "Vehicle Identification Number: LSJWH4099PN101557\nBrand: MG\n"
    assert extract_vin(body) == "LSJWH4099PN101557"


def test_extract_vin_missing():
    assert extract_vin("please help my app") is None


def test_extract_vin_from_attachment_extra():
    body = "Please see the attached letter."
    extra = "Réclamation MG HS\nVIN: LSJWH4099PN101557\nACC / LKA failure"
    facts = extract_mail_facts(body, extra_text=extra)
    assert facts["vin"] == "LSJWH4099PN101557"
    assert facts["brand"] == "MG"


def test_extract_brand_from_field():
    body = "• Brand: MG\n• Vehicle Model: ZS EV\n"
    assert extract_brand(body) == "MG"


def test_extract_brand_from_model_not_ismart_footer():
    assert extract_brand("Please contact iSMART Service Team") is None
    assert extract_brand("I bought an MG4 Electric last month") == "MG"


def test_extract_issue_charging():
    body = (
        "Issue Type: Charging Management\n"
        "Description: The car stops charging and will not do a full charge."
    )
    assert extract_known_issue_type(body) == "非iSMART-充电相关"


def test_extract_issue_other_skipped():
    body = "Issue Type: Other\nDescription: door will not close"
    assert extract_known_issue_type(body) is None


def test_extract_issue_french_charge():
    body = (
        "Type de problème: Gestion de la charge\n"
        "La voiture ne charge plus, charging stops at 80%."
    )
    assert extract_known_issue_type(body) == "非iSMART-充电相关"


def test_profile_form_not_account():
    body = (
        "• Tipo di problema: Il mio profilo\n"
        "• Descrizione: Non riesco più ad attivare le funzionalità online "
        "del veicolo. QRCode cannot be created until ACTIVATED. "
        "safetycert e mappe errore. The activation doesn't work.\n"
    )
    assert extract_known_issue_type(body) == "FICM-激活相关"


def test_cannot_bind_not_activation_from_cs_quote():
    body = (
        "cannot bind my car to the app.\n\n"
        "From: customer.care <customer.care@ismart-service.com>\n"
        "Please activate online services, then scan the QR code.\n"
    )
    assert extract_known_issue_type(body) == "App-绑车解绑相关"


def test_app_update_ios_not_car_status():
    body = (
        "Country: Austria Brand: MG Model: MG3 Hybrid+\n"
        "I think it seems an iOS issue. Please for an APP Update with the "
        "newest iOS. The app is not on the home page after reinstall.\n"
    )
    assert extract_known_issue_type(body) == "App-软件更新"


def test_sim_data_not_cpaa():
    body = (
        "The Sim or Data in my MG4 shows not connected. Because of this "
        "navigation stopped working as well.\n"
        "I have read the latest Privacy Notice...\n"
    )
    assert extract_known_issue_type(body) == "FICM-网络相关"


def test_danilo_latest_turn_activation():
    latest = (
        "The app now asks me to add a new vehicle, but the QRCode that the "
        "vehicle must generate in order to add it to the APP, cannot be "
        "created until the vehicle is ACTIVATED. The activation doesn't work."
    )
    assert extract_known_issue_type(latest) == "FICM-激活相关"


def test_form_profile_only_no_symptoms():
    body = "Issue Type: My Profile\nBrand: MG\nVIN: LSJW74099PZ099787\n"
    assert extract_known_issue_type(body) is None


def test_merge_add_when_empty():
    facts = extract_mail_facts(
        "Brand: MG\nVIN: LSJWH4099PN101557\n"
        "The activation doesn't work and QRCode cannot be created until ACTIVATED.\n"
    )
    merged, actions = merge_fact_tags([], facts)
    kinds = {a["dim"]: a["kind"] for a in actions}
    assert kinds["vin"] == "add"
    assert kinds["brand"] == "add"
    assert kinds["issue"] == "add"
    assert "问题:FICM-激活相关" in merged
    assert "VIN:LSJWH4099PN101557" in merged
    assert "品牌:MG" in merged


def test_merge_keep_when_correct():
    facts = {"vin": "LSJWH4099PN101557", "brand": "MG", "issue": "App-账号管理相关"}
    existing = ["问题:App-账号管理相关", "VIN:LSJWH4099PN101557", "品牌:MG"]
    merged, actions = merge_fact_tags(existing, facts)
    assert merged == existing
    assert all(a["kind"] == "keep" for a in actions)


def test_merge_add_correction_keeps_wrong():
    facts = {"vin": "LSJWH4099PN101557", "brand": "MG", "issue": None}
    existing = ["VIN:AAAAAAAAAAAAAAAAA", "品牌:Roewe"]
    merged, actions = merge_fact_tags(existing, facts)
    by = {a["dim"]: a for a in actions}
    assert by["vin"]["kind"] == "add_correction"
    assert by["brand"]["kind"] == "add_correction"
    assert by["issue"]["kind"] == "skip_no_info"
    assert "VIN:AAAAAAAAAAAAAAAAA" in merged
    assert "VIN:LSJWH4099PN101557" in merged
    assert "品牌:Roewe" in merged
    assert "品牌:MG" in merged


def test_canonicalize_human_aliases():
    assert canonicalize_issue_l2("iSMART-支持车区域") == "iSMART-支持区域"
    assert canonicalize_issue_l2("APP-蓝牙钥匙") == "App-蓝牙钥匙"
    assert canonicalize_issue_l2("FICM-自动重启") == "FICM-自动重启或黑屏"
    assert canonicalize_issue_l2("问题:绑车") == "App-绑车解绑相关"
    assert canonicalize_issue_l2("iSMART-数字钥匙") == "App-蓝牙钥匙"


def test_pick_human_prefers_specific_over_region():
    assert pick_human_issue_l2(
        ["iSMART-支持区域", "App-绑车解绑相关"]
    ) == "App-绑车解绑相关"
    assert pick_human_issue_l2(["AUTO:App-车控车态相关"]) is None
    assert pick_human_issue_l2(["问题:App-车控车态相关"]) is None


def test_merge_learns_human_bare_issue():
    """人工光杆绑车 → 再打 问题:App-绑车解绑相关（紫），光杆红标不动。"""
    existing = ["App-绑车解绑相关"]
    # push 路径会把 issue 改成人工名；这里直接测 merge 目标已是人工名
    merged, actions = merge_fact_tags(
        existing, {"vin": None, "brand": None, "issue": "App-绑车解绑相关"},
    )
    by = {a["dim"]: a for a in actions}
    assert by["issue"]["kind"] == "add"
    assert by["issue"]["tag"] == "问题:App-绑车解绑相关"
    assert "App-绑车解绑相关" in merged
    assert "问题:App-绑车解绑相关" in merged
    assert "问题:App-车控车态相关" not in merged


def test_push_prefers_human_over_template_l2():
    class G:
        def __init__(self):
            self.cats = ["App-绑车解绑相关", "iSMART-支持区域"]
        def get_categories(self, mail_id):
            return list(self.cats)
        def set_categories(self, mail_id, categories):
            self.cats = list(categories)

    g = G()
    rec = push_fact_tags(
        g, "m1", "The app cannot connect to the car.",
        issue_l2="App-车控车态相关",
    )
    assert "App-绑车解绑相关" in g.cats
    assert "iSMART-支持区域" in g.cats
    assert "问题:App-绑车解绑相关" in g.cats
    assert "问题:iSMART-支持区域" in g.cats
    assert "App-车控车态相关" not in g.cats
    assert "问题:App-车控车态相关" not in g.cats


def test_push_replaces_mismatched_issue_with_red_text():
    class G:
        def __init__(self):
            self.cats = ["iSMART-支持区域", "问题:App-绑车解绑相关", "VIN:LSJW74099PZ099787"]
        def get_categories(self, mail_id):
            return list(self.cats)
        def set_categories(self, mail_id, categories):
            self.cats = list(categories)

    g = G()
    rec = push_fact_tags(g, "m1", "I cannot bind the car, this country is not supported.")
    assert rec["applied"] is True
    assert g.cats[0] == "iSMART-支持区域"
    assert "问题:iSMART-支持区域" in g.cats
    assert "问题:App-绑车解绑相关" not in g.cats
    assert "VIN:LSJW74099PZ099787" in g.cats


def test_push_rewrites_short_issue_alias():
    class G:
        def __init__(self):
            self.cats = ["App-绑车解绑相关", "问题:绑车"]
        def get_categories(self, mail_id):
            return list(self.cats)
        def set_categories(self, mail_id, categories):
            self.cats = list(categories)

    g = G()
    push_fact_tags(g, "m1", "hello")
    assert "App-绑车解绑相关" in g.cats
    assert "问题:App-绑车解绑相关" in g.cats
    assert "问题:绑车" not in g.cats


def test_push_region_beats_bind_template():
    class G:
        def __init__(self):
            self.cats = []
        def get_categories(self, mail_id):
            return list(self.cats)
        def set_categories(self, mail_id, categories):
            self.cats = list(categories)

    g = G()
    push_fact_tags(
        g, "m1",
        "iSMART is not supported in my country. I cannot bind the car to the app.",
        issue_l2="App-绑车解绑相关",
    )
    assert "问题:iSMART-支持区域" in g.cats
    assert "问题:App-绑车解绑相关" not in g.cats


def test_region_unsupported_not_bind_tag():
    body = (
        "iSMART is not supported in my country. "
        "I cannot bind the car to the app."
    )
    assert extract_known_issue_type(body) == "iSMART-支持区域"


def test_guide_template_is_not_an_issue_tag():
    from agent.mail_tags import issue_l2_from_template
    assert issue_l2_from_template({
        "template_id": "APP-GUIDE-001",
        "category": "App-引导手册",
    }) is None


def test_status_not_updating_is_not_app_update():
    body = "The app does not update the vehicle status. It stays on yesterday."
    assert extract_known_issue_type(body) != "App-软件更新"


def test_app_connect_with_carplay_mention_is_vehicle_status():
    body = (
        "The app doesn't connect to the car. "
        "CarPlay is installed on the phone."
    )
    assert extract_known_issue_type(body) == "App-车控车态相关"


def test_carplay_disconnect_stays_cpaa():
    body = "CarPlay disconnects every few minutes while I am driving."
    assert extract_known_issue_type(body) == "FICM-CPAA相关"


def test_push_region_beats_bind_template():
    class G:
        def __init__(self):
            self.cats = []
        def get_categories(self, mail_id):
            return list(self.cats)
        def set_categories(self, mail_id, categories):
            self.cats = list(categories)

    g = G()
    push_fact_tags(
        g, "m1",
        "iSMART is not supported in my country. I cannot bind the car to the app.",
        issue_l2="App-绑车解绑相关",
    )
    assert "问题:iSMART-支持区域" in g.cats
    assert "问题:App-绑车解绑相关" not in g.cats


def test_issue_l2_from_template_skips_gen():
    from agent.mail_tags import issue_l2_from_template
    assert issue_l2_from_template({"template_id": "GEN-001", "category": "请求完整车辆信息"}) is None
    assert issue_l2_from_template({
        "template_id": "FICM-001", "category": "FICM-软件更新",
    }) == "FICM-软件更新"


def test_push_fact_tags_template_l2_overrides_keyword():
    from agent.mail_tags import push_fact_tags

    class G:
        def __init__(self):
            self.cats = []
        def get_categories(self, mail_id):
            return list(self.cats)
        def set_categories(self, mail_id, categories):
            self.cats = list(categories)

    g = G()
    body = (
        "VIN LSJWH4099PN101557. Will the current software update also improve LKA?"
    )
    rec = push_fact_tags(g, "m1", body, issue_l2="FICM-软件更新")
    assert rec["applied"] is True
    assert "问题:FICM-软件更新" in g.cats
    assert "问题:Adas-LKA" not in g.cats
    assert "Adas-LKA" not in g.cats
    assert "FICM-软件更新" not in g.cats  # AI 不用光杆，留给人工红标


def test_push_fact_tags_merges():
    class G:
        def __init__(self):
            self.cats = ["AUTO:FICM-隐私协议相关"]
        def get_categories(self, mail_id):
            return list(self.cats)
        def set_categories(self, mail_id, categories):
            self.cats = list(categories)

    g = G()
    body = (
        "Brand: MG VIN: LSJW74099PZ099787\n"
        "QRCode cannot be created until ACTIVATED. The activation doesn't work."
    )
    rec = push_fact_tags(g, "m1", body)
    assert rec["applied"] is True
    assert "问题:FICM-激活相关" in g.cats
    assert "AUTO:FICM-隐私协议相关" in g.cats
    assert "VIN:LSJW74099PZ099787" in g.cats


def test_skip_all_when_no_info():
    merged, actions = merge_fact_tags(["关闭-无效邮件"], {"vin": None, "brand": None, "issue": None})
    assert merged == ["关闭-无效邮件"]
    assert all(a["kind"] == "skip_no_info" for a in actions)


class _G:
    def __init__(self, cats=None):
        self.cats = list(cats or [])

    def get_categories(self, mail_id):
        return list(self.cats)

    def set_categories(self, mail_id, categories):
        self.cats = list(categories)


class _ScriptedLlm:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def invoke_json(self, *, schema, messages, **kwargs):
        self.calls += 1
        payload = self.replies.pop(0)
        return schema(**payload), None


_HOME_COLUMN = (
    "App is connected to the car I cannot talk to it. "
    "Not even when recharging from a home column. "
    "Charging from a supplier column works regularly via the supplier's app."
)


def test_semantic_tag_replaces_partial():
    llm = _ScriptedLlm([
        {"category": "App-车控车态相关"},
        {"ok": False, "category": "非iSMART-充电相关", "reason": "home column fails"},
    ])
    g = _G(["问题:App-车控车态相关", "AUTO:FICM-隐私", "VIN:LSJWH4099PN101557"])
    rec = push_fact_tags(
        g, "m1", _HOME_COLUMN,
        issue_l2="App-车控车态相关",
        llm=llm,
    )
    assert rec["applied"] is True
    assert llm.calls == 2
    assert "问题:非iSMART-充电相关" in g.cats
    assert "问题:App-车控车态相关" not in g.cats
    assert "AUTO:FICM-隐私" in g.cats
    assert "VIN:LSJWH4099PN101557" in g.cats


def test_semantic_tag_rejects_partial():
    llm = _ScriptedLlm([
        {"category": "App-车控车态相关"},
        {"ok": False, "category": "", "reason": "only one clause"},
    ])
    g = _G(["问题:App-车控车态相关", "品牌:MG"])
    rec = push_fact_tags(g, "m1", _HOME_COLUMN, issue_l2="App-车控车态相关", llm=llm)
    assert rec["applied"] is True
    assert not any(c.startswith("问题:") for c in g.cats)
    assert "品牌:MG" in g.cats


def test_semantic_tag_falls_back_when_llm_fails():
    class Boom:
        def invoke_json(self, **kwargs):
            raise RuntimeError("down")

    g = _G()
    rec = push_fact_tags(
        g, "m1", _HOME_COLUMN,
        issue_l2="非iSMART-充电相关",
        llm=Boom(),
    )
    assert rec["applied"] is True
    assert "问题:非iSMART-充电相关" in g.cats


def test_semantic_tag_does_not_override_red():
    llm = _ScriptedLlm([
        {"category": "App-车控车态相关"},
        {"ok": True, "category": "App-车控车态相关", "reason": ""},
    ])
    g = _G(["非iSMART-充电相关", "问题:App-车控车态相关"])
    rec = push_fact_tags(g, "m1", _HOME_COLUMN, llm=llm)
    assert rec["applied"] is True
    assert llm.calls == 0
    assert "问题:非iSMART-充电相关" in g.cats
    assert "问题:App-车控车态相关" not in g.cats
    assert "非iSMART-充电相关" in g.cats


def test_closed_invalid_skips_issue_classification():
    llm = _ScriptedLlm([
        {"category": "App-车控车态相关"},
        {"ok": True, "category": "App-车控车态相关", "reason": ""},
    ])
    g = _G(["关闭-无效邮件", "问题:App-绑车解绑相关"])
    rec = push_fact_tags(g, "m1", _HOME_COLUMN, llm=llm, issue_l2="App-车控车态相关")
    assert rec["applied"] is False
    assert llm.calls == 0
    assert g.cats == ["关闭-无效邮件", "问题:App-绑车解绑相关"]
    g2 = _G(["VIN:LSJWH4099PN101557"])
    rec2 = push_fact_tags(
        g2, "m2", "VIN LSJWH4099PN101557. The app cannot connect to the car.",
        llm=llm, classify_issue=False,
    )
    assert llm.calls == 0
    assert not any(c.startswith("问题:") for c in g2.cats)
    assert "VIN:LSJWH4099PN101557" in g2.cats
    assert rec2["applied"] is False or "问题:" not in "".join(g2.cats)


def test_tried_steps_do_not_steal_the_issue():
    messages = (
        "Hello MG,\n"
        "i don't get any messages in the app, but i get notification bubbles "
        "on the app symbol (iphone). I tried everything: reinstall, "
        "unbind/bind vehicle, clear cache."
    )
    assert extract_known_issue_type(messages) == "App-消息中心相关"
    greeting = (
        "Hello MG, i don't get any messages in the app, but i get "
        "notification bubbles on the app symbol."
    )
    assert extract_known_issue_type(greeting) == "App-消息中心相关"
    assert extract_known_issue_type(
        "The radio does not come on. I already tried unbind/bind."
    ) == "FICM-Radio相关"
    assert extract_known_issue_type(
        "The app cannot lock the doors. I tried unbind and bind again."
    ) != "App-绑车解绑相关"
    assert extract_known_issue_type(
        "Charging from my home column does not work. "
        "I tried to unbind and bind the car."
    ) != "App-绑车解绑相关"
    assert extract_known_issue_type(
        "The app does not update the vehicle status. I reinstalled the app."
    ) != "App-软件更新"
    assert extract_known_issue_type(
        "Remote control failed. I already reinstalled the app."
    ) != "App-软件更新"
    assert extract_known_issue_type(
        "Hello MG does not work. Voice recognition only works when the "
        "phone is connected via USB."
    ) == "FICM-Voice Service相关"


def test_semantic_issue_l2_keeps_verified_name():
    llm = _ScriptedLlm([
        {"category": "App-绑车解绑相关"},
        {"ok": True, "category": "App-绑车解绑相关", "reason": "whole ask"},
    ])
    cat, status = semantic_issue_l2(
        "Bind is successful. I now need to contact the previous owner. "
        "I have always owned this car from new.",
        llm,
    )
    assert status == "ok"
    assert cat == "App-绑车解绑相关"
    assert llm.calls == 2
    g = _G(["问题:App-账号管理相关", "AUTO:FICM-网络"])
    llm2 = _ScriptedLlm([
        {"category": "App-绑车解绑相关"},
        {"ok": True, "category": "App-绑车解绑相关", "reason": "whole ask"},
    ])
    rec = push_fact_tags(
        g, "m1",
        "Bind is successful. I now need to contact the previous owner. "
        "I have always owned this car from new. Earlier the app did not start.",
        issue_l2="App-账号管理相关",
        llm=llm2,
    )
    assert rec["applied"] is True
    assert "问题:App-绑车解绑相关" in g.cats
    assert "问题:App-账号管理相关" not in g.cats
    assert "AUTO:FICM-网络" in g.cats


TESTS = [
    ("VIN 从表单抽出", test_extract_vin_from_form),
    ("无 VIN 不抽", test_extract_vin_missing),
    ("附件正文抽 VIN/品牌", test_extract_vin_from_attachment_extra),
    ("品牌字段", test_extract_brand_from_field),
    ("车型暗示品牌 / 页脚不打", test_extract_brand_from_model_not_ismart_footer),
    ("充电问题类型", test_extract_issue_charging),
    ("Other 不打", test_extract_issue_other_skipped),
    ("法文充电类型", test_extract_issue_french_charge),
    ("表单 My Profile 不改账号", test_profile_form_not_account),
    ("Danilo 最新段=激活", test_danilo_latest_turn_activation),
    ("最新段绑车不被客服激活带偏", test_cannot_bind_not_activation_from_cs_quote),
    ("App 更新不是车控", test_app_update_ios_not_car_status),
    ("SIM 无网不是 CPAA", test_sim_data_not_cpaa),
    ("仅表单无症状不打问题", test_form_profile_only_no_symptoms),
    ("空标签则新增", test_merge_add_when_empty),
    ("已正确则保留", test_merge_keep_when_correct),
    ("错标再打一条", test_merge_add_correction_keeps_wrong),
    ("无信息不改已有标签", test_skip_all_when_no_info),
    ("人工别名对齐", test_canonicalize_human_aliases),
    ("人工标优先具体 L2", test_pick_human_prefers_specific_over_region),
    ("学人工光杆打问题:", test_merge_learns_human_bare_issue),
    ("有人工标不盖车控", test_push_prefers_human_over_template_l2),
    ("问题:改成红标同字", test_push_replaces_mismatched_issue_with_red_text),
    ("旧短名改成红标全名", test_push_rewrites_short_issue_alias),
    ("本区不支持不打绑车", test_region_unsupported_not_bind_tag),
    ("绑车模板不盖过区域", test_push_region_beats_bind_template),
    ("引导手册不是问题类型", test_guide_template_is_not_an_issue_tag),
    ("车况不刷新不是软件更新", test_status_not_updating_is_not_app_update),
    ("提到 CarPlay 仍是车控", test_app_connect_with_carplay_mention_is_vehicle_status),
    ("CarPlay 断开仍是 CPAA", test_carplay_disconnect_stays_cpaa),
    ("push 合并不删 AUTO", test_push_fact_tags_merges),
    ("GEN 模板不打问题类型", test_issue_l2_from_template_skips_gen),
    ("模板 L2 覆盖关键词 ADAS", test_push_fact_tags_template_l2_overrides_keyword),
    ("全文核对改掉只对上一句的类", test_semantic_tag_replaces_partial),
    ("核对不过就不留半句标签", test_semantic_tag_rejects_partial),
    ("模型失败才退回模板类别", test_semantic_tag_falls_back_when_llm_fails),
    ("有红标不再用模型改", test_semantic_tag_does_not_override_red),
    ("核对通过才留下问题标", test_semantic_issue_l2_keeps_verified_name),
    ("关闭-无效邮件不再做问题分类", test_closed_invalid_skips_issue_classification),
    ("试过的步骤不抢主诉", test_tried_steps_do_not_steal_the_issue),
]


def main() -> int:
    passed = 0
    for label, fn in TESTS:
        try:
            fn()
            passed += 1
            print(f"  ✓ {label}")
        except Exception as e:
            print(f"  ✗ {label}: {type(e).__name__}: {e}")
    print(f"{passed}/{len(TESTS)} passed")
    return 0 if passed == len(TESTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
