"""Intent tie-break: keyword hybrid should not lose to Privacy Notice footer."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.email_agent import classify_intent, load_keyword_resources
from scripts.intent_keyword_classifier import _strip_form_noise

_PRIVACY_FOOTER = """
I can read the latest Privacy Notice for more details of the data processing activities.
I can find in the Privacy Notice at https://page.soimt.com/soimt/privacy/en-GB_privacy.html?t=1&language=en-GB&themeType=1.
By sending this email, I consent to the data processing for the purpose of the troubleshooting.
I can also contact the privacy office as written in the Privacy Notice to exercise my personal data protection rights.
"""


def _classify(body: str, subject: str = "") -> dict:
    idx, cur = load_keyword_resources()
    return classify_intent(body, subject, kw_index=idx, curated=cur)


def test_strip_form_noise_drops_privacy_url():
    out = _strip_form_noise("cannot bind in Malaysia." + _PRIVACY_FOOTER)
    assert "privacy notice" not in out.lower()
    assert "themeType" not in out
    assert "malaysia" in out.lower()
    print("  ✓ 表单 Privacy 页脚被剥掉")


def test_malaysia_bind_overrides_to_region_support():
    body = (
        "Country/Region: Malaysia\n"
        "cannot bind mg MGS5 EV CKD in Malaysia kindly assist\n"
        + _PRIVACY_FOOTER
    )
    r = _classify(body, "Issues with Vehicle or iSmart App")
    assert r["should_override"], r
    assert r["override_intent"] == "ismart_region_support", r
    print("  ✓ Malaysia 绑不上 + 页脚 → region_support override")


def test_uk_previous_owner_not_region():
    body = (
        "Country/Region: United Kingdom\n"
        "I bought a used MG. Please force unbind the previous owner so I can scan the QR code."
    )
    r = _classify(body)
    assert r.get("override_intent") != "ismart_region_support", r
    print("  ✓ 英国二手车解绑不是 region_support")


def test_israel_service_not_available_is_region():
    body = (
        "Country/Region: Israel\n"
        "The QR code is scanned successfully, but during the binding process I receive "
        "the message: “The service is not available, please try again later.”"
    )
    r = _classify(body)
    assert r["should_override"] and r["override_intent"] == "ismart_region_support", r
    print("  ✓ Israel service not available → region_support")


def test_bound_security_verification_not_connectivity_override():
    body = (
        "The app binds to the car and shows location, but any remote command "
        "sends a security verification failed error (code 242)."
    )
    r = _classify(body)
    assert r.get("override_intent") != "ismart_connectivity", r
    print("  ✓ 已绑定远控失败不会被 connectivity 覆盖")


def test_android_auto_not_connectivity_override():
    body = "Country/Region: Spain\nno puedo conectar Android auto por cable"
    r = _classify(body)
    assert r.get("override_intent") != "ismart_connectivity", r
    print("  ✓ 有线 Android Auto 不会被 connectivity 覆盖")


def test_mexico_qr_overrides_to_region_support():
    body = (
        "País/Región: México\n"
        "No me permite escanear el código QR\n"
        + _PRIVACY_FOOTER
    )
    r = _classify(body)
    assert r["should_override"] and r["override_intent"] == "ismart_region_support", r
    print("  ✓ Mexico 扫码失败 → region_support")


TESTS = [
    ("剥 Privacy 页脚", test_strip_form_noise_drops_privacy_url),
    ("Malaysia 绑不上 → region", test_malaysia_bind_overrides_to_region_support),
    ("英国二手车不是 region", test_uk_previous_owner_not_region),
    ("Israel 服务不可用 → region", test_israel_service_not_available_is_region),
    ("远控 242 不是 connectivity override", test_bound_security_verification_not_connectivity_override),
    ("有线 AA 不是 connectivity override", test_android_auto_not_connectivity_override),
    ("Mexico 扫码 → region", test_mexico_qr_overrides_to_region_support),
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
