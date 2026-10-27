"""Resolve private test_up locations without embedding deployment paths in source."""
from __future__ import annotations

import json
import os
from pathlib import Path

LOCAL_CONFIG = Path(__file__).resolve().parents[1] / '.env.local.json'
SOURCE_KEY = 'TEST_UP_SOURCE_ROOT'


def resolve_source_root(explicit: str | None = None, *, config_path: Path = LOCAL_CONFIG) -> str:
    """CLI overrides the process environment, then the ignored local JSON file.

    Resolution reads configuration only: it never opens or writes the share.
    Each caller validates path semantics appropriate to its operating system.
    """
    if explicit is not None:
        if not isinstance(explicit, str) or not explicit.strip():
            raise ValueError('共享根目录参数不能为空')
        return explicit.strip()
    environment = os.getenv(SOURCE_KEY, '').strip()
    if environment:
        return environment
    try:
        document = json.loads(config_path.read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        document = {}
    except (OSError, ValueError) as exc:
        raise ValueError('无法读取本机配置 .env.local.json，请检查文件权限和 JSON 格式') from exc
    if not isinstance(document, dict):
        raise ValueError('本机配置 .env.local.json 必须是 JSON 对象')
    value = document.get(SOURCE_KEY)
    if value is not None:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f'本机配置 {SOURCE_KEY} 必须是非空字符串')
        return value.strip()
    raise ValueError(f'未配置共享根目录；请使用命令行参数、环境变量 {SOURCE_KEY}，'
                     f'或在项目 .env.local.json 中设置 {SOURCE_KEY}')
