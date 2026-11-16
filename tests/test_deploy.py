import sys
import types

from splitwise_mcp_server.server import load_ssm_parameters


class FakeSSM:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get_parameters_by_path(self, **kwargs):
        self.calls.append(kwargs)
        return self.pages[len(self.calls) - 1]


def test_ssm_parameters_load_into_env_across_pages_without_overriding(monkeypatch):
    ssm = FakeSSM([
        {"Parameters": [{"Name": "/app/SERVER_SECRET", "Value": "s3cret"}], "NextToken": "t1"},
        {"Parameters": [{"Name": "/app/PUBLIC_URL", "Value": "https://from-ssm"}]},
    ])
    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda name: ssm))
    monkeypatch.delenv("SERVER_SECRET", raising=False)
    monkeypatch.setenv("PUBLIC_URL", "https://already-set")

    load_ssm_parameters("/app")

    assert [c.get("NextToken") for c in ssm.calls] == [None, "t1"]
    assert all(c["Path"] == "/app" and c["WithDecryption"] for c in ssm.calls)
    import os
    assert os.environ["SERVER_SECRET"] == "s3cret"
    assert os.environ["PUBLIC_URL"] == "https://already-set"  # an explicit setting wins
