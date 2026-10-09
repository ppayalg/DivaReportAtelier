"""Command line: validate configs, generate reports, mine insights, run the server."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .agents import GenerationError, InsightsAgent, ReportOrchestrator
from .llm import LLMError, get_llm
from .render import to_html, to_markdown
from .retrieval import InMemoryRetriever, load_knowledge
from .schema import parse_config, validate_config


def _load(path: str):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def cmd_validate(a: argparse.Namespace) -> int:
    bad = 0
    for p in a.configs:
        issues = validate_config(_load(p))
        errors = [i for i in issues if i.severity == "error"]
        bad += bool(errors)
        print(f"{p}: {'INVALID' if errors else 'ok'}")
        for i in issues:
            print(f"  {i.severity:7} {i.path}: {i.message}")
    return 1 if bad else 0


def cmd_generate(a: argparse.Namespace) -> int:
    knowledge = Path(a.knowledge)
    retriever = load_knowledge(knowledge) if knowledge.is_dir() else InMemoryRetriever()
    orch = ReportOrchestrator(get_llm(a.llm), retriever, use_llm_judge=(a.llm or "mock") != "mock")
    try:
        report = orch.generate(_load(a.config), _load(a.vars), key=Path(a.config).stem,
                               author=a.author, auto_approve=a.auto_approve)
    except GenerationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        for p in exc.problems:
            print(f"  - {p}", file=sys.stderr)
        return 2
    except LLMError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    out = {"md": to_markdown, "html": to_html, "json": lambda r: json.dumps(r.to_dict(), indent=2)}[a.format](report)
    if a.out:
        Path(a.out).write_text(out, encoding="utf-8")
        print(f"wrote {a.out} ({report.status}, {len(report.sections)} sections)")
    else:
        print(out)
    return 0


def cmd_insights(a: argparse.Namespace) -> int:
    config = parse_config(_load(a.config), Path(a.config).stem)
    recs = InsightsAgent(min_occurrences=a.min_occurrences).analyze(config, _load(a.feedback))
    print(json.dumps([r.to_dict() for r in recs], indent=2, ensure_ascii=False))
    return 0


def cmd_serve(a: argparse.Namespace) -> int:
    from .server import create_server
    from .service import AtelierService
    service = AtelierService.from_env(a.data_dir)
    srv = create_server(service, a.host, a.port)
    print(f"Diva Atelier AI on http://{a.host}:{a.port}  (llm: {service.llm.name})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="diva-atelier", description="Agentic dress-report generation.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate", help="Validate configuration files")
    v.add_argument("configs", nargs="+")
    v.set_defaults(fn=cmd_validate)

    g = sub.add_parser("generate", help="Generate a report from a configuration")
    g.add_argument("config")
    g.add_argument("--vars", required=True, help="JSON file of input variables")
    g.add_argument("--knowledge", default="data/knowledge")
    g.add_argument("--llm", choices=["mock", "anthropic"], default=None)
    g.add_argument("--format", choices=["md", "html", "json"], default="md")
    g.add_argument("--author", default="system")
    g.add_argument("--auto-approve", action="store_true")
    g.add_argument("--out")
    g.set_defaults(fn=cmd_generate)

    i = sub.add_parser("insights", help="Recommend configuration changes from feedback")
    i.add_argument("config")
    i.add_argument("--feedback", required=True)
    i.add_argument("--min-occurrences", type=int, default=2)
    i.set_defaults(fn=cmd_insights)

    s = sub.add_parser("serve", help="Run the API and dashboard")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--data-dir", default=None)
    s.set_defaults(fn=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
