#!/usr/bin/env python3
"""检查 mschema 中的表名格式"""
import sys
sys.path.insert(0, 'src')

# 先注册 GreptimeDB dialect
from xiyan_mcp_server.utils import greptimedb_dialect
from xiyan_mcp_server.utils.greptimedb_source import GreptimeDBSource
from sqlalchemy import create_engine

engine = create_engine('greptimedb+psycopg2://root:@10.0.0.8:4003/public')
source = GreptimeDBSource(engine, system_prefix='cockroachdb')

print('mschema 中的表名格式:')
for t in source._usable_tables[:10]:
    print(f'  {t!r}')
    print(f'  小写: {t.lower()}')
    print()

print(f'\n总共 {len(source._usable_tables)} 个表')
