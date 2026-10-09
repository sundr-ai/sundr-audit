"""The open-source audit CLI ships without the hosted proxy, web app, server and billing; it must work without them."""

import subprocess
import sys
import textwrap
from pathlib import Path

HOSTED = ("sundr.proxy", "sundr.web", "sundr.server", "sundr.billing")
CORE = ("audit", "bench", "certify", "claude", "cli", "client", "combiner", "compiler", "demo", "estimate", "features",
        "ingest", "label", "mcp_server", "monitor", "report", "spend", "types", "backends.systemone",
        "backends.simulated", "connectors", "connectors.pull")


def test_cli_and_mcp_server_work_without_the_hosted_modules(tmp_path):
    script = textwrap.dedent(f"""
        import importlib, sys
        for name in {HOSTED!r}:
            sys.modules[name] = None  # import fails, importlib.util.find_spec returns None
        for name in {CORE!r}:
            importlib.import_module("sundr." + name)
        import typer
        from typer.testing import CliRunner
        from sundr.cli import app
        commands = set(typer.main.get_command(app).commands)
        assert {{"audit", "demo", "pull", "mcp"}} <= commands and not {{"proxy", "serve"}} & commands, commands
        result = CliRunner().invoke(app, ["demo", "--n", "2000", "--out", {str(tmp_path / "demo.html")!r}])
        assert result.exit_code == 0, result.output
        leaked = sorted(m for m in sys.modules if m.startswith({HOSTED!r}) and sys.modules[m] is not None)
        assert not leaked, leaked
        print("ok")
    """)
    root = Path(__file__).resolve().parents[1]
    run = subprocess.run([sys.executable, "-c", script], cwd=root, capture_output=True, text=True, timeout=300)
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout.strip().endswith("ok") and (tmp_path / "demo.html").exists()
