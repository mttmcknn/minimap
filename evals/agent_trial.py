#!/usr/bin/env python3
"""Run one isolated Codex navigation trial through a bounded device bridge.

Invoking --run starts an additional agent and requires the caller's explicit
authorization. --prepare only writes the concrete prompt and bridge files.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time

from paired_navigation import SAMPLES, PairedEval, graph_size, verifies
from compose_recovery import graph_digest

BRIDGE_CLIENT = '''#!/usr/bin/env python3
import json,sys,time,uuid
from pathlib import Path
root=Path(__file__).parent
request=json.loads(sys.argv[1])
key=uuid.uuid4().hex
path=root/'requests'/key
tmp=path.with_suffix('.tmp')
tmp.write_text(json.dumps(request))
tmp.rename(path.with_suffix('.json'))
deadline=time.monotonic()+65
while time.monotonic()<deadline:
 response=root/'responses'/f'{key}.json'
 if response.exists():
  print(response.read_text())
  raise SystemExit(0)
 time.sleep(.05)
raise SystemExit('Evaluation bridge timed out')
'''

PROXY = '''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
root=Path(os.environ['EVAL_TRIAL_ROOT'])
args=sys.argv[1:]
if '-s' in args and args[args.index('-s')+1]!=os.environ['ANDROID_SERIAL']:
 raise SystemExit('Device is outside this trial')
if 'input' in args and 'shell' in args:
 p=root/'inputs.jsonl'
 count=len(p.read_text().splitlines()) if p.exists() else 0
 if count>=32: raise SystemExit('Trial input budget exhausted')
 with p.open('a') as f: f.write(json.dumps(args)+'\\n')
os.execv(os.environ['EVAL_REAL_ADB'],[os.environ['EVAL_REAL_ADB'],*args])
'''

SCHEMA = {"type": "object", "properties": {
    "outcome": {"type": "string", "enum": ["completed", "unresolved", "product_defect"]},
    "evidence": {"type": "string"}, "source_evidence": {"type": "string"},
    "alert_user": {"type": "boolean"}},
    "required": ["outcome", "evidence", "source_evidence", "alert_user"], "additionalProperties": False}


def agent_spec(sample):
    spec = dict(SAMPLES[sample])
    if sample == "jetsnack":
        # A legitimate tab rename must not change the semantic destination.
        spec["checks"] = ["text=Categories", "text=Lifestyles"]
    return spec


def prepare(root, arm, sample, source, seed):
    spec = agent_spec(sample)
    root.mkdir(parents=True, exist_ok=False)
    for name in ["requests", "responses", "proxy"]:
        (root / name).mkdir()
    (root / "evalctl.py").write_text(BRIDGE_CLIENT)
    (root / "proxy/adb").write_text(PROXY)
    (root / "proxy/adb").chmod(0o755)
    (root / "result.schema.json").write_text(json.dumps(SCHEMA))
    if source:
        shutil.copytree(source, root / "app-source")
    if arm == "reused_graph":
        assert seed and (seed / ".minimap").is_dir(), "Reused arm requires a verified seed"
        shutil.copytree(seed / ".minimap", root / ".minimap")
    common = f"""Navigate the running {sample} Android sample to {spec['target']} and verify these visible requirements: {', '.join(spec['checks'])}.
The app begins at its home screen. Work only in this isolated directory and use the device bridge below for ALL Android actions. Do not launch or reset apps, inspect other directories, use external tools/services, edit app source, send messages, or change account/system settings. Matching application source, when supplied, is in app-source. You may read it with ordinary local file tools. Do not ask the user questions. Keep routine navigation/recovery quiet. Preserve the intended destination; if blocked, return a truthful result. Alert only for a reproduced product defect corroborated in matching source, with a file/line reference. The controller independently checks the final screen and graph. You have 180 seconds and 32 inputs for the entire task.

