"""@profile tag routing: re-home a leading ``@profile`` message to a served profile.

Opt in with ``gateway.tag_routes`` on a multiplexed gateway. A matching message in
an approved chat runs in that profile's session lane and runtime home, while the
receiving adapter remains the transport for authorization and reply delivery.

Explicit ``gateway.profile_routes`` and dedicated secondary bots take precedence.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_TAG_PATTERN = r"^\s*@([a-z0-9_-]+)(?:\s+|$)"
_WHATSAPP_PLATFORMS = {"whatsapp", "whatsapp_cloud"}
_DECIDED_ATTR = "_tag_route_decided"


@dataclass(frozen=True)
class TagRouteConfig:
    enabled: bool = False
    platforms: Tuple[str, ...] = ()
    chats: Tuple[str, ...] = ()
    pattern: str = DEFAULT_TAG_PATTERN
    _regex: Any = field(default=None, compare=False, repr=False)

    @property
    def regex(self) -> "re.Pattern[str]":
        return self._regex if self._regex is not None else re.compile(self.pattern, re.IGNORECASE)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "platforms": list(self.platforms),
            "chats": list(self.chats),
            "pattern": self.pattern,
        }


def parse_tag_routes(raw: Any) -> Optional[TagRouteConfig]:
    """Parse ``gateway.tag_routes``; invalid or disabled configuration is inert."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        logger.warning("Ignoring gateway.tag_routes: expected a mapping, got %s", type(raw).__name__)
        return None
    enabled = raw.get("enabled", True)
    if not (enabled is True or str(enabled).strip().lower() in {"1", "true", "yes", "on"}):
        return None

    def _str_list(key: str) -> Tuple[str, ...]:
        value = raw.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            value = [value]
        if not isinstance(value, (list, tuple)):
            return ()
        return tuple(str(item).strip() for item in value if item is not None and str(item).strip())

    platforms = tuple(platform.lower() for platform in _str_list("platforms"))
    chats = _str_list("chats")
    if not platforms or not chats:
        logger.warning("Ignoring gateway.tag_routes: 'platforms' and 'chats' must both be non-empty")
        return None
    pattern = raw.get("pattern") or DEFAULT_TAG_PATTERN
    try:
        regex = re.compile(str(pattern), re.IGNORECASE)
    except re.error as exc:
        logger.warning("Ignoring gateway.tag_routes: invalid pattern %r (%s)", pattern, exc)
        return None
    if regex.groups < 1:
        logger.warning("Ignoring gateway.tag_routes: pattern %r needs a capture group for the profile name", pattern)
        return None
    return TagRouteConfig(enabled=True, platforms=platforms, chats=chats, pattern=str(pattern), _regex=regex)


def chat_matches(cfg: TagRouteConfig, platform: str, chat_id: Optional[str]) -> bool:
    """Match a configured chat, resolving WhatsApp number/JID/LID aliases."""
    if not chat_id:
        return False
    chat_id = str(chat_id)
    if chat_id in cfg.chats:
        return True
    if platform not in _WHATSAPP_PLATFORMS:
        return False
    from gateway.profile_routing import _whatsapp_user_chat_ids_match
    return any(_whatsapp_user_chat_ids_match(platform, configured, chat_id) for configured in cfg.chats)


def extract_tag(cfg: TagRouteConfig, text: Optional[str]) -> Optional[Tuple[str, "re.Match[str]"]]:
    """Return a lower-cased leading tag and its match, or None."""
    if not isinstance(text, str) or not text:
        return None
    match = cfg.regex.search(text)
    if match is None or not match.group(1):
        return None
    return match.group(1).strip().lower(), match


def _routing_text(event: Any, platform: str) -> Optional[str]:
    """Return the trusted leading-tag view of an inbound event.

    WhatsApp owner-authored messages retain an ``[owner reply]`` transcript marker.
    The adapter sets its metadata from the bridge's ``fromOwner`` payload, so only that
    provenance may make the marker transparent for leading-tag routing. A user-written
    lookalike remains ordinary text and cannot turn an inline mention into a route.
    """
    text = getattr(event, "text", None)
    metadata = getattr(event, "metadata", None)
    if (platform in _WHATSAPP_PLATFORMS and isinstance(metadata, dict)
            and metadata.get("whatsapp_from_owner") is True
            and isinstance(text, str) and text.startswith("[owner reply] ")):
        return text[len("[owner reply] "):]
    return text


def _primary_profile(runner: Any) -> str:
    name = getattr(runner, "_primary_profile_name", None)
    if isinstance(name, str) and name.strip():
        return name.strip()
    active = getattr(runner, "_active_profile_name", None)
    resolved = None
    if callable(active):
        try:
            resolved = active()
        except Exception:
            resolved = None
    return resolved.strip() if isinstance(resolved, str) and resolved.strip() else "default"


def apply_tag_route(adapter: Any, event: Any) -> Optional[str]:
    """Stamp a configured leading tag's served profile on the inbound source.

    This must execute before the adapter's first canonicalization so the session key,
    busy lane, and runtime profile all follow the selected profile.
    """
    source = getattr(event, "source", None)
    if source is None or getattr(event, "internal", False):
        return None
    if getattr(event, _DECIDED_ATTR, False) or getattr(source, "profile_route_rejected", False) is True:
        return None
    runner = getattr(adapter, "gateway_runner", None)
    config = getattr(runner, "config", None)
    if not getattr(config, "multiplex_profiles", False):
        return None
    cfg = getattr(config, "tag_routes", None)
    if not isinstance(cfg, TagRouteConfig) or not cfg.enabled:
        return None
    platform = getattr(getattr(source, "platform", None), "value", None) or str(getattr(source, "platform", ""))
    platform = platform.lower()
    if platform not in cfg.platforms or not chat_matches(cfg, platform, getattr(source, "chat_id", None)):
        return None
    routing_text = _routing_text(event, platform) or ""
    found = extract_tag(cfg, routing_text)
    if found is None:
        return None
    tag, match = found

    from gateway.session_identity import clear_identity, identity_of
    primary = _primary_profile(runner)
    # A present source.profile is authoritative even when it is the default profile:
    # profile_routes already made an explicit routing decision.
    if getattr(source, "profile", None) is not None:
        logger.info("tag route: @%s ignored; %s/%s already routed to profile %s",
                    tag, platform, getattr(source, "chat_id", "?"), source.profile)
        setattr(event, _DECIDED_ATTR, True)
        return None
    identity = identity_of(source)
    try:
        from gateway.run import _multiplex_profile_homes
        served = {name for name, _home in _multiplex_profile_homes(config)}
    except Exception:
        logger.warning("tag route: @%s not applied: served-profile set could not be resolved; staying on profile %s",
                       tag, primary, exc_info=True)
        setattr(event, _DECIDED_ATTR, True)
        return None
    if tag not in served:
        logger.warning("tag route: @%s names a profile this gateway does not serve; staying on profile %s", tag, primary)
        setattr(event, _DECIDED_ATTR, True)
        return None
    if tag == primary:
        remainder = routing_text[match.end():].lstrip()
        if remainder.startswith("/"):
            event.text = remainder
        logger.info("tag route: @%s -> profile %s", tag, tag)
        setattr(event, _DECIDED_ATTR, True)
        return None
    if identity is not None:
        clear_identity(source)
    source.profile = tag
    setattr(event, _DECIDED_ATTR, True)
    remainder = routing_text[match.end():].lstrip()
    if remainder.startswith("/"):
        event.text = remainder
    logger.info("tag route: @%s -> profile %s", tag, tag)
    return tag
