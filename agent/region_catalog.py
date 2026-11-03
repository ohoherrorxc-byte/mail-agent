"""国家 → 大区：以《国家清单-大区分类对照表.xlsx》为准。

xlsx 分区：欧洲 / 大洋洲 / 中东 / 亚洲 / 非洲 / 美洲。
映射到 pipeline Region：
  欧洲 → Europe（英国仍用 country 走 UK GDPR）
  大洋洲 → Oceania
  中东 → Middle East（M2 未接合规，升人工）
  亚洲 → Asia（不含 GCC / 以色列 / 俄罗斯）
  非洲 → Africa
  美洲 → South America（含墨西哥、加勒比；表里没有美加）
美加不在对照表，单独记 North America。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
XLSX_NAME = "国家清单-大区分类对照表.xlsx"

XLSX_ZONE_TO_REGION = {
    "欧洲": "Europe",
    "大洋洲": "Oceania",
    "中东": "Middle East",
    "亚洲": "Asia",
    "非洲": "Africa",
    "美洲": "South America",
}

# 中文名 → 英文/ISO 别名（小写）。不把 MG 当成马达加斯加、不把 NA 当成纳米比亚。
_ALIASES_BY_CN: dict[str, tuple[str, ...]] = {
    "英国": ("united kingdom", "uk", "u.k.", "gb", "great britain", "england", "scotland", "wales", "northern ireland"),
    "德国": ("germany", "deutschland", "de"),
    "法国": ("france", "fr"),
    "意大利": ("italy", "italia", "it"),
    "西班牙": ("spain", "españa", "espana", "es"),
    "荷兰": ("netherlands", "holland", "nl"),
    "比利时": ("belgium", "be"),
    "瑞典": ("sweden", "se"),
    "挪威": ("norway", "no"),
    "芬兰": ("finland", "fi"),
    "丹麦": ("denmark", "dk"),
    "奥地利": ("austria", "at"),
    "瑞士": ("switzerland", "ch"),
    "葡萄牙": ("portugal", "pt"),
    "爱尔兰": ("ireland", "ie", "republic of ireland"),
    "波兰": ("poland", "pl"),
    "捷克": ("czech", "czechia", "czech republic", "cz"),
    "匈牙利": ("hungary", "hu"),
    "斯洛伐克": ("slovakia", "sk"),
    "罗马尼亚": ("romania", "ro"),
    "保加利亚": ("bulgaria", "bg"),
    "希腊": ("greece", "gr"),
    "克罗地亚": ("croatia", "hr"),
    "斯洛文尼亚": ("slovenia", "si"),
    "塞尔维亚": ("serbia", "rs"),
    "乌克兰": ("ukraine", "ua"),
    "卢森堡": ("luxembourg", "lu"),
    "冰岛": ("iceland", "is"),
    "马耳他": ("malta", "mt"),
    "塞浦路斯": ("cyprus", "cy"),
    "北马其顿": ("north macedonia", "macedonia", "mk"),
    "波黑": ("bosnia", "bosnia and herzegovina", "ba"),
    "黑山": ("montenegro", "me"),
    "阿尔巴尼亚": ("albania", "al"),
    "法罗群岛": ("faroe", "faroe islands", "fo"),
    "拉脱维亚": ("latvia", "lv"),
    "立陶宛": ("lithuania", "lt"),
    "摩纳哥": ("monaco", "mc"),
    "摩尔多瓦": ("moldova", "md"),
    "列支敦士登": ("liechtenstein", "li"),
    "法属留尼汪": ("reunion", "réunion", "re"),
    "土耳其": ("turkey", "türkiye", "turkiye", "tr"),
    "俄罗斯": ("russia", "russian federation", "ru"),
    "白俄罗斯": ("belarus", "by"),
    "澳大利亚": ("australia", "au"),
    "新西兰": ("new zealand", "nz"),
    "波利尼西亚": ("polynesia", "french polynesia", "pf"),
    "新喀里多尼亚（法属）": ("new caledonia", "nc"),
    "斐济": ("fiji", "fj"),
    "萨摩亚": ("samoa", "ws"),
    "塔希提岛(法属)": ("tahiti",),
    "沙特阿拉伯": ("saudi arabia", "saudi", "ksa", "sa"),
    "阿联酋": ("united arab emirates", "uae", "u.a.e.", "ae"),
    "卡塔尔": ("qatar", "qa"),
    "科威特": ("kuwait", "kw"),
    "阿曼": ("oman", "om"),
    "巴林": ("bahrain", "bh"),
    "黎巴嫩": ("lebanon", "lb"),
    "伊拉克": ("iraq", "iq"),
    "约旦": ("jordan", "jo"),
    "巴勒斯坦": ("palestine", "ps"),
    "以色列": ("israel", "il"),
    "泰国": ("thailand", "th"),
    "印度尼西亚": ("indonesia", "id"),
    "马来西亚": ("malaysia", "my"),
    "菲律宾": ("philippines", "ph"),
    "越南": ("vietnam", "vn"),
    "新加坡": ("singapore", "sg"),
    "柬埔寨": ("cambodia", "kh"),
    "老挝": ("laos", "la"),
    "缅甸": ("myanmar", "burma", "mm"),
    "文莱": ("brunei", "bn"),
    "东帝汶": ("timor-leste", "east timor", "timor", "tl"),
    "印度": ("india", "in"),
    "巴基斯坦": ("pakistan", "pk"),
    "孟加拉国": ("bangladesh", "bd"),
    "斯里兰卡": ("sri lanka", "lk"),
    "尼泊尔": ("nepal", "np"),
    "不丹": ("bhutan", "bt"),
    "中国香港": ("hong kong", "hk"),
    "中国澳门": ("macau", "macao", "mo"),
    "蒙古": ("mongolia", "mn"),
    "阿塞拜疆": ("azerbaijan", "az"),
    "塔吉克斯坦": ("tajikistan", "tj"),
    "格鲁吉亚": ("georgia", "ge"),
    "南非": ("south africa", "za"),
    "肯尼亚": ("kenya", "ke"),
    "坦桑尼亚": ("tanzania", "tz"),
    "赞比亚": ("zambia", "zm"),
    "加纳": ("ghana", "gh"),
    "尼日利亚": ("nigeria", "ng"),
    "索马里兰": ("somaliland",),
    "中非共和国": ("central african republic", "cf"),
    "喀麦隆": ("cameroon", "cm"),
    "埃及": ("egypt", "eg"),
    "几内亚": ("guinea", "gn"),
    "冈比亚": ("gambia", "gm"),
    "佛得角": ("cape verde", "cabo verde", "cv"),
    "赤道几内亚": ("equatorial guinea", "gq"),
    "几内亚比绍": ("guinea-bissau", "gw"),
    "刚果": ("congo", "congo-brazzaville", "cg"),
    "塞拉利昂": ("sierra leone", "sl"),
    "马达加斯加": ("madagascar",),
    "多哥": ("togo", "tg"),
    "厄立特里亚": ("eritrea", "er"),
    "科摩罗": ("comoros", "km"),
    "科特迪瓦": ("ivory coast", "cote d'ivoire", "côte d'ivoire", "ci"),
    "索马里": ("somalia", "so"),
    "突尼斯": ("tunisia", "tn"),
    "布基纳法索": ("burkina faso", "bf"),
    "吉布提": ("djibouti", "dj"),
    "埃塞俄比亚": ("ethiopia", "et"),
    "利比亚": ("libya", "ly"),
    "毛里塔尼亚": ("mauritania", "mr"),
    "苏丹": ("sudan", "sd"),
    "西撒哈拉": ("western sahara", "eh"),
    "阿尔及利亚": ("algeria", "dz"),
    "摩洛哥": ("morocco", "ma"),
    "毛里求斯": ("mauritius", "mu"),
    "圣多美和普林西比": ("sao tome", "são tomé", "st"),
    "马约特岛(法属)": ("mayotte", "yt"),
    "博茨瓦纳": ("botswana", "bw"),
    "纳米比亚": ("namibia",),
    "莱索托": ("lesotho", "ls"),
    "斯威士兰": ("eswatini", "swaziland", "sz"),
    "塞舌尔": ("seychelles", "sc"),
    "墨西哥": ("mexico", "méxico", "mx"),
    "特立尼达和多巴哥": ("trinidad", "trinidad and tobago", "tt"),
    "库拉索": ("curacao", "curaçao", "cw"),
    "阿鲁巴": ("aruba", "aw"),
    "海地": ("haiti", "ht"),
    "巴拿马": ("panama", "pa"),
    "危地马拉": ("guatemala", "gt"),
    "牙买加": ("jamaica", "jm"),
    "多米尼加": ("dominican republic", "dominican", "do"),
    "古巴": ("cuba", "cu"),
    "萨尔瓦多": ("el salvador", "sv"),
    "尼加拉瓜": ("nicaragua", "ni"),
    "波多黎各": ("puerto rico", "pr"),
    "百慕大": ("bermuda", "bm"),
    "巴巴多斯": ("barbados", "bb"),
    "阿根廷": ("argentina", "ar"),
    "智利": ("chile", "cl"),
    "秘鲁": ("peru", "pe"),
    "厄瓜多尔": ("ecuador", "ec"),
    "哥伦比亚": ("colombia", "co"),
    "哥斯达黎加": ("costa rica", "cr"),
    "委内瑞拉": ("venezuela", "ve"),
    "乌拉圭": ("uruguay", "uy"),
    "玻利维亚": ("bolivia", "bo"),
    "巴拉圭": ("paraguay", "py"),
    "巴西": ("brazil", "brasil", "br"),
}

# 对照表没有美加。不要用 "america"：会误伤 South America 正文。
_EXTRA_NORTH_AMERICA = (
    "united states",
    "usa",
    "u.s.a.",
    "u.s.",
    "us",
    "canada",
    "ca",
)

_COUNTRY_FIELD_RE = re.compile(
    r"(?:Country\s*/\s*Region|Country|Pays\s*/\s*R[eé]gion|Pays|"
    r"Paese\s*/\s*Regione|Paese|Pa[ií]s\s*/\s*Regi[oó]n|Pa[ií]s|"
    r"Land\s*/\s*Region|Land|Maa\s*/\s*Alue|Krajina\s*/\s*Regi[oó]n|"
    r"国家\s*/\s*地区|国家)\s*[:：]\s*(.+)",
    re.I,
)

_BODY_SKIP_ALIASES = frozenset({
    "georgia", "jordan", "guinea", "congo", "macedonia",
})
_INDEX: dict[str, str] | None = None
_CN_TO_REGION: dict[str, str] | None = None


@dataclass(frozen=True)
class CountryHit:
    region: str
    canonical: str
    matched: str


def _norm(s: str) -> str:
    s = (s or "").replace("\u3000", " ").strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s


def load_xlsx_cn_to_region(path: Path | None = None) -> dict[str, str]:
    path = path or (ROOT / XLSX_NAME)
    out: dict[str, str] = {}
    if not path.exists():
        return out
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active
    zone = None
    for row in ws.iter_rows(min_row=2, values_only=True):
        cells = list(row)
        if cells and cells[0]:
            zone = str(cells[0]).strip()
        region = XLSX_ZONE_TO_REGION.get(zone or "")
        if not region:
            continue
        for c in cells[1:]:
            if c is None:
                continue
            cn = str(c).replace("\u3000", " ").strip()
            if cn:
                out[cn] = region
    return out


def _build_index() -> dict[str, str]:
    global _INDEX, _CN_TO_REGION
    if _INDEX is not None:
        return _INDEX
    cn_map = load_xlsx_cn_to_region()
    if not cn_map:
        # xlsx 不在仓库时，仍用别名表里的中文键分区（与对照表一致）
        cn_map = {}
        europe = {
            "英国", "德国", "法国", "意大利", "西班牙", "荷兰", "比利时", "瑞典",
            "挪威", "芬兰", "丹麦", "奥地利", "瑞士", "葡萄牙", "爱尔兰", "波兰",
            "捷克", "匈牙利", "斯洛伐克", "罗马尼亚", "保加利亚", "希腊", "克罗地亚",
            "斯洛文尼亚", "塞尔维亚", "乌克兰", "卢森堡", "冰岛", "马耳他", "塞浦路斯",
            "北马其顿", "波黑", "黑山", "阿尔巴尼亚", "法罗群岛", "拉脱维亚", "立陶宛",
            "摩纳哥", "摩尔多瓦", "列支敦士登", "法属留尼汪", "土耳其", "俄罗斯", "白俄罗斯",
        }
        oceania = {"澳大利亚", "新西兰", "波利尼西亚", "新喀里多尼亚（法属）", "斐济", "萨摩亚", "塔希提岛(法属)"}
        me = {"沙特阿拉伯", "阿联酋", "卡塔尔", "科威特", "阿曼", "巴林", "黎巴嫩", "伊拉克", "约旦", "巴勒斯坦", "以色列"}
        asia = {
            "泰国", "印度尼西亚", "马来西亚", "菲律宾", "越南", "新加坡", "柬埔寨",
            "老挝", "缅甸", "文莱", "东帝汶", "印度", "巴基斯坦", "孟加拉国", "斯里兰卡",
            "尼泊尔", "不丹", "中国香港", "中国澳门", "蒙古", "阿塞拜疆", "塔吉克斯坦", "格鲁吉亚",
        }
        africa = {
            "南非", "肯尼亚", "坦桑尼亚", "赞比亚", "加纳", "尼日利亚", "索马里兰",
            "中非共和国", "喀麦隆", "埃及", "几内亚", "冈比亚", "佛得角", "赤道几内亚",
            "几内亚比绍", "刚果", "塞拉利昂", "马达加斯加", "多哥", "厄立特里亚", "科摩罗",
            "科特迪瓦", "索马里", "突尼斯", "布基纳法索", "吉布提", "埃塞俄比亚", "利比亚",
            "毛里塔尼亚", "苏丹", "西撒哈拉", "阿尔及利亚", "摩洛哥", "毛里求斯",
            "圣多美和普林西比", "马约特岛(法属)", "博茨瓦纳", "纳米比亚", "莱索托",
            "斯威士兰", "塞舌尔",
        }
        americas = {
            "墨西哥", "特立尼达和多巴哥", "库拉索", "阿鲁巴", "海地", "巴拿马",
            "危地马拉", "牙买加", "多米尼加", "古巴", "萨尔瓦多", "尼加拉瓜", "波多黎各",
            "百慕大", "巴巴多斯", "阿根廷", "智利", "秘鲁", "厄瓜多尔", "哥伦比亚",
            "哥斯达黎加", "委内瑞拉", "乌拉圭", "玻利维亚", "巴拉圭", "巴西",
        }
        for names, region in (
            (europe, "Europe"), (oceania, "Oceania"), (me, "Middle East"),
            (asia, "Asia"), (africa, "Africa"), (americas, "South America"),
        ):
            for n in names:
                cn_map[n] = region
    _CN_TO_REGION = cn_map
    idx: dict[str, str] = {}
    for cn, region in cn_map.items():
        idx[_norm(cn)] = region
        for alias in _ALIASES_BY_CN.get(cn, ()):
            idx[_norm(alias)] = region
    for alias in _EXTRA_NORTH_AMERICA:
        idx[_norm(alias)] = "North America"
    _INDEX = idx
    return idx


def lookup_region(raw: str | None) -> Optional[CountryHit]:
    """把国家字段（中/英/ISO）映射到对照表大区。"""
    if not raw:
        return None
    key = _norm(raw.split(",")[0].split("/")[0])
    if not key:
        return None
    idx = _build_index()
    if key in idx:
        return CountryHit(region=idx[key], canonical=raw.strip(), matched=key)
    key2 = re.sub(r"\(.*?\)", "", key).strip()
    if key2 != key and key2 in idx:
        return CountryHit(region=idx[key2], canonical=raw.strip(), matched=key2)
    return None


def extract_country_field(body: str) -> Optional[str]:
    if not body:
        return None
    m = _COUNTRY_FIELD_RE.search(body)
    if not m:
        return None
    line = m.group(1).strip().split("\n")[0]
    line = re.sub(r"\s+", " ", line).strip(" .;，。")
    return line or None


def americas_form_country(body: str) -> Optional[str]:
    """表单 Country/Region 落到对照表「美洲」时返回该栏原文。

    只认这一栏，不扫正文里随口出现的国名，也不看模型猜的大区。
    美加是 North America，摩洛哥是非洲，都返回 None。
    """
    field = extract_country_field(body)
    if not field:
        return None
    hit = lookup_region(field)
    if hit and hit.region == "South America":
        return field
    return None


def lookup_region_in_text(body: str) -> Optional[CountryHit]:
    """在正文里找最长国家别名（避免短码误伤）。"""
    if not body:
        return None
    idx = _build_index()
    blob = _norm(body)
    best: Optional[CountryHit] = None
    for alias, region in idx.items():
        if alias in _BODY_SKIP_ALIASES:
            continue
        if len(alias) < 4 and not any("\u4e00" <= ch <= "\u9fff" for ch in alias):
            continue
        if alias in blob:
            if best is None or len(alias) > len(best.matched):
                best = CountryHit(region=region, canonical=alias, matched=alias)
    return best


def refine_region(
    region: str,
    country: str | None,
    body: str = "",
) -> tuple[str, Optional[str]]:
    """对照表覆盖 LLM 分区。命中则改 region；country 尽量保留原字段。"""
    field = extract_country_field(body)
    for candidate in (country, field):
        hit = lookup_region(candidate)
        if hit:
            return hit.region, (candidate or country)
    hit = lookup_region_in_text(body)
    if hit:
        return hit.region, country or hit.canonical
    return region, country


def apply_to_triage(triage, body: str = ""):
    region, country = refine_region(triage.region, triage.country, body)
    if region == triage.region and country == triage.country:
        return triage
    return triage.model_copy(update={"region": region, "country": country})
