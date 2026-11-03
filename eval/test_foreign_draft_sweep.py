"""旧隐私套话草稿：只有套话才删，带手册或编号清单的留下。"""
import os

from agent.foreign_draft_sweep import ensure_trust_store, is_foreign_privacy_reply


_BOILER = """
Dear customer,

Thanks for your email.

Since your feedback involves technical details, we need to collect your Country/region, Brand and Vehicle identification number to better solve the issue.

Your personal data will be used to monitor the status of your vehicle over the next several days and deleted right after our use, your personal data for after-sale troubleshooting will not be sold, neither retained.
For more detail, you can find in our privacy notice as attached.
And your consent to data processing for the purpose of the troubleshooting shall be regarded as granted, once your personal data for troubleshooting have been received,
You can raise your complaint on Personal Data Rights to the following mail,
Privacyoffice@ismart-service.com

Thank you!

Best regards,

ismart-service team
"""


def test_trust_store_includes_certs(tmp_path):
    bundle = ensure_trust_store(tmp_path / "ca-bundle.pem")
    text = bundle.read_text()
    assert "BEGIN CERTIFICATE" in text
    assert os.environ["REQUESTS_CA_BUNDLE"] == str(bundle)
    assert os.environ["SSL_CERT_FILE"] == str(bundle)


def test_boilerplate_only_is_foreign():
    assert is_foreign_privacy_reply(_BOILER)


def test_manual_steps_are_kept():
    text = (
        "Dear customer, Thank you for your email. "
        "The user manual is available within the iSMART App. "
        "Please follow the steps below to access it: "
        "1. Open the iSMART App 2. Go to My Profile. "
        "Your personal data will be used to monitor the status of your vehicle "
        "over the next several days and deleted right after our use, "
        "your personal data for after-sale troubleshooting will not be sold, neither retained. "
        "Please provide your Country/region, Brand and Vehicle identification number "
        "for further assistant."
    )
    assert not is_foreign_privacy_reply(text)


def test_numbered_intake_is_kept():
    text = (
        "Dear Mr./Ms. Pat, Thank you for your email. "
        "As your feedback involves technical details, we kindly request the following "
        "information to better assist you and resolve the issue: "
        "1. Country/region 2. Brand 3. Vehicle Model 4. VIN "
        "5. Mobile Phone Model 6. iSMART App Version 7. Detailed Description. "
        "Your personal data will be used to monitor the status of your vehicle "
        "over the next several days."
    )
    assert not is_foreign_privacy_reply(text)
