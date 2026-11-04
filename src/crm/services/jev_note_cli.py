"""Read-only CRM note policy/replay CLI; no provider or apply command exists.

Caller must supply an authorized tenant and DATABASE_URL. No dotenv/credential
file loading; use a read-only DB role in any future approved target run.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
from uuid import UUID

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.crm.services.jev_note_policy import catalog, render_catalog
from src.crm.services.jev_note_replay import create_plan, replay_batch
from src.crm.services.jev_private_files import private_write


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('catalog', 'plan', 'replay'):
        command = commands.add_parser(name)
        command.add_argument('--output', type=Path, required=True)
        if name != 'catalog':
            command.add_argument('--workspace', type=UUID, required=True)
        if name == 'plan':
            command.add_argument('--shadow-point', action='append', default=[], choices=list(catalog()['points']))
        if name == 'replay':
            command.add_argument('--plan', type=Path, required=True)
            command.add_argument('--checkpoint', type=Path, required=True)
            command.add_argument('--limit', type=int, default=50)
    args = parser.parse_args(argv)
    engine = None
    try:
        if args.command == 'catalog':
            private_write(args.output, render_catalog())
            return 0
        # Avoid destroying input/checkpoint with a colliding output path.
        if args.command == 'replay':
            paths = [args.plan.resolve(), args.checkpoint.resolve(), args.output.resolve()]
            if len(set(paths)) != len(paths):
                raise ValueError('paths_must_differ')
            plan = json.loads(args.plan.read_text())
        engine = create_engine(os.environ['DATABASE_URL'], hide_parameters=True)
        factory = sessionmaker(engine)
        if args.command == 'plan':
            result = create_plan(factory, args.workspace,
                flags=dict.fromkeys(args.shadow_point, 'shadow'))
        else:
            result = asyncio.run(replay_batch(factory, args.workspace, plan,
                args.checkpoint, limit=args.limit))
        private_write(args.output, json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
        return 0
    except Exception:
        # DB/provider errors may carry URLs or private SQL values. Never print.
        print('CRM note dry-run failed: check authorized DB, tenant, policy/plan/checkpoint and private output paths.', file=sys.stderr)
        return 2
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == '__main__':
    raise SystemExit(main())
