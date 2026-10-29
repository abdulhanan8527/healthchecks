from __future__ import annotations

from django.conf import settings
from django.core import signing

from hc.api.models import Flip, Notification
from hc.api.transports import TransportError
from hc.integrations.slack.transport import Slackalike
from hc.lib.typealias import JSONDict

# Salt for signing the interactive-message button tokens. The Mattermost
# callback view (hc.front.views.mattermost_callback) verifies with the same
# salt, so only tokens issued by this transport are accepted:
ACTIONS_SALT = "mattermost-probe-actions"


class Mattermost(Slackalike):
    def notify(self, flip: Flip, notification: Notification) -> None:
        if not settings.MATTERMOST_ENABLED:
            raise TransportError("Mattermost notifications are not enabled.")

        prepared_payload = self.payload(flip)
        self.post(self.channel.slack_webhook_url, json=prepared_payload)

    def probe_actions(self, probe: dict) -> list[JSONDict]:
        """Add Snooze/Un-snooze buttons to probe report cards.

        Mattermost posts the button clicks back to our callback endpoint
        (requires "Interactive Messages" enabled on the Mattermost server).
        The check code travels inside a signed token, so buttons cannot be
        forged or replayed against other checks.
        """

        check = probe.get("check")
        if check is None:
            return []

        token = signing.dumps(str(check.code), salt=ACTIONS_SALT)
        url = settings.SITE_ROOT + "/integrations/mattermost/callback/"

        actions = []
        for action, name in (
            ("snooze_1h", "Snooze 1h"),
            ("snooze_4h", "Snooze 4h"),
            ("snooze_24h", "Snooze 24h"),
            ("unsnooze", "Un-snooze"),
        ):
            actions.append(
                {
                    "id": action,
                    "name": name,
                    "integration": {
                        "url": url,
                        "context": {"token": token, "action": action},
                    },
                }
            )
        return actions
