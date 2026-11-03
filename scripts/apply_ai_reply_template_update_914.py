#!/usr/bin/env python3
"""把《AI回复模版更新9-14.xlsx》金稿写进 20260914 全量知识库。不调接口。"""
from __future__ import annotations

from copy import copy
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
KB = ROOT / "iSMART自动回复知识库_全量模板_20260914.xlsx"

SVC_001 = """Dear Customer,

Thank you for your email.

As service reminder functions may vary depending on the market, vehicle specification, model year, and software version, we recommend contacting your local MG dealer to confirm whether your vehicle supports displaying the remaining service mileage and/or remaining service days on the instrument cluster or infotainment system.

For remaining manufacturer warranty or an existing service plan, especially if the vehicle was purchased second-hand, please also contact your local MG dealer. The dealer can check the vehicle records using the VIN and advise whether any warranty remains and whether a service plan is associated with the vehicle.

They will be able to provide the most accurate information based on your vehicle configuration.

Best regards,
iSMART Service Team"""

PRIV_002 = """Dear Customer,

Thank you for your email.

We have received your feedback regarding the repeated Privacy Notice messages.

To help us investigate further, please provide:
Screenshots showing the Privacy Notice message and the time when it appeared.
A short video showing that the Privacy Notice has been accepted on the vehicle infotainment system.
Your iSMART App version.

Once we receive the above information, we will review the related vehicle communication records and continue our investigation.

Best regards,
iSMART Service Team"""

MAP_001 = """Dear Customer,

Thank you for your email.

We understand that you would like to use a specific language for infotainment navigation voice guidance, such as Italian.

Please kindly note that the navigation voice guidance currently supports English, French, German, and Spanish. Italian voice guidance is not currently available. Other requested languages that are not in this list are also not currently available.

We have recorded your feedback regarding additional navigation voice language support. If this function is improved or added in the future, it will be made available through future system updates where applicable.

Thank you for your understanding.

Best regards,
iSMART Service Team"""

NET_PLAN = """Dear Customer,

Thank you for your email.

To help us investigate the issue further, could you please provide:
A photo showing the 3G signal displayed on your vehicle screen.
A screenshot of the iSMART App when you encounter the message or situation that you can no longer connect to your car.
A detailed description of what is not working.

The more details you can provide, the faster we can identify the cause of the issue.

Best regards,
iSMART Service Team"""

APP_VPN = """Dear Customer,

Thank you for your feedback.

In some cases, VPN connections may affect communication between the app and the server, which could result in certain functions not working as expected. Testing the app without a VPN connection will help us determine whether this is related to the issue you are experiencing.

If the problem persists after disabling the VPN, please let us know and provide any screenshots of the error message or affected page, and we will be happy to investigate further.

Thank you for your cooperation and understanding.

Best regards,
iSMART Service Team"""

BIND = """Dear Customer,

Thank you for your email.

Please refer to the vehicle binding instructions provided in the attachment and try to bind your vehicle again.

Before starting the binding process, please make sure that the date, time and time zone on both your mobile phone and the vehicle infotainment system are correct and synchronized. Also, please ensure that your mobile phone has a stable network connection during the process.

If the issue still persists, please provide the following information so that we can check further:

- Country/Region
- Brand
- Vehicle model
- Complete 17-character VIN
- iSMART App version
- Mobile phone model and operating system version
- A screenshot or short video showing the binding process and the error message

Best regards,
iSMART Service Team"""

ACC_002 = """Dear Customer,

Thank you for your email.

You can reset your password from the login page by selecting “Forgot Password”, then following the verification steps to set a new password.

Please note that the password should be 8–16 characters and contain letters and numbers. Special characters such as “%” are not supported.

If the reset process still does not complete, please provide screenshots or a short screen recording showing the full password reset process and the error message.

Best regards,
iSMART Service Team"""


def _copy_style(src, dst) -> None:
    if src.has_style:
        dst.font = copy(src.font)
        dst.border = copy(src.border)
        dst.fill = copy(src.fill)
        dst.number_format = src.number_format
        dst.protection = copy(src.protection)
        dst.alignment = copy(src.alignment)


def _find_id_row(ws, tid: str) -> int | None:
    for r in range(2, ws.max_row + 1):
        if str(ws.cell(r, 1).value or "").strip() == tid:
            return r
    return None


def _upsert(ws, *, row: int | None, after_tid: str, values: list) -> None:
    if row is None:
        anchor = _find_id_row(ws, after_tid) or ws.max_row
        ws.insert_rows(anchor + 1)
        row = anchor + 1
        for c in range(1, 14):
            _copy_style(ws.cell(anchor, c), ws.cell(row, c))
    for i, v in enumerate(values, 1):
        ws.cell(row, i).value = v


