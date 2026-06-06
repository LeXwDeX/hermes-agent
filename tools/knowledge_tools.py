#!/usr/bin/env python3
"""
Knowledge Tool Module

Declarative knowledge/fact management.  Knowledge is objective facts — server
addresses, configuration parameters, system architecture, preferences, constraints.

Stored as YAML files in ~/.hermes/knowledge/<user_id>/ with CRUD operations.
Knowledge is distinct from Skill (executable capability) and Memory (temporal experience).

| Storage   | Meaning    | Content                                          |
|-----------|-----------|--------------------------------------------------|
| Skill     | 技能/能力  | Workflows, tools, known pitfalls, hard constraints |
| Knowledge | 事实/知识  | Server addresses, paths, ports, configs, preferences |
| Memory    | 经历/记忆  | Past decisions, lessons, user preferences, corrections |
"""

import json
import os
import re
import time
import yaml
from pathlib import Path

from tools.registry import registry, tool_error
from hermes_constants import get_hermes_home

logger = __import__('logging').getLogger(__name__)

# Shared helper from skills_tool (FTS5 session search)
from tools.skills_tool import _call_session_search

# ---------------------------------------------------------------------------
# Knowledge store helpers — declarative knowledge in ~/.hermes/knowledge/<user_id>/
# ---------------------------------------------------------------------------

_DEFAULT_USER_ID = "329433061294866433"


def _get_knowledge_dir(user_id: str = None) -> str:
    """Get the knowledge directory for a user."""
    uid = user_id or _DEFAULT_USER_ID
    return os.path.join(os.path.expanduser("~/.hermes/knowledge"), uid)


def _knowledge_filepath(memory_id: str, user_id: str = None) -> str:
    """Get file path for a knowledge entry."""
    return os.path.join(_get_knowledge_dir(user_id), f"{memory_id}.yaml")


def _read_knowledge_entry(memory_id: str, user_id: str = None) -> dict:
    """Read a knowledge entry from disk."""
    path = _knowledge_filepath(memory_id, user_id)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return yaml.safe_load(f)


def _write_knowledge_entry(entry: dict, user_id: str = None) -> None:
    """Write a knowledge entry to disk."""
    _kd = _get_knowledge_dir(user_id)
    os.makedirs(_kd, exist_ok=True)
    path = os.path.join(_kd, f"{entry['name']}.yaml")
    with open(path, "w") as f:
        yaml.dump(entry, f, allow_unicode=True, default_flow_style=False)


def _list_knowledge_entries(user_id: str = None) -> list:
    """List all knowledge entry IDs (filenames without .yaml)."""
    _kd = _get_knowledge_dir(user_id)
    if not os.path.isdir(_kd):
        return []
    return sorted([
        fname[:-5] for fname in os.listdir(_kd)
        if fname.endswith(".yaml")
    ])


# ---------------------------------------------------------------------------
# Knowledge CRUD — local YAML files (no Hindsight dependency for writes)
# ---------------------------------------------------------------------------