Call the bridge by running: python3 evalctl.py '<JSON object>'
Each call must complete before the next. Responses contain exit_code, stdout, and stderr. You may inspect the bridge documentation here, but do not edit the bridge or create device commands outside it. Return the requested final JSON schema after finishing.
"""
    if arm == "raw":
        instructions = """There is no navigation graph. Available bridge requests:
{"op":"layout"} returns the fresh Android layout.
{"op":"tap","point":[X,Y]} taps coordinates from the observed layout.
{"op":"back"} presses Back.
Use fresh observations to choose controls and verify the requested content. Read source if navigation is unexpected. Do not use or create Minimap files.
"""
    else:
        skill = (Path(__file__).resolve().parents[1] / "plugins/minimap-claude-code/skills/minimap-app-navigation/SKILL.md").read_text()
        instructions = f"""Use Minimap for navigation. The {'verified graph is already present; only runtime state is fresh' if arm == 'reused_graph' else 'graph is absent; initialize it and record the route as you navigate'}.
The package is {spec['package']}. The label for the target is {spec['target']}.
Request format: {{"op":"minimap","args":["go","{spec['target']}","--recovery-seconds","180","--expect","{spec['checks'][0]}"]}}
The bridge injects the selected emulator serial. All standard Minimap commands/flags are available except screenshots, --force, and skill installation. Initialize with init --no-skills --package {spec['package']} if needed. Use all requested goal checks. Propagate the returned recovery token through every command for that unresolved goal. Read matching source for unexpected navigation.

