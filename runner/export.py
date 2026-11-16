"""Export an existing recording without changing its manifest or Discord uploads."""

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path

from .pdf import ExportPlan, build_export
from .state import Session, atomic_write


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path, help="Session directory or manifest.json")
    parser.add_argument("--mode", choices=("all", "sampled"), default="all")
    parser.add_argument("--max-mb", type=float, default=9.5, help="Maximum bytes per PDF in MiB (default 9.5)")
    parser.add_argument("--output", type=Path, help="New output directory; existing directories are refused")
    args = parser.parse_args()
    if not math.isfinite(args.max_mb) or args.max_mb * 1024 * 1024 < 2048:
        parser.error("--max-mb must be finite and at least 2048 bytes")
    manifest = args.session if args.session.name == "manifest.json" else args.session / "manifest.json"
    session = Session.load(manifest)
    if session.data["status"] == "recording":
        parser.error("Export a finished session so the capture list is complete")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    output = args.output or session.directory / "exports" / f"{stamp}-{args.mode}"
    output.mkdir(parents=True, exist_ok=False)
    plan = ExportPlan.from_session(session, args.mode)
    result = build_export(plan, output, int(args.max_mb * 1024 * 1024))
    report = {"source_manifest": str(manifest.resolve()), "mode": result.mode,
              "captured_images": len(plan.captures), "pdf_pages": len(result.slide_numbers),
              "slide_numbers": list(result.slide_numbers), "seconds": result.elapsed_seconds,
              "parts": [part.manifest() for part in result.parts]}
    atomic_write(output / "export.json", json.dumps(report, indent=2).encode())
    print(f"{len(plan.captures)} captures -> {len(result.slide_numbers)} pages in {len(result.parts)} PDFs "
          f"({result.elapsed_seconds:.1f}s): {output}")


if __name__ == "__main__":
    main()
