"""角色配置管理：从 roles.json 加载角色与默认角色。

roles.json 结构：
{
  "roles": {
    "assistant": {
      "name": "通用助手",
      "system_prompt": "...",
      "temperature": 0.7,
      "model": "deepseek-chat"
    },
    "translator": { ... }
  },
  "default_role": "assistant"
}

兼容说明：get_role(None) 与 get_role("default") 都返回默认角色，
便于调用方用 current_role_id="default" 表示「未显式切换」。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# roles.json 位于项目根目录
ROLES_FILE = Path(__file__).resolve().parent.parent / "roles.json"

# 角色字段默认值（roles.json 缺字段时回退）
DEFAULT_TEMPERATURE = 0.7


def _normalize_role(key: str, role: dict) -> dict:
    """把 roles.json 里的角色对象补齐为标准结构，附带 id（即 key）。"""
    return {
        "id": key,
        "name": role.get("name", key),
        "system_prompt": role.get("system_prompt", ""),
        "temperature": role.get("temperature", DEFAULT_TEMPERATURE),
        "model": role.get("model"),
    }


class RoleManager:
    """roles.json 的轻量读写封装。"""

    def __init__(self, file_path: Path = ROLES_FILE) -> None:
        self.file_path = file_path

    def _load(self) -> dict:
        """读取 roles.json。文件不存在或解析失败返回空结构（不抛异常）。"""
        if not self.file_path.exists():
            logger.warning("角色文件不存在: %s", self.file_path)
            return {"roles": {}, "default_role": None}
        try:
            data = json.loads(self.file_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.exception("读取角色文件失败: %s", e)
            return {"roles": {}, "default_role": None}
        if not isinstance(data, dict) or not isinstance(data.get("roles"), dict):
            logger.error("roles.json 结构不合法：需要 {roles: {...}, default_role: ...}")
            return {"roles": {}, "default_role": None}
        return data

    def list_roles(self) -> list[dict]:
        """返回所有角色，每项含 id / name / system_prompt / temperature / model。"""
        data = self._load()
        out: list[dict] = []
        for key, role in data["roles"].items():
            if isinstance(role, dict):
                out.append(_normalize_role(key, role))
        return out

    def get_role(self, name: Optional[str]) -> Optional[dict]:
        """按 key 查找角色。

        - name 为 None 或 "default" 时返回默认角色（兼容未显式切换的会话）
        - 找不到返回 None
        """
        data = self._load()
        roles = data["roles"]

        # None / "default" → 解析为 default_role
        if not name or name == "default":
            name = data.get("default_role")
        if not name:
            return None

        role = roles.get(name)
        if not isinstance(role, dict):
            return None
        return _normalize_role(name, role)

    def get_default_role(self) -> Optional[dict]:
        """返回默认角色（由 roles.json 的 default_role 指定）。"""
        data = self._load()
        default_key = data.get("default_role")
        if not default_key:
            return None
        return self.get_role(default_key)


role_manager = RoleManager()
