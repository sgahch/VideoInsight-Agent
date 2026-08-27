from __future__ import annotations

import argparse
import json
import sys
from uuid import uuid4

from app.config import settings
from app.schemas import AnalysisMode, AnalyzeRequest
from app.workflow import analyze


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Run VideoInsight analysis from the command line.")
    parser.add_argument("url", help="Video URL to analyze.")
    parser.add_argument(
        "--mode",
        choices=[mode.value for mode in AnalysisMode],
        default=AnalysisMode.summary.value,
        help="Analysis mode.",
    )
    parser.add_argument("--real", action="store_true", help="Disable mock mode for this run.")
    args = parser.parse_args()

    request = AnalyzeRequest(
        url=args.url,
        mode=AnalysisMode(args.mode),
        use_mock=False if args.real else None,
    )
    result = analyze(request, task_id=uuid4().hex, runtime_settings=settings)
    print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
