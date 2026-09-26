"""Canonical company names shared by mail, Feishu, tasks, and the dashboard."""

from __future__ import annotations

import re
import unicodedata


# Keep this list limited to aliases that unambiguously identify one employer.
# The canonical name is always considered an alias even when omitted below.
COMPANY_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("阿里巴巴", ("alibaba",)),
    ("艾为电子", ("awinic", "上海艾为电子技术股份有限公司")),
    ("百度", ("baidu",)),
    ("步科", ("kinco",)),
    ("比亚迪", ("byd", "比亚迪股份有限公司")),
    ("CVTE", ("视源股份", "视源电子")),
    ("字节跳动", ("bytedance", "字节跳动 ByteDance")),
    ("滴滴", ("didiglobal",)),
    ("恒玄科技", ("bestechnic",)),
    ("虹软科技", ("虹软", "arcsoft")),
    ("汇川技术", ("汇川", "inovance")),
    ("吉利控股", ("吉利汽车集团", "来自吉利汽车集团", "geely")),
    ("华勤技术", ("华勤",)),
    ("江波龙", ("深圳市江波龙电子股份有限公司", "longsys")),
    ("联发科技", ("mediatek",)),
    ("联影医疗", ("联影集团", "united imaging")),
    ("吉芯科技", ()),
    ("经纬恒润", ("hirain",)),
    ("海光信息", ("海光", "hygon")),
    ("联想", ("联想集团", "Lenovo Campus Recruitment", "lenovo")),
    ("米哈游", ("hoyoverse",)),
    ("大疆", ("dji", "dji 大疆")),
    ("小米", ("小米集团", "Xiaomi Hire", "xiaomi")),
    ("新华三", ("新华三技术有限公司", "h3c", "ma.dongliang@shmail.ibeisen.com")),
    ("星宸科技", ("星宸科技股份有限公司",)),
    ("上海贝岭", ("上海贝岭股份有限公司",)),
    ("龙旗集团", ("龙旗", "longcheer")),
    ("传音控股", ("传音", "transsion")),
    ("零跑科技", ("浙江零跑科技股份有限公司", "零跑")),
    ("舜宇光学", ("舜宇", "舜宇集团", "舜宇集团有限公司", "sunny optical")),
    ("腾讯", ("tencent",)),
    ("元戎启行", ("深圳元戎启行科技有限公司", "deeproute")),
    ("远景科技", ("远景能源", "远景科技集团 Envision Group", "envision group")),
    ("兆易创新", ("gigadevice",)),
    ("芯动科技", ("innosilicon",)),
    ("新易盛", ("eoptolink",)),
    ("新凯来", ("sicarrier",)),
    ("新紫光", ("紫光",)),
    ("芯原股份", ("芯原", "verisilicon")),
    ("燧原科技", ("燧原", "enflame")),
    ("亿道集团", ("亿道控股", "深圳市亿道控股有限公司", "emdoor")),
    ("知存科技", ("知存", "witinmem")),
    ("阳光电源", ("阳光电源股份有限公司",)),
    ("科大讯飞", ("科大讯飞股份有限公司", "科大讯飞（集团", "iflytek")),
    ("创维", ("创维集团",)),
)

_COMPANY_SUFFIXES = ("股份有限公司", "有限责任公司", "有限公司")


def _token(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value or "")).lower().strip()
    return re.sub(r"[\s·•._\-—–_/\\()（）\[\]【】]+", "", value)


def _build_lookup() -> dict[str, str]:
    lookup: dict[str, str] = {}
    for canonical, aliases in COMPANY_ALIASES:
        for alias in (canonical, *aliases):
            key = _token(alias)
            if key:
                lookup[key] = canonical
    return lookup


_ALIAS_LOOKUP = _build_lookup()


def canonical_company_name(value: str) -> str:
    """Return a stable display name for a known alias, preserving unknown names."""
    display = re.sub(r"\s+", " ", str(value or "")).strip()
    if not display:
        return ""
    direct = _ALIAS_LOOKUP.get(_token(display))
    if direct:
        return direct
    shortened = display
    for suffix in _COMPANY_SUFFIXES:
        if shortened.endswith(suffix):
            shortened = shortened[: -len(suffix)].strip()
            break
    return _ALIAS_LOOKUP.get(_token(shortened), display)


def company_key(value: str) -> str:
    """Return the comparison key used across every recruitment subsystem."""
    canonical = canonical_company_name(value)
    key = _token(canonical)
    for suffix in _COMPANY_SUFFIXES:
        suffix_key = _token(suffix)
        if key.endswith(suffix_key):
            key = key[: -len(suffix_key)]
            break
    return key


def company_search_aliases() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Return aliases suitable for identifying a company inside mail text."""
    return tuple((canonical, (canonical, *aliases)) for canonical, aliases in COMPANY_ALIASES)
