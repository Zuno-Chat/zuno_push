from __future__ import annotations

from dataclasses import dataclass

from synapse.module_api import ModuleApi

from zuno_push.canary import Canary
from zuno_push.catchup import CatchUp
from zuno_push.config import ZunoPushConfig
from zuno_push.credential import CredentialAuth
from zuno_push.features import FeatureGate
from zuno_push.janitor import Janitor
from zuno_push.nse_scope import NseScope
from zuno_push.poller import DeltaPoller
from zuno_push.ratelimit import RateLimiter
from zuno_push.records import Internals
from zuno_push.ringer import Ringer
from zuno_push.rules import RingRules
from zuno_push.sender import SygnalSender
from zuno_push.store import PushStore


@dataclass
class Services:
    api: ModuleApi
    config: ZunoPushConfig
    internals: Internals
    store: PushStore
    limiter: RateLimiter
    gate: FeatureGate
    sender: SygnalSender
    rules: RingRules
    ringer: Ringer
    poller: DeltaPoller
    janitor: Janitor
    canary: Canary | None
    credentials: CredentialAuth
    scope: NseScope
    catchup: CatchUp

    def now(self) -> int:
        return self.api.get_current_time_msec()


def build_services(api: ModuleApi, config: ZunoPushConfig, internals: Internals) -> Services:
    store = PushStore(api)
    limiter = RateLimiter(api)
    sender = SygnalSender(api, internals, config.sygnal_notify_url)
    rules = RingRules(api, internals, store, limiter, config.max_ring_targets)
    ringer = Ringer(api, store, sender)
    scope = NseScope(api, internals)
    return Services(
        api=api,
        config=config,
        internals=internals,
        store=store,
        limiter=limiter,
        gate=FeatureGate(),
        sender=sender,
        rules=rules,
        ringer=ringer,
        poller=DeltaPoller(api, internals, store, rules, ringer.kick),
        janitor=Janitor(api, internals, store),
        canary=Canary(api, internals, store, sender, config.canary) if config.canary else None,
        credentials=CredentialAuth(api, store, internals),
        scope=scope,
        catchup=CatchUp(api, internals, store, scope),
    )
