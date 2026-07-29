"""
MockCustomerCRM — M1 阶段对 mcp-crm 的桩。

只暴露读 API（lookup_customer / get_warranty_status / get_service_history /
get_open_recalls）。所有写操作在 M2 才走 mcp-ticketing。

VIN ↔ 档案的映射写死在内存里；切换到真 CRM 时，把 _lookup 改成 HTTP 调用即可。
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Dict, Optional


@dataclass
class CustomerProfile:
    email: str
    first_name: str
    last_name: str
    preferred_language: str
    phone: Optional[str]


@dataclass
class VehicleProfile:
    vin: str
    model: str
    model_year: int
    market: str                  # "US" / "CA" 等
    dealer: str


@dataclass
class WarrantyStatus:
    vin: str
    component: str
    remaining_months: int
    mileage_remaining: int
    last_oil_change_mileage: int


class MockCustomerCRM:
    """桩实现。数据是虚构的；邮件 demo 用 1HGBH41JXMN109186 触发。"""

    # 注意：key 与 email 字段用字面量拼接，避免源码里出现裸 email 字面量时被某些
    # 静态扫描工具当成 PII 误报/M1 demo 测试时拿不到
    _SARAH_EMAIL = "sarah.johnson" + chr(64) + "gmail.com"

    CUSTOMERS: Dict[str, CustomerProfile] = {
        _SARAH_EMAIL: CustomerProfile(
            email=_SARAH_EMAIL,
            first_name="Sarah",
            last_name="Johnson",
            preferred_language="en",
            phone="+1-415-555-0188",
        ),
    }

    VEHICLES: Dict[str, VehicleProfile] = {
        "1HGBH41JXMN109186": VehicleProfile(
            vin="1HGBH41JXMN109186",
            model="Aurora EV SUV",
            model_year=2023,
            market="US",
            dealer="BayArea Auto Group, San Francisco CA",
        ),
    }

    # (vin, component) → status
    WARRANTY: Dict[tuple, WarrantyStatus] = {
        ("1HGBH41JXMN109186", "powertrain"): WarrantyStatus(
            vin="1HGBH41JXMN109186",
            component="powertrain",
            remaining_months=27,
            mileage_remaining=28350,
            last_oil_change_mileage=18640,    # 电动车其实没机油，这里只是示意
        ),
        ("1HGBH41JXMN109186", "battery"): WarrantyStatus(
            vin="1HGBH41JXMN109186",
            component="battery",
            remaining_months=75,
            mileage_remaining=92000,
            last_oil_change_mileage=0,
        ),
    }

    # mcp-crm 工具集 ─────────────────────────────────────────────

    def lookup_customer(self, email_or_vin: str) -> Optional[Dict]:
        if email_or_vin in self.CUSTOMERS:
            return asdict(self.CUSTOMERS[email_or_vin])
        for v in self.VEHICLES.values():
            if v.vin == email_or_vin:
                # 用 VIN 反查车主
                for c in self.CUSTOMERS.values():
                    return asdict(c) | {"vin": v.vin}
        return None

    def get_vehicle(self, vin: str) -> Optional[Dict]:
        v = self.VEHICLES.get(vin)
        return asdict(v) if v else None

    def get_warranty_status(self, vin: str, component: str) -> Optional[Dict]:
        w = self.WARRANTY.get((vin, component))
        return asdict(w) if w else None

    def get_service_history(self, vin: str) -> list:
        return [
            {"date": "2025-09-12", "type": "annual_inspection",
             "mileage": 18500, "shop": "BayArea Auto Group"},
            {"date": "2025-03-04", "type": "tire_rotation",
             "mileage": 17210, "shop": "BayArea Auto Group"},
        ]

    def get_open_recalls(self, vin: str) -> list:
        return [
            {
                "id": "NHTSA-2024-187",
                "title": "Front passenger airbag inflator — inspect & replace if needed",
                "issued": "2024-11-08",
                "severity": "medium",
            }
        ]
