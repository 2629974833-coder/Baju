"""User-owned recurring scan rules. All launches go through the shared scan queue."""

from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from backend.automation import RuleNotFound, next_check, public_rule
from backend.series import page_origin
from backend.store import new_id, record, timestamp
from backend.tasks import Conflict, find

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
SiteId = Annotated[str, Field(min_length=1, max_length=128)]
Url = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8192)]
Interval = Annotated[int, Field(strict=True, ge=15, le=10080)]
Kinds = Annotated[list[Literal["image", "video"]], Field(min_length=1, max_length=2)]


class RuleInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: Name
    siteId: SiteId
    url: Url
    intervalMinutes: Interval = 1440
    kinds: Kinds = Field(default_factory=lambda: ["image", "video"])
    autoDownload: bool = False
    enabled: bool = True


class RuleTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    siteId: SiteId
    url: Url


class RuleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: Name | None = None
    siteId: SiteId | None = None
    url: Url | None = None
    intervalMinutes: Interval | None = None
    kinds: Kinds | None = None
    autoDownload: bool | None = None
    enabled: bool | None = None
    expectedTarget: RuleTarget | None = None

    @model_validator(mode="after")
    def require_changes(self):
        if not self.model_fields_set or any(getattr(self, k) is None for k in self.model_fields_set):
            raise ValueError("请提供有效的规则修改项")
        if self.expectedTarget and not {"url", "siteId"} & self.model_fields_set:
            raise ValueError("请提供要修改的目标")
        return self


def create_automation_router(store, automation, policy):
    router = APIRouter(prefix="/api/rules")

    def rule_for(state, identifier):
        rule = find(state.get("rules", []), identifier)
        if not rule:
            raise HTTPException(404, "找不到自动化规则")
        return rule

    def editable(state, identifier):
        rule = rule_for(state, identifier)
        if rule["status"] == "running":
            raise Conflict("规则正在运行，请等本轮结束后再修改")
        return rule

    async def validated_target(site_id, url):
        site = find(store.read()["sites"], site_id)
        if not site:
            raise HTTPException(404, "找不到规则的来源站点")
        origin = page_origin(url)
        if not origin or origin != page_origin(site["url"]):
            raise ValueError("规则网址须与所选站点使用相同的协议、域名和端口")
        return await policy.validate(url)

    @router.post("")
    async def create(data: RuleInput):
        config = data.model_dump()
        config["url"] = await validated_target(data.siteId, data.url)
        config["kinds"] = list(dict.fromkeys(data.kinds))
        rule = {**config, "id": new_id("rule"), "status": "idle", "createdAt": timestamp(),
                "nextRunAt": next_check(config), "lastRunAt": None, "lastScanId": None,
                "lastNewCount": 0, "lastQueuedCount": 0, "error": ""}

        def add(state):
            rules = state.setdefault("rules", [])
            if len(rules) >= 100:
                raise Conflict("规则已达到 100 条上限，可先删除闲置规则")
            rules.append(rule)
            record(state, "已创建自动化规则", "automation")

        store.mutate(add)
        automation.wake()
        return rule

    @router.patch("/{identifier}")
    async def update(identifier: str, data: RuleUpdate):
        changes = data.model_dump(exclude_unset=True)
        expected = changes.pop("expectedTarget", None)
        before = editable(store.read(), identifier)
        if expected and any(before[k] != expected[k] for k in expected):
            raise Conflict("规则的目标已变更，请刷新后重试")
        if "kinds" in changes:
            changes["kinds"] = list(dict.fromkeys(changes["kinds"]))
        if any(k in changes and changes[k] != before[k] for k in ("siteId", "url")):
            changes["url"] = await validated_target(
                changes.get("siteId", before["siteId"]), changes.get("url", before["url"])
            )

        def apply(state):
            rule = editable(state, identifier)
            # DNS validation may yield. Resolve racing edits against the actual stored target.
            if any(before[k] != rule[k] for k in ("siteId", "url")):
                raise Conflict("规则的目标已变更，请刷新后重试")
            changed = {k for k in changes if rule[k] != changes[k]}
            rule.update(changes)
            retarget = bool(changed & {"url", "siteId", "kinds"})
            if retarget or "autoDownload" in changed:
                rule.pop("_pendingAssetIds", None)
            if retarget:
                rule.update(lastRunAt=None, lastScanId=None, lastNewCount=0, lastQueuedCount=0,
                            error="", status="idle")
            if retarget or changed & {"enabled", "intervalMinutes"}:
                rule["nextRunAt"] = next_check(rule)
            if "enabled" in changed and rule["enabled"]:
                rule.update(error="", status="idle")
            if changed:
                record(state, "已更新自动化规则", "automation")
            return public_rule(rule)

        result = store.mutate(apply)
        automation.wake()
        return result

    @router.delete("/{identifier}")
    async def remove(identifier: str):
        def delete(state):
            state["rules"].remove(editable(state, identifier))
            record(state, "已删除自动化规则", "automation")

        store.mutate(delete)
        automation.wake()
        return {"deleted": True}

    @router.post("/{identifier}/run")
    async def run_now(identifier: str):
        try:
            return automation.run_now(identifier)
        except RuleNotFound:
            raise HTTPException(404, "找不到自动化规则") from None

    return router