def main() -> None:
    wb = openpyxl.load_workbook(KB)
    ws = wb["自动回复知识库"]

    r = _find_id_row(ws, "GEN-001")
    if r:
        ws.cell(r, 4).value = (
            "客户描述过于笼统，无法判断问题；需要基础信息后再分类。"
            "自动回复不查询后台接口。"
        )
        ws.cell(r, 9).value = "等待客户补充信息后再分类；不查后台"

    r = _find_id_row(ws, "ACC-002")
    if r:
        ws.cell(r, 5).value = (
            "forgot password, forgot login, reset password, verification code, "
            "password wrong, cannot reset, expired email, 忘记密码, forgot lonin"
        )
        ws.cell(r, 7).value = "screenshot/video if reset fails"
        ws.cell(r, 8).value = ACC_002
        ws.cell(r, 9).value = "可直接回复；重置失败再要录屏。不查后台、不补齐 8 项"

    for tid in ("BIND-003", "APP-GUIDE-001"):
        r = _find_id_row(ws, tid)
        if r:
            ws.cell(r, 5).value = (
                "how to bind, binding tutorial, connect the car to the iSMART app, "
                "how can I proceed, bind vehicle, 不会绑车"
            )
            ws.cell(r, 8).value = BIND
            ws.cell(r, 9).value = "需要贴上pdf作为附件"

    r = _find_id_row(ws, "SVC-001")
    if r:
        ws.cell(r, 4).value = (
            "保养提醒标识/剩余保养里程或天数；保养记录、保修状态、二手车保修过户、"
            "服务计划。引导当地经销商，自动回复不查保修/计划接口。"
        )
        ws.cell(r, 5).value = (
            "service history, warranty, maintenance, service record, "
            "maintenance alert, service reminder, km remaining, days remaining, "
            "second-hand, service plan, outstanding service plan, 7-year warranty"
        )
        ws.cell(r, 7).value = "none"
        ws.cell(r, 8).value = SVC_001
        ws.cell(r, 9).value = "引导当地经销商用 VIN 查询；本邮箱不查后台"

    r = _find_id_row(ws, "FICM-PRIV-002")
    if r:
        ws.cell(r, 5).value = (
            "privacy accepted declined, repeated privacy notification, "
            "privacy needs to be agreed, Privacy Notice, 隐私协议"
        )
        ws.cell(r, 7).value = "screenshots with time; video of accepting on vehicle; iSMART App version"
        ws.cell(r, 8).value = PRIV_002
        ws.cell(r, 9).value = "坐席复核通信记录；自动回复不先查接口"

    r = _find_id_row(ws, "MAP-001")
    if r:
        ws.cell(r, 8).value = MAP_001

    _upsert(
        ws,
        row=_find_id_row(ws, "NET-PLAN-001"),
        after_tid="NET-001",
        values=[
            "NET-PLAN-001",
            "iSMART平台",
            "iSMART-流量套餐",
            "车机停在 3G / 无法连车 / 询问如何重启 iSMART 套餐。不报到期日或价格，不查套餐接口。",
            "3G, reactivate iSmart plan, iSMART plan, no longer connect to my car, screen remains in 3G",
            "可自动回复-补信息",
            "photo of 3G on vehicle screen; App screenshot; description of what fails",
            NET_PLAN,
            "等客户补截图后由坐席判断；自动回复不查套餐状态",
            "客户已提供截图仍无法判断 / 涉及计费争议",
            "不要报套餐到期日、价格或自动续费金额",
            "明确规则",
            None,
        ],
    )
    _upsert(
        ws,
        row=_find_id_row(ws, "APP-VPN-001"),
        after_tid="NET-PLAN-001",
        values=[
            "APP-VPN-001",
            "App功能",
            "App-账号管理相关",
            "客户要求去掉 VPN 限制。有专属行：请客户关闭 VPN 再试 App，不套 NET-001 热点/月初流量。",
            "VPN, remove this restriction, VPN again, disable VPN, VPN restriction",
            "可自动回复",
            "screenshot if still failing after VPN off",
            APP_VPN,
            "先请客户关 VPN 复测",
            "关闭 VPN 后仍失败且持续投诉",
            "不要套 NET-001 热点/月初流量话术",
            "明确规则",
            None,
        ],
    )

    wb.save(KB)
    print("updated", KB.name)


if __name__ == "__main__":
    main()
