"""账户组（组合）注册表：成员归属与组级限额。

线程安全。一个账户至多属于一个账户组；重新 assign 即把账户迁移到新组。
归属关系与组限额是两张独立的表：组可以先有限额再加入成员，反之亦然。
"""
from __future__ import annotations

import math
import threading

from app.models import GroupMembership, GroupRule


class GroupRegistry:
    """维护 账户->组 归属、组限额与组内成员集合。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._limits: dict[str, GroupRule] = {}
        self._account_group: dict[str, str] = {}
        self._group_members: dict[str, set[str]] = {}

    def set_group_limit(self, rule: GroupRule) -> None:
        if not rule.group_id:
            raise ValueError("group_id 不能为空")
        if rule.max_exposure < 0 or not math.isfinite(rule.max_exposure):
            raise ValueError(f"非法账户组限额: {rule.max_exposure}")
        with self._lock:
            self._limits[rule.group_id] = rule
            self._group_members.setdefault(rule.group_id, set())

    def assign(self, membership: GroupMembership) -> None:
        if not membership.account_id or not membership.group_id:
            raise ValueError("account_id/group_id 不能为空")
        with self._lock:
            previous = self._account_group.get(membership.account_id)
            if previous is not None and previous != membership.group_id:
                self._group_members.get(previous, set()).discard(membership.account_id)
            self._account_group[membership.account_id] = membership.group_id
            self._group_members.setdefault(membership.group_id, set()).add(
                membership.account_id
            )

    def group_of(self, account_id: str) -> str | None:
        with self._lock:
            return self._account_group.get(account_id)

    def group_limit(self, group_id: str) -> GroupRule | None:
        with self._lock:
            return self._limits.get(group_id)

    def members_of(self, group_id: str) -> list[str]:
        """按 account_id 排序返回组成员，保证结果确定性。"""
        with self._lock:
            return sorted(self._group_members.get(group_id, set()))

    def groups(self) -> list[str]:
        with self._lock:
            return sorted(self._limits | {g: None for g in self._group_members})
