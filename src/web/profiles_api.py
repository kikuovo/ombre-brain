"""
========================================
web/profiles_api.py — API Profiles + Prompt 配置（2026-07-15，fork 自定义）
========================================

老师版（OmbreBrain-folio）配置页的移植：
- /api/profiles*：存多套 LLM 配置（name/model/base_url/api_key），点谁激活谁。
  激活 = 写进 config["dehydration"] + 持久化 config.yaml + 热重建 dehydrator client。
  解决"面板保存不上"的老问题：一律直接写 config.yaml（env 已清空不再覆盖）。
- /api/prompts*：脱水/合并/导入提取三段提示词页面直改，存 config["prompts"]，
  调用点实时读取，保存立即生效；置空 = 恢复默认。

对外暴露：register(mcp)
========================================
"""

import os

import httpx
import yaml

from starlette.requests import Request
from starlette.responses import Response

from . import _shared as sh

try:
    import import_memory as _im  # type: ignore
    from dehydrator import DEHYDRATE_PROMPT, MERGE_PROMPT  # type: ignore
    from utils import config_file_path  # type: ignore
except ImportError:  # pragma: no cover
    from .. import import_memory as _im  # type: ignore
    from ..dehydrator import DEHYDRATE_PROMPT, MERGE_PROMPT  # type: ignore
    from ..utils import config_file_path  # type: ignore

logger = sh.logger

_MAX_PROFILES = 12
_MAX_FIELD_CHARS = 500
_MAX_PROMPT_CHARS = 8000
_TEST_TIMEOUT = 12.0


def _prompt_defs() -> list[dict]:
    """可编辑提示词清单。default 从模块常量现取，永远有出厂值可回退。"""
    return [
        {"key": "import_extract", "label": "导入提取", "default": _im.IMPORT_EXTRACT_PROMPT},
        {"key": "dehydrate", "label": "脱水压缩", "default": DEHYDRATE_PROMPT},
        {"key": "merge", "label": "新旧合并", "default": MERGE_PROMPT},
    ]


def _mask_key(k: str) -> str:
    k = (k or "").strip()
    if not k:
        return ""
    if len(k) <= 10:
        return "***"
    return f"{k[:6]}…{k[-4:]}"


def _persist_sections(*sections: str) -> None:
    """把 sh.config 里指定的顶层键原样写入 config.yaml（bind mount，重启不丢）。"""
    cfg_path = config_file_path()
    saved: dict = {}
    if os.path.exists(cfg_path):
        with open(cfg_path, "r", encoding="utf-8") as f:
            saved = yaml.safe_load(f) or {}
    for key in sections:
        if key in sh.config:
            saved[key] = sh.config[key]
        else:
            saved.pop(key, None)
    with open(cfg_path, "w", encoding="utf-8") as f:
        yaml.dump(saved, f, allow_unicode=True, default_flow_style=False)


def _apply_dehydration_runtime() -> None:
    """config["dehydration"] 变更后热同步到 dehydrator 实例（与 env-config 第5步同思路）。"""
    try:
        dehy_cfg = sh.config.get("dehydration", {})
        sh.dehydrator.api_key = dehy_cfg.get("api_key", "")  # type: ignore[attr-defined]
        sh.dehydrator.base_url = dehy_cfg.get("base_url", "")  # type: ignore[attr-defined]
        sh.dehydrator.model = dehy_cfg.get("model", "")  # type: ignore[attr-defined]
        sh.dehydrator.api_available = bool(sh.dehydrator.api_key)  # type: ignore[attr-defined]
        if sh.dehydrator.api_available and getattr(sh.dehydrator, "api_format", "openai_compat") == "openai_compat":
            from openai import AsyncOpenAI as _OAI
            sh.dehydrator.client = _OAI(  # type: ignore[attr-defined]
                api_key=sh.dehydrator.api_key,
                base_url=sh.dehydrator.base_url,
                timeout=getattr(sh.dehydrator, "timeout_seconds", 60.0),
            )
        else:
            sh.dehydrator.client = None  # type: ignore[attr-defined]
    except Exception as e:  # pragma: no cover
        logger.warning(f"profiles: dehydrator hot-reload failed: {e}")


def _profiles() -> list[dict]:
    lst = sh.config.get("api_profiles")
    if not isinstance(lst, list):
        lst = []
        sh.config["api_profiles"] = lst
    return lst


