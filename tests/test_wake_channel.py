"""Channel selection regressions: temporary projects and fake services only."""
from unittest import mock

from tests import test_wake_install as installer


class InstallerChannelTests(installer.Base):
    def test_frozen_default_does_not_hide_selected_live_channel(self):
        default = self.project / ".ai-comm"
        live = self.project / ".ai-comm-v3"
        live.mkdir()
        installer.git(live, "init", "-q")
        installer.git(live, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                      "commit", "-q", "--allow-empty", "-m", "successor")
        selected = []

        def registry(cli, channel):
            selected.append(str(channel))
            if str(channel) == str(default):
                raise installer.wake.WakeError("channel_state_unavailable", "transient")
            self.assertEqual(str(channel), str(live))
            return {"participants": self.host.participants}

        before = self.everything()
        with mock.patch.object(self.host, "registry", registry):
            self.assertEqual(self.run_wake(self.args("plan")),
                             (75, {"status": "stopped", "code": "channel_state_unavailable"}))
            self.assertEqual(selected, [str(default)])
            selected.clear()
            code, plan = self.run_wake(self.args("plan", extra=["--channel", str(live)]))
            self.assertEqual(code, 0, plan)
            self.assertEqual(plan["project"]["channel_path"], str(live))
            self.assertEqual(selected, [str(live)])
        self.assertEqual(self.everything(), before)
