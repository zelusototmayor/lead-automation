"""Bounded Jev factual batch through the existing Sales HTTP client.

Default is local manifest validation only. --apply is an explicit request to the
canonical CRM APIs; server allowlist, credential and reserved budget remain the
authority. Never reads the provider credential or connects to a database.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import time

from src.crm.services.jev_facts_batch import run_batch, validate_entries


class PacedClient:
    """Stay below the shared 60/minute API bucket without retrying any request."""
    def __init__(self, client):
        self.client, self.base, self.previous = client, client.base, None

    def call(self, method, path, payload=None):
        if self.previous is not None:
            time.sleep(max(0, 1.25 - (time.monotonic() - self.previous)))
        self.previous = time.monotonic()
        return self.client.call(method, path, payload)


def load_sales_client(config_path):
    # Reuse the deployed Sales client's TLS/redirect/scope/timeouts, not another
    # transport or writer. Import does not run its collect/scheduler entrypoint.
    adapter_path = Path.home() / '.hermes/scripts/head_of_sales_adapter.py'
    spec = importlib.util.spec_from_file_location('_canonical_sales_adapter', adapter_path)
    if spec is None or spec.loader is None:
        raise ValueError('sales_adapter_unavailable')
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    config = json.loads(config_path.read_text())
    if config.get('enabled') is not True:
        raise ValueError('sales_not_enabled')
    credential_path = Path(os.environ.get('CRM_AUTOMATION_BEARER_TOKEN_FILE')
        or config.get('token_file') or Path.home() / '.hermes/ops/head-of-sales/automation-token')
    fd = os.open(credential_path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('private_sales_credential_required')
        token = stream.read(16385).strip()
        if not token or len(token) > 16384:
            raise ValueError('invalid_sales_credential')
    return adapter.Client(os.environ.get('CRM_AGENT_BASE_URL') or config.get('base_url', ''),
        token, timeout=min(20, int(config.get('request_timeout_seconds', 10))))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--checkpoint', type=Path, required=True)
    parser.add_argument('--config', type=Path, default=Path.home() / '.hermes/ops/head-of-sales/config.json')
    parser.add_argument('--limit', type=int, default=1)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    try:
        paths = [args.manifest.resolve(), args.checkpoint.resolve(),
            Path(str(args.checkpoint) + '.lock').resolve(), args.config.resolve()]
        if len(set(paths)) != len(paths):
            raise ValueError('paths_must_differ')
        entries = json.loads(args.manifest.read_text())
        validate_entries(entries, args.limit)
        if not args.apply:
            result = {'mode': 'preview', 'total': len(entries), 'limit': args.limit,
                'scope': 'validated manifest only; no HTTP or classification'}
        else:
            client = PacedClient(load_sales_client(args.config))
            result = run_batch(client, entries, args.checkpoint, limit=args.limit)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if not args.apply or set(result['counts']) <= {'verified', 'abstained', 'reviewed_adjudication', 'pending'} else 3
    except Exception:
        # Never print unknown exceptions (can contain credentials or CRM notes).
        print('Jev batch refused: check private paths, Sales config, manifest and checkpoint; no automatic retries.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
