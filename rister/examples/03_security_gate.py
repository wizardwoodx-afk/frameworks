"""03 — Security scanning as a CI gate: rules, suppressions, SARIF, policy draft.

Run:  python examples/03_security_gate.py
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import rister as rs

SAMPLE = '''
import hashlib, subprocess

TOKEN = "ghp_" + "a" * 36                      # hardcoded credential (SEC001)

def handle(user_input):
    subprocess.run(f"ls {user_input}", shell=True)   # command injection (SEC003)
    return hashlib.md5(user_input.encode()).hexdigest()  # weak hash (SEC009)

def trusted():
    eval(compile_source())                      # rister:ignore SEC002
'''

with tempfile.TemporaryDirectory() as root:
    root = Path(root)
    (root / "app").mkdir()
    (root / "app" / "handler.py").write_text(SAMPLE)
    (root / "app" / "clean.py").write_text("def add(a, b):\n    return a + b\n")

    print("1. scan a directory")
    report = rs.scan(root / "app")
    print(f"   {report.summary()}")
    for finding in report.findings:
        print(f"   {finding.severity:<8} {finding.rule} {finding.path}:{finding.line} "
              f"{finding.title}")

    print("2. the inline suppression was honoured — and recorded, not hidden")
    print(f"   suppressed: {report.suppressed}")
    for item in report.suppressions:
        print(f"   {item}")

    print("3. severity gates drive the exit code")
    print(f"   any high+?: {report.has_at_or_above('high')}")
    print(f"   any critical?: {report.has_at_or_above('critical')}")

    print("4. formats for humans and for machines")
    sarif = report.to_sarif()
    print(f"   sarif 2.1.0: {len(sarif['runs'][0]['results'])} results, "
          f"{len(sarif['runs'][0]['tool']['driver']['rules'])} rules")
    print(f"   json: {len(json.loads(report.to_json())['findings'])} findings")
    print(f"   csv rows: {len(report.to_csv().strip().splitlines()) - 1}")

    print("5. a SECURITY.md that reflects what was actually found")
    draft = rs.draft_security_policy(report, contact="security@example.test")
    highlights = [line for line in draft.splitlines() if "SEC00" in line][:3]
    for line in highlights:
        print(f"   {line.strip()[:78]}")

print("6. the ruleset itself is data you can inspect")
for rule in rs.get_rules()[:3]:
    print(f"   {rule.id} [{rule.severity}] {rule.title}")
print(f"   ... {len(rs.get_rules())} rules in total")
