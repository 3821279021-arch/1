import argparse
import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from zipfile import ZipFile

parser = argparse.ArgumentParser(description="Compare identical synthetic prompts with a supplied V2.2 source archive")
parser.add_argument("--baseline-zip", required=True, type=Path)
args = parser.parse_args()
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
temporary = tempfile.TemporaryDirectory()
base = Path(temporary.name) / "app"
base.mkdir(parents=True)
with ZipFile(args.baseline_zip) as z:
    for filename in z.namelist():
        if filename.startswith("ai-werewolf/app/") and filename.endswith(".py"):
            (base / Path(filename).name).write_bytes(z.read(filename))
spec = importlib.util.spec_from_file_location(
    "baseline23", base / "__init__.py", submodule_search_locations=[str(base)]
)
module = importlib.util.module_from_spec(spec)
sys.modules["baseline23"] = module
spec.loader.exec_module(module)
from baseline23.ai import AIOrchestrator as OldAI
from baseline23.llm import LLMRouter as OldRouter
from baseline23.memory import new_memory as old_memory
from baseline23.memory import update_memory as old_update

from app.ai import AIOrchestrator
from app.game import WerewolfGame
from app.llm import LLMRouter
from app.memory import new_memory, update_memory
from app.rules import RuleEngine
from app.scope import InformationScope
from app.tokens import estimate_tokens

old = old_memory()
new = new_memory()
old_ai = OldAI(OldRouter())
new_ai = AIOrchestrator(LLMRouter())
game = WerewolfGame("benchmark", "owner")
engine = RuleEngine(game)
engine.join("owner", "玩家", 1)
engine.start("owner")
view = InformationScope.ai_view(game, 1)
rows = []
for day in range(1, 31):
    for offset in range(18):
        seq = day * 100 + offset
        pid = offset % 6 + 1
        event = {
            "type": "speech",
            "event_id": f"s:{seq}",
            "day": day,
            "audience": "public",
            "data": {"player_id": pid, "speech": "我是村民，怀疑3号，支持4号，请核对公开票型。"},
        }
        old = old_update(old, event, 1)
        new = update_memory(new, event, 1)
    view["day"] = day
    old_sys, old_user, _ = old_ai.context(view, "detective", old)
    new_sys, new_user, _ = new_ai.context(view, "detective", new)
    old_tokens = estimate_tokens("dashscope", "qwen-plus", old_sys + old_user)
    new_tokens = estimate_tokens("dashscope", "qwen-plus", new_sys + new_user)
    rows.append({"day": day, "v22_estimated_prompt_tokens": old_tokens, "v23_estimated_prompt_tokens": new_tokens})
report = {
    "scenario": "30 simulated days, 18 scoped speeches/day; no provider calls",
    "estimator": "same V2.3 DashScope heuristic for both versions; these are not actual usage values",
    "samples": rows,
    "final_prompt_reduction": round(
        1 - rows[-1]["v23_estimated_prompt_tokens"] / rows[-1]["v22_estimated_prompt_tokens"], 4
    ),
    "aggregate_prompt_reduction": round(
        1 - sum(r["v23_estimated_prompt_tokens"] for r in rows) / sum(r["v22_estimated_prompt_tokens"] for r in rows), 4
    ),
}
path = ROOT / "docs/playtests/v23-memory-benchmark.json"
path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
print(json.dumps({k: v for k, v in report.items() if k != "samples"}, ensure_ascii=False))
print("D10/D20/D30:", [rows[i] for i in (9, 19, 29)])

temporary.cleanup()
