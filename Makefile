install:
	uv sync

run-observation:
	uv run python -m app.main --mode observation --profile trident

run-dry:
	uv run python -m app.main --mode dry-run --profile trident

run-live:
	uv run python -m app.main --mode live --profile trident

test:
	uv run pytest

healthcheck:
	./scripts/trident_healthcheck.sh

docker-start:
	./scripts/trident_start.sh

docker-stop:
	./scripts/trident_stop.sh

docker-restart:
	./scripts/trident_restart.sh

backtest-stdlib:
	python3.12 -m app.backtest.runner --input $(INPUT)

convert-gbot-stdlib:
	python3.12 -m app.backtest.gbot_converter --data-dir $(DATA_DIR) --date $(DATE) --coins $(COINS) --output $(OUTPUT)

replay-archive-stdlib:
	python3.12 -m app.backtest.archive_replay --data-dir $(DATA_DIR) --date-from $(DATE_FROM) --date-to $(DATE_TO) --coins $(COINS) --report-output $(REPORT) --journal-output $(JOURNAL)