def _clean_field(v, limit: int = _MAX_FIELD_CHARS) -> str:
    if not isinstance(v, str):
        return ""
    v = v.strip()
    if len(v) > limit or "\n" in v or "\r" in v:
        return ""
    return v


def register(mcp) -> None:
    from starlette.responses import JSONResponse

    # ---------------- API Profiles ----------------

    @mcp.custom_route("/api/profiles", methods=["GET"])
    async def api_profiles_get(request: Request) -> Response:
        err = sh._require_auth(request)
        if err:
            return err
        dehy = sh.config.get("dehydration", {}) or {}
        active = sh.config.get("active_profile", -1)
        try:
            active = int(active)
        except (TypeError, ValueError):
            active = -1
        return JSONResponse({
            "ok": True,
            "active": active,
            "current": {
                "model": dehy.get("model", ""),
                "base_url": dehy.get("base_url", ""),
                "api_key_masked": _mask_key(dehy.get("api_key", "")),
                "key_set": bool((dehy.get("api_key") or "").strip()),
            },
            "profiles": [
                {
                    "name": p.get("name", f"Profile {i+1}"),
                    "model": p.get("model", ""),
                    "base_url": p.get("base_url", ""),
                    "api_key_masked": _mask_key(p.get("api_key", "")),
                }
                for i, p in enumerate(_profiles())
            ],
        })

    @mcp.custom_route("/api/profiles/save", methods=["POST"])
    async def api_profiles_save(request: Request) -> Response:
        err = sh._require_auth(request)
        if err:
            return err
        try:
            body = await sh._read_json_object(request)
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)

        lst = _profiles()
        try:
            idx = int(body.get("index", -1))
        except (TypeError, ValueError):
            idx = -1

        name = _clean_field(body.get("name")) or "未命名"
        model = _clean_field(body.get("model"))
        base_url = _clean_field(body.get("base_url"))
        api_key = _clean_field(body.get("api_key"))
        if base_url and not base_url.startswith(("http://", "https://")):
            return JSONResponse({"ok": False, "error": "Base URL 必须以 http(s):// 开头"}, status_code=400)
        # key 里混入非 ASCII（全角/圆点打码字符）是踩过的坑，直接拦下
        if api_key and not all(ord(ch) < 128 for ch in api_key):
            return JSONResponse({"ok": False, "error": "API Key 含非英文字符（可能混入了打码圆点/全角字符），请重新粘贴"}, status_code=400)

        if 0 <= idx < len(lst):
            p = lst[idx]
            p["name"] = name
            if model:
                p["model"] = model
            if base_url:
                p["base_url"] = base_url
            if api_key:  # 编辑时留空 = 不改 key
                p["api_key"] = api_key
        else:
            if len(lst) >= _MAX_PROFILES:
                return JSONResponse({"ok": False, "error": f"最多 {_MAX_PROFILES} 套配置"}, status_code=400)
            if not (model and base_url and api_key):
                return JSONResponse({"ok": False, "error": "新建需要 模型名 / Base URL / API Key 三样齐全"}, status_code=400)
            lst.append({"name": name, "model": model, "base_url": base_url, "api_key": api_key})
            idx = len(lst) - 1

        # 编辑的是激活中的那套 → 顺手同步到 dehydration
        if int(sh.config.get("active_profile", -1) or -1) == idx:
            p = lst[idx]
            sh.config.setdefault("dehydration", {}).update(
                {"api_key": p["api_key"], "base_url": p["base_url"], "model": p["model"]}
            )
            _apply_dehydration_runtime()
            _persist_sections("api_profiles", "active_profile", "dehydration")
        else:
            _persist_sections("api_profiles")
        return JSONResponse({"ok": True, "index": idx})

    @mcp.custom_route("/api/profiles/activate", methods=["POST"])
    async def api_profiles_activate(request: Request) -> Response:
        err = sh._require_auth(request)
        if err:
            return err
        try:
            body = await sh._read_json_object(request)
            idx = int(body.get("index"))
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid index"}, status_code=400)
        lst = _profiles()
        if not (0 <= idx < len(lst)):
            return JSONResponse({"ok": False, "error": "profile 不存在"}, status_code=404)
        p = lst[idx]
        if not (p.get("api_key") and p.get("base_url") and p.get("model")):
            return JSONResponse({"ok": False, "error": "这套配置不完整（缺模型/地址/Key），先编辑补全"}, status_code=400)
        sh.config["active_profile"] = idx
        sh.config.setdefault("dehydration", {}).update(
            {"api_key": p["api_key"], "base_url": p["base_url"], "model": p["model"]}
        )
        _apply_dehydration_runtime()
        _persist_sections("api_profiles", "active_profile", "dehydration")
        return JSONResponse({"ok": True, "active": idx})

    @mcp.custom_route("/api/profiles/delete", methods=["POST"])
    async def api_profiles_delete(request: Request) -> Response:
        err = sh._require_auth(request)
        if err:
            return err
        try:
            body = await sh._read_json_object(request)
            idx = int(body.get("index"))
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid index"}, status_code=400)
        lst = _profiles()
        if not (0 <= idx < len(lst)):
            return JSONResponse({"ok": False, "error": "profile 不存在"}, status_code=404)
        lst.pop(idx)
        active = int(sh.config.get("active_profile", -1) or -1)
        if active == idx:
            sh.config["active_profile"] = -1  # 删的是激活中的：dehydration 保持现值继续工作
        elif active > idx:
            sh.config["active_profile"] = active - 1
        _persist_sections("api_profiles", "active_profile")
        return JSONResponse({"ok": True})

    @mcp.custom_route("/api/profiles/test", methods=["POST"])
    async def api_profiles_test(request: Request) -> Response:
        """现场调 /models 验证 key 是否可用（openai 兼容格式）。"""
        err = sh._require_auth(request)
        if err:
            return err
        try:
            body = await sh._read_json_object(request)
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
        idx = body.get("index")
        if idx is not None:
            lst = _profiles()
            try:
                idx = int(idx)
            except (TypeError, ValueError):
                return JSONResponse({"ok": False, "error": "invalid index"}, status_code=400)
            if not (0 <= idx < len(lst)):
                return JSONResponse({"ok": False, "error": "profile 不存在"}, status_code=404)
            base_url, api_key = lst[idx].get("base_url", ""), lst[idx].get("api_key", "")
        else:
            base_url, api_key = _clean_field(body.get("base_url")), _clean_field(body.get("api_key"))
        if not (base_url and api_key):
            return JSONResponse({"ok": False, "error": "缺 Base URL 或 API Key"})
        try:
            async with httpx.AsyncClient(timeout=_TEST_TIMEOUT) as c:
                r = await c.get(
                    f"{base_url.rstrip('/')}/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            r.raise_for_status()
            models = [m.get("id", "") for m in (r.json().get("data") or []) if m.get("id")]
            return JSONResponse({"ok": True, "model_count": len(models), "models": models[:50]})
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)[:300]})

    # ---------------- Prompt 配置 ----------------

    @mcp.custom_route("/api/prompts", methods=["GET"])
    async def api_prompts_get(request: Request) -> Response:
        err = sh._require_auth(request)
        if err:
            return err
        overrides = sh.config.get("prompts", {}) or {}
        items = []
        for d in _prompt_defs():
            ov = (overrides.get(d["key"]) or "").strip()
            items.append({
                "key": d["key"],
                "label": d["label"],
                "custom": bool(ov),
                "value": ov or d["default"],
                "default": d["default"],
            })
        return JSONResponse({"ok": True, "prompts": items})

    @mcp.custom_route("/api/prompts/save", methods=["POST"])
    async def api_prompts_save(request: Request) -> Response:
        err = sh._require_auth(request)
        if err:
            return err
        try:
            body = await sh._read_json_object(request)
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
        key = body.get("key", "")
        defs = {d["key"]: d for d in _prompt_defs()}
        if key not in defs:
            return JSONResponse({"ok": False, "error": "未知的 prompt key"}, status_code=400)
        value = body.get("value", "")
        if not isinstance(value, str):
            return JSONResponse({"ok": False, "error": "value 必须是字符串"}, status_code=400)
        if len(value) > _MAX_PROMPT_CHARS:
            return JSONResponse({"ok": False, "error": f"超过 {_MAX_PROMPT_CHARS} 字上限"}, status_code=400)
        value = value.strip()
        overrides = sh.config.setdefault("prompts", {})
        if not value or value == defs[key]["default"].strip():
            overrides.pop(key, None)  # 置空或改回原样 = 恢复默认
            custom = False
        else:
            overrides[key] = value
            custom = True
        if not overrides:
            sh.config.pop("prompts", None)
        _persist_sections("prompts")
        return JSONResponse({"ok": True, "custom": custom})
