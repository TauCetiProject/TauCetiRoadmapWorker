import datetime as dt
import importlib.util
from pathlib import Path

from tauceti_roadmap_worker import agents
from tauceti_roadmap_worker.agents import AgentRun, agent_env, command_for, reset_time
from tauceti_roadmap_worker.paths import SANDBOX

BASE_ENV = {"HOME": "/h", "PATH": "/opt/tool/bin", "GH_TOKEN": "secret", "GITHUB_TOKEN": "secret2",
            "CODEX_HOME": "/h/.codex-w", "CLAUDE_CONFIG_DIR": "/h/.claude-w", "ANTHROPIC_API_KEY": "k"}


def run(agent, tmp_path, **kw):
    return AgentRun(agent=agent, model="m", workdir=tmp_path, brief="b", log_out=tmp_path / "o", log_err=tmp_path / "e",
                    settings_dir=tmp_path / "settings", **kw)


def test_agent_env_is_minimal():
    env = agent_env("codex", BASE_ENV)
    assert "GH_TOKEN" not in env and "GITHUB_TOKEN" not in env and "ANTHROPIC_API_KEY" not in env
    assert env["CODEX_HOME"] == "/h/.codex-w" and "CLAUDE_CONFIG_DIR" not in env
    assert env["PATH"].split(":")[:2] == ["/h/.elan/bin", "/h/.local/bin"] and env["PATH"].endswith("/opt/tool/bin")
    assert agent_env("claude", BASE_ENV)["ANTHROPIC_API_KEY"] == "k"


def test_codex_command(tmp_path):
    argv, env, cwd = command_for(run("codex", tmp_path), BASE_ENV)
    assert argv[:4] == ["codex", "exec", "-m", "m"] and argv[-3:] == ["-C", str(tmp_path), "-"]
    assert cwd == tmp_path and "HTTPS_PROXY" not in env


def test_claude_command_with_hook_and_proxy(tmp_path):
    argv, env, _ = command_for(run("claude", tmp_path, sandbox_profile="proxy", proxy_url="http://127.0.0.1:9"), BASE_ENV)
    assert argv[:4] == ["claude", "-p", "--model", "m"] and "--settings" in argv
    settings = Path(argv[argv.index("--settings") + 1]).read_text()
    assert str(SANDBOX / "url-guard.py") in settings and "/home/" not in settings.replace(str(SANDBOX), "")
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:9" and env["NO_PROXY"] == "127.0.0.1,localhost"


def test_netjail_falls_back_to_proxy(tmp_path):
    argv, env, _ = command_for(run("codex", tmp_path, sandbox_profile="netjail", netjail=str(tmp_path / "missing")), BASE_ENV)
    assert argv[0] == "codex" and env["HTTP_PROXY"] == "http://127.0.0.1:3128"


def test_limit_detection_and_reset_time():
    assert agents.LIMIT.search("You've hit your usage limit · resets 3pm (Europe/Paris)")
    assert not agents.LIMIT.search("all good")
    now = dt.datetime(2026, 10, 20, 12, 0, tzinfo=dt.timezone.utc)
    t = reset_time("limit reached; resets 3pm (Europe/Paris)", now)
    assert (t.hour, t.day, str(t.tzinfo)) == (15, 20, "Europe/Paris")     # 12:00 UTC is 14:00 in Paris: later today
    t2 = reset_time("resets Oct 25, 9am (UTC)", now)
    assert (t2.month, t2.day, t2.hour) == (10, 25, 9)
    assert reset_time("no time here", now) is None


def test_bundled_proxy_deny_list():
    spec = importlib.util.spec_from_file_location("allowproxy", SANDBOX / "allowproxy.py")
    proxy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(proxy)
    rules = proxy.deny_rules()
    assert ".sci-hub.se" in rules and "pastebin.com" in rules
    assert not proxy.allowed("sci-hub.se", rules) and not proxy.allowed("www.sci-hub.se", rules)
    assert proxy.allowed("arxiv.org", rules) and proxy.allowed("pastebin.com.example.org", rules)
