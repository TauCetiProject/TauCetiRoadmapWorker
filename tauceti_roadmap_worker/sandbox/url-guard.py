#!/usr/bin/env python3
"""Claude Code PreToolUse hook for worker agents (anti-injection): blocks shell commands that execute
content fetched from the network, raw network tools and remote shells, proxy tampering, and credential
access. Fetching and browsing are allowed. Prints a deny decision as JSON; exits 0 otherwise.
The worker installs it through a generated settings file (agents.claude_settings)."""
import json, re, sys


def deny(reason):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": reason}}))
    sys.exit(0)


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:  # noqa: BLE001
        sys.exit(0)
    tool = data.get("tool_name", ""); inp = data.get("tool_input") or {}
    if tool == "Bash":
        cmd = str(inp.get("command", ""))
        if re.search(r"(curl|wget|fetch)\b[^|;&]*\|\s*(sudo\s+)?(ba|z|da|k)?sh\b|\|\s*(python3?|perl|node|ruby)\s*(-|$)|\bbase64\s+(-d|--decode)[^|]*\|\s*(ba|z)?sh|\beval\s+\"?\$\(\s*(curl|wget)", cmd, re.I):
            deny("Executing content fetched from the network is not allowed for worker agents.")
        if re.search(r"\b(nc|ncat|netcat|socat|telnet)\b|/dev/(tcp|udp)/|\bssh\s|\bscp\s|\bsftp\s|\brsync\s+\S*:|\bcryptsetup\b|\bmkfs\b", cmd, re.I):
            deny("Raw network tools and remote shells are not allowed for worker agents.")
        if re.search(r"(unset|export)\s+-?\w*(https?_proxy|all_proxy)|\benv\s+(-i|-u\s)|\benv\s+-\w*i\b", cmd, re.I):
            deny("Changing the proxy settings is not allowed for worker agents.")
        if re.search(r"\b(gh|git)\s+(auth|credential)\b|~/\.config/gh/hosts\.yml|\.claude\d?/\.credentials|\.codex\d?/auth\.json|GH_TOKEN|ANTHROPIC_API_KEY|OPENAI_API_KEY|/\.ssh/", cmd, re.I):
            deny("Reading or using credentials is not allowed for worker agents.")
    sys.exit(0)


if __name__ == "__main__":
    main()
