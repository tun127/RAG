"""Command-line entry point for the M3.2 pilot evaluation adapter."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .contracts import ContractError
from .evaluate import evaluate_pilot
from .render import render_markdown


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-root",
        type=Path,
        required=True,
        help="pilot run root, executions root, or a single execution directory",
    )
    parser.add_argument(
        "--cases-manifest",
        type=Path,
        required=True,
        help="JSON list of case_id and annotation paths",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="new directory for evaluation.json and evaluation.md",
    )
    parser.add_argument(
        "--inputs-root",
        type=Path,
        default=None,
        help="optional root containing <run_id>/input.json for live runs",
    )
    parser.add_argument(
        "--final-answers-root",
        type=Path,
        default=None,
        help="optional directory containing <task_id>.json fallback answers",
    )
    parser.add_argument("--title", default=None)
    args = parser.parse_args(argv)

    if args.output.exists():
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "reason": "output_directory_exists",
                    "path": str(args.output),
                },
                ensure_ascii=False,
            )
        )
        return 2
    try:
        report = evaluate_pilot(
            run_root=args.run_root,
            cases_manifest=args.cases_manifest,
            inputs_root=args.inputs_root,
            final_answers_root=args.final_answers_root,
            title=args.title,
        )
        args.output.mkdir(parents=True, exist_ok=False)
        json_path = args.output / "evaluation.json"
        markdown_path = args.output / "evaluation.md"
        json_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        markdown_path.write_text(render_markdown(report), encoding="utf-8")
    except FileExistsError:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "reason": "output_directory_exists",
                    "path": str(args.output),
                },
                ensure_ascii=False,
            )
        )
        return 2
    except ContractError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False))
        return 2
    except OSError as exc:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "reason": "filesystem_error",
                    "detail": str(exc),
                },
                ensure_ascii=False,
            )
        )
        return 2

    aggregate = report["aggregate"]
    summary = {
        "status": "evaluated",
        "schema": report["schema"],
        "evaluation_v2": aggregate["evaluation_v2"],
        "environment": report["environment"],
        "executions": aggregate["executions"],
        "slots": aggregate["slots"],
        "business_accuracy_measured": report["business_accuracy_measured"],
        "semantic_support_measured": report["semantic_support_measured"],
        "program_success_all_slots": aggregate["program"]["metrics"][
            "program_success_all_slots"
        ],
        "citation_unique_verbatim_rate": aggregate["citation_precision"]["metric"],
        "classification_agreement_with_single_author_draft": aggregate["classification"][
            "metric"
        ],
        "report_json": str(json_path),
        "report_markdown": str(markdown_path),
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0