Canonical Minimap skill:
{skill}
"""
    (root / "PROMPT.md").write_text(common + instructions)
    return common + instructions


def usage_from_events(path):
    totals = {}
    for line in path.read_text().splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
            for key, value in event["usage"].items():
                if isinstance(value, int):
                    totals[key] = totals.get(key, 0) + value
    return totals or None


class AgentEval(PairedEval):
    def run(self, phase, argv, cwd=None, check=True):
        deadline = getattr(self, "agent_deadline", None)
        previous = getattr(self, "command_timeout", 60)
        if deadline is not None:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                raise TimeoutError("Agent trial deadline reached")
            self.command_timeout = min(previous, remaining)
        try:
            return super().run(phase, argv, cwd, check)
        finally:
            self.command_timeout = previous

    def request(self, req, root, arm):
        op = req.get("op")
        if arm == "raw":
            if op == "layout":
                argv = ["android", "layout", f"--device={self.args.serial}"]
            elif op == "tap":
                point = req.get("point")
                assert isinstance(point, list) and len(point) == 2 and all(isinstance(v, int) for v in point)
                assert all(v >= 0 for v in point)
                argv = ["adb", "-s", self.args.serial, "shell", "input", "tap", *point]
            elif op == "back":
                argv = ["adb", "-s", self.args.serial, "shell", "input", "keyevent", "4"]
            else:
                raise ValueError("Raw arm supports layout, tap point, and back")
        else:
            assert op == "minimap", "Use Minimap for this arm"
            args = req.get("args")
            assert isinstance(args, list) and args and all(isinstance(v, str) for v in args)
            assert args[0] in ["init", "doctor", "whereami", "go", "tap", "scroll", "back", "layout"]
            assert not any(arg in ["--serial", "--force", "--refresh-skills", "--agents", "--screenshot", "--screenshot-label"] or arg.startswith(("--serial=", "--screenshot=")) for arg in args)
            assert args[0] != "init" or "--no-skills" in args
            argv = [self.args.binary, "--serial", self.args.serial, *args]
        stdout = self.run("agent-tool", argv, cwd=root, check=False)
        return {"exit_code": self.calls[-1]["exit_code"], "stdout": stdout,
                "stderr": (self.output / f"{len(self.calls)-1:03d}.stderr").read_text()}

    def execute(self):
        args = self.args
        spec = {**agent_spec(args.sample), "apk": args.apk}
        root = self.output / "trial"
        prompt = prepare(root, args.arm, args.sample, args.source, args.seed)
        self.metadata.update({"suite": "controlled-v1-agent", "arm": args.arm, "sample": args.sample,
                              "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(), "deadline_seconds": 180,
                              "input_limit": 32, "case_id": args.case_id})
        self.metadata["graph_before"] = graph_digest(root)
        self.run("deployment", ["android", "run", f"--device={args.serial}", f"--apks={args.apk}"])
        self.restart(spec)
        agent_env = self.env.copy()
        # Only the controller's device commands use this input-counting proxy.
        self.env.update({"PATH": str(root / "proxy") + os.pathsep + os.environ["PATH"],
                         "ANDROID_SERIAL": args.serial, "EVAL_TRIAL_ROOT": str(root),
                         "EVAL_REAL_ADB": shutil.which("adb")})
        start = time.perf_counter()
        self.agent_deadline = start + 180
        argv = ["codex", "exec", "--json", "--ephemeral", "--sandbox", "workspace-write", "--skip-git-repo-check",
                "--cd", str(root), "--output-schema", str(root / "result.schema.json"),
                "--output-last-message", str(root / "answer.json"), "-"]
        self.metadata["codex_argv"] = argv
        timed_out = False
        with (root / "events.jsonl").open("w") as events, (root / "agent.stderr").open("w") as errors:
            process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=events, stderr=errors,
                                       env=agent_env, text=True, start_new_session=True)
            process.stdin.write(prompt)
            process.stdin.close()
            seen = set()
            try:
                while process.poll() is None:
                    if time.perf_counter() - start >= 180:
                        timed_out = True
                        break
                    for path in sorted((root / "requests").glob("*.json")):
                        if path.stem in seen:
                            continue
                        seen.add(path.stem)
                        try:
                            result = self.request(json.loads(path.read_text()), root, args.arm)
                        except Exception as error:
                            result = {"exit_code": 1, "stdout": "", "stderr": str(error)}
                        destination = root / "responses" / path.name
                        temporary = destination.with_suffix(".tmp")
                        temporary.write_text(json.dumps(result))
                        temporary.rename(destination)
                    time.sleep(.05)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
        elapsed = time.perf_counter() - start
        self.agent_deadline = None
        self.env = agent_env
        answer, answer_error, oracle_error, reached = None, None, None, None
        try:
            answer = json.loads((root / "answer.json").read_text()) if (root / "answer.json").exists() else None
        except (ValueError, OSError) as error:
            answer_error = str(error)
        try:
            reached = verifies(self.android_layout("oracle"), spec["checks"])
        except (ValueError, OSError, RuntimeError) as error:
            oracle_error = str(error)
        if answer is not None and (not isinstance(answer, dict) or answer.get("outcome") not in SCHEMA["properties"]["outcome"]["enum"]):
            answer_error = "Agent answer does not satisfy the outcome schema"
        reported = isinstance(answer, dict) and answer.get("outcome") == "completed"
        self.metadata["agent_result"] = {"answer": answer, "exit_code": process.returncode, "timed_out": timed_out,
                                        "elapsed_seconds": elapsed, "usage": usage_from_events(root / "events.jsonl"),
                                        "oracle_reached_target": reached,
                                        "false_success": reported and not reached if reached is not None else None,
                                        "answer_error": answer_error, "oracle_error": oracle_error,
                                        "input_actions": len((root / "inputs.jsonl").read_text().splitlines()) if (root / "inputs.jsonl").exists() else 0,
                                        "graph_after": graph_digest(root), "graph": graph_size(root),
                                        "tool_cost": self.measure("agent-tool")}
        self.metadata["graph_unchanged"] = self.metadata["graph_before"] == graph_digest(root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Start the explicitly authorized independent agent")
    parser.add_argument("--prepare", action="store_true", help="Write the trial prompt and bridge without starting an agent or device commands")
    for name in ["binary", "apk", "output"]:
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--seed", type=Path)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--sample", choices=list(SAMPLES), required=True)
    parser.add_argument("--arm", choices=["raw", "new_graph", "reused_graph"], required=True)
    parser.add_argument("--case-id", default="baseline")
    parser.add_argument("--startup-seconds", type=float, default=30)
    args = parser.parse_args()
    assert args.serial.startswith("emulator-")
    assert 0 < args.startup_seconds <= 60
    for name in ["binary", "apk", "source", "seed", "output"]:
        value = getattr(args, name)
        if value is not None:
            setattr(args, name, value.resolve())
    if args.prepare:
        prepare(args.output, args.arm, args.sample, args.source, args.seed)
        return
    assert args.run, "Choose --prepare or explicitly authorize --run"
    args.repetitions = 1
    evaluation = AgentEval(args)
    try:
        evaluation.execute()
    finally:
        evaluation.save()


if __name__ == "__main__":
    main()
