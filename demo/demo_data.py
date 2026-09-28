"""Synthetic Claude Code transcripts for the README demo recording.

    python3 demo/demo_data.py build <root>   # write the sessions under <root>
    python3 demo/demo_data.py play <root>    # append the live part in real time

Everything here is made up: a fictional "acme-shop" project, generic prompts
and ids, so a recording shows no real work.
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

CWD = "/home/dev/src/acme-shop"
SLUG = "-home-dev-src-acme-shop"
BRANCH = "feature/discount-codes"
OPUS = "claude-opus-5-5"
HAIKU = "claude-haiku-4-5-20251001"

LIVE = "3f1c2a90-5b7d-4e61-9a2c-8d0e4f6b1a01"
EXPLORE = "a1e2f3a4b5c6d7e80"
TESTS = "a2b3c4d5e6f708192"
RUNNER = "a3c4d5e6f70819203"
QA = "a4d5e6f7081920314"
REVIEW = "a5e6f708192031425"

PAST = [
    ("9b2d4e61-0c3a-4f7e-8b15-2a6c9d0e7f02", "Fix flaky login test", 5),
    ("c7e1a3f5-2d4b-4c6e-9a8f-1b3d5e7f9a03", "Upgrade payment SDK", 26),
]


def ts(seconds_ago=0.0):
    t = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (t.microsecond // 1000)


class Writer:
    """Appends Claude Code records for one agent file, chaining uuids."""

    def __init__(self, path, session_id, agent_id=None):
        self.path, self.session_id, self.agent_id = path, session_id, agent_id
        self.prev, self.n, self.msg = None, 0, 0
        os.makedirs(os.path.dirname(path), exist_ok=True)

    def resume(self):
        """Continue the uuid chain of an existing file."""
        with open(self.path, encoding="utf-8") as fh:
            records = [json.loads(line) for line in fh if line.strip()]
        chained = [r for r in records if "uuid" in r]
        self.prev, self.n, self.msg = chained[-1]["uuid"], len(chained) + 100, len(chained) + 100
        return self

    def _write(self, record):
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

    def _base(self, rtype, ago):
        self.n += 1
        uuid = "%s-%s-%04d" % (self.agent_id or "main", rtype[0], self.n)
        record = {"type": rtype, "uuid": uuid, "parentUuid": self.prev, "sessionId": self.session_id,
                  "isSidechain": self.agent_id is not None, "cwd": CWD, "gitBranch": BRANCH, "timestamp": ts(ago)}
        if self.agent_id:
            record["agentId"] = self.agent_id
        self.prev = uuid
        return record

    def title(self, text):
        self._write({"type": "custom-title", "customTitle": text, "sessionId": self.session_id})

    def user(self, content, ago=0.0, **extra):
        record = self._base("user", ago)
        record["message"] = {"role": "user", "content": content}
        record.update(extra)
        self._write(record)

    def assistant(self, blocks, stop, model=OPUS, ago=0.0, tokens=(40, 300)):
        self.msg += 1
        record = self._base("assistant", ago)
        record["message"] = {"id": "msg_%s_%d" % (self.agent_id or "main", self.msg), "model": model,
                             "stop_reason": stop, "content": blocks,
                             "usage": {"input_tokens": tokens[0], "output_tokens": tokens[1],
                                       "cache_read_input_tokens": 18000 + 900 * self.msg,
                                       "cache_creation_input_tokens": 1200}}
        self._write(record)

    def say(self, text, model=OPUS, ago=0.0):
        self.assistant([{"type": "text", "text": text}], "end_turn", model, ago)

    def call(self, tool_id, name, tool_input, model=OPUS, ago=0.0):
        self.assistant([{"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}], "tool_use", model, ago)

    def result(self, tool_id, text, ago=0.0, is_error=False, **extra):
        block = {"type": "tool_result", "tool_use_id": tool_id, "content": text}
        if is_error:
            block["is_error"] = True
        self.user([block], ago, **extra)


def agent_file(root, session_id, agent_id):
    return os.path.join(root, SLUG, session_id, "subagents", "agent-%s.jsonl" % agent_id)


def write_meta(root, session_id, agent_id, agent_type, description, tool_use_id, depth=1, parent=None):
    meta = {"agentType": agent_type, "description": description, "toolUseId": tool_use_id, "spawnDepth": depth,
            "requestShape": "background", "requestNonInteractive": True}
    if parent:
        meta["parentAgentId"] = parent
    with open(agent_file(root, session_id, agent_id).replace(".jsonl", ".meta.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh)


def spawn(parent, root, tool_id, agent_id, agent_type, description, prompt, ago, depth=1, parent_id=None):
    """The parent's Agent call plus its async-launch result, and the child's sidecar."""
    parent.call(tool_id, "Agent", {"description": description, "subagent_type": agent_type, "prompt": prompt},
                model=OPUS if depth == 1 else HAIKU, ago=ago)
    launched = {"isAsync": True, "status": "async_launched", "agentId": agent_id, "description": description}
    parent.result(tool_id, "Async agent launched successfully.\nagentId: %s" % agent_id, ago=max(ago - 0.5, 0),
                  toolUseResult=launched)
    child = Writer(agent_file(root, parent.session_id, agent_id), parent.session_id, agent_id)
    write_meta(root, parent.session_id, agent_id, agent_type, description, tool_id, depth, parent_id)
    return child


def build_past(root):
    for session_id, title, hours in PAST:
        path = os.path.join(root, SLUG, session_id + ".jsonl")
        w = Writer(path, session_id)
        w.title(title)
        ago = hours * 3600
        tag = session_id[:6]
        w.user("%s. Keep the change small." % title, ago=ago)
        w.call("toolu_%s_r" % tag, "Read", {"file_path": CWD + "/src/app.ts"}, ago=ago - 20)
        w.result("toolu_%s_r" % tag, "export function app() { ... }", ago=ago - 19)
        w.call("toolu_%s_b" % tag, "Bash", {"command": "npm test", "description": "Run the tests"}, ago=ago - 60)
        w.result("toolu_%s_b" % tag, "42 passing", ago=ago - 50)
        w.say("Done: %s, tests pass." % title.lower(), ago=ago - 45)
        finished_at = time.time() - ago + 45
        os.utime(path, (finished_at, finished_at))


def build_explore(main, root):
    explore = spawn(main, root, "toolu_m03", EXPLORE, "Explore", "Map the checkout module",
                    "Find where totals, taxes and promotions are computed.", ago=228)
    explore.user("Find where totals, taxes and promotions are computed.", ago=227)
    steps = [("Glob", {"pattern": "src/checkout/**/*.ts"}), ("Read", {"file_path": CWD + "/src/checkout/tax.ts"}),
             ("Grep", {"pattern": "promo", "path": "src"})]
    for i, (tool, arg) in enumerate(steps):
        explore.call("toolu_e%02d" % i, tool, arg, model=HAIKU, ago=225 - i * 4)
        explore.result("toolu_e%02d" % i, "ok", ago=224 - i * 4)
    explore.say("Totals live in cart.ts, taxes in tax.ts; there is no promotion code path yet.", model=HAIKU, ago=210)
    main.user("<task-notification>\n<task-id>%s</task-id>\n<status>completed</status>\n</task-notification>" % EXPLORE, ago=209)


def build_tests(main, root):
    tests = spawn(main, root, "toolu_m05", TESTS, "general-purpose", "Write discount tests",
                  "Write unit tests for validateCode and applyDiscount.", ago=190)
    tests.user("Write unit tests for validateCode and applyDiscount.", ago=189)
    tests.call("toolu_t01", "Write", {"file_path": CWD + "/test/discount.test.ts", "content": "describe('discount', ...)"},
               model=HAIKU, ago=185)
    tests.result("toolu_t01", "File created", ago=184)
    runner = spawn(tests, root, "toolu_t02", RUNNER, "general-purpose", "Run the test suite",
                   "Run npm test and report failures.", ago=180, depth=2, parent_id=TESTS)
    runner.user("Run npm test and report failures.", ago=179)
    runner.call("toolu_r01", "Bash", {"command": "npm test -- discount", "description": "Run discount tests"}, model=HAIKU, ago=176)
    runner.result("toolu_r01", "1 failing: expired codes are accepted", ago=170, is_error=True)
    runner.call("toolu_r02", "Bash", {"command": "npm test -- discount", "description": "Re-run after fix"}, model=HAIKU, ago=150)
    runner.result("toolu_r02", "12 passing", ago=146)
    runner.say("All 12 discount tests pass.", model=HAIKU, ago=145)
    tests.say("Added 12 tests; one caught expired codes, now fixed.", model=HAIKU, ago=140)


def build_qa(main, root):
    qa = spawn(main, root, "toolu_m06", QA, "qa-specialist", "Check edge cases",
               "Check rounding and stacking edge cases for discounts.", ago=130)
    preload = ("<command-message>testing-guide</command-message>\n<command-name>testing-guide</command-name>\n"
               "<skill-format>true</skill-format>\nBase directory for this skill: " + CWD + "/.claude/skills/testing-guide")
    qa.user([{"type": "text", "text": preload}], ago=129, isMeta=True)
    qa.user("Check rounding and stacking edge cases for discounts.", ago=128)
    qa.call("toolu_q01", "Read", {"file_path": CWD + "/src/checkout/discount.ts"}, model=HAIKU, ago=124)
    qa.result("toolu_q01", "export function applyDiscount(...) {...}", ago=123)
    qa.say("Rounding is correct; stacking two codes is rejected as intended.", model=HAIKU, ago=110)


def build_live(root):
    main = Writer(os.path.join(root, SLUG, LIVE + ".jsonl"), LIVE)
    main.title("Add discount codes to checkout")
    main.user("Add discount codes to the checkout: validate the code, apply it to the cart total, and cover it with tests.", ago=240)
    main.call("toolu_m01", "Grep", {"pattern": "applyTotal", "path": "src"}, ago=236)
    main.result("toolu_m01", "src/checkout/cart.ts:42: export function applyTotal(cart)", ago=235)
    main.call("toolu_m02", "Read", {"file_path": CWD + "/src/checkout/cart.ts"}, ago=233)
    main.result("toolu_m02", "export function applyTotal(cart) {\n  return cart.items.reduce(...)\n}", ago=232)
    build_explore(main, root)
    main.call("toolu_m04", "Edit", {"file_path": CWD + "/src/checkout/discount.ts", "old_string": "",
                                    "new_string": "export function validateCode(code) {...}"}, ago=200)
    main.result("toolu_m04", "File updated", ago=199)
    build_tests(main, root)
    build_qa(main, root)


def build(root):
    build_past(root)
    build_live(root)


def play(root):
    """The live part: pending calls, a new reviewer agent, a skill, and the answer."""
    main = Writer(os.path.join(root, SLUG, LIVE + ".jsonl"), LIVE).resume()
    main.call("toolu_p01", "Edit", {"file_path": CWD + "/src/checkout/cart.ts", "old_string": "return total",
                                    "new_string": "return applyDiscount(total, code)"})
    time.sleep(2.5)
    main.result("toolu_p01", "File updated")
    time.sleep(1.0)
    main.call("toolu_p02", "Bash", {"command": "npm test", "description": "Run the full test suite"})
    time.sleep(4.0)
    main.result("toolu_p02", "148 passing")
    time.sleep(1.0)
    review = spawn(main, root, "toolu_p03", REVIEW, "code-reviewer", "Review the diff",
                   "Review the discount change for correctness and security.", ago=0)
    review.user("Review the discount change for correctness and security.")
    time.sleep(1.5)
    review.call("toolu_v01", "Bash", {"command": "git diff main", "description": "Show the diff"}, model=HAIKU)
    time.sleep(2.5)
    review.result("toolu_v01", "4 files changed, 120 insertions(+)")
    review.call("toolu_v02", "Grep", {"pattern": "parseFloat", "path": "src/checkout"}, model=HAIKU)
    time.sleep(2.5)
    review.result("toolu_v02", "src/checkout/discount.ts:18")
    review.say("Looks good. Consider integer cents instead of parseFloat.", model=HAIKU)
    main.user("<task-notification>\n<task-id>%s</task-id>\n<status>completed</status>\n</task-notification>" % REVIEW)
    time.sleep(1.0)
    main.call("toolu_p04", "Skill", {"skill": "changelog", "args": "discount codes"})
    main.result("toolu_p04", "Launching skill: changelog", toolUseResult={"success": True, "commandName": "changelog"})
    time.sleep(2.0)
    main.say("Discount codes are in: validation, cart totals, 12 new tests, reviewed, and a changelog entry.")


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("build", "play"):
        sys.exit(__doc__)
    {"build": build, "play": play}[sys.argv[1]](sys.argv[2])
