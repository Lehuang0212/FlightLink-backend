"""Offline maintenance, invoked as flightlink-api with the API's EnvironmentFile."""
from __future__ import annotations

from contextlib import closing
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

from .config import settings


def main(action: str) -> None:
    # Don't silently create an empty DB and report a successful backup/cleanup.
    path = settings.database_path.as_uri() + ('?mode=rw' if action == 'clean' else '?mode=ro')
    with closing(sqlite3.connect(path, uri=True)) as db:
        db.row_factory = sqlite3.Row
        if action == 'backup':
            with tempfile.TemporaryDirectory() as directory:
                backup = Path(directory) / 'snapshot'
                with closing(sqlite3.connect(backup)) as out:
                    db.backup(out)
                with backup.open('rb') as stream:
                    shutil.copyfileobj(stream, sys.stdout.buffer)
            return
        if action == 'status':
            for row in db.execute('SELECT id,uav_udp_port,ground_station_protocol,ground_station_port,enabled,runtime_intent FROM port_channels'):
                print(*row)
            return
        if action not in ('rules', 'clean'):
            raise ValueError(action)
        records = list(db.execute('SELECT * FROM security_group_rules ORDER BY channel_id,id'))
        if action == 'rules':
            print(settings.aliyun_region_id or '-', settings.aliyun_security_group_id or '-')
            for r in records:
                print(r['channel_id'], r['rule_id'] or 'pending', r['protocol'], r['port'], r['source_cidr'])
            return
        # Never restore an enabled channel after removing its ingress. Commit
        # before the first cloud call, including on partial failure/retry.
        db.execute("UPDATE port_channels SET runtime_intent='blocked',security_group_state='pending' WHERE runtime_intent!='deleting'")
        db.commit()
        if records:
            from .services.aliyun_security_group import get_security_group_provider
            from .services.security_group_rules import _source_matches

            if settings.security_group_provider != 'aliyun':
                raise RuntimeError('Aliyun provider disabled / 云托管未启用')
            provider = get_security_group_provider()
            for r in records:
                rules = provider.describe_security_group().rules
                marker = f"flightlink:channel:{r['channel_id']}:role:{r['role']}"
                matches = [rule for rule in rules if marker in (rule.description or '').split()
                           and rule.direction.lower() == 'ingress' and rule.policy.lower() == 'accept'
                           and rule.protocol.lower() == r['protocol'] and _source_matches(rule.source_cidr, r['source_cidr'])
                           and rule.port_range == f"{r['port']}/{r['port']}" and rule.rule_id]
                if r['rule_id']:
                    if any(rule.rule_id == r['rule_id'] for rule in rules) and not any(rule.rule_id == r['rule_id'] for rule in matches):
                        raise RuntimeError('Rule ownership changed / 规则所有权已变更')
                    matches = [rule for rule in matches if rule.rule_id == r['rule_id']]
                for rule in matches:
                    provider.revoke_rule(rule.rule_id)
                if matches and {x.rule_id for x in matches}.intersection(x.rule_id for x in provider.describe_security_group().rules):
                    raise RuntimeError('Revocation not confirmed / 尚未确认规则撤销')
                db.execute('DELETE FROM security_group_rules WHERE id=?', (r['id'],))
                db.commit()


if __name__ == '__main__':
    main(sys.argv[1])
