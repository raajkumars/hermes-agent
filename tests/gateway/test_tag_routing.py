"""Behavior contracts for opt-in multiplexed ``gateway.tag_routes``."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.pairing import PairingStore
from gateway.platforms.base import BasePlatformAdapter
from gateway.platforms.event import MessageEvent, MessageType
from gateway.profile_routing import parse_profile_routes
from gateway.session_identity import identity_of
from gateway.tag_routing import TagRouteConfig, apply_tag_route, chat_matches, extract_tag, parse_tag_routes

SELF = "19785669223@s.whatsapp.net"
OTHER = "15550001111@s.whatsapp.net"
GROUP = "120363409708974012@g.us"


class _Stub(BasePlatformAdapter):
    pass


_Stub.__abstractmethods__ = frozenset()


def _adapter(runner):
    adapter = _Stub.__new__(_Stub)
    BasePlatformAdapter.__init__(adapter, PlatformConfig(enabled=True, extra={}), Platform.WHATSAPP)
    adapter.gateway_runner = runner
    adapter._session_store = SimpleNamespace(_resolve_profile_for_key=lambda source: "default")
    return adapter


@pytest.fixture
def rig(tmp_path, monkeypatch):
    from gateway.run import GatewayRunner

    home = tmp_path / "hermes"
    for name in ("prime", "ops"):
        (home / "profiles" / name).mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner.config.platforms = {Platform.WHATSAPP: PlatformConfig(enabled=True, extra={})}
    runner.config.profile_routes = parse_profile_routes([
        {"name": "ops-group", "platform": "whatsapp", "profile": "ops", "chat_id": GROUP},
    ])
    runner.config.tag_routes = parse_tag_routes({"platforms": ["whatsapp"], "chats": [SELF, GROUP]})
    runner.pairing_store = PairingStore(profile="default")
    runner.pairing_stores = {}
    runner._primary_profile_name = "default"
    bot = _adapter(runner)
    runner.adapters = {Platform.WHATSAPP: bot}
    runner._profile_adapters = {"prime": {}, "ops": {}}
    served = [("default", home), ("prime", home / "profiles" / "prime"), ("ops", home / "profiles" / "ops")]
    with patch("hermes_cli.profiles.profiles_to_serve", return_value=served), \
            patch("hermes_cli.profiles.get_profile_dir", side_effect=lambda name: home if name == "default" else home / "profiles" / name), \
            patch("hermes_cli.profiles.profile_exists", return_value=True):
        yield SimpleNamespace(runner=runner, home=home, bot=bot)


def _event(adapter, chat_id, text, *, chat_type="dm", **kwargs):
    source = adapter.build_source(chat_id=chat_id, chat_type=chat_type, user_id=SELF)
    return MessageEvent(text=text, message_type=MessageType.TEXT, source=source, message_id="m1", **kwargs)


def _drain(adapter):
    for task in list(adapter._pending_text_batch_tasks.values()):
        task.cancel()
    adapter._pending_text_batches.clear()
    for task in list(adapter._session_tasks.values()):
        task.cancel()


def test_config_parse_round_trip_and_loader_bridge():
    raw = {"enabled": True, "platforms": ["whatsapp"], "chats": [SELF]}
    cfg = GatewayConfig.from_dict({"gateway": {"tag_routes": raw}})
    assert isinstance(cfg.tag_routes, TagRouteConfig)
    assert cfg.to_dict()["tag_routes"] == {**raw, "pattern": cfg.tag_routes.pattern}
    assert GatewayConfig.from_dict(cfg.to_dict()).tag_routes == cfg.tag_routes

    from gateway.config_loader import bridge_toplevel_keys
    bridged = {}
    bridge_toplevel_keys({}, {"tag_routes": raw}, bridged)
    assert bridged["tag_routes"] == raw


def test_invalid_config_and_whatsapp_alias_matching(caplog):
    assert parse_tag_routes({"platforms": ["whatsapp"]}) is None
    assert parse_tag_routes({"platforms": ["whatsapp"], "chats": [SELF], "pattern": "^@profile"}) is None
    cfg = parse_tag_routes({"platforms": "WhatsApp", "chats": SELF})
    assert cfg is not None
    assert extract_tag(cfg, " @Prime status")[0] == "prime"
    assert extract_tag(cfg, "@prime: status") is None
    assert chat_matches(cfg, "whatsapp", SELF)
    assert chat_matches(cfg, "whatsapp", "19785669223")
    assert not chat_matches(cfg, "whatsapp", OTHER)


def test_explicit_profile_route_has_precedence(rig):
    event = _event(rig.bot, GROUP, "@prime investigate", chat_type="group")
    assert event.source.profile == "ops"
    assert apply_tag_route(rig.bot, event) is None
    assert rig.bot._event_session_key(event).startswith("agent:ops:")


def test_explicit_default_profile_route_and_unknown_tag_do_not_reroute(rig, caplog):
    explicit_default = _event(rig.bot, SELF, "@prime investigate")
    explicit_default.source.profile = "default"
    assert apply_tag_route(rig.bot, explicit_default) is None
    assert explicit_default.source.profile == "default"

    unknown = _event(rig.bot, SELF, "@missing investigate")
    assert apply_tag_route(rig.bot, unknown) is None
    assert unknown.source.profile is None
    assert getattr(unknown.source, "_routing_identity", None) is None
    assert sum("@missing names a profile this gateway does not serve" in record.getMessage()
               for record in caplog.records) == 1


@pytest.mark.asyncio
async def test_tagged_self_chat_uses_tagged_profile_lane_and_transport(rig):
    seen = []

    async def handler(event):
        identity = identity_of(event.source)
        seen.append((identity.transport_profile, identity.runtime_profile, identity.runtime_home, event.text))

    rig.bot.set_message_handler(handler)
    tagged = _event(rig.bot, SELF, "@prime do X")
    plain = _event(rig.bot, SELF, "do Y")
    await rig.bot.handle_message(tagged)
    await rig.bot.handle_message(plain)
    for _ in range(5):
        await asyncio.sleep(0)

    tagged_key = rig.bot._event_session_key(tagged)
    plain_key = rig.bot._event_session_key(plain)
    assert tagged_key.startswith("agent:prime:whatsapp:dm:")
    assert plain_key.startswith("agent:main:whatsapp:dm:")
    assert tagged.source.profile == "prime"
    assert ("default", "prime", rig.home / "profiles" / "prime", "@prime do X") in seen
    assert rig.runner._delivery_adapter_for(tagged.source) is rig.bot
    _drain(rig.bot)


@pytest.mark.asyncio
async def test_tagged_gateway_command_reaches_tagged_lane(rig):
    seen = []

    async def handler(event):
        seen.append((identity_of(event.source).runtime_profile, event.get_command()))

    rig.bot.set_message_handler(handler)
    event = _event(rig.bot, SELF, "@prime /new", allow_gateway_control=True)
    await rig.bot.handle_message(event)
    for _ in range(5):
        await asyncio.sleep(0)
    assert event.text == "/new"
    assert ("prime", "new") in seen
    _drain(rig.bot)


@pytest.mark.asyncio
async def test_busy_tagged_and_untagged_self_chat_lanes_are_independent(rig):
    started = []

    async def handler(event):
        started.append(identity_of(event.source).runtime_profile)

    rig.bot.set_message_handler(handler)
    first = _event(rig.bot, SELF, "@prime long task")
    apply_tag_route(rig.bot, first)
    prime_key = rig.bot._event_session_key(first)
    rig.bot._active_sessions[prime_key] = asyncio.Event()
    rig.bot._session_tasks[prime_key] = asyncio.create_task(asyncio.sleep(30))

    follow_up = _event(rig.bot, SELF, "@prime also this")
    await rig.bot.handle_message(follow_up)
    assert prime_key in rig.bot._pending_messages

    plain = _event(rig.bot, SELF, "unrelated")
    await rig.bot.handle_message(plain)
    for _ in range(5):
        await asyncio.sleep(0)
    assert rig.bot._event_session_key(plain) != prime_key
    assert "default" in started
    _drain(rig.bot)
