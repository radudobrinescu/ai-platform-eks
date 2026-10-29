"""Tests for the GitOps helpers — focused on the hf-token Secret creation, which
must never place the token on a kubectl argv (readable via /proc/<pid>/cmdline)."""

import base64
import json

from recommend_instance import gitops


class _C:
    """Stub colour palette — _ensure_hf_token_secret only reads these attrs."""
    RESET = YELLOW = GREEN = DIM = RED = BOLD = CYAN = ""


TOKEN = "hf_sekret_TOKEN_do_not_leak_123"


class TestHfTokenSecret:
    def test_token_never_on_argv_and_applied_as_base64(self, monkeypatch):
        calls = []

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_run(args, **kwargs):
            calls.append((args, kwargs))
            return _Result()

        monkeypatch.setattr(gitops.shutil, "which", lambda _cmd: "/usr/bin/kubectl")
        monkeypatch.setattr(gitops.subprocess, "run", fake_run)

        gitops._ensure_hf_token_secret("inference", TOKEN, "/tmp", _C)

        # Exactly one kubectl call: `apply -f -` (no `create secret --from-literal`).
        assert len(calls) == 1, calls
        argv, kwargs = calls[0]
        assert argv[:2] == ["kubectl", "apply"]
        assert "-f" in argv and "-" in argv
        # The token appears NOWHERE on the command line.
        assert not any(TOKEN in str(a) for a in argv), argv
        # It reaches kubectl only via stdin, base64-encoded in a Secret manifest.
        manifest = json.loads(kwargs["input"])
        assert manifest["kind"] == "Secret"
        assert manifest["metadata"]["name"] == "hf-token"
        assert base64.b64decode(manifest["data"]["token"]).decode() == TOKEN
        # And the raw token is not present verbatim in the stdin payload.
        assert TOKEN not in kwargs["input"]

    def test_no_kubectl_makes_no_calls(self, monkeypatch):
        calls = []
        monkeypatch.setattr(gitops.shutil, "which", lambda _cmd: None)
        monkeypatch.setattr(gitops.subprocess, "run",
                            lambda *a, **k: calls.append((a, k)))
        gitops._ensure_hf_token_secret("inference", TOKEN, "/tmp", _C)
        assert calls == []  # prints a manual fallback; never shells the token


class TestArgocdSyncRefreshesAppSet:
    """Deploy/undeploy ask the workloads-models ApplicationSet to re-read git, so a
    namespace's first model (no models-<ns> app yet) does not wait ~5 minutes."""

    def _capture(self, monkeypatch, patch_rc=0, patch_err=""):
        calls = []

        def fake_run(args, cwd):
            calls.append(args)
            if args[:2] == ["kubectl", "patch"]:
                return gitops.subprocess.CompletedProcess(args, patch_rc, "", patch_err)
            return gitops.subprocess.CompletedProcess(args, 0, "abc1234\n", "")

        monkeypatch.setattr(gitops.shutil, "which", lambda _cmd: "/usr/bin/kubectl")
        monkeypatch.setattr(gitops, "_run", fake_run)
        return calls

    def _annotations(self, calls):
        return [a for a in calls if a[:3] == ["kubectl", "annotate", "applicationset"]]

    def test_models_app_requests_refresh_before_sync(self, monkeypatch, capsys):
        calls = self._capture(monkeypatch)
        gitops._argocd_sync("/repo", gitops._palette(False), "models-inference")
        ann = self._annotations(calls)
        assert ann and ann[0][3] == "workloads-models"
        assert "argocd.argoproj.io/application-set-refresh=true" in ann[0]
        assert calls.index(ann[0]) < next(i for i, a in enumerate(calls) if a[:2] == ["kubectl", "patch"])

    def test_first_model_message_says_now(self, monkeypatch, capsys):
        self._capture(monkeypatch, patch_rc=1, patch_err='applications.argoproj.io "models-team-x" not found')
        gitops._argocd_sync("/repo", gitops._palette(False), "models-team-x")
        assert "refresh requested" in capsys.readouterr().out

    def test_scale_models_app_does_not_touch_the_appset(self, monkeypatch):
        calls = self._capture(monkeypatch)
        gitops._argocd_sync("/repo", gitops._palette(False), gitops.ARGOCD_SCALE_MODELS_APP)
        assert not self._annotations(calls)