def knowledge_create(name: str, content: str, title: str = None, user_id: str = None) -> str:
    """Create a knowledge entry (YAML file in ~/.hermes/knowledge/<user_id>/)."""
    if not re.match(r'^[a-z0-9][a-z0-9.-]*$', name):
        return tool_error(
            f"Invalid name '{name}'. Use lowercase letters, digits, hyphens, dots only.",
            success=False,
        )

    path = _knowledge_filepath(name, user_id)
    if os.path.exists(path):
        return tool_error(
            f"Knowledge entry '{name}' already exists. Use update to modify.",
            success=False,
        )

    entry = {
        "name": name,
        "title": title or name,
        "content": content,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    _write_knowledge_entry(entry, user_id)

    return json.dumps({
        "success": True,
        "action": "create",
        "name": name,
        "message": f"Knowledge entry '{name}' created.",
    }, ensure_ascii=False)


def knowledge_update(memory_id: str, content: str = None, name: str = None, title: str = None, user_id: str = None) -> str:
    """Update a knowledge entry. Provide at least one of content/name/title."""
    entry = _read_knowledge_entry(memory_id, user_id)
    if entry is None:
        return tool_error(f"Knowledge entry '{memory_id}' not found.", success=False)

    renamed = False
    old_path = None
    if name is not None and name != memory_id:
        new_path = _knowledge_filepath(name, user_id)
        if os.path.exists(new_path):
            return tool_error(f"Knowledge entry '{name}' already exists.", success=False)
        renamed = True
        old_path = _knowledge_filepath(memory_id, user_id)
        entry["name"] = name

    if content is not None:
        entry["content"] = content
    if title is not None:
        entry["title"] = title
    entry["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    _write_knowledge_entry(entry, user_id)

    if renamed:
        os.remove(old_path)

    return json.dumps({
        "success": True,
        "action": "update",
        "name": entry["name"],
        "message": f"Knowledge entry '{entry['name']}' updated.",
    }, ensure_ascii=False)


def knowledge_delete(memory_id: str, user_id: str = None) -> str:
    """Delete a knowledge entry."""
    path = _knowledge_filepath(memory_id, user_id)
    if not os.path.exists(path):
        return tool_error(f"Knowledge entry '{memory_id}' not found.", success=False)

    os.remove(path)
    return json.dumps({
        "success": True,
        "action": "delete",
        "name": memory_id,
        "message": f"Knowledge entry '{memory_id}' deleted.",
    }, ensure_ascii=False)


# ---------------------------------------------------------------------------
# knowledge_recall — local YAML + Hindsight piggyback + session memories
# ---------------------------------------------------------------------------

def _call_hindsight_recall(query: str, limit: int) -> list:
    """Hindsight REST recall endpoint removed. Use native hindsight_recall tool instead."""
    return []


def _call_hindsight_retain(content: str) -> dict:
    """POST to Hindsight retain API."""
    import urllib.request

    url = "http://192.168.33.110:8888/v1/default/banks/hermes-329433061294866433/memories"
    body = json.dumps({"items": [{"content": content}]}).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            return {"success": True, "result": result}
    except Exception as e:
        return {"success": False, "error": f"Failed to store knowledge: {e}"}


def knowledge_recall(query: str, limit: int = 5, user_id: str = None) -> str:
    """Search declarative knowledge: local YAML entries + Hindsight + session memories."""
    # 1. Token-based search on local knowledge entries
    query_terms = query.lower().split()
    scored = []
    for mem_id in _list_knowledge_entries(user_id):
        try:
            entry = _read_knowledge_entry(mem_id, user_id)
            if entry is None:
                continue
            content_lower = (entry.get("content") or "").lower()
            title_lower = (entry.get("title") or "").lower()
            name_lower = (entry.get("name") or "").lower()
            
            # Score: each matching term adds points weighted by field
            score = 0.0
            match_on = []
            for term in query_terms:
                if term in name_lower:
                    score += 5.0
                    if "name" not in match_on:
                        match_on.append("name")
                if term in title_lower:
                    score += 3.0
                    if "title" not in match_on:
                        match_on.append("title")
                if term in content_lower:
                    score += 2.0
                    if "content" not in match_on:
                        match_on.append("content")
            
            if score > 0:
                scored.append({
                    "memory_id": entry["name"],
                    "title": entry.get("title", entry["name"]),
                    "content": entry["content"],
                    "created": entry.get("created", ""),
                    "score": score,
                    "match_on": match_on,
                    "related_skills": entry.get("related_skills", []),
                })
        except Exception:
            pass
    
    # Sort by score descending
    scored.sort(key=lambda x: x["score"], reverse=True)
    local_results = scored[:limit]

    # 2. Search Hindsight (piggyback)
    hindsight_results = _call_hindsight_recall(query, limit)
    clean = [r for r in hindsight_results if not r.get("error")]

    # 3. Search session memories
    try:
        session_results = _call_session_search(query, 3)
    except Exception:
        session_results = []

    return json.dumps({
        "query": query,
        "knowledge": {
            "total": len(scored),
            "results": local_results,
        },
        "hindsight": {
            "total": len(clean),
            "results": [{"content": r.get("content", ""), "score": r.get("score", 0)} for r in clean[:limit]],
        },
        "memory": {"sessions": session_results},
    }, ensure_ascii=False)


# ---------------------------------------------------------------------------
# knowledge_tool — unified dispatch
# ---------------------------------------------------------------------------

def knowledge_tool(
    action: str,
    query: str = None,
    content: str = None,
    name: str = None,
    title: str = None,
    memory_id: str = None,
    limit: int = 5,
    user_id: str = None,
) -> str:
    """知识/事实管理。搜索、创建、更新、删除声明式环境事实和配置信息。

    Knowledge 是客观静态事实（服务器地址、配置参数、系统架构、偏好设置、约束规则）。
    不同于 Skill（可执行能力）和 Memory（时态性经历）。

    边界规约：
    - Knowledge 只存静态事实（地址/路径/端口/配置/偏好/约束），不存操作指令
    - 如果内容是「如何做某事」，应放入 Skill，不是 Knowledge
    - 如果内容是「某次会话发生的事」，应放入 Memory，不是 Knowledge
    """
    if action == "recall":
        if not query:
            return tool_error("Parameter 'query' is required for 'recall' action.", success=False)
        return knowledge_recall(query=query, limit=limit, user_id=user_id)
    elif action == "create":
        if not name:
            return tool_error("Parameter 'name' is required for 'create' action.", success=False)
        if not content:
            return tool_error("Parameter 'content' is required for 'create' action.", success=False)
        return knowledge_create(name=name, content=content, title=title, user_id=user_id)
    elif action == "update":
        if not memory_id:
            return tool_error("Parameter 'memory_id' is required for 'update' action.", success=False)
        return knowledge_update(memory_id=memory_id, content=content, name=name, title=title, user_id=user_id)
    elif action == "delete":
        if not memory_id:
            return tool_error("Parameter 'memory_id' is required for 'delete' action.", success=False)
        return knowledge_delete(memory_id=memory_id, user_id=user_id)
    else:
        return tool_error(
            f"Unknown action '{action}'. Valid actions: recall, create, update, delete.",
            success=False,
        )


# ---------------------------------------------------------------------------
# Schema & Registration
# ---------------------------------------------------------------------------

KNOWLEDGE_TOOL_SCHEMA = {
    "name": "knowledge",
    "description": (
        "知识/事实管理。搜索、创建、更新、删除声明式环境事实和配置信息。\n"
        "Knowledge 是客观静态事实——服务器地址、配置参数、系统架构、偏好设置、约束规则。\n"
        "与 Skill（可执行技能/工作流）和 Memory（时态性经历/教训）严格区分。\n\n"
        "边界规约：\n"
        "- Skill = 技能/能力（如何做某事：SSH方法、API调用方式、工作流步骤）\n"
        "- Knowledge = 事实/知识（是什么：服务器地址、文件路径、端口号、配置参数、偏好设置）\n"
        "- Memory = 经历/记忆（发生过什么：过往决策、教训、用户习惯、会话纠正）\n\n"
        "Actions:\n"
        "  recall — 搜索知识库中的事实（词级分词匹配+打分，自动附带 Hindsight 和会话记忆）\n"
        "  create — 写入一条知识事实（保存为本地 YAML 文件）\n"
        "  update — 更新指定知识条目的内容\n"
        "  delete — 删除指定知识条目"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["recall", "create", "update", "delete"],
                "description": "操作类型。recall=搜索, create=创建, update=更新, delete=删除",
            },
            "query": {
                "type": "string",
                "description": "自然语言查询。recall 时必填。",
            },
            "content": {
                "type": "string",
                "description": "知识内容文本。create/update 时必填。",
            },
            "name": {
                "type": "string",
                "description": "知识条目名称（可选）。",
            },
            "title": {
                "type": "string",
                "description": "知识条目标题（可选）。",
            },
            "limit": {
                "type": "integer",
                "description": "返回结果数量上限（recall时使用，默认5）。",
            },
            "memory_id": {
                "type": "string",
                "description": "记忆条目ID。update/delete 时必填。",
            },
        },
        "required": ["action"],
    },
}


registry.register(
    name="knowledge",
    toolset="knowledge",
    schema=KNOWLEDGE_TOOL_SCHEMA,
    handler=lambda args, **kw: knowledge_tool(
        action=args.get("action", ""),
        query=args.get("query"),
        content=args.get("content"),
        name=args.get("name"),
        title=args.get("title"),
        memory_id=args.get("memory_id"),
        limit=args.get("limit", 5),
        user_id=kw.get("user_id"),
    ),
    emoji="🧠",
)
