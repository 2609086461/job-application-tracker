"""飞书配置接口：读取/保存 .env、测试飞书连接。"""
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from app import feishu, state

router = APIRouter(prefix="/api/config", tags=["config"])


class FeishuConfig(BaseModel):
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_app_token: str = ""
    main_table_id: str = ""


class TestConfig(FeishuConfig):
    pass


def _masked(value: Optional[str], left: int = 6, right: int = 4) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if len(value) <= left + right:
        return value[:2] + "***"
    return value[:left] + "***" + value[-right:]


@router.get("")
def get_config():
    cfg = feishu.get_config()
    required = getattr(feishu, "REQUIRED_CONFIG_KEYS", ["FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_APP_TOKEN", "MAIN_TABLE_ID"])
    missing = [k for k in required if not cfg.get(k)]
    return {
        "configured": not missing,
        "missing": missing,
        "values": {
            "feishu_app_id": cfg.get("FEISHU_APP_ID", ""),
            "feishu_app_secret_masked": _masked(cfg.get("FEISHU_APP_SECRET", "")),
            "feishu_app_token": cfg.get("FEISHU_APP_TOKEN", ""),
            "main_table_id": cfg.get("MAIN_TABLE_ID", ""),
        },
    }


def _build_payload(cfg: FeishuConfig) -> dict:
    """把表单输入归一化成配置。

    目标：用户只需粘贴一条飞书链接到 App Token 框，其余自动解析。
    - App Token：从 /base/ 或 /wiki/ 链接里提取 token（纯 token 原样保留）。
    - Table ID：优先用用户单独填写的；否则从粘贴的链接 ?table= 里解析；
      两者都没有时才回退到已保存的旧值（避免换表时被旧值覆盖）。
    """
    current = feishu.get_config()
    raw_token = cfg.feishu_app_token.strip()
    app_token = feishu.parse_app_token(raw_token) or current.get("FEISHU_APP_TOKEN", "")

    table_id = feishu.parse_table_id(cfg.main_table_id.strip())
    if not table_id and raw_token:
        table_id = feishu.parse_table_id(raw_token)
    if not table_id:
        table_id = current.get("MAIN_TABLE_ID", "")

    return {
        "FEISHU_APP_ID": cfg.feishu_app_id.strip() or current.get("FEISHU_APP_ID", ""),
        "FEISHU_APP_SECRET": cfg.feishu_app_secret.strip() or current.get("FEISHU_APP_SECRET", ""),
        "FEISHU_APP_TOKEN": app_token,
        "MAIN_TABLE_ID": table_id,
    }


@router.post("")
def save_config(cfg: FeishuConfig):
    feishu.save_config(_build_payload(cfg))
    state.set_cache({})
    return {"success": True, "message": "飞书配置已保存"}


@router.post("/test")
def test_config(cfg: TestConfig):
    payload = _build_payload(cfg)
    try:
        feishu.test_config(payload)
        return {"success": True, "message": "连接成功：已能读取飞书主表"}
    except Exception as e:
        return {"success": False, "error": feishu.friendly_error(e)}
