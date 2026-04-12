import logging
from app.settings import load_config
from app.backtest.full_bot_replay import FullBotBacktestRunner

logging.basicConfig(level=logging.INFO, format='%(levelname)s:%(name)s:%(message)s')
config = load_config('config/trident.toml')
result = FullBotBacktestRunner(config).run_jsonl('server-data/live_snapshots')
print('RESULT', result.records_processed, result.total_realized_pnl_usd, result.pod_b.get('realized_pnl_usd'))
